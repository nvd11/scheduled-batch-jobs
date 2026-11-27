# demo-email

最小可用的批处理 demo：**每天 09:00（Asia/Shanghai）往固定邮箱发一封固定格式的邮件**。

- 发信：`smtp.163.com:465`（SSL），账号 `nvd11@163.com`，**授权码由 ESO 从 OCI Vault 注入**
- 收信：`nvd11@163.com`（自收自发）
- 运行：k8s `default/demo-email`（CronJob 挂起，由 AWS Scheduler + GitHub Actions 唤醒）

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SMTP_HOST` | `smtp.163.com` | SMTP 服务器 |
| `SMTP_PORT` | `465` | SSL 端口 |
| `MAIL_FROM` / `SMTP_USER` | `nvd11@163.com` | 发信账号 |
| `MAIL_TO` | `nvd11@163.com` | 收信地址 |
| `SMTP_AUTH_CODE` | — | **Secret 注入**，163 授权码 |
| `DRY_RUN` | `false` | `true` 时只打印邮件内容不发送 |
| `JOB_NAME` | `demo-email` | 写进邮件正文 |
| `MAIL_SUBJECT_PREFIX` | `demo-batch 执行成功` | 主题前缀 |
| `FORCE_NOW` | — | 测试用，固定时间 `YYYY-MM-DDTHH:MM:SS` |

## 本地试跑

```bash
cd jobs/demo-email
DRY_RUN=true FORCE_NOW=2026-11-28T09:00:00 python3 send.py           # 只打印
SMTP_AUTH_CODE='<163 授权码>' DRY_RUN=false python3 send.py          # 真发
```

## 测试

```bash
cd jobs/demo-email && python3 -m unittest discover -s tests -v
```

8 个用例：主题/正文/UTF-8 编码、SMTP 调用顺序、dry-run 不发信、缺凭据告警、发送失败告警。

## 相关

- 部署策略：`docs/CICD-STRATEGY.md`
- 镜像：`ghcr.io/nvd11/demo-email`（linux/arm64）
- k8s 清单：`my-argocd-manifests` 的 `argocd-apps/demo-email-app.yaml`
