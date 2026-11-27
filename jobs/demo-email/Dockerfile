FROM python:3.12-slim

LABEL org.opencontainers.image.title="demo-email" \
      org.opencontainers.image.description="Minimal scheduled batch demo: send a fixed email daily" \
      org.opencontainers.image.source="https://github.com/nvd11/scheduled-batch-jobs"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY send.py /app/send.py

RUN useradd -r -u 10001 -s /usr/sbin/nologin demo \
    && chown -R demo:demo /app
USER 10001

ENTRYPOINT ["python", "/app/send.py"]
