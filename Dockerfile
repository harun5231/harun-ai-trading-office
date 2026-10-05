FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --uid 10001 --create-home --shell /bin/sh office
WORKDIR /app
COPY worker /app/worker
COPY deploy/container_boot.py /app/deploy/container_boot.py
ENTRYPOINT ["python", "/app/deploy/container_boot.py"]
