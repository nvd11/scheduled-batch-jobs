package com.gateman.demo.email;

import jakarta.mail.Message;
import jakarta.mail.MessagingException;
import jakarta.mail.Session;
import jakarta.mail.Transport;
import jakarta.mail.internet.InternetAddress;
import jakarta.mail.internet.MimeMessage;

import java.io.PrintStream;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Properties;

/**
 * demo-email-java —— 每天往固定邮箱发一封固定格式的演示邮件（Java 版）。
 *
 * <p>与 Python 版行为一致：凭据只从环境变量读取（由 ESO 从 OCI Vault 注入），
 * 失败打印 {@code [ALERT]} 并以退出码 1 结束，供 workflow grep 判定。
 */
public final class Main {

    static final ZoneId CST = ZoneId.of("Asia/Shanghai");
    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    private Main() {
    }

    public static void main(String[] args) {
        int code = run(System.getenv(), System.out, new SmtpMailer());
        System.exit(code);
    }

    /** 便于单测：不直接依赖 System.exit。 */
    static int run(java.util.Map<String, String> env, PrintStream out, Mailer mailer) {
        ZonedDateTime now = parseNow(env.get("FORCE_NOW"));
        String smtpHost = envOrDefault(env, "SMTP_HOST", "smtp.163.com");
        int smtpPort = Integer.parseInt(envOrDefault(env, "SMTP_PORT", "465"));
        String mailFrom = envOrDefault(env, "MAIL_FROM", "nvd11@163.com");
        String mailTo = envOrDefault(env, "MAIL_TO", "nvd11@163.com");
        String smtpUser = envOrDefault(env, "SMTP_USER", mailFrom);
        String authCode = envOrDefault(env, "SMTP_AUTH_CODE", "");
        String jobName = envOrDefault(env, "JOB_NAME", "demo-email-java");
        String prefix = envOrDefault(env, "MAIL_SUBJECT_PREFIX", "demo-batch-java 执行成功");
        boolean dryRun = "true".equalsIgnoreCase(envOrDefault(env, "DRY_RUN", "false"));

        out.println("================================================================");
        out.println("demo-email-java  " + smtpHost + ":" + smtpPort + "  " + mailFrom + " -> " + mailTo);
        out.printf("                 time=%s  host=%s  dry_run=%s%n", now.format(FMT), hostname(), dryRun);
        out.println("================================================================");

        if (authCode.isBlank() && !dryRun) {
            out.println("[ALERT] 缺少 SMTP_AUTH_CODE（应由 ESO 从 OCI Vault 注入）");
            return 1;
        }

        String subject = "[" + prefix + "] " + now.format(FMT);
        String body = String.join("\n",
                "这是一封由定时批处理任务自动发送的演示邮件（Java 实现）。",
                "",
                "执行时间 : " + now.format(FMT),
                "任务名称 : " + jobName,
                "运行主机 : " + hostname(),
                "发信账号 : " + mailFrom,
                "",
                "-- 由 scheduled-batch-jobs / jobs/demo-email-java 自动生成");

        out.println("Subject: " + subject);

        if (dryRun) {
            out.println("--- DRY_RUN：以下内容不会真的发送 ---");
            out.println(body);
            return 0;
        }

        try {
            mailer.send(smtpHost, smtpPort, smtpUser, authCode, mailFrom, mailTo, subject, body);
        } catch (Exception e) {
            out.println("[ALERT] 发送失败: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            return 1;
        }

        out.println("✅ 已发送到 " + mailTo);
        return 0;
    }

    static ZonedDateTime parseNow(String forced) {
        if (forced != null && !forced.isBlank()) {
            return ZonedDateTime.parse(forced.length() == 19 ? forced + "+08:00" : forced);
        }
        return ZonedDateTime.now(CST);
    }

    private static String envOrDefault(java.util.Map<String, String> env, String key, String def) {
        String v = env.get(key);
        return (v == null || v.isBlank()) ? def : v.trim();
    }

    private static String hostname() {
        try {
            return InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            return "unknown";
        }
    }

    /** 抽象出来便于测试注入。 */
    interface Mailer {
        void send(String host, int port, String user, String authCode,
                  String from, String to, String subject, String body) throws Exception;
    }

    /** 真实实现：jakarta.mail SSL(465)。 */
    static final class SmtpMailer implements Mailer {
        @Override
        public void send(String host, int port, String user, String authCode,
                         String from, String to, String subject, String body) throws Exception {
            Properties props = new Properties();
            props.put("mail.smtp.host", host);
            props.put("mail.smtp.port", String.valueOf(port));
            props.put("mail.smtp.ssl.enable", "true");
            props.put("mail.smtp.auth", "true");
            props.put("mail.smtp.connectiontimeout", "30000");
            props.put("mail.smtp.timeout", "30000");
            props.put("mail.smtp.writetimeout", "30000");

            Session session = Session.getInstance(props);
            MimeMessage message = new MimeMessage(session);
            message.setFrom(new InternetAddress(from));
            message.setRecipients(Message.RecipientType.TO, InternetAddress.parse(to));
            message.setSubject(subject, StandardCharsets.UTF_8.name());
            message.setText(body, StandardCharsets.UTF_8.name());
            Transport.send(message, user, authCode);
        }
    }

    /** 供测试使用：构造一封不发送的消息，验证头与正文。 */
    static MimeMessage buildMessage(String from, String to, String subject, String body) throws MessagingException {
        Session session = Session.getInstance(new Properties());
        MimeMessage message = new MimeMessage(session);
        message.setFrom(new InternetAddress(from));
        message.setRecipients(Message.RecipientType.TO, InternetAddress.parse(to));
        message.setSubject(subject, StandardCharsets.UTF_8.name());
        message.setText(body, StandardCharsets.UTF_8.name());
        return message;
    }
}
