FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DSE_DATA_DIR=/data APP_HOST=0.0.0.0
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY dse_alerts ./dse_alerts
COPY calibration ./calibration
COPY scripts ./scripts
RUN pip install --no-cache-dir -e ".[web,ai]"

# Run as an unprivileged user. The data volume must be writable by uid 1000: a fresh volume
# inherits it from here, an existing one needs `chown -R 1000:1000` once (scripts/vm-setup.sh does it).
RUN useradd --system --uid 1000 --create-home app && mkdir -p /data && chown app:app /data /app
USER app

VOLUME ["/data"]
EXPOSE 8000

# default: the web UI. docker-compose.yml runs the daily chain as a second service
# (scripts/scheduler.sh); a single-machine host runs both via `sh scripts/start.sh`.
CMD ["dse", "web", "serve"]
