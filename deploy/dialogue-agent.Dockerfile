# Restore the proven Mini runtime; retain its CLI, user and volume contract.
FROM ghcr.io/harun5231/harun-neurobro-agent@sha256:83e5d17b04bfe2b577d9e401859a5e1221eeb564f7e5d776391b757909fa4626
ARG REVISION
ARG CONTRACT_SHA=e761f33a1575ff404535d71dc512d959eb80aca17b34a028b78640ee74d114fd
LABEL org.opencontainers.image.revision="${REVISION}" \
      io.harun.agent.contract.sha256="${CONTRACT_SHA}"
ENV HARUN_SOURCE_COMMIT="${REVISION}"
COPY --chmod=0444 deploy/.mini-dialogue-runtime/agent/dialogue_service.py /app/agent/
