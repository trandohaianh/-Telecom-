#!/usr/bin/env bash
set -uo pipefail
BASE="${1:?Cần URL, VD https://laichan-demo.duckdns.org}"
fail=0
# Kiểm chứng chứng chỉ TLS thật; SMOKE_INSECURE=1 chỉ dùng khi đang chờ ACME cấp chứng chỉ lần đầu.
TLS_FLAG=""; [ "${SMOKE_INSECURE:-0}" = "1" ] && TLS_FLAG="-k"
check() { local name="$1" url="$2" want="$3"; shift 3
  for i in $(seq 1 30); do
    code=$(curl -s $TLS_FLAG -o /dev/null -w '%{http_code}' "$@" "$url")
    [ "$code" = "$want" ] && { echo "✓ $name ($code)"; return 0; }; sleep 6; done
  echo "✗ $name: mong $want, nhận $code"; fail=1; }
check "App thuê bao"      "$BASE/app/"           200
check "Bảng vận hành"     "$BASE/admin/"         200
check "Engine sẵn sàng"   "$BASE/health/ready"   200
check "Cấu hình đăng nhập" "$BASE/api/auth/config" 200
check "Metrics bị khoá"   "$BASE/metrics"        401
check "Demo ẩn danh tắt"  "$BASE/api/app/demo-numbers" 403
# Qua HTTPS, Caddy (riêng hoặc chung) phải gắn HSTS: thiếu là dấu hiệu proxy chạy sai Caddyfile.
if [[ "$BASE" == https://* ]]; then
  if curl -sI $TLS_FLAG "$BASE/app/" | grep -qi '^strict-transport-security:'; then echo "✓ HSTS (Caddy)"
  else echo "✗ HSTS: thiếu Strict-Transport-Security"; fail=1; fi
fi
# Header đọc từ file 600 (curl -H @file): token không xuất hiện trong danh sách tiến trình (ps).
if [ -n "${METRICS_TOKEN:-}" ]; then
  hdr="$(mktemp)"; chmod 600 "$hdr"; trap 'rm -f -- "$hdr"' EXIT
  printf 'Authorization: Bearer %s\n' "$METRICS_TOKEN" > "$hdr"
  check "Metrics có token" "$BASE/metrics" 200 -H "@$hdr"
fi
exit $fail
