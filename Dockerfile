FROM python:3.12-slim

WORKDIR /opt/geomonitor

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends g++ libexpat1-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y g++ libexpat1-dev && apt-get autoremove -y && \
    apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*

COPY geomonitor/ geomonitor/

# Config directory — settings persist here
RUN mkdir -p /etc/sigmon/sigmon.d
ENV SIGMON_CONFIG_DIR=/etc/sigmon
ENV WEB_PORT=8080
ENV BUFFER_MAX_MESSAGES=50000

CMD uvicorn geomonitor.core.app:app --host 0.0.0.0 --port ${WEB_PORT}
