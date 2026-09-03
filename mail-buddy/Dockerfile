# syntax=docker/dockerfile:1.7
# Used only by scripts/package-haos-addon.sh. That script copies the exact
# reviewed source tree into a local HAOS add-on folder, so no GitHub access is
# needed during the Supervisor build.
FROM ollama/ollama:0.9.3@sha256:45008241d61056449dd4f20cebf64bfa5a2168b0c078ecf34aa2779760502c2f AS ollama

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_MODELS=/data/ollama/models

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.lock README.md LICENSE ./
COPY src ./src
COPY verify-model-manifest.sh /usr/local/bin/verify-model-manifest
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps . \
    && chmod 0755 /usr/local/bin/verify-model-manifest

COPY --from=ollama /bin/ollama /usr/local/bin/ollama
COPY run.sh /run.sh
RUN chmod 0755 /run.sh

EXPOSE 8099 8765
CMD ["/run.sh"]
