#!/bin/sh
# Daily chain for a long-running container: load data on an empty volume, then refresh,
# rebuild features and run the alerts at 16:30 Bangladesh time on trading days (Sun-Thu).
# Used by docker-compose.yml (scheduler service) and by scripts/start.sh (single container).
# Needs TZ=Asia/Dhaka in the environment so the clock below is Dhaka time.
set -u

if [ ! -f "${DSE_DATA_DIR:-/data}/market.duckdb" ] || [ "${DSE_FORCE_BOOTSTRAP:-0}" = "1" ]; then
  echo "[scheduler] empty data directory: bootstrapping (archive, live refresh, instruments, features, baselines)"
  dse data bootstrap && dse data refresh && dse data instruments && dse analysis features && dse analysis import-stats
fi
if [ "${APP_DEMO:-0}" = "1" ] || [ "${APP_DEMO:-}" = "true" ]; then
  dse demo seed || echo "[scheduler] demo seed failed; the web UI still works"
fi

last_run=""
while true; do
  dow=$(date +%u)     # 1=Mon .. 7=Sun
  hm=$(date +%H%M)
  today=$(date +%F)
  if { [ "$dow" -le 4 ] || [ "$dow" -eq 7 ]; } && [ "$hm" -ge 1630 ] && [ "$last_run" != "$today" ]; then
    echo "[scheduler] $today $hm: refresh, features, alerts"
    dse data refresh && dse analysis features && dse alerts run || echo "[scheduler] run failed; will retry tomorrow"
    if [ "$dow" -eq 7 ]; then dse data instruments || true; fi   # weekly company profiles
    last_run="$today"
  fi
  sleep 60
done
