#!/usr/bin/env bash
# Turn a fresh Ubuntu machine (Azure for Students, Oracle Cloud Always Free, or any VPS) into
# a public dse-alerts instance: Docker, swap on small machines, firewall, a clone of the
# repository, a filled-in .env, and the three containers (web UI, daily scheduler, HTTPS
# proxy). Idempotent: safe to run again.
#
#   curl -fsSL https://raw.githubusercontent.com/ZubayerOjhor/dse-alerts/main/scripts/vm-setup.sh | bash
#
# Optional environment: DOMAIN=alerts.example.com (default: <ip>.sslip.io, which needs no DNS setup),
#                       REPO=<git url>, DIR=<checkout path>.
set -euo pipefail
REPO="${REPO:-https://github.com/ZubayerOjhor/dse-alerts.git}"
DIR="${DIR:-$HOME/dse-alerts}"

echo "== Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

echo "== Swap (the 1 GB machines need it for the nightly feature build)"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 3000 ] && [ ! -f /swapfile ]; then
  sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "   added a 3 GB swap file (RAM: ${MEM_MB} MB)"
fi

echo "== Firewall: allow 80 and 443 at the OS level (Oracle's image blocks them; harmless elsewhere)"
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || sudo iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
done
if ! dpkg -s netfilter-persistent >/dev/null 2>&1; then
  echo iptables-persistent iptables-persistent/autosave_v4 boolean true | sudo debconf-set-selections
  echo iptables-persistent iptables-persistent/autosave_v6 boolean true | sudo debconf-set-selections
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q netfilter-persistent iptables-persistent >/dev/null
fi
sudo netfilter-persistent save >/dev/null 2>&1 || true

echo "== Code"
if [ ! -d "$DIR/.git" ]; then git clone "$REPO" "$DIR"; else git -C "$DIR" pull --ff-only || echo "   (could not pull; using the checkout as it is)"; fi
cd "$DIR"

echo "== .env"
if [ ! -f .env ]; then
  cp .env.example .env
  IP=$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)
  DOMAIN="${DOMAIN:-$(echo "$IP" | tr . -).sslip.io}"
  SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))' 2>/dev/null || openssl rand -hex 32)
  sed -i "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=$SECRET|; s|^APP_HOST=.*|APP_HOST=0.0.0.0|; s|^APP_DEMO=.*|APP_DEMO=1|; s|^APP_PUBLIC_URL=.*|APP_PUBLIC_URL=https://$DOMAIN|" .env
  printf '\n# HTTPS proxy (docker compose --profile https); a hostname that points at this machine\nDOMAIN=%s\n' "$DOMAIN" >> .env
  if [ "$MEM_MB" -lt 3000 ]; then
    sed -i "s|^DSE_DUCKDB_MEMORY=.*|DSE_DUCKDB_MEMORY=300MB|; s|^DSE_DUCKDB_THREADS=.*|DSE_DUCKDB_THREADS=2|" .env
    echo "   small machine (${MEM_MB} MB): DuckDB capped at 300MB per process, spills to disk"
  fi
  echo "   wrote .env for https://$DOMAIN"
  echo "   add SMTP_USER / SMTP_PASSWORD (Gmail app password) and GROQ_API_KEY later with:  nano $DIR/.env  then  sudo docker compose --profile https up -d"
fi

echo "== Start (web + scheduler + HTTPS)"
sudo docker compose --profile https up -d --build
DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2)
echo
echo "Up. Open https://$DOMAIN in a few minutes (the certificate is issued on first visit)."
echo "The first boot downloads the market archive and builds features, 10-20 minutes:"
echo "    sudo docker compose logs -f scheduler"
