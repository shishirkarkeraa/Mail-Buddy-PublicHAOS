# syntax=docker/dockerfile:1.7
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system --gid 10001 mailbuddy \
    && useradd --system --uid 10001 --gid mailbuddy --home-dir /nonexistent \
       --shell /usr/sbin/nologin mailbuddy \
    && install -d -o mailbuddy -g mailbuddy -m 0700 /data /backups

COPY pyproject.toml requirements.lock README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps .

USER 10001:10001
EXPOSE 8000
VOLUME ["/data", "/backups"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

CMD ["mail-buddy", "serve", "--host", "0.0.0.0", "--port", "8000", "--forwarded-allow-ips", "*"]
