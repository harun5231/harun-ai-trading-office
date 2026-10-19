# Reuse the existing verified dependency/runtime image; never rebuild the worker.
FROM ghcr.io/harun5231/harun-neurobro-agent@sha256:00c1a2b1cf383e44522c8330dd00144dc45304bc72b0a738d78cb01045c8ae3d
ARG REVISION
ARG CONTRACT_SHA
LABEL org.opencontainers.image.source="https://github.com/harun5231/harun-ai-trading-office" \
      org.opencontainers.image.revision="${REVISION}" \
      io.harun.agent.contract.sha256="${CONTRACT_SHA}" \
      io.harun.agent.mode="NATIVE_DELEGATED_DIALOGUE" \
      io.harun.service="harun-neurobro-agent"
ENV HARUN_SOURCE_COMMIT="${REVISION}" HARUN_AGENT_MODE="NATIVE_DELEGATED_DIALOGUE"
WORKDIR /app
# Only delegated reasoning and the honest native public-context converter change.
COPY --chmod=0444 agent/dialogue_service.py agent/dialogue_client.py agent/native_market_context.py agent/llm.py agent/native_status_bridge.py /app/agent/
COPY --chmod=0444 deploy/neurobro-dialogue.contract.json /app/contracts/neurobro-dialogue.contract.json
RUN CONTRACT_SHA="${CONTRACT_SHA}" REVISION="${REVISION}" python -c "import hashlib,os,pathlib,re; assert re.fullmatch('[0-9a-f]{40}',os.environ['REVISION']); assert set(os.environ['REVISION']) != {'0'}; assert re.fullmatch('[0-9a-f]{64}',os.environ['CONTRACT_SHA']); assert hashlib.sha256(pathlib.Path('/app/contracts/neurobro-dialogue.contract.json').read_bytes()).hexdigest() == os.environ['CONTRACT_SHA']; import agent.dialogue_service, agent.dialogue_client, agent.native_market_context, agent.native_status_bridge"
USER 10002:10002
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health/live', timeout=2).read(16385)"]
ENTRYPOINT ["python", "-m", "agent.dialogue_service"]
CMD ["--socket", "/run/harun-dialogue/dialogue.sock", "--data-directory", "/data", "--token-file", "/run/secrets/dialogue_token", "--key-file", "/run/secrets/openai_api_key"]
