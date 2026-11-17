#!/usr/bin/env bash
# Cập nhật Telecom trên VM: backup trước, kéo code, build lại, smoke test.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/telecom}"
cd "$APP_DIR"
git fetch --prune origin
# DEPLOY_REF = commit CI đã kiểm thử (workflow_run.head_sha). Không có thì lấy đầu nhánh (chạy tay).
# Chỉ nhận SHA đầy đủ nằm trên nhánh deploy: không build một commit CI chưa chạy, cũng không build nhánh lạ.
TARGET="origin/${DEPLOY_BRANCH:-main}"
if [ -n "${DEPLOY_REF:-}" ]; then
  [[ "$DEPLOY_REF" =~ ^[0-9a-f]{40}$ ]] || { echo "DEPLOY_REF phải là SHA 40 ký tự" >&2; exit 2; }
  git merge-base --is-ancestor "$DEPLOY_REF" "$TARGET" || { echo "$DEPLOY_REF không thuộc $TARGET" >&2; exit 2; }
  TARGET="$DEPLOY_REF"
fi
git reset --hard "$TARGET"
cd deploy/cloud
set -a; source .env; set +a
# Backup trước deploy phải thành công (trước đây "|| true" cho deploy chạy tiếp khi không có backup).
# Lần cài đầu chưa có container: bỏ qua có chủ đích. SKIP_PREDEPLOY_BACKUP=1 để ép bỏ qua.
if [ "${SKIP_PREDEPLOY_BACKUP:-0}" != "1" ] && sudo docker ps --format '{{.Names}}' | grep -q '^telecom-telecom-1$'; then
  status="$(sudo docker exec telecom-telecom-1 python -c "import sys; sys.path.insert(0, 'src'); import portal, ops_pipeline; print(ops_pipeline.run_job(portal.db(), None, 'backup', 'pre-deploy')['status'])")"
  if [ "$status" != "ok" ] && [ "$status" != "warn" ]; then
    echo "Backup trước deploy không thành công (status=$status); dừng deploy." >&2
    exit 3
  fi
fi
FORWARDED_ALLOW_IPS="$(bash ./proxy_ip.sh)" sudo -E docker compose --env-file .env up -d --build telecom
bash ./smoke.sh "https://$TELECOM_DOMAIN"
