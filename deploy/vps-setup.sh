#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu 22.04/24.04 VPS. Run as root (or with sudo):
#   sudo bash vps-setup.sh
# Installs Docker + Caddy, runs the Zoyra API image, and serves it over HTTPS.
set -euo pipefail

IMAGE="${IMAGE:-kottai2003/zoyra:latest}"
read -rp "Docker Hub username (for the private image): " DH_USER
read -rsp "Docker Hub access token: " DH_TOKEN; echo
read -rp "API_KEY to require on /api/* (leave empty to generate): " API_KEY
[ -z "$API_KEY" ] && API_KEY="$(openssl rand -hex 24)"
read -rp "CORS_ORIGINS (comma separated, e.g. https://myapp.com): " CORS_ORIGINS
read -rp "WEBSHARE_API_KEY (empty = none; use if YouTube blocks the VPS IP): " WEBSHARE_API_KEY
read -rp "YT_PROXY (empty = none, e.g. http://user:pass@host:port): " YT_PROXY
read -rp "Domain for HTTPS (empty = use <ip>.sslip.io): " DOMAIN
if [ -z "$DOMAIN" ]; then
  IP="$(curl -4 -s https://api.ipify.org)"
  DOMAIN="${IP//./-}.sslip.io"
fi

echo "==> Installing Docker"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

echo "==> Firewall (ssh + web only)"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null; ufw allow 80,443/tcp >/dev/null; ufw --force enable >/dev/null
fi

echo "==> Pulling image"
echo "$DH_TOKEN" | docker login -u "$DH_USER" --password-stdin
docker pull "$IMAGE"

echo "==> Writing /opt/zoyra/.env"
mkdir -p /opt/zoyra
cat > /opt/zoyra/.env <<ENV
API_KEY=$API_KEY
CORS_ORIGINS=$CORS_ORIGINS
PREFETCH_COUNT=10
PREFETCH_CONCURRENCY=4
WEBSHARE_API_KEY=$WEBSHARE_API_KEY
YT_PROXY=$YT_PROXY
# Paste a single-line cookies.txt here ONLY if YouTube blocks this server's IP:
# YT_COOKIES=
ENV
chmod 600 /opt/zoyra/.env

echo "==> Starting container"
docker rm -f zoyra >/dev/null 2>&1 || true
docker run -d --name zoyra --restart unless-stopped \
  -p 127.0.0.1:8000:7860 --env-file /opt/zoyra/.env "$IMAGE"

echo "==> Installing Caddy (automatic HTTPS)"
if ! command -v caddy >/dev/null; then
  apt-get update -y && apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi
echo "$DOMAIN { reverse_proxy localhost:8000 }" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo
echo "Waiting for the API..."
for i in $(seq 1 30); do curl -fs localhost:8000/api/health >/dev/null && break; sleep 2; done
echo "Health: $(curl -s localhost:8000/api/health)"
echo
echo "URL:     https://$DOMAIN"
echo "API_KEY: $API_KEY   (send as X-Zoyra-Key header, or ?key= on audio URLs)"
echo "Update later:  docker pull $IMAGE && docker rm -f zoyra && re-run the docker run line in this script"
