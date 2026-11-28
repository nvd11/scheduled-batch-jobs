package com.gateman.demo.email;

import jakarta.mail.internet.MimeMessage;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.util.HashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class MainTest {

    private final ByteArrayOutputStream buf = new ByteArrayOutputStream();
    private final PrintStream out = new PrintStream(buf, true);

    private String output() {
        return buf.toString();
    }

    private Map<String, String> baseEnv() {
        Map<String, String> env = new HashMap<>();
        env.put("FORCE_NOW", "2026-11-28T09:05:00");
        env.put("SMTP_AUTH_CODE", "dummy");
        return env;
    }

    @Test
    void dryRunDoesNotSend() {
        Map<String, String> env = baseEnv();
        env.put("DRY_RUN", "true");
        env.remove("SMTP_AUTH_CODE");

        boolean[] called = {false};
        int code = Main.run(env, out, (h, p, u, a, f, t, s, b) -> called[0] = true);

        assertEquals(0, code);
        assertFalse(called[0], "dry-run 不应调用 SMTP");
        assertTrue(output().contains("DRY_RUN"));
        assertTrue(output().contains("2026-11-28 09:05:00"));
    }

    @Test
    void missingAuthCodeAlerts() {
        Map<String, String> env = baseEnv();
        env.remove("SMTP_AUTH_CODE");
        env.put("DRY_RUN", "false");

        int code = Main.run(env, out, (h, p, u, a, f, t, s, b) -> {
        });

        assertEquals(1, code);
        assertTrue(output().contains("[ALERT]"));
        assertTrue(output().contains("SMTP_AUTH_CODE"));
    }

    @Test
    void sendFailureAlerts() {
        int code = Main.run(baseEnv(), out, (h, p, u, a, f, t, s, b) -> {
            throw new RuntimeException("connection refused");
        });

        assertEquals(1, code);
        assertTrue(output().contains("[ALERT]"));
        assertTrue(output().contains("connection refused"));
    }

    @Test
    void happyPathPassesAllParameters() {
        String[][] captured = new String[1][];
        int code = Main.run(baseEnv(), out, (h, p, u, a, f, t, s, b) -> {
            captured[0] = new String[]{h, String.valueOf(p), u, a, f, t, s, b};
        });

        assertEquals(0, code);
        assertNotNull(captured[0]);
        String[] c = captured[0];
        assertEquals("smtp.163.com", c[0]);
        assertEquals("465", c[1]);
        assertEquals("nvd11@163.com", c[2]);   // SMTP_USER 缺省取 MAIL_FROM
        assertEquals("dummy", c[3]);
        assertEquals("nvd11@163.com", c[4]);
        assertEquals("nvd11@163.com", c[5]);
        assertTrue(c[6].startsWith("[demo-batch-java 执行成功]"));
        assertTrue(c[6].contains("2026-11-28 09:05:00"));
        assertTrue(c[7].contains("Java 实现"));
        assertTrue(output().contains("已发送到"));
    }

    @Test
    void customPrefix() {
        Map<String, String> env = baseEnv();
        env.put("MAIL_SUBJECT_PREFIX", "自定义前缀");
        String[] subject = new String[1];

        Main.run(env, out, (h, p, u, a, f, t, s, b) -> subject[0] = s);

        assertTrue(subject[0].startsWith("[自定义前缀]"));
    }

    @Test
    void buildMessageSetsHeadersAndUtf8Body() throws Exception {
        MimeMessage m = Main.buildMessage("a@b.com", "c@d.com", "主题", "正文内容");
        assertEquals("a@b.com", m.getFrom()[0].toString());
        assertEquals("c@d.com", m.getRecipients(jakarta.mail.Message.RecipientType.TO)[0].toString());
        assertEquals("主题", m.getSubject());
        assertTrue(m.getContent() instanceof String);
        assertEquals("正文内容", ((String) m.getContent()).trim());
    }

    @Test
    void parseNowDefaultsToCst() {
        assertNotNull(Main.parseNow(null));
        assertEquals(java.time.ZoneId.of("Asia/Shanghai"), Main.parseNow(null).getZone());
        assertEquals(2026, Main.parseNow("2026-11-28T09:05:00").getYear());
    }
}
