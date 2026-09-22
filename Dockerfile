# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim-trixie

# ----------------------------------------------------------------------------- build
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/app \
    PATH=/opt/venv/bin:$PATH
# Node da distro (LTS, versão fixada pela Debian) em vez do "latest" que o nodeenv baixaria:
# o CLI do Prisma é um programa Node e roda as migrações do banco no boot.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl nodejs npm \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app

RUN python -m venv /opt/venv
COPY requirements.lock ./
# --require-hashes: qualquer pacote diferente do lockfile (supply chain) aborta o build.
RUN pip install --require-hashes -r requirements.lock

# Gera o client Prisma e baixa node + engines para $HOME/.cache (usados pelas migrações
# no boot). Feito no build para que o container não precise de internet em runtime.
RUN prisma generate --schema /opt/venv/lib/python3.12/site-packages/litellm/proxy/schema.prisma \
 && ln -s "$(find /app/.cache/prisma-python/binaries -path '*node_modules/.bin/prisma' | head -n1)" /app/.cache/prisma-cli

# --------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="ai-gateway" \
      org.opencontainers.image.description="LiteLLM gateway para Azure OpenAI com virtual keys, orçamento e observabilidade" \
      org.opencontainers.image.source="https://github.com/edumartinsrib/ai-gateway" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    HOME=/app \
    GATEWAY_CONFIG=/app/config/config.yaml \
    PRISMA_HIDE_UPDATE_MESSAGE=1 \
    PRISMA_SKIP_POSTINSTALL_GENERATE=1 \
    PRISMA_OFFLINE_MODE=true \
    PRISMA_CLI_PATH=/app/.cache/prisma-cli

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=10001:10001 /app/.cache /app/.cache
COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 config ./config
COPY --chown=10001:10001 provisioning ./provisioning

USER 10001
EXPOSE 4000 9090
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=4).status != 200)"

ENTRYPOINT ["python", "-m", "ai_gateway"]
