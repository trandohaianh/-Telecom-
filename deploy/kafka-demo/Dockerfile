# Kafka demo image: same code, bundle and pinned dependencies as the production image, plus the Kafka
# client. Built from the repository root (see docker-compose.yml). Not used by the production stack.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMBA_NUM_THREADS=1 \
    NUMBA_CACHE_DIR=/tmp/numba-cache FRAUD_ARTIFACT_DIR=/srv/app/runtime/artifacts_v22 \
    KAFKA_DEMO_STATE=/srv/state FRAUD_SQLITE_SYNCHRONOUS=FULL
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/app
COPY requirements-serve.txt deploy/kafka-demo/requirements-kafka.txt ./
RUN python -m pip install --no-cache-dir -r requirements-serve.txt -r requirements-kafka.txt && python -m pip check
COPY src ./src
COPY static ./static
COPY runtime/artifacts_v22 ./runtime/artifacts_v22
# Writable paths live on the state volume (the code directory is read-only for the app user).
ENV FRAUD_LOG_DIR=/srv/state/logs FRAUD_APP_DB=/srv/state/app.sqlite3
RUN useradd --system --uid 10001 app && mkdir -p /srv/state && chown -R app /srv/state
USER app
ENTRYPOINT ["python", "-B", "src/kafka_pipeline.py"]
