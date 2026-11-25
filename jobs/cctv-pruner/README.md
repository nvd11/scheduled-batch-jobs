# cctv-pruner

清理夸克网盘上的监控录像目录，只保留最近 N 个日历日。

- 判定依据：**目录名里的日期**（`^YYYY-MM-DD$`），不使用 `modified`（那是上传时间，存在补传）
- 删除通道：AList `POST /api/fs/remove`（一次调用删多个目录，幂等，夸克侧进回收站）
- 运行位置：k3s `cctv-system/cctv-prune`（CronJob 挂起，由 AWS Scheduler + GitHub Actions 唤醒）

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ALIST_URL` | | `http://10.0.1.227:5244` | 内网直连 |
| `ALIST_USERNAME` | | `cctv-pruner` | 专用最小权限用户 |
| `ALIST_PASSWORD` | ✅ | — | 由 ESO 从 OCI Vault 注入 |
| `TARGET_PATH` | | `/Quark/CCTV_Records/锦绣世家_客厅` | 作用域，**必须与内置预期值完全一致** |
| `RETENTION_DAYS` | | `7` | 保留天数（含今天） |
| `DRY_RUN` | | `true` | `false` 才真正删除 |
| `MAX_DELETE_DIRS` | | `40` | 单次删除目录数上限，超出即中止 |
| `FORCE_NOW` | | — | 测试用：固定“今天”，格式 `YYYY-MM-DD` |

## 行为

1. 登录 AList → 列目标目录（`refresh=true`，无权限时自动降级并告警）
2. 守卫：作用域必须精确匹配；日期目录数必须在 1~90 之间
3. `cutoff = today - (RETENTION_DAYS - 1)`，删除所有 `< cutoff` 的日期目录
4. 打印清单表（日期 / 文件数 / 大小 / 合计）
5. `DRY_RUN=true` 到此结束；否则一次调用删除
6. 回查：断言待删目录全部消失，保留集完好

退出码：`0` 成功（含 dry-run），`1` 失败。失败一律打印 `[ALERT]`，workflow 会 grep 到并判红。

## 本地试跑

```bash
cd jobs/cctv-pruner
ALIST_URL=http://10.0.1.227:5244 \
ALIST_USERNAME=cctv-pruner ALIST_PASSWORD='<Vault 里取>' \
DRY_RUN=true RETENTION_DAYS=7 \
python3 pruner.py
```

用 `FORCE_NOW=2026-11-25` 可固定“今天”，方便复现。

## 测试

```bash
cd jobs/cctv-pruner
python3 -m unittest discover -s tests -v
```

21 个用例，**纯内存、无网络**：日期边界（跨月/跨年）、目录过滤、删除集、四道守卫、
dry-run 不落刀、一次调用删多个、删除失败、回查残留、登录失败、列表失败。

CI 会在构建镜像**之前**跑一遍（`build-cctv-pruner.yml` 的 `lint` job），测试挂了就不出镜像。

## 镜像

`ghcr.io/nvd11/cctv-pruner:latest` —— **linux/arm64**，由 `.github/workflows/build-cctv-pruner.yml`
在原生 ARM runner 上构建。

## 相关

- 设计与上线节奏：`docs/JOB-cctv-prune.md`
- 夸克 API 与 Cookie 机制：`docs/QUARK-API-NOTES.md`
- k8s 模板：`my-argocd-manifests` 的 `argocd-apps/cctv-prune-app.yaml`
