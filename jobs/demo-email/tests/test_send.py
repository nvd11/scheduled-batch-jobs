#!/usr/bin/env python3
"""demo-email 单元测试（标准库 unittest，无网络）。

运行：
    cd jobs/demo-email && python3 -m unittest discover -s tests -v
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import send  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 28, 9, 0, 0, tzinfo=CST)


class TestBuildMessage(unittest.TestCase):
    def test_headers(self):
        m = send.build_message(NOW, "demo-email", "radxa", "nvd11@163.com", "nvd11@163.com")
        self.assertEqual(m["From"], "nvd11@163.com")
        self.assertEqual(m["To"], "nvd11@163.com")
        self.assertIn("2026-11-28 09:00:00", m["Subject"])
        self.assertIn("demo-batch 执行成功", m["Subject"])
        self.assertTrue(m["Message-ID"].endswith("@163.com>"))

    def test_body_contains_context(self):
        m = send.build_message(NOW, "demo-email", "radxa-cubie-a7a", "a@b.com", "c@d.com")
        body = m.get_content()
        self.assertIn("radxa-cubie-a7a", body)
        self.assertIn("demo-email", body)
        self.assertIn("2026-11-28 09:00:00", body)

    def test_custom_prefix(self):
        m = send.build_message(NOW, "j", "h", "a@b.com", "c@d.com", prefix="自定义前缀")
        self.assertIn("[自定义前缀]", m["Subject"])

    def test_utf8_body_encoded(self):
        m = send.build_message(NOW, "j", "h", "a@b.com", "c@d.com")
        raw = m.as_bytes()
        self.assertIn(b"Content-Type: text/plain; charset=\"utf-8\"", raw)


class FakeSMTP:
    """记录调用，不联网。"""

    def __init__(self, record, host, port, timeout=30):
        self.record = record
        self.record["connect"] = (host, port)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.record["closed"] = True
        return False

    def ehlo(self):
        self.record["ehlo"] = True

    def login(self, user, code):
        self.record["login"] = (user, code)

    def send_message(self, msg):
        self.record["msg"] = msg


class TestDeliver(unittest.TestCase):
    def test_calls_in_right_order(self):
        rec = {}
        m = send.build_message(NOW, "j", "h", "a@b.com", "c@d.com")
        send.deliver(m, "smtp.163.com", 465, "a@b.com", "code", smtp_factory=lambda h, p, timeout: FakeSMTP(rec, h, p, timeout))
        self.assertEqual(rec["connect"], ("smtp.163.com", 465))
        self.assertTrue(rec["ehlo"])
        self.assertEqual(rec["login"], ("a@b.com", "code"))
        self.assertIs(rec["msg"], m)
        self.assertTrue(rec["closed"])


class TestMain(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        for k in ("SMTP_HOST", "SMTP_PORT", "MAIL_FROM", "MAIL_TO", "SMTP_USER",
                  "SMTP_AUTH_CODE", "DRY_RUN", "JOB_NAME", "MAIL_SUBJECT_PREFIX", "FORCE_NOW"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def run_main(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = send.main()
        return code, buf.getvalue()

    def test_dry_run_does_not_send(self):
        os.environ["DRY_RUN"] = "true"
        os.environ["FORCE_NOW"] = "2026-11-28T09:00:00"
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("DRY_RUN", out)
        self.assertIn("2026-11-28 09:00:00", out)

    def test_missing_auth_code_alerts(self):
        os.environ["DRY_RUN"] = "false"
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("[ALERT]", out)

    def test_send_failure_alerts(self):
        os.environ.update({"DRY_RUN": "false", "SMTP_AUTH_CODE": "x"})
        orig = send.deliver

        def boom(*a, **kw):
            raise OSError("connection refused")

        send.deliver = boom
        try:
            code, out = self.run_main()
        finally:
            send.deliver = orig
        self.assertEqual(code, 1)
        self.assertIn("[ALERT]", out)
        self.assertIn("connection refused", out)


if __name__ == "__main__":
    unittest.main()
