# Extend the actual published dialogue runtime without changing its process contract.
FROM ghcr.io/harun5231/harun-neurobro-agent@sha256:41702f11e004a1bbcceb86dc09f116b920f3cccc8df9cae1356319fc09e9e2db
ARG REVISION
ARG CONTRACT_SHA=e761f33a1575ff404535d71dc512d959eb80aca17b34a028b78640ee74d114fd
LABEL org.opencontainers.image.revision="${REVISION}" \
      io.harun.agent.contract.sha256="${CONTRACT_SHA}"
ENV HARUN_SOURCE_COMMIT="${REVISION}"
COPY --chmod=0444 agent/hermes_dialogue.py /app/agent/
