# 架构详解（逐跳）

本文把 `Scheduler → Rule → API Destination → Actions → k3s Job` 这条链**逐跳拆开**，给出真实的资源名、payload 形状、重试语义和失败表现。所有 ARN 与字段均来自 `ap-southeast-1` 实测，核对于 2026-11-23。

---

## 0. 全链路一览

```
① Schedule           arn:aws:scheduler:ap-southeast-1:186004631963:schedule/default/<name>
       │  Target = default event bus + EventBridgeParameters(source, detail-type) + Input(JSON)
       ▼
② Event Bus          arn:aws:events:ap-southeast-1:186004631963:event-bus/default
       │  被 Rule 匹配
       ▼
③ Rule               rule-trigger-github-*
       │  Target = API Destination, Input = {"ref":"main","inputs":{...}}
       ▼
④ API Destination    arn:aws:events:...:api-destination/<name>/<uuid>
       │  Endpoint = https://api.github.com/repos/<owner>/<repo>/actions/workflows/<wf>.yml/dispatches
       │  RateLimit = 1 req/s
       ▼
⑤ Connection         github-api-conn (API_KEY / AUTHORIZED)
       │  Secrets Manager: events!connection/github-api-conn/<uuid>
       │  Header: Authorization = "Bearer <PAT>"
       ▼
⑥ GitHub Actions     runs-on: ubuntu-latest, triggered by workflow_dispatch
       │  SSH 到 k3s 控制面，kubectl create job --from=cronjob/<模板>
       ▼
⑦ k3s Job / Pod      namespace batch-jobs，pod 落 nuc 节点
```

---

## ① 时钟层：AWS EventBridge Scheduler

**唯一职责：到点发一条事件。** 它不直接调外网 —— Scheduler 的内置 target 只覆盖 AWS 自家 API（Lambda / SQS / SNS / `putEvents` …），要打外网 HTTPS 必须经 EventBridge 的 Rule + API Destination 绕一步。这不是设计选择，是平台硬约束。

实测样例：

```json
{
  "Name": "sms-flink-ingest-10m",
  "ScheduleExpression": "rate(10 minutes)",
  "ScheduleExpressionTimezone": "UTC",
  "State": "ENABLED",
  "Target": {
    "Arn": "arn:aws:events:ap-southeast-1:186004631963:event-bus/default",
    "RoleArn": "arn:aws:iam::186004631963:role/eventbridge-scheduler-exec-role",
    "Input": "{\"action\":\"cron-trigger\"}",
    "EventBridgeParameters": { "Source": "finance.sms-flink", "DetailType": "minutely-tick" },
    "RetryPolicy": { "MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185 }
  },
  "FlexibleTimeWindow": { "Mode": "OFF" },
  "ActionAfterCompletion": "NONE"
}
```

要点：

- **`FlexibleTimeWindow: OFF`** —— 到点就跑，不给自己 ±15 分钟的自由拖延。
- **时区显式写死**：高频任务用 `UTC`，业务报表类用 `Asia/Shanghai`。绝不留空（留空等于 UTC，最容易出现"差 8 小时"的事故）。
- **重试 185 次 / 事件最长 86400 秒**：这是 Scheduler 侧对"投递到 event bus 失败"的重试，不是对业务失败的重试。业务失败由 workflow 自己的退出码负责。
- **`ActionAfterCompletion: NONE`**：一次性任务不用，周期任务无意义。

---

## ② 路由层：EventBridge Rule → API Destination → Connection

### Rule 匹配

```json
{ "source": ["finance.sms-flink"], "detail-type": ["daily-report"] }
```

Rule 的 target 里同时塞了 **HTTP body**，这是把"调度参数"传给 GitHub 的唯一通道：

```json
{
  "ref": "main",
  "inputs": { "target_env": "finance", "pipeline_stage": "report", "report_period": "DAILY" }
}
```

- `ref` 必须是**存在该 workflow 文件的分支**。`workflow_dispatch` 只能派发 default branch 上已定义的 workflow 文件，测试期可以填 feature 分支做灰度。
- `inputs` 的键必须与 workflow 里 `on.workflow_dispatch.inputs` 声明的一一对应，否则 GitHub 返回 422。

### 为什么必须有 API Destination + Connection

- **API Destination** = 目标 URL + HTTP 方法 + 限流，**可被任何人 `describe-api-destination` 读出来**，所以这里绝不能放凭据。
- **Connection** = 运行时把密钥注入 header 的部件，凭据实体存在 Secrets Manager（`events!connection/<conn>/<uuid>`），配置里只有引用。
- 收益：3 个 destination 共用一个 connection，轮换 token 只改一处；且是可观测的（`ConnectionState: AUTHORIZED`）。
- 代价：概念多、IAM 要同时给 `events:InvokeApiDestination` 与 `secretsmanager:GetSecretValue`。

> 换个说法：AWS 是"凭据与流程解耦"，GCP 的 Workflows 是在 YAML 里内联（`auth: OAuth2(...)`）。job 只有个位数时前者啰嗦但省心，涨到几十个时后者开始复制粘贴。

### 实测：3 个 API Destination

| Destination | Endpoint |
|---|---|
| `github-trigger-nuc-batch` | `.../repos/nvd11/sms-flink-etl/actions/workflows/trigger-nuc-batch-runner.yml/dispatches` |
| `github-trigger-lakehouse-maintain` | `.../repos/nvd11/sms-flink-etl/actions/workflows/maintain-lakehouse.yml/dispatches` |
| `github-trigger-tls-renew` | `.../repos/nvd11/my-argocd-manifests/actions/workflows/renew-jppwl-tls.yml/dispatches` |

限流统一 `1 req/s`。注意：同一个 destination 若被多个 Schedule 共用（当前 4 个 SMS 报表/采集任务共用 `github-trigger-nuc-batch`），**它们是共享这 1 req/s 的**。

---

## ③ 编排层：GitHub Actions

### 为什么会话要设 `concurrency`

高频任务（`rate(10 minutes)`）最典型的故障是**上一次还没跑完，下一次又被 dispatch 进来**，两个 runner 同时 `kubectl create job`，同一批数据被处理两遍。

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ inputs.pipeline_stage || 'default' }}
  cancel-in-progress: false      # 不要取消在跑的，让它跑完；后来者排队
```

`cancel-in-progress: true` 只适合"结果可丢"的任务（如缓存刷新）；批处理应当排队而非互相打断。

### workflow 的四个固定动作

1. **解析 inputs** —— 从 `inputs.*` 或 `github.event.client_payload.*` 取参数
2. **派活** —— SSH 到 k3s 控制面，`kubectl create job --from=cronjob/<模板> <唯一名> --dry-run=client -o json` → 用 `jq` 注入 env → `kubectl apply -f -`
3. **跟单** —— 每 5 秒轮询 job 状态，直到 `Complete` / `Failed` / 超时
4. **归档** —— 成功把末尾 2000 行日志写进 Step Summary；失败则是 `describe` + 末尾 150 行日志 + `exit 1`

### 失败语义

| 现象 | 真实含义 |
|---|---|
| runner 在**恰好等于轮询上限**的时刻失败 | 不是崩溃，是前置条件卡住了（job 根本没起、卡在 ImagePull 或调度不上去） |
| workflow 绿了但数据没变 | 派活成功 ≠ 业务成功；必须靠 pod 内的 `[ALERT]` 之类标记把业务失败抬成 workflow 失败 |
| GitHub 侧 422 | `inputs` 键名和 workflow 声明不一致，或 `ref` 上没有这个 workflow |
| Scheduler 显示 `DISPATCHED` 但 GitHub 没收到 | Connection 的 PAT 过期（`ConnectionState` 会变），或 destination 被禁用 |

---

## ④ 执行层：k3s Job on nuc

### "Flink batch" 的真实形态

集群里**没有常驻 Flink 集群**（`kubectl get pods -A | grep -i flink` 为空，无 JobManager / TaskManager）。所谓 Flink 作业是 **application mode**：每个 K8s Job 的 pod 里起一个自带 mini-cluster，跑完即销毁。

这带来两个直接影响：

- **起得慢**：mini-cluster 引导需要几秒到几十秒，为 3 秒的活付这个代价不值。
- **"放到 Flink 里跑" ≈ "在 batch-jobs 里起个 Job"**：容器里装 Flink 还是装 20 行 Python，是**两个独立决策**，别混为一谈。

### CronJob 只是 Job 模板

```yaml
# kubectl -n batch-jobs get cronjob nuc-batch-runner
SCHEDULE: 0 0 31 2 *   # 永不触发（2 月没有 31 日）
SUSPEND:  True
```

**它自己不会跑。** 时钟完全在 AWS Scheduler 手里。保留 CronJob 的价值是：Job 的 podSpec（镜像、env、资源、nodeSelector）只有一份真相，`kubectl create job --from=cronjob/...` 可以直接从它派生。

### 内网直连

pod 落在 nuc 上，与 Trino / Iceberg(R2) / AList 等在同一张局域网里，**不需要任何穿透**。这是选择"执行体放在内网"而不是"让 GitHub runner 借 tailscale 进内网"的根本原因 —— 后者是给"执行体必须在云上"的场景用的。

实测：nuc 上的 pod 直连 `http://10.0.1.227:5244/api/public/settings` 返回 `200`。

---

## ⑤ 备选方案，以及为什么没选

| 方案 | 为什么没选 |
|---|---|
| **GitHub `on: schedule` 原生 cron** | 最小粒度 5 分钟；高负载时延迟甚至**丢弃**排队任务；public repo 60 天无活动自动禁用。它便宜但不能当准确时钟。 |
| **自托管 runner 装在内网** | public repo + self-hosted runner = 任何人 fork 提 PR 就能在你机器上执行任意代码（GitHub 官方红线）。private repo 才考虑。 |
| **AWS Batch** | 实测 job definition 数量为 0 —— 现有体系根本没用到它。它的价值在 EC2 队列伸缩，而执行体本来就在自有 k3s 里。 |
| **Scheduler → Lambda → 发 HTTP** | 链路短，但每加一个 job 就多一个函数要维护；且 Lambda 在云上，仍然碰不到内网，最后还是要加一层跳板。 |
| **全部塞进 Flink 流作业** | 流作业的价值在 checkpoint / exactly-once / 窗口 / 背压。删目录、发报表这类"扫一遍干一件事"没有状态，用 Flink 只会把 3 秒变 40 秒，并把"删除重放"变成新风险。 |

---

## ⑥ 已知薄弱点（如实记录）

1. **CI 的 SSH 私钥是操作者的通用私钥**（`gateman@current-machine`），在控制面上等于免密 sudo。CI 若写错一行，误伤面是整个控制面，而不是 `batch-jobs` 命名空间。
   - 更优解：namespace 级 ServiceAccount + Role（只给 `batch/jobs` 的 create/get/delete、`pods` / `pods/log` / `events` 只读），workflow 用 `https://<api-server>:6443` + Bearer token，不走 SSH、不 sudo。
   - 代价：6443 必须公网可达（当前可，未鉴权返回 401），暴露面靠 RBAC 兜。
2. **Connection 里的 PAT 是 classic 且权限过大**（`admin:org + repo + user`），长期未轮换。持有它等于拥有组织级写权限 —— 可以 push workflow 到 main 从而间接取得 CI 里的任何 secret。建议换 fine-grained（仅 `Actions: write` + 单仓）。
   - 注意区分：**fork 提 PR 拿不到 secret**（保护的是"无写权限的人"）；这条防线对"持有写权限 PAT 的人"无效。
3. **API Destination 限流 1 req/s 被 4 个任务共享**，若未来增加高频任务需重新评估。
4. **依赖 GitHub Actions 可用性**。GitHub 故障期定时任务会积压；Scheduler 侧重试上限（185 次 / 24h）决定了最多能容忍多长的中断。

---

## ⑦ 排查入口速查

```bash
# 我到底有哪些定时任务？下一次什么时候跑？
aws scheduler list-schedules --region ap-southeast-1

# 这个任务几点跑、发给谁？
aws scheduler get-schedule --region ap-southeast-1 --name <name>

# 这条规则把事件发到哪？带了什么 body？
aws events list-targets-by-rule --region ap-southeast-1 --rule <rule>

# destination 端点是什么？Connection 还健康吗？
aws events list-api-destinations --region ap-southeast-1
aws events list-connections --region ap-southeast-1     # 看 ConnectionState

# GitHub 侧这次到底跑了没有？
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/<owner>/<repo>/actions/workflows/<wf>.yml/runs?per_page=5" \
  | jq '.workflow_runs[] | {created_at, event, status, conclusion, html_url}'

# 集群侧这个 job 到底起了没有？
ssh <k3s-host> 'sudo kubectl -n batch-jobs get job,pods | tail -20'
```
