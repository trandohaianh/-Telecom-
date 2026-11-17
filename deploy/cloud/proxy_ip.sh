#!/usr/bin/env bash
# In ra IP của Caddy trên mạng "edge" để Telecom chỉ tin X-Forwarded-For từ đúng proxy đó.
# Không tìm thấy thì in "*" (hành vi cũ) và cảnh báo ra stderr: container khác trên "edge"
# khi đó giả được IP nguồn và vượt rate limit theo IP.
set -euo pipefail
for name in fieldops-caddy-1 telecom-caddy-1; do
  ip="$(sudo docker inspect -f '{{with index .NetworkSettings.Networks "edge"}}{{.IPAddress}}{{end}}' "$name" 2>/dev/null || true)"
  if [ -n "$ip" ]; then echo "$ip"; exit 0; fi
done
echo "Cảnh báo: không thấy Caddy trên mạng edge; FORWARDED_ALLOW_IPS=*" >&2
echo "*"
