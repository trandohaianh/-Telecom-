#!/bin/sh
set -eu
exec celery -A app.celery_app:celery worker --loglevel=INFO \
  --concurrency="${CELERY_CONCURRENCY:-2}" \
  --max-tasks-per-child="${CELERY_MAX_TASKS_PER_CHILD:-50}" \
  --without-gossip --without-mingle
