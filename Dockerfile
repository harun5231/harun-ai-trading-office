FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb xauth x11-utils x11vnc novnc websockify gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --break-system-packages playwright==1.63.0 \
    && useradd --uid 10001 --create-home --shell /bin/sh office
WORKDIR /app
COPY worker /app/worker
COPY deploy/container_boot.py deploy/supervise.py /app/deploy/
ENTRYPOINT ["python", "/app/deploy/container_boot.py"]
