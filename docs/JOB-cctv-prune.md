# 任务设计：CCTV 录像只保留一周（`cctv-prune-daily`）

> 状态：**方案定稿，待接线**
> 定稿日期：2026-11-24
> 相关：`docs/QUARK-API-NOTES.md`（API 实测结论）、`docs/RUNBOOK-add-a-job.md`（接线五步）

---

## 1. 目标与非目标

**目标**：夸克网盘上的家庭监控录像，**只保留最近 7 天**，每天自动清理更早的日期目录。

**非目标**：

- 不做上传（上传链路在摄像头那侧，本任务只做下行清理）
- 不做转码、不做归档到冷存储（以后需要另立任务）
- 不负责“空间告警”，只负责把过期数据删掉
- **不负责清理回收站**——那是独立任务 `quark-recycle-purge-weekly`（§6.5），凭据更重、风险更高、频率更低

---

## 2. 数据现状（2026-11-24 实测）

| 项目 | 值 |
|---|---|
| 磁盘路径 | `/Quark/CCTV_Records/锦绣世家_客厅/` |
| 目录规则 | 一天一个 `YYYY-MM-DD` 目录 |
| 文件规则 | `cctv_YYYYMMDD_HHMMSS.mp4`，**每 5 分钟一段** |
| 单日体量 | 约 96 段 / **12~15 GB** |
| 当前存量 | **40 天 / 3626 文件 / 486.4 GB**（从未清理） |
| 只留 7 天 | 保留约 **79 GB**，可回收 **407 GB** |

⚠️ **关键事实：文件的 `modified` 是上传时间，不是拍摄时间**（曾出现 11-20 的录像在 11-22 08:56 补传）。
→ 保留判定**必须用目录名里的日期**，用 mtime 会永久留下旧录像。

---

## 3. 保留口径定义

```
today   = 当前日期（Asia/Shanghai）
cutoff  = today - (RETENTION_DAYS - 1)      # 保留 cutoff … today 共 RETENTION_DAYS 个日历日
删除集   = { 目录 d | d 匹配 ^\d{4}-\d{2}-\d{2}$ 且 d < cutoff }
```

- 按**日历日**而非 7×24 小时，符合人的直觉，也便于人工核对。
- 某天因断电/断流缺录像时，不影响判定（只按现有目录算）。
- **今天这个目录永不在删除集内**（即使 `RETENTION_DAYS=0` 也必须被守卫拦下）。

---

## 4. 上线节奏（已确认）

| 阶段 | RETENTION_DAYS | 说明 |
|---|---|---|
| 第 1 晚 | **14** | 温和试探。只砍到剩 14 天，验证 API、权限、日志、风控都没问题 |
| 第 2 晚 | **7** | 收到目标水位（约 79 GB） |
| 第 3 晚起 | 7 | 每天只删 1 个目录，进入稳态 |

首晚即为 `DRY_RUN=false` 的**正式执行**，但 `DRY_RUN=true` 的手动演练必须先跑过一轮（见 §9 验收）。

---

## 5. 架构落点

沿用本仓统一架构，不引入新模式：

```
AWS EventBridge Scheduler  cron(20 4 * * ? *)  Asia/Shanghai
  └─> Rule: source=custom.cctv, detail-type=prune-daily
        └─> API Destination: github dispatches (reuse Connection github-api-conn)
              └─> GitHub Actions: .github/workflows/prune-cctv.yml
                    └─> SSH 到 k3s 控制面 → kubectl create job --from=cronjob/cctv-prune -n batch-jobs
                          └─> pod on nuc → HTTP 调 AList(10.0.1.227:5244) → 夸克
```

**为什么是 k3s Job 而不是 systemd timer**：时钟留在 AWS（本仓铁律）、日志进 GitHub Step Summary、执行体在内网能直连 AList。轮询与失败可见性都由 workflow 负责。

**为什么不在 GitHub runner 直连夸克**：要把 Cookie 复制一份到 GitHub Secrets，等于第二处真相，且 Cookie 轮换会静默断掉。

### 5.1 出口 IP 约束（2026-11-24 实测）

夸克对“异地登录”很敏感，所以执行体落点不是性能问题，是**出口 IP 问题**。

| 主机 | 直连出口 IP |
|---|---|
| Starfive `10.0.1.227`（AList 所在） | `223.73.61.33`（广东 广州 移动） |
| NUC `10.0.1.113`（pod 落点） | `223.73.61.33` ← 完全相同 |
| NixOS `10.0.1.3` | `223.73.61.33` |

且 AList 的 systemd 环境里带 `NO_PROXY=...quark.cn,.quark.cn` —— **访问夸克走直连，不经过 mihomo**（mihomo 的代理出口是 `129.221.158.123`，那是另一条路）。

结论：

- 只要在 `10.0.1.0/24` 内且**不走代理**，Quark 看到的出口就是同一个 IP → **k3s Job 落 nuc 与 AList 行为一致，不需要为了 IP 把任务搬到星光板**。
- pod 必须用 `nodeSelector: kubernetes.io/hostname: nuc` **钉死**。
- pod 内**禁止注入 `HTTP_PROXY` / `HTTPS_PROXY`**（继承代理后出口变成 OCI 的 IP，等于异地登录）。
- **绝不允许**调度到 free-arm-vm / 腾讯云 CP / 任何云节点：出口是云厂商 IP。

### 5.2 集群已有的 OCI Vault 集成（直接复用，不另造）

实测（2026-11-25）：`tencent-dp1-cluster` 已部署 **External Secrets Operator v2.9.0**，机制如下：

```
OCI Vault: shared-secrets-vault（gzviwf7s…，litellm-prod compartment）
   ↑ UserPrincipal（邮箱用户 API Key）
SecretStore/<ns>/cctv-vault-store
   ├─ auth.secretRef → k8s Secret `oci-vault-reader`（fingerprint + privateKey）
   └─ vault / tenancy / user OCID 内联
ExternalSecret（refreshInterval: 1h）
   └─ remoteRef.key = Vault 里的 secret 名  →  k8s Secret 的某个 key
```

| 组件 | 实况 |
|---|---|
| 控制器 | `external-secrets/external-secrets` v2.9.0 |
| 现役 SecretStore | `cctv-system/cctv-vault-store`、`higress-system/…`、`llm-system/…`（均 Valid） |
| 坏的 SecretStore | `default/oci-vault-store` → `InvalidProviderConfig / unable to create client`（待查） |
| GitOps 落位 | `my-argocd-manifests`：`infrastructure/cctv/secretstore.yaml` + `argocd-apps/cctv-*.yaml` |
| 渲染方式 | Helm chart `generic-web-service-v2` 的 `externalSecret:` 段 |

**对本案的意义**：本任务不手工建 k8s Secret、不在集群里放 OCI 凭据；只增一条 ExternalSecret，Vault 那边换密码后 1 小时内自动生效。

---

## 6. 执行体：pod 里跑什么

### 6.1 认证：专用最小权限用户（经 ESO 从 OCI Vault 注入）

- 在 AList 建专用用户 `cctv-pruner`，**只授予 `/Quark/CCTV_Records` 的删除权限**
- 密码存 **OCI Vault `shared-secrets-vault`**，由 **External Secrets Operator** 自动同步成 k8s Secret（`refreshInterval: 1h`）
- 集群已有成熟机制（详见 §5.2），Git 里只出现 secret 名字，永不出现明文
- **不使用 AList 管理员 token**：即使泄露，坏人也只能动这个监控目录

**凭据落位**

| 凭据 | 存放位置 | 用途 |
|---|---|---|
| AList 管理员账号 `alist-admin-credentials` | OCI Vault `shared-secrets-vault`（ap-singapore-1） | **仅供运维**取 Cookie / 排查；**禁止注入 Job** |
| `cctv-pruner` 密码（待建） | OCI Vault `shared-secrets-vault` → ESO → k8s Secret | Job 运行时使用 |
| 夸克 Cookie | **不入 Vault** | 分钟级轮换，快照必然过期；运行时在星光板本地现取（见 §6.5） |
| 加密密钥 | `shared-secrets-vault` 的 `litellm-secrets-key`（HSM） | 复用现有密钥，不新建 |

⚠️ OCI Vault 运维注意（实测）：secret 删除是**预约制**（最短 1 天），期间同名不可重建；更新内容时 `secretContent.name` **不能与已有版本重名**，否则返回 `name already exists`；OCI CLI 的 `ServiceError` 不是 JSON，`jq` 会解析失败——先看原始输出再管道。

### 6.2 参数（env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ALIST_URL` | `http://10.0.1.227:5244` | 内网直连 |
| `ALIST_USERNAME` / `ALIST_PASSWORD` | （ESO 注入的 Secret） | `cctv-pruner` 账号，每次运行 `POST /api/auth/login` 换 JWT（AList JWT 有效期 48h，因此存密码而非存 token） |
| `TARGET_PATH` | `/Quark/CCTV_Records/锦绣世家_客厅` | 唯一作用域 |
| `RETENTION_DAYS` | `7` | 见 §4 分阶段 |
| `DRY_RUN` | `true` | 默认只读；正式执行显式设 `false` |
| `MAX_DELETE_DIRS` | `40` | 单次删除目录数上限，超出则中止并告警（防跑飞） |

> **本任务不碰回收站**，因此没有 `CLEAR_RECYCLE_BIN` 这个开关。回收站清理是**独立任务**，见 §6.5。

### 6.3 主流程（伪代码）

```
0. 列目录若返回 401 / code 31001 → 打 [ALERT] "AList 的夸克 Cookie 已失效，需运行 alist-quark-cookie-updater" → 退出 1
   （零成本顺带当 Cookie 健康监测，因为每天都会跑）
1. 列出 TARGET_PATH 下所有子目录（refresh=true 绕缓存）
2. 过滤：只保留 name 匹配 ^\d{4}-\d{2}-\d{2}$ 的目录
3. 校验：TARGET_PATH 必须精确等于预期值；目录数 < 1 或 > 90 → [ALERT] 退出 1（防挂错盘）
4. cutoff = today - (RETENTION_DAYS - 1)
5. victims = [d | d < cutoff]；断言 today ∉ victims
6. 若 len(victims) > MAX_DELETE_DIRS → [ALERT] 中止，不删
7. 对每个 victim 统计 (文件数, 字节数)  -- 用于清单
8. 打印清单表（无论 dry-run 与否都打）：
     [DRY_RUN] 将删除 2026-11-05  96 files  13.2 GB
     -------------------------------------------------
     合计 12 天 / 1148 文件 / 158.7 GB；保留 7 天
9. DRY_RUN=true → 正常退出 0
10. DRY_RUN=false → POST /api/fs/remove {dir: TARGET_PATH, names: [全部 victims]}（一次调用）
11. 回查：重列目录，断言 victims 全部消失、保留集完好
12. 任一校验失败 → 打 [ALERT] → 退出 1
```

### 6.4 幂等与重跑

- `remove` 对不存在的名字是空操作 → 重跑安全
- 若上传侧往旧日期补传，下一次运行会再次命中 `< cutoff` 并清掉（自愈）
- 一次调用删多个目录，避免“删了一半”的中间态

### 6.5 第二把钥匙：回收站清理（独立任务）

**决策：拆成两个任务，不塞进同一个 job。**

理由不是洁癖，是这三个维度全都对不上：

| 维度 | 任务一 `cctv-prune-daily` | 任务二 `quark-recycle-purge-weekly` |
|---|---|---|
| 凭据 | AList 专用用户 token（作用域仅 `/Quark/CCTV_Records`） | 夸克 Cookie（**等价整个网盘的读写删权限**） |
| 可逆性 | 可恢复（进回收站） | **不可逆**（物理销毁） |
| 需要的频率 | 每天 | 每周一次，甚至可人工触发 |
| 失败后果 | 今晚没删掉，明天再删 | 空间不回血，但数据没丢 |

实现要点：

- 独立 workflow `purge-quark-recycle.yml` + 独立 Schedule（暂定 `cron(0 5 ? * SUN *)` Asia/Shanghai）
- **凭据获取：跑在星光板（10.0.1.227）本地，直接只读打开 AList 的 `data/data.db`，从 `x_storages.addition` 取 Cookie**
  - 零 AList 凭据、零 Vault 副本、永远拿到最新值（夸克 Cookie 是分钟级轮换的）
  - 形态：时钟仍在 AWS，workflow SSH 到星光板执行本地脚本
  - 备选（不推荐）：`GET /api/admin/storage/get?id=1` 取 Cookie，但需要管理员身份，权限过大
- **不要在 OCI Vault 里存 Cookie 快照**：实测 25 分钟内就会变；快照既不能用于运行，也几乎无冷备价值（真丢了重新登录夸克即可）
- 调用：`POST /1/clouddrive/file/recycle/remove?pr=ucpro&fr=pc`，body `{"select_mode":1}`（同步返回 `finish:true`）
- **清空前必须先 `recycle/list` 快照并写入日志**：`select_mode=1` 是“全清”，无法区分是谁删的，清单是唯一的追溯凭据
- 第一版**不上线**，等任务一稳定运行两周后再启用

⚠️ **已知风险**：`select_mode=1` 会连带清掉人工删除的其他内容（历史上 09-20 的错别字目录、10-16 的旧 `CCTV_Records` 都是人工删的）。三条应对路线：

| 路线 | 做法 | 代价 |
|---|---|---|
| A | 约定“回收站只放监控录像”，人工删除的东西自行在客户端先清 | 依赖人的自律 |
| B | 清空前把清单打进日志，事后可追责/可发现误伤 | 只能事后发现 |
| C | 探测夸克是否有**“彻底删除/跳过回收站”**参数，有则任务二整个消失 | 需要一次参数探测（只读试探，不执行删除） |

---

## 7. 编排层（GitHub Actions）

复制 `templates/workflow-dispatch-consumer.yml`，本任务的差异点：

| 项 | 值 |
|---|---|
| workflow 文件 | `prune-cctv.yml` |
| `on.workflow_dispatch.inputs` | `dry_run`(choice true/false)、`retention_days`(string, default `7`) |
| `concurrency.group` | `scheduled-cctv-prune`，`cancel-in-progress: false` |
| `timeout-minutes` | `20` |
| Job `activeDeadlineSeconds` | `1200` |
| 失败判定 | pod 退出码非 0 **或** 日志出现 `[ALERT]` |
| 归档 | Step Summary 贴删除清单表 + 末尾 2000 行日志 |

---

## 8. 路由层与时钟层

```bash
# API Destination（新端点，需新建；Connection 复用 github-api-conn）
aws events create-api-destination --region ap-southeast-1 \
  --name github-trigger-cctv-prune \
  --invocation-endpoint "https://api.github.com/repos/<owner>/<repo>/actions/workflows/prune-cctv.yml/dispatches" \
  --http-method POST --invocation-rate-limit-per-second 1 \
  --connection-arn <github-api-conn ARN>

# Rule
aws events put-rule --region ap-southeast-1 \
  --name rule-trigger-github-cctv-prune \
  --event-pattern '{"source":["custom.cctv"],"detail-type":["prune-daily"]}'
# target Input: {"ref":"main","inputs":{"dry_run":"false","retention_days":"7"}}

# Schedule（04:20 Asia/Shanghai；上传高峰在 00:00~01:50，避开）
DRY_RUN=0 ./scripts/register-schedule.sh \
  --name cctv-prune-daily \
  --expression 'cron(20 4 * * ? *)' \
  --timezone Asia/Shanghai \
  --source custom.cctv --detail-type prune-daily \
  --input '{"action":"prune-daily"}'
```

⚠️ `inputs` 键名必须与 workflow 声明逐字一致，否则 422 只在 EventBridge 指标里静默出现。

---

## 9. 验收清单（每晚必看）

| # | 检查 | 期望 |
|---|---|---|
| 1 | GitHub run 的 Step Summary | 有删除清单表（日期/文件数/GB/合计） |
| 2 | run 结论 | `success`，无 `[ALERT]` |
| 3 | AList 目录数 | 从 N 降到 N - 删除数，且保留集一天不少 |
| 4 | 记录异常次日复核 | `recycle/list` 的 `total` 相应增加（说明是入回收站而非物理删） |

**首晚（RETENTION_DAYS=14）额外人工确认**：打开 PC 客户端回收站，抽查是否有刚删的日期目录，确认可恢复路径存在。

---

## 10. 回滚 / 误删恢复

1. `GET /1/clouddrive/file/recycle/list` 找到对应 `file_name` 与 `fid`
2. `POST /1/clouddrive/file/recycle/recover`，**必须带 `select_mode`**
   - ⚠️ 实测：`select_mode=1` 在真实数据上表现为**全量恢复**（曾一次性捞出 67 项历史垃圾）。本任务**不内置自动恢复**，恢复一律人工执行并先小范围验证语义。
3. 恢复后如需再次清理，重新跑一次 job 即可

---

## 11. 待办与开放问题

| # | 事项 | 归属 |
|---|---|---|
| 1 | 建 AList 专用用户 `cctv-pruner`（只给 `/Quark/CCTV_Records` 删除权限） | 需主人批准（改 AList 配置） |
| 2 | 确定 workflow + k8s 清单落哪个仓（建议 `my-argocd-manifests`） | 待主人拍板 |
| 3 | ~~`recycle/remove` 的 body 形状~~ → **已实测**：`{"select_mode":1}`，同步 `finish:true`（见 `QUARK-API-NOTES.md`） | ✅ 完成 |
| 4 | 是否把 `CCTV_Records` 下其他场景目录（如未来的 `_卧室`）纳入同一 job | 出现时再扩 |
| 5 | 是否加“容量水位告警”（>N GB 时 Discord 提醒） | 可选增强 |
| 6 | **探测夸克是否有“彻底删除/跳过回收站”参数**（路线 C）；若有，任务二可取消 | 待验证 |
| 7 | 任务二 `quark-recycle-purge-weekly` 上线时间 | 任务一稳定两周后 |
