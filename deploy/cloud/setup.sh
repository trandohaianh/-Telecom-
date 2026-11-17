#!/usr/bin/env bash
# Cài Telecom Fraud Shield lên VM (đã có Docker; nên cài FieldOps trước để dùng chung Caddy HTTPS).
#   bash deploy/cloud/setup.sh            # lần 1 tạo .env, lần 2 build + chạy
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^METRICS_TOKEN=.*|METRICS_TOKEN=$(openssl rand -hex 24)|" .env
  sed -i "s|^FRAUD_ADMIN_PASSWORD=.*|FRAUD_ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')|" .env
  chmod 600 .env
  echo "Đã tạo $HERE/.env — điền TELECOM_DOMAIN (và GOOGLE_CLIENT_ID nếu có) rồi chạy lại."
  echo "Mật khẩu admin ban đầu nằm trong .env (FRAUD_ADMIN_PASSWORD); đổi ngay sau khi đăng nhập."
  exit 0
fi
set -a; source .env; set +a
[ -n "${TELECOM_DOMAIN:-}" ] || { echo "Thiếu TELECOM_DOMAIN trong .env"; exit 1; }

sudo docker network inspect edge >/dev/null 2>&1 || sudo docker network create edge

if [ -n "${DUCKDNS_TOKEN:-}" ] && [[ "$TELECOM_DOMAIN" == *.duckdns.org ]]; then
  sub="${TELECOM_DOMAIN%.duckdns.org}"
  # Token nằm trong file cấu hình curl quyền 600, không nằm trong crontab hay dòng lệnh (ps).
  conf="$HOME/.config/duckdns/${sub}.curl"
  mkdir -p "$(dirname "$conf")" && chmod 700 "$(dirname "$conf")"
  ( umask 077; printf 'url = "https://www.duckdns.org/update?domains=%s&token=%s&ip="\nsilent\nfail\n' \
      "$sub" "$DUCKDNS_TOKEN" > "$conf" )
  line="*/5 * * * * curl -K '$conf' >/dev/null 2>&1 # duckdns:${sub}"
  ( crontab -l 2>/dev/null | grep -v -e "domains=${sub}&" -e "# duckdns:${sub}\$" ; echo "$line" ) | crontab -
fi

if sudo docker ps --format '{{.Names}}' | grep -q '^fieldops-caddy-1$'; then
  # Caddy chung của FieldOps: thêm site cho Telecom rồi reload (không gián đoạn FieldOps).
  sudo mkdir -p /opt/edge/sites
  sed "s|__DOMAIN__|${TELECOM_DOMAIN}|" telecom.caddy | sudo tee /opt/edge/sites/telecom.caddy >/dev/null
  sudo docker exec fieldops-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
else
  echo "Không thấy Caddy của FieldOps → chạy Caddy riêng (profile standalone)."
  sudo docker compose --env-file .env --profile standalone up -d --no-deps caddy
fi
# Telecom chỉ tin X-Forwarded-For từ đúng Caddy (rate limit theo IP không bị container khác giả).
proxy_ip="$(bash "$HERE/proxy_ip.sh")"   # dừng (set -e) nếu không tìm thấy Caddy
FORWARDED_ALLOW_IPS="$proxy_ip" sudo -E docker compose --env-file .env up -d --build telecom
bash "$HERE/smoke.sh" "https://${TELECOM_DOMAIN}" || echo "Cảnh báo: smoke test thất bại" >&2
echo "Xong: app thuê bao https://${TELECOM_DOMAIN}/app/ · vận hành https://${TELECOM_DOMAIN}/admin/"
