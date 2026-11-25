# cctv-pruner —— 仅用标准库，镜像越小越好
# 目标平台：linux/arm64（radxa-cubie-a7a），构建时由 workflow 指定
FROM python:3.12-slim

LABEL org.opencontainers.image.title="cctv-pruner" \
      org.opencontainers.image.description="Prune expired CCTV date folders on Quark drive via AList" \
      org.opencontainers.image.source="https://github.com/nvd11/scheduled-batch-jobs"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pruner.py /app/pruner.py

# 非 root 运行
RUN useradd -r -u 10001 -s /usr/sbin/nologin pruner \
    && chown -R pruner:pruner /app
USER 10001

ENTRYPOINT ["python", "/app/pruner.py"]
