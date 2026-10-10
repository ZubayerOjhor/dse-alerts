FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DSE_DATA_DIR=/data APP_HOST=0.0.0.0
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY dse_alerts ./dse_alerts
COPY calibration ./calibration
RUN pip install --no-cache-dir -e ".[web,ai]"

VOLUME ["/data"]
EXPOSE 8000

# default: the web UI. The scheduler service in docker-compose.yml runs the daily chain.
CMD ["dse", "web", "serve"]
