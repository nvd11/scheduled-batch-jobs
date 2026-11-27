#!/usr/bin/env python3
"""
demo-email — 最小可用的批处理 demo：每天往指定邮箱发一封固定格式的邮件。

设计要点：
  1. 只用标准库（smtplib + email），镜像小、无依赖。
  2. 凭据只从环境变量读取，由 ESO 从 OCI Vault 注入，代码里不出现明文。
  3. 失败一律打印 [ALERT] 并 exit 1（workflow 会 grep 到并判红）。

退出码：0 = 成功（含 DRY_RUN），1 = 失败
"""

import os
import smtplib
import socket
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

CST = timezone(timedelta(hours=8))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def alert(msg: str) -> None:
    print(f"[ALERT] {msg}", flush=True)


def build_message(now: datetime, job_name: str, host: str, mail_from: str, mail_to: str,
                  prefix: str = "demo-batch 执行成功") -> EmailMessage:
    """纯函数：拼一封邮件，便于单测。"""
    subject = f"[{prefix}] {now:%Y-%m-%d %H:%M:%S}"
    body = "\n".join([
        "这是一封由定时批处理任务自动发送的演示邮件。",
        "",
        f"执行时间 : {now:%Y-%m-%d %H:%M:%S} ({now.tzname()})",
        f"任务名称 : {job_name}",
        f"运行主机 : {host}",
        f"发信账号 : {mail_from}",
        "",
        "-- 由 scheduled-batch-jobs / jobs/demo-email 自动生成",
    ])
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=mail_from.split("@")[-1])
    msg.set_content(body, charset="utf-8")
    return msg


def deliver(msg: EmailMessage, host: str, port: int, user: str, auth_code: str,
            smtp_factory=smtplib.SMTP_SSL) -> None:
    """可注入 smtp_factory，便于无网络单测。"""
    with smtp_factory(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.login(user, auth_code)
        smtp.send_message(msg)


def main() -> int:
    now = datetime.now(CST)
    forced = env("FORCE_NOW")
    if forced:
        now = datetime.strptime(forced, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=CST)

    smtp_host = env("SMTP_HOST", "smtp.163.com")
    smtp_port = int(env("SMTP_PORT", "465"))
    mail_from = env("MAIL_FROM", "nvd11@163.com")
    mail_to = env("MAIL_TO", "nvd11@163.com")
    mail_user = env("SMTP_USER", mail_from)
    auth_code = env("SMTP_AUTH_CODE")
    job_name = env("JOB_NAME", "demo-email")
    dry_run = env("DRY_RUN", "false").lower() == "true"
    prefix = env("MAIL_SUBJECT_PREFIX", "demo-batch 执行成功")

    log("=" * 64)
    log(f"demo-email  {smtp_host}:{smtp_port}  {mail_from} → {mail_to}")
    log(f"            time={now:%Y-%m-%d %H:%M:%S %Z}  host={socket.gethostname()}  dry_run={dry_run}")
    log("=" * 64)

    if not auth_code and not dry_run:
        alert("缺少 SMTP_AUTH_CODE（应由 ESO 从 OCI Vault 注入）")
        return 1

    msg = build_message(now, job_name, socket.gethostname(), mail_from, mail_to, prefix)
    log(f"Subject: {msg['Subject']}")

    if dry_run:
        log("--- DRY_RUN：以下内容不会真的发送 ---")
        log(msg.get_content())
        return 0

    try:
        deliver(msg, smtp_host, smtp_port, mail_user, auth_code)
    except Exception as e:  # noqa: BLE001
        alert(f"发送失败: {type(e).__name__}: {e}")
        return 1

    log(f"✅ 已发送到 {mail_to}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        alert(f"未捕获异常: {type(e).__name__}: {e}")
        sys.exit(1)
