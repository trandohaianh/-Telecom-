"""Distributed CALL replay and durable local or per-key Kafka streaming.

Batch defaults to 27 causal features on workers. --engine reference is explicitly
local; --engine sql-basics retains the five-feature window exercise.
Streaming selects local driver state or durable per-key snapshots on workers.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import pyspark
from pyspark.sql import SparkSession, Window, functions as F, types as T

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
from config import LOCAL_UTC_OFFSET_S  # noqa: E402  (same local-hour convention as FeatureStore)

EVENT_SCHEMA = T.StructType([
    T.StructField(name, T.DoubleType() if name in ("ts", "caller_age_days", "duration_s", "label")
                  else T.StringType()) for name in
    ("event_id", "event_type", "ts", "caller", "callee", "cell_id", "imei", "origin_trunk",
     "network", "caller_age_days", "duration_s", "label", "fraud_type")
])


def add_sql_basics(df):
    def window(seconds):
        return Window.partitionBy("caller").orderBy("ts").rangeBetween(-seconds, 0)
    result = df.withColumn("hour_of_day", ((F.col("ts") + LOCAL_UTC_OFFSET_S) / 3600.0) % 24)  # == config.local_hour
    for name, seconds in (("calls_1m", 60), ("calls_10m", 600), ("calls_1h", 3600)):
        result = result.withColumn(name, F.count("*").over(window(seconds)).cast("double"))
    return result.withColumn("callee_dispersion_1h",
        F.size(F.array_distinct(F.collect_list("callee").over(window(3600)))) / F.col("calls_1h"))


def add_features(df, partitions=8):
    from distributed_features import add_distributed_features
    return add_distributed_features(df, partitions)


def run_batch(spark, events_csv, out, *, engine="distributed", partitions=8):
    from event_contract import validate_call
    from feature_store import CALL_FEATURES, FeatureStore
    data = spark.read.csv(str(events_csv), header=True, schema=EVENT_SCHEMA, enforceSchema=False)
    validated = data.rdd.map(lambda row: validate_call(row.asDict())).filter(lambda row: row is not None)
    data = spark.createDataFrame(validated, EVENT_SCHEMA)
    if data.groupBy("event_id").count().filter(F.col("count") > 1).limit(1).count():
        raise ValueError("Batch input has duplicate event_id; deduplicate upstream explicitly")
    if engine == "distributed":
        features = add_features(data, partitions)
    elif engine == "reference":
        print("REFERENCE ENGINE: feature computation runs locally on the driver", flush=True)
        store, rows = FeatureStore(), []
        for record in data.orderBy("ts", "event_id").toLocalIterator():
            row = record.asDict()
            values = store.call_features(row)
            store.commit_cdr(row)
            rows.append((row["event_id"], *[float(values[key]) for key in CALL_FEATURES]))
        schema = "event_id string, " + ", ".join(f"{name} double" for name in CALL_FEATURES)
        features = spark.createDataFrame(rows, schema)
    else:
        print("SQL BASICS: only 5 features; RANGE peers differ from causal ordering for tied timestamps")
        enriched = add_sql_basics(data)
        features = enriched.select("event_id", *[name for name in CALL_FEATURES if name in enriched.columns])
    features.write.mode("overwrite").parquet(str(out))
    print(f"engine={engine} features={len(features.columns)-1} output={out}", flush=True)


def run_stream(spark, bootstrap, topic, checkpoint, max_offsets, *, available_now=False,
               allowed_lateness_s=0, dedup_horizon_s=86400, late_policy="quarantine",
               retain_snapshots=3, max_buffered_events=100000, metadata_ttl_s=None,
               engine="local-stream", partitions=8):
    from stateful_stream import StatefulCallStream
    options = dict(allowed_lateness_s=allowed_lateness_s, dedup_horizon_s=dedup_horizon_s,
        late_policy=late_policy, retain_snapshots=retain_snapshots, max_buffered_events=max_buffered_events,
        metadata_ttl_s=metadata_ttl_s)
    if engine == "distributed-stream":
        from distributed_stream import DistributedCallStream
        processor = DistributedCallStream(spark, checkpoint, {"bootstrap": bootstrap, "topic": topic},
                                          partitions=partitions, **options)
    else:
        processor = StatefulCallStream(checkpoint, {"bootstrap": bootstrap, "topic": topic}, **options)
    raw = (spark.readStream.format("kafka").option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", topic).option("startingOffsets", "earliest")
        .option("maxOffsetsPerTrigger", max_offsets).load())
    parsed = raw.select(F.from_json(F.col("value").cast("string"), EVENT_SCHEMA).alias("e"),
        F.col("value").cast("string").alias("_raw"), F.col("partition").alias("_partition"),
        F.col("offset").alias("_offset"))
    events = parsed.select("e.*", "_raw", "_partition", "_offset",
        F.when(F.col("e.event_type").isNull(), F.lit("malformed JSON or missing event_type"))
         .alias("_parse_error"))

    def handle(batch, batch_id):
        processor.process(batch if engine == "distributed-stream" else batch.toPandas(), batch_id)
        print(json.dumps(processor.last_metrics, sort_keys=True), flush=True)

    writer = events.writeStream.foreachBatch(handle).option("checkpointLocation", str(checkpoint))
    writer = writer.trigger(availableNow=True) if available_now else writer.trigger(processingTime="5 seconds")
    query = writer.start()
    try:
        query.awaitTermination()
        if available_now:
            processor.flush()
            print(json.dumps(processor.last_metrics, sort_keys=True), flush=True)
    finally:
        if query.isActive:
            query.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("batch", "stream"), default="batch")
    parser.add_argument("--engine", choices=("distributed", "reference", "sql-basics",
                        "local-stream", "distributed-stream"), default=None)
    parser.add_argument("--events", type=Path, default=HERE / "sample_events.csv")
    parser.add_argument("--out", type=Path, default=HERE / "spark_call_features.parquet")
    parser.add_argument("--kafka", default=os.environ.get("KAFKA_BOOTSTRAP", "localhost:29092"))
    parser.add_argument("--topic", default="telecom.events.ordered.v2")
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument("--max-offsets", type=int, default=10000)
    parser.add_argument("--partitions", type=int, default=8)
    parser.add_argument("--allowed-lateness-seconds", type=float, default=0)
    parser.add_argument("--dedup-horizon-seconds", type=float, default=86400)
    parser.add_argument("--late-policy", choices=("quarantine", "fail"), default="quarantine")
    parser.add_argument("--retain-snapshots", type=int, default=3)
    parser.add_argument("--max-buffered-events", type=int, default=100000)
    parser.add_argument("--metadata-ttl-seconds", type=float, default=None,
                        help="Optional idle-metadata expiry >=24h; restarts subscriber history")
    parser.add_argument("--available-now", action="store_true")
    args = parser.parse_args()
    args.engine = args.engine or ("distributed" if args.mode == "batch" else "local-stream")
    if args.mode == "batch" and args.engine.endswith("-stream"):
        parser.error("stream engines require --mode stream")
    if args.mode == "stream" and args.engine not in ("local-stream", "distributed-stream"):
        parser.error("select local-stream or distributed-stream for --mode stream")
    args.checkpoint = args.checkpoint or HERE / ("_checkpoint_distributed_stream_v1"
        if args.engine == "distributed-stream" else "_checkpoint_stateful_v2")
    if args.max_offsets < 1 or args.partitions < 1:
        parser.error("offsets and partitions must be positive")
    builder = (SparkSession.builder.appName("telecom-causal-call-features")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", str(args.partitions)))
    if args.mode == "stream":
        builder = builder.config("spark.jars.packages",
            f"org.apache.spark:spark-sql-kafka-0-10_2.13:{pyspark.__version__}")
        if os.environ.get("IVY_HOME"):
            builder = builder.config("spark.jars.ivy", os.environ["IVY_HOME"])
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    for source in ("feature_store.py", "data_generator.py"):
        spark.sparkContext.addPyFile(str(SRC / source))
    for source in ("distributed_features.py", "distributed_stream.py", "event_contract.py"):
        spark.sparkContext.addPyFile(str(HERE / source))
    try:
        if args.mode == "batch":
            run_batch(spark, args.events, args.out, engine=args.engine, partitions=args.partitions)
        else:
            print(f"STREAM ENGINE: {args.engine}", flush=True)
            run_stream(spark, args.kafka, args.topic, args.checkpoint, args.max_offsets,
                available_now=args.available_now, allowed_lateness_s=args.allowed_lateness_seconds,
                dedup_horizon_s=args.dedup_horizon_seconds, late_policy=args.late_policy,
                retain_snapshots=args.retain_snapshots, max_buffered_events=args.max_buffered_events,
                metadata_ttl_s=args.metadata_ttl_seconds, engine=args.engine, partitions=args.partitions)
    finally:
        spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
