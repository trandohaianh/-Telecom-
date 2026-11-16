ARG PYTHON_BASE=python:3.11.16-slim-trixie
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMBA_NUM_THREADS=1 \
    NUMBA_CACHE_DIR=/tmp/numba-cache HOME=/tmp XDG_CACHE_HOME=/tmp/cache \
    PYTHONPATH=/workspace/src FRAUD_DEMO=0 FRAUD_REAL_TRAFFIC_VALIDATED=0 \
    FRAUD_REQUIRE_MANIFEST=1 FRAUD_MODEL_DIR=/artifacts/models \
    FRAUD_DATA_DIR=/tmp/telecom/data FRAUD_LOG_DIR=/tmp/telecom/logs
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-linux.txt /tmp/requirements-linux.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements-linux.txt
COPY verify_entrypoint.py /usr/local/bin/backend-verify.py
RUN mkdir -p /workspace/src /workspace/static/app /artifacts/models /artifacts/data
USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["python", "-B", "/usr/local/bin/backend-verify.py"]
