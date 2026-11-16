#!/usr/bin/env bash
# Cập nhật Telecom trên VM: backup trước, kéo code, build lại, smoke test.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/telecom}"
cd "$APP_DIR"
git fetch --prune origin
git reset --hard "origin/${DEPLOY_BRANCH:-main}"
cd deploy/cloud
set -a; source .env; set +a
sudo docker exec telecom-telecom-1 python -c "import sys; sys.path.insert(0, 'src'); import portal, ops_pipeline; print(ops_pipeline.run_job(portal.db(), None, 'backup', 'pre-deploy')['status'])" || true
sudo docker compose --env-file .env up -d --build telecom
bash ./smoke.sh "https://$TELECOM_DOMAIN"
