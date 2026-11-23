# 任务总账（INVENTORY）

> 以 AWS `ap-southeast-1` 实测为准，核对于 **2026-11-23**。
> 任何新增 / 变更 / 下线任务，**必须同步改这张表**，否则这张表就废了。

## 汇总

| # | Schedule | 表达式 | 时区 | 事件 source / detail-type | Rule | API Destination | 执行 |
|---|---|---|---|---|---|---|---|
| 1 | `sms-flink-ingest-10m` | `rate(10 minutes)` | UTC | `finance.sms-flink` / `minutely-tick` | `rule-trigger-github-workflow` | `github-trigger-nuc-batch` | `sms-flink-etl` · `trigger-nuc-batch-runner.yml` |
| 2 | `sms-flink-report-daily` | `cron(50 23 * * ? *)` | Asia/Shanghai | `finance.sms-flink-report` / `daily-report` | `rule-trigger-github-daily-report` | `github-trigger-nuc-batch` | `sms-flink-etl` · `trigger-nuc-batch-runner.yml` |
| 3 | `sms-flink-report-weekly` | `cron(0 9 ? * MON *)` | Asia/Shanghai | `finance.sms-flink-report` / `weekly-report` | `rule-trigger-github-weekly-report` | `github-trigger-nuc-batch` | `sms-flink-etl` · `trigger-nuc-batch-runner.yml` |
| 4 | `sms-flink-report-monthly` | `cron(30 9 1 * ? *)` | Asia/Shanghai | `finance.sms-flink-report` / `monthly-report` | `rule-trigger-github-monthly-report` | `github-trigger-nuc-batch` | `sms-flink-etl` · `trigger-nuc-batch-runner.yml` |
| 5 | `sms-flink-lakehouse-maintain` | `cron(0 4 * * ? *)` | Asia/Shanghai | `finance.sms-flink-maintain` / `lakehouse-maintain` | `rule-trigger-github-lakehouse-maintain` | `github-trigger-lakehouse-maintain` | `sms-flink-etl` · `maintain-lakehouse.yml` |
| 6 | `jppwl-tls-cert-renew-weekly` | `cron(0 4 ? * SUN *)` | Asia/Shanghai | `infra.tls-cert` / `renew-jppwl-tls` | `rule-trigger-github-tls-renew` | `github-trigger-tls-renew` | `my-argocd-manifests` · `renew-jppwl-tls.yml` |

全部 6 个 Schedule 状态 `ENABLED`，`GroupName=default`，`FlexibleTimeWindow=OFF`，`ActionAfterCompletion=NONE`，执行角色 `arn:aws:iam::186004631963:role/eventbridge-scheduler-exec-role`，重试策略统一 `MaximumRetryAttempts=185 / MaximumEventAgeInSeconds=86400`。

---

## 逐条详情

### 1. `sms-flink-ingest-10m`

| 字段 | 值 |
|---|---|
| 用途 | 每 10 分钟把 Gmail IMAP 里的动账短信增量抓进 Iceberg ODS（`raw_sms_records`）并推进 DWD |
| Schedule Target Input | `{"action":"cron-trigger"}` |
| Rule → Destination body | `{"ref":"main"}` |
| CronJob 模板 | `batch-jobs/nuc-batch-runner` |
| 备注 | 与另外 3 个报表任务**共用同一个 API Destination**，共享 1 req/s 限流 |

### 2. `sms-flink-report-daily`

| 字段 | 值 |
|---|---|
| 用途 | 每日报表 |
| Rule → Destination body | `{"ref":"main","inputs":{"target_env":"finance","pipeline_stage":"report","report_period":"DAILY"}}` |
| CronJob 模板 | `batch-jobs/nuc-batch-runner`（注入 `REPORT_PERIOD=DAILY`） |

### 3. `sms-flink-report-weekly`

| 字段 | 值 |
|---|---|
| 用途 | 每周报表（周一 09:00 Asia/Shanghai） |
| Rule → Destination body | `{"ref":"main","inputs":{"target_env":"finance","pipeline_stage":"report","report_period":"WEEKLY"}}` |

### 4. `sms-flink-report-monthly`

| 字段 | 值 |
|---|---|
| 用途 | 每月 1 日 09:30 Asia/Shanghai 报表 |
| Rule → Destination body | `{"ref":"main","inputs":{"target_env":"finance","pipeline_stage":"report","report_period":"MONTHLY"}}` |

### 5. `sms-flink-lakehouse-maintain`

| 字段 | 值 |
|---|---|
| 用途 | Iceberg 湖仓维护（小文件合并 / snapshot 过期 / 孤儿文件清理） |
| Rule → Destination body | `{"ref":"main","inputs":{"retention_threshold":"3d","target_env":"finance"}}` |
| workflow | `sms-flink-etl/.github/workflows/maintain-lakehouse.yml` |

### 6. `jppwl-tls-cert-renew-weekly`

| 字段 | 值 |
|---|---|
| 用途 | 检查 `*.jppwl.asia` 证书剩余天数，≤30 天则用 Cloudflare DNS-01 重签并热更新 k3s secret |
| Rule → Destination body | `{"ref":"main","inputs":{"force_renew":"false","threshold_days":"30"}}` |
| workflow | `my-argocd-manifests/.github/workflows/renew-jppwl-tls.yml` |
| 目标对象 | `default/jppwl-asia-tls`（Kong 网关证书） |

---

## 共用资源

| 类型 | 名称 | 说明 |
|---|---|---|
| Event bus | `default`（`ap-southeast-1`） | 全部任务共用 |
| Connection | `github-api-conn` | `API_KEY` / `AUTHORIZED`；header `Authorization: Bearer <PAT>`；凭据在 Secrets Manager `events!connection/github-api-conn/<uuid>` |
| 执行角色 | `eventbridge-scheduler-exec-role` | 全部 Schedule 共用 |
| k3s 命名空间 | `batch-jobs` | 所有 Job / Job 模板所在 |

---

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-23 | 建仓，首次登记 6 个在跑任务（实测核对） |
