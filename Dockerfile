# Telecom Fraud Shield — image phục vụ (amd64 + arm64/Oracle Ampere).
# Ghim bản vá (cùng base với telecom_grpc); Dockerfile không cho phép chú thích cuối dòng.
FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMBA_NUM_THREADS=1 \
    NUMBA_CACHE_DIR=/tmp/numba-cache \
    FRAUD_ARTIFACT_DIR=/srv/app/runtime/artifacts_v22 \
    FRAUD_LOG_DIR=/srv/state/logs FRAUD_USERS_FILE=/srv/state/secrets/users.json \
    FRAUD_NETWORK_KEY=/srv/state/secrets/network_link.key FRAUD_AUDIT_FILE=/srv/state/logs/audit.jsonl \
    FRAUD_APP_DB=/srv/state/app.sqlite3 PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/app
COPY requirements-serve.txt .
RUN python -m pip install --no-cache-dir -r requirements-serve.txt && python -m pip check
COPY src ./src
COPY static ./static
COPY runtime/artifacts_v22 ./runtime/artifacts_v22
COPY serve.py .
# Dữ liệu ghi được (CSDL app, khoá, nhật ký, backup) nằm trong volume /srv/state.
RUN useradd --system --uid 10001 app && mkdir -p /srv/state && chown -R app /srv/state
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=4).status==200 else 1)"
CMD ["python", "serve.py"]
