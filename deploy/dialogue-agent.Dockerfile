# Extend the actual published dialogue runtime without changing its process contract.
FROM ghcr.io/harun5231/harun-neurobro-agent@sha256:696219259ef3d2e8b5d336a4f784389a3a9626527ce387b9db2798752ad3c41d
ARG REVISION
ARG CONTRACT_SHA=e761f33a1575ff404535d71dc512d959eb80aca17b34a028b78640ee74d114fd
LABEL org.opencontainers.image.revision="${REVISION}" \
      io.harun.agent.contract.sha256="${CONTRACT_SHA}"
ENV HARUN_SOURCE_COMMIT="${REVISION}"
COPY --chmod=0444 agent/dialogue_service.py /app/agent/
