# demo-email-java

`demo-email` 的 **Java 版**：每天 09:05（Asia/Shanghai）往 `nvd11@163.com` 发一封固定格式的邮件。

行为与 Python 版完全对齐，用来验证「同一套流水线可以承载不同语言/构建体系」。

- 发信：`smtp.163.com:465`（SSL），`jakarta.mail` + Angus Mail
- 凭据：`SMTP_AUTH_CODE` 由 ESO 从 OCI Vault 注入（与 Python 版共用 `demo-email-secrets`）
- 构建：Maven 多阶段（`maven:3.9-eclipse-temurin-21` → `eclipse-temurin:21-jre-alpine`）
- 镜像：`ghcr.io/nvd11/demo-email-java`（linux/arm64）

## 本地

```bash
cd jobs/demo-email-java
mvn -B test                                        # 7 个单测
mvn -B package -DskipTests                         # 产出可执行 fat jar
DRY_RUN=true FORCE_NOW=2026-11-28T09:05:00 java -jar target/demo-email-java.jar
SMTP_AUTH_CODE='<163 授权码>' java -jar target/demo-email-java.jar   # 真发
```

## 环境变量

与 Python 版一致：`SMTP_HOST` / `SMTP_PORT` / `MAIL_FROM` / `SMTP_USER` / `MAIL_TO` /
`SMTP_AUTH_CODE` / `DRY_RUN` / `JOB_NAME` / `MAIL_SUBJECT_PREFIX` / `FORCE_NOW`。

失败一律打印 `[ALERT]` 并退出码 1。
