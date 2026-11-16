#!/usr/bin/env bash
set -uo pipefail
BASE="${1:?Cần URL, VD https://laichan-demo.duckdns.org}"
fail=0
check() { local name="$1" url="$2" want="$3"; shift 3
  for i in $(seq 1 30); do
    code=$(curl -ks -o /dev/null -w '%{http_code}' "$@" "$url")
    [ "$code" = "$want" ] && { echo "✓ $name ($code)"; return 0; }; sleep 6; done
  echo "✗ $name: mong $want, nhận $code"; fail=1; }
check "App thuê bao"      "$BASE/app/"           200
check "Bảng vận hành"     "$BASE/admin/"         200
check "Engine sẵn sàng"   "$BASE/health/ready"   200
check "Cấu hình đăng nhập" "$BASE/api/auth/config" 200
check "Metrics bị khoá"   "$BASE/metrics"        401
check "Demo ẩn danh tắt"  "$BASE/api/app/demo-numbers" 403
[ -n "${METRICS_TOKEN:-}" ] && check "Metrics có token" "$BASE/metrics" 200 -H "Authorization: Bearer $METRICS_TOKEN"
exit $fail
