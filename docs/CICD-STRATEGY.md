# CI/CD 与部署策略（本仓 + ArgoCD GitOps）

> 适用范围：`scheduled-batch-jobs` 仓库承载的自研定时任务（当前：`cctv-pruner`）。
> 记录时间：2026-11-25 ｜ 所有路径、行为均为实测，不含推测。

---

## 1. 一句话策略

> **代码与流水线在代码仓，镜像不可变在 GHCR，集群状态声明在 manifests 仓，同步交给 ArgoCD，时钟留在 AWS。**

四条边界不互相越界：任何一层都能单独替换，不牵动其他层。

---

## 2. 全景

```
┌─ 代码仓 nvd11/scheduled-batch-jobs ────────────────────────────┐
│  jobs/cctv-pruner/pruner.py        ← 业务逻辑（仅标准库）        │
│  jobs/cctv-pruner/Dockerfile       ← linux/arm64               │
│  .github/workflows/build-cctv-pruner.yml  ← CI（测试 + 构建）   │
│  .github/workflows/prune-cctv.yml         ← 运行时派发          │
└───────────────┬────────────────────────────────────────────────┘
                │ push（jobs/** 变更）
                ▼
┌─ CI ───────────────────────────────────────────────────────────┐
│  ① Unit tests      py_compile + 21 个 unittest（失败即停）       │
│  ② Build & Push    ubuntu-24.04-arm 原生 ARM runner             │
│                    → ghcr.io/nvd11/cctv-pruner@sha256:…         │
│  ③ CD dispatch     repository_dispatch(update-app-image-digest) │
└───────────────┬────────────────────────────────────────────────┘
                │ HTTP 204
                ▼
┌─ manifests 仓 nvd11/my-argocd-manifests ───────────────────────┐
│  .github/workflows/update-app-image-digest.yml                  │
│    ├ 校验 svc_name 白名单                                       │
│    ├ sed 改写 argocd-apps/cctv-prune-app.yaml 的 digest 行      │
│    └ bot commit + push（消息带 [skip ci]）                       │
└───────────────┬────────────────────────────────────────────────┘
                │ ArgoCD 轮询（约 3 分钟）
                ▼
┌─ 集群 tencent-dp1-cluster ─────────────────────────────────────┐
│  Application cctv-prune（Synced / selfHeal）                    │
│    └ CronJob cctv-prune（suspend=true，永不自触发）              │
└───────────────┬────────────────────────────────────────────────┘
                │ AWS EventBridge Scheduler → API Destination
                ▼
┌─ 运行时 ───────────────────────────────────────────────────────┐
│  GitHub Actions prune-cctv.yml                                  │
│    └ kubectl create job --from=cronjob/cctv-prune -n cctv-system│
│         └ pod on radxa（arm64，直连出口与 AList 一致）           │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. 三层职责（不要混）

| 层 | 回答的问题 | 载体 | 变更频率 |
|---|---|---|---|
| **代码/镜像** | 跑什么逻辑 | `jobs/<name>/` + GHCR | 每次改代码 |
| **声明/同步** | 集群里应该有什么 | `my-argocd-manifests`（ArgoCD） | 改配置时 |
| **时钟/参数** | 什么时候跑、传什么参数 | AWS Scheduler + workflow inputs | 改调度时 |

> 这条分层是本仓 `README.md`「时钟、编排、执行三件事解耦」的落地版本。
> CronJob 的 `schedule` 永远是 `0 0 31 2 *`（2 月没有 31 日）+ `suspend: true`，**它自己永不触发**。

---

## 4. 详细流程

### 4.1 CI：构建（`.github/workflows/build-cctv-pruner.yml`）

| 步骤 | 内容 | 失败后果 |
|---|---|---|
| 触发 | `push` 到 main 且命中 `jobs/cctv-pruner/**`；打 `v*` tag；手动 dispatch | — |
| `job: lint` | `py_compile` + `python -m unittest discover -s tests` | **不构建镜像** |
| `job: build-and-push` | `needs: lint`，原生 arm 机器 `ubuntu-24.04-arm` | — |
| 推送 | `ghcr.io/nvd11/cctv-pruner`，标签 `latest` / `sha-<40>` / `v*` | — |
| 摘要 | Step Summary 记录 image、platform、digest、commit | — |
| CD | `repository_dispatch` 通知 manifests 仓 | 镜像已存在，回写可手工补 |

### 4.2 CD：digest 回写（manifests 仓）

```yaml
on:
  repository_dispatch:
    types: [update-app-image-digest]
client_payload: { svc_name: "cctv-prune", image_digest: "sha256:<64hex>" }
```

workflow 行为（实测）：

1. `svc_name` 必须在白名单内（已加入 `cctv-prune`），否则直接失败
2. `manifest_file = argocd-apps/${SVC_NAME}-app.yaml`，文件必须存在
3. **必须存在**形如 `          digest: sha256:<64hex>` 的行，否则失败（所以清单里保留了占位 digest）
4. `sed` 就地替换该行
5. 以 `github-actions[bot]` 身份 `git commit -m "ci: update … [skip ci]"` 并 push

### 4.3 部署：ArgoCD

- Application `cctv-prune`，source = `charts/generic-batch-job`，destination = `tencent-dp1-cluster/cctv-system`
- `syncPolicy.automated: { prune: true, selfHeal: true }` → 检测到新 commit 后自动同步，约 3 分钟
- 同步结果：CronJob 的 `image` 变成 `ghcr.io/nvd11/cctv-pruner@sha256:…`
- `argocd-apps/` 目录本身由 app-of-apps `root-bootstrap` 纳管：**push 即创建 Application**，无需手工 apply

### 4.4 运行时：派发

```
AWS Scheduler (cron 04:20 Asia/Shanghai)
  → EventBridge Rule → API Destination → workflow_dispatch
     inputs: { dry_run: "false", retention_days: "7" }
  → prune-cctv.yml：
       kubectl create job --from=cronjob/cctv-prune cctv-prune-<dry>-<ts> -n cctv-system
       （jq 去重后注入 DRY_RUN / RETENTION_DAYS）
  → 轮询 ≤ 150×5s；Job 消失 6 次判失败
  → Step Summary 贴日志；日志含 [ALERT] 判红
```

---

## 5. 镜像版本的三种更新方式

| 方式 | 触发者 | 适用场景 | 注意 |
|---|---|---|---|
| **自动（默认）** | `main` 构建成功 | 日常迭代 | 依赖 CD 开关与 dispatch token |
| **手动改 digest** | 人或脚本编辑清单 | 回滚、跨仓对齐 | digest 不可变，最安全 |
| **改 tag** | 编辑 `tag:` 并清空 `digest:` | 临时调试 | `digest` 非空时 tag 被完全忽略 |

chart 渲染逻辑：

```gotemplate
{{ if .Values.image.digest }}{{ repository }}@{{ digest }}{{ else }}{{ repository }}:{{ tag }}{{ end }}
```

**回滚示例**（把 digest 改回上一个）：

```bash
git log --oneline -5 -- argocd-apps/cctv-prune-app.yaml
git show <旧commit>:argocd-apps/cctv-prune-app.yaml | grep digest
# 改回该 digest → push → ArgoCD 自动同步
```

---

## 6. 必需的仓库配置

### 6.1 `scheduled-batch-jobs`（代码仓）

| 类型 | 名称 | 用途 | 缺失后果 |
|---|---|---|---|
| Variable | `ENABLE_GITOPS_DIGEST_DISPATCH=true` | 开启 CD 回写 | CD 步骤被跳过，digest 停在占位符 |
| Secret | `ARGOCD_MANIFESTS_DISPATCH_TOKEN` | 跨仓 dispatch | CD 步骤失败（镜像已构建） |
| Secret | `K3S_SSH_HOST` / `K3S_SSH_USER` / `K3S_SSH_PRIVATE_KEY` | 派发 workflow 连控制面 | 无法触发任何任务 |

> `ARGOCD_MANIFESTS_DISPATCH_TOKEN` 建议用 **fine-grained PAT**，只给 `my-argocd-manifests`：
> `Contents: Read and write` + `Actions: Read and write`，设 90 天过期。

### 6.2 集群侧

| 项 | 说明 |
|---|---|
| `oci-vault-reader` Secret | 存在于 `cctv-system`，是 ESO 访问 OCI Vault 的凭据 |
| `SecretStore cctv-vault-store` | 指向 `shared-secrets-vault`（ap-singapore-1） |
| `ExternalSecret cctv-pruner-secrets` | 把 `cctv-pruner-alist-password` 同步成 `ALIST_PASSWORD` |

---

## 7. 约定与红线

| # | 约定 | 原因 |
|---|---|---|
| 1 | **用 digest 部署，不用 tag** | tag 可变；回滚时无法确定实际版本 |
| 2 | **镜像必须 `linux/arm64`** | 目标节点 radxa 是 aarch64 |
| 3 | **测试不过不出镜像** | `build` job `needs: lint` |
| 4 | **凭据不入 Git** | 全部走 OCI Vault + ESO；仓库里只有 secret 名字 |
| 5 | **CD 提交带 `[skip ci]`** | 避免 manifests 仓库的改动反向触发构建 |
| 6 | **CronJob 永久 suspend** | 双重调度 = 同一批数据跑两遍 |
| 7 | **镜像包需可匿名拉取** | 集群无 `imagePullSecrets`；现有 cctv 镜像实测匿名 HTTP 200 |
| 8 | **fail loud** | 失败一律 `[ALERT]` + 非零退出，禁止只打 warning |

---

## 8. 失败模式速查

| 现象 | 根因 | 处理 |
|---|---|---|
| build 里 `Unit tests` 红 | 逻辑或测试有问题 | 本地 `python3 -m unittest discover -s tests -v` 复现 |
| build 成功但 digest 没变 | `ENABLE_GITOPS_DIGEST_DISPATCH` 未设 / token 过期 | 看 build 日志的 `GitOps dispatch HTTP status` |
| manifests workflow 报 `Unsupported service` | `svc_name` 不在白名单 | 在 `update-app-image-digest.yml` 补名字 |
| manifests workflow 报 `grep` 失败 | 清单里缺 `digest:` 行 | 补占位行（64 位 hex） |
| ArgoCD 一直不同步 | 轮询间隔 / selfHeal 被关 | 检查 Application `sync.status`，必要时手动 refresh |
| pod `ImagePullBackOff` | 包是 private | 把 GHCR 包设为 Public，或加 `imagePullSecrets` |
| pod `exec format error` | 镜像不是 arm64 | 检查 buildx `platforms` |
| Job 派发了但秒退 | 参数错误 / 镜像问题 | 看 Step Summary 里的日志；注意 `[ALERT]` |

---

## 9. 新增一个服务的 checklist

1. `jobs/<name>/`：程序 + `Dockerfile`（`linux/arm64`）+ `tests/` + `README.md`
2. `.github/workflows/build-<name>.yml`：测试 job + 构建 job + CD dispatch（`svc_name: <name>`）
3. manifests 仓：`argocd-apps/<name>-app.yaml`（含 `digest:` 占位行）+ `external-secret`（如需凭据）
4. manifests 仓：把 `<name>` 加进 `update-app-image-digest.yml` 的白名单
5. 代码仓 secrets/variables：见 §6.1
6. `docs/INVENTORY.md` 与 README 摘要表登记
7. 首次上线：先 `DRY_RUN=true` 手工派生一次，确认输出后再接 AWS

---

## 10. 与既有任务的关系

| 任务族 | 代码位置 | 镜像更新方式 | 时钟 |
|---|---|---|---|
| `sms-flink-etl`（ODS/DWD/REPORT） | 独立仓 | 手动/独立流水线 | AWS → GH Actions |
| `cctv-collector` / `cctv-uploader` | `nvd11/cctv-collector` | 各自的 build workflow → digest 回写 | 无常驻时钟（长跑服务） |
| **`cctv-prune`（本仓）** | `jobs/cctv-pruner/` | **本仓 build workflow → digest 回写** | AWS → GH Actions |

> 共同点：**k8s 里只有 CronJob 模板，真正执行的是运行时派生的 Job**（`nuc-*` / `cctv-prune-*`），跑完 TTL 自删，不进 ArgoCD。
