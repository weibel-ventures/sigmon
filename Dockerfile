FROM python:3.12-slim

WORKDIR /opt/geomonitor

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends g++ libexpat1-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y g++ libexpat1-dev && apt-get autoremove -y && \
    apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*

COPY geomonitor/ geomonitor/

# Seed default config (only used if volume is empty)
COPY config/ /etc/sigmon/
ENV SIGMON_CONFIG_DIR=/etc/sigmon
ENV WEB_PORT=8080

CMD uvicorn geomonitor.core.app:app --host 0.0.0.0 --port ${WEB_PORT}
