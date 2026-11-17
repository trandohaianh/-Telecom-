#!/usr/bin/env bash
# In ra IP của Caddy trên mạng "edge" để Telecom chỉ tin X-Forwarded-For từ đúng proxy đó.
# Không tìm thấy thì THOÁT LỖI (N5): tin "*" nghĩa là mọi container trên "edge" giả được IP nguồn
# và vượt rate limit theo IP. Đặt TELECOM_PROXY_CONTAINER nếu Caddy có tên khác.
set -euo pipefail
for name in ${TELECOM_PROXY_CONTAINER:-} fieldops-caddy-1 telecom-caddy-1; do
  ip="$(sudo docker inspect -f '{{with index .NetworkSettings.Networks "edge"}}{{.IPAddress}}{{end}}' "$name" 2>/dev/null || true)"
  if [ -n "$ip" ]; then echo "$ip"; exit 0; fi
done
echo "Lỗi: không thấy Caddy trên mạng edge (đặt TELECOM_PROXY_CONTAINER=<tên container>)." >&2
exit 1
