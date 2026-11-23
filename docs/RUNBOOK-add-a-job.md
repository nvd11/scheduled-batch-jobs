# RUNBOOK：新增一个定时任务

目标：让一个已经能手动跑通的活，变成"每天/每 10 分钟自动跑、失败能看见、时间能一处改"的定时任务。

前置：**执行体必须先能手动跑通。** 排错顺序永远是"先手动、再自动"；把没能手动跑通的东西挂上调度，只会得到一个没人看得懂的定时失败。

---

## Step 1 · 执行体（在做活的仓库里）

### 1a. 普通批处理（推荐体量）

在目标集群建一个 **CronJob 当 Job 模板**，schedule 设成永不触发的值并 suspend：

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: <job-template-name>
  namespace: batch-jobs
spec:
  schedule: "0 0 31 2 *"        # 2 月没有 31 号 = 永不触发
  suspend: true                 # 时钟在 AWS Scheduler 手里
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 1200   # 必须有硬超时
      template:
        spec:
          restartPolicy: OnFailure
          nodeSelector:
            kubernetes.io/hostname: nuc
          containers:
            - name: runner
              image: <your-image>:<tag>
              env:
                - name: RETENTION_DAYS
                  value: "7"
```

```bash
kubectl -n batch-jobs apply -f job-template.yaml
kubectl -n batch-jobs create job --from=cronjob/<job-template-name> manual-test-1   # 手动验证
kubectl -n batch-jobs logs job/manual-test-1 --tail=100
kubectl -n batch-jobs delete job manual-test-1
```

### 1b. 不适用 Flink 的判断标准

用法是"扫一遍数据、干一件事、退出"，就别上 Flink：

- 没有窗口 / 没有 keyed state / 不需要 exactly-once
- 不需要并行拆分
- 单次几秒到几分钟

这种情况用 `python:3.12-slim` 之类的小镜像，20 行脚本即可。Flink 只在这些条件成立时才值得：多源流 join、需要 checkpoint 断点续跑、要跟 Iceberg 湖仓做大吞吐写入。

---

## Step 2 · 编排（在 GitHub 上）

把 [`templates/workflow-dispatch-consumer.yml`](../templates/workflow-dispatch-consumer.yml) 复制进执行仓库的 `.github/workflows/`，改名并填空：

必须写死的四件事：

- `timeout-minutes`
- `concurrency`（`cancel-in-progress: false`，让后来者排队）
- `activeDeadlineSeconds`
- 失败时 `describe` + 日志 + `exit 1`

验证：

```bash
# 手动 dispatch 一次，确认 workflow 本身可用
curl -s -X POST \
  -H "Authorization: Bearer *** \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/actions/workflows/<wf>.yml/dispatches \
  -d '{"ref":"main","inputs":{"dry_run":"true"}}'
```

---

## Step 3 · 路由（Rule / API Destination / Connection）

### 3a. 优先复用现有 Connection

```bash
aws events list-connections --region ap-southeast-1
# 看 ConnectionState 是否为 AUTHORIZED
```

### 3b. 建 API Destination（仅当端点与现有 3 个都不同）

```bash
aws events create-api-destination \
  --region ap-southeast-1 \
  --name github-trigger-<job> \
  --invocation-endpoint "https://api.github.com/repos/<owner>/<repo>/actions/workflows/<wf>.yml/dispatches" \
  --http-method POST \
  --invocation-rate-limit-per-second 1 \
  --connection-arn <github-api-conn 的 ARN>
```

### 3c. 建 Rule（事件 → HTTP body 的翻译层）

```bash
aws events put-rule \
  --region ap-southeast-1 \
  --name rule-trigger-github-<job> \
  --event-pattern '{"source":["custom.<domain>"],"detail-type":["<job-detail-type>"]}'

aws events put-targets \
  --region ap-southeast-1 \
  --rule rule-trigger-github-<job> \
  --targets '[{
    "Id":"1",
    "Arn":"<上一步 api-destination 的 ARN>",
    "RoleArn":"arn:aws:iam::186004631963:role/eventbridge-scheduler-exec-role",
    "Input":"{\"ref\":\"main\",\"inputs\":{\"dry_run\":\"true\"}}"
  }]'
```

> `Input` 里的 `inputs` 键名必须与 workflow 的 `workflow_dispatch.inputs` 声明完全一致，否则 GitHub 返回 422（而这 422 只会出现在 EventBridge 的 metrics 里，不会有人主动告诉你 —— 这是最典型的静默失败）。

---

## Step 4 · 时钟（Schedule）

```bash
export DRY_RUN=1            # 先看它要干什么
./scripts/register-schedule.sh \
  --name <job-name> \
  --expression 'cron(0 5 * * ? *)' \
  --timezone Asia/Shanghai \
  --source custom.<domain> \
  --detail-type <job-detail-type> \
  --input '{"action":"<job>"}'

export DRY_RUN=0            # 确认后真建
```

脚本是幂等的：已存在则走 `update-schedule`。

**时区务必显式指定**，不要依赖默认 UTC。

---

## Step 5 · 登记与验收

1. 更新 [`docs/INVENTORY.md`](INVENTORY.md) 与 README 摘要表
2. 验收三件事都要亲眼看到：
   - `aws scheduler get-schedule --name <job>` 显示下一次触发时间正确
   - 到点后 GitHub 上有对应的 `workflow_dispatch` run，且 `conclusion: success`
   - `kubectl -n batch-jobs get job` 里有那个 job，日志里有预期输出
3. 破坏性任务：**第一次上线必须是 dry-run**，把"本来会删/会改什么"的清单贴出来人工确认，再把 `dry_run` 翻成 `false`

---

## 常见坑

| 坑 | 症状 | 处理 |
|---|---|---|
| `inputs` 键名不匹配 | EventBridge 有 InvocationFailed 指标，GitHub 上什么都没有 | 对照 workflow 的 `inputs` 声明逐字校验 |
| `ref` 指向的分支没有该 workflow | 同上，422 | 用 default branch，或临时用 feature 分支做灰度 |
| 忘写 `concurrency` | 高频任务出现两个 job 同时跑、数据重复 | 加 `concurrency` + `cancel-in-progress: false` |
| 只看 pod 退出码 | workflow 全绿但业务其实失败 | pod 日志打 `[ALERT]`，workflow 里 grep 到就判失败 |
| 把 CronJob 的 schedule 当真的用 | 任务被触发两次（AWS 一次 + k8s 一次） | CronJob 必须 `suspend: true` + 永不触发的表达式 |
| 时区留空 | 差 8 小时 | 显式 `--timezone` |
| 用 self-hosted runner 跑 public repo | 任何人 fork PR 即可在你机器上执行代码 | 一律用 GitHub 托管 runner |
