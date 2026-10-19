FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG REVISION
ARG CONTRACT_SHA
LABEL org.opencontainers.image.source="https://github.com/harun5231/harun-ai-trading-office" \
      org.opencontainers.image.revision="${REVISION}" \
      io.harun.agent.contract.sha256="${CONTRACT_SHA}" \
      io.harun.service="harun-neurobro-agent"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    HARUN_SOURCE_COMMIT="${REVISION}"
WORKDIR /app
COPY agent/requirements.txt /tmp/agent-requirements.txt
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes \
      --only-binary=:all: -r /tmp/agent-requirements.txt \
    && mkdir -p /app/agent /app/worker /data \
    && chown 10002:10002 /data
# The legacy gateway, coordinator, credentials and financial database are absent.
# Only unchanged pure arithmetic and exact user prompt literals are reused.
COPY --chmod=0444 agent/*.py /app/agent/
COPY --chmod=0444 worker/__init__.py worker/core.py worker/prompts.py /app/worker/
COPY --chmod=0444 deploy/neurobro-agent.contract.json /app/contracts/neurobro-agent.contract.json
RUN CONTRACT_SHA="${CONTRACT_SHA}" REVISION="${REVISION}" python -c "import hashlib,os,pathlib,re; assert re.fullmatch('[0-9a-f]{40}',os.environ['REVISION']); assert set(os.environ['REVISION']) != {'0'}; assert hashlib.sha256(pathlib.Path('/app/contracts/neurobro-agent.contract.json').read_bytes()).hexdigest() == os.environ['CONTRACT_SHA']"
USER 10002:10002
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health/live', timeout=2).read(16385)"]
ENTRYPOINT ["python", "-m", "agent"]
