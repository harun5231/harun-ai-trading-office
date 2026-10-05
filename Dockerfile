FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --uid 10001 --create-home --shell /bin/sh office
WORKDIR /app
COPY worker /app/worker
COPY deploy/container_boot.py /app/deploy/container_boot.py
COPY deploy/runtime_permissions.py /app/deploy/runtime_permissions.py
RUN python /app/deploy/runtime_permissions.py
USER office
RUN python -B -c "import worker.http_client, worker.binance_shadow, worker.api_service"
USER root
ENTRYPOINT ["python", "/app/deploy/container_boot.py"]
