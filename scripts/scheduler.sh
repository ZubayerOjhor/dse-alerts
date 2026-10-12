#!/bin/sh
# Daily chain for a long-running container: load data on an empty volume, then refresh,
# rebuild features and run the alerts at 16:30 Bangladesh time on trading days (Sun-Thu).
# If the exchange has not published the session yet, or a step fails, it tries again every
# 30 minutes until 21:30, and only then gives the day up.
# Used by docker-compose.yml (scheduler service) and by scripts/start.sh (single container).
# Needs TZ=Asia/Dhaka in the environment so the clock below is Dhaka time.
set -u

if [ ! -f "${DSE_DATA_DIR:-/data}/market.duckdb" ] || [ "${DSE_FORCE_BOOTSTRAP:-0}" = "1" ]; then
  echo "[scheduler] empty data directory: bootstrapping (archive, live refresh, instruments, features, baselines)"
  dse data bootstrap || echo "[scheduler] archive download failed (the fallback archive host refuses some datacenter IPs); continuing with live data only. Copy a market.duckdb built on your PC into /data for the full history."
  dse data refresh && dse data instruments && dse analysis features && dse analysis import-stats
fi
if [ "${APP_DEMO:-0}" = "1" ] || [ "${APP_DEMO:-}" = "true" ]; then
  dse demo seed || echo "[scheduler] demo seed failed; the web UI still works"
fi
# the web UI reads a published copy, so pages keep working while this chain writes
dse data snapshot || echo "[scheduler] snapshot failed; the web UI reads the live file"

done_day=""
next_try=0
# after a restart later in the day, do not redo a chain whose session is already in
if dse data latest --expect-today >/dev/null 2>&1; then done_day=$(date +%F); fi
while true; do
  dow=$(date +%u)     # 1=Mon .. 7=Sun
  hm=$(date +%H%M)
  today=$(date +%F)
  now=$(date +%s)
  trading_day=0
  if [ "$dow" -le 4 ] || [ "$dow" -eq 7 ]; then trading_day=1; fi
  if [ "$trading_day" -eq 1 ] && [ "$hm" -ge 1630 ] && [ "$done_day" != "$today" ] && [ "$now" -ge "$next_try" ]; then
    echo "[scheduler] $today $hm: refresh, features, alerts"
    if dse data refresh && dse data latest --expect-today && dse analysis features && dse alerts run; then
      if [ "$dow" -eq 7 ]; then dse data instruments || true; fi   # weekly company profiles
      dse data snapshot || true
      done_day="$today"
      echo "[scheduler] $today done"
    elif [ "$hm" -ge 2130 ]; then
      echo "[scheduler] $today: no session published by 21:30 (holiday or outage); giving up for today"
      done_day="$today"
    else
      echo "[scheduler] $today $hm: session not in yet or a step failed; retrying in 30 minutes"
      next_try=$((now + 1800))
    fi
  fi
  sleep 60
done
