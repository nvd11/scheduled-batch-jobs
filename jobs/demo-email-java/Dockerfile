# 多阶段构建：Maven 编译 + 精简 JRE 运行
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY pom.xml .
# 先只拷 pom 拉依赖，利用层缓存
RUN mvn -B -q dependency:go-offline
COPY src ./src
RUN mvn -B -q package -DskipTests

FROM eclipse-temurin:21-jre-alpine

LABEL org.opencontainers.image.title="demo-email-java" \
      org.opencontainers.image.description="Scheduled batch demo (Java): send a fixed email daily" \
      org.opencontainers.image.source="https://github.com/nvd11/scheduled-batch-jobs"

ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75 -XX:+UseSerialGC"

WORKDIR /app
COPY --from=build /src/target/demo-email-java.jar /app/app.jar

RUN adduser -D -u 10001 demo && chown -R demo:demo /app
USER 10001

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
