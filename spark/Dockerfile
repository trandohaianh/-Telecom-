# Ảnh Spark gọn, dựng từ python chính thức + JRE, thay vì ảnh Spark dựng sẵn.
# Lý do: cần đúng các thư viện của dự án (pandas, pyarrow, numpy) để import được
# src/feature_store.py ngay trong container, và cần chốt phiên bản PySpark.
FROM python:3.11-slim

# Spark 4.x cần Java 17 trở lên; procps để có lệnh ps mà spark-submit gọi tới.
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless procps \
 && rm -rf /var/lib/apt/lists/*

ENV PYSPARK_PYTHON=python3 \
    PYSPARK_DRIVER_PYTHON=python3 \
    PYTHONUNBUFFERED=1

# Chốt phiên bản: jar kết nối Kafka phải trùng đúng phiên bản Spark, nên không để pip tự nâng.
RUN pip install --no-cache-dir \
      pyspark==4.2.0 \
      pandas==2.2.3 \
      pyarrow==18.1.0 \
      numpy==2.1.3 \
      kafka-python==2.0.2

WORKDIR /work/spark
