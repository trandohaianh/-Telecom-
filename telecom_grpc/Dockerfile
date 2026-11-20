FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY ram_online /app/ram_online
COPY telecom_grpc /app/telecom_grpc
RUN pip install --no-cache-dir -r /app/telecom_grpc/requirements.txt \
 && python -m grpc_tools.protoc -I/app/telecom_grpc/proto \
    --python_out=/app/telecom_grpc --grpc_python_out=/app/telecom_grpc \
    /app/telecom_grpc/proto/telecom.proto \
 && sed -i 's/^import telecom_pb2 as telecom__pb2/from . import telecom_pb2 as telecom__pb2/' /app/telecom_grpc/telecom_pb2_grpc.py \
 && useradd --system --uid 10001 --no-create-home appuser
USER 10001:10001
EXPOSE 50051
CMD ["python", "-m", "telecom_grpc.server"]
