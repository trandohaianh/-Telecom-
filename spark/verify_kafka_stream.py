"""Isolated four-partition Kafka check: reorder, restart, dedup and quarantine."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pandas as pd
from event_contract import json_safe
from kafka import KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic
from make_sample import reference_features
from stateful_stream import CALL_FEATURES, StatefulCallStream


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092"))
    parser.add_argument("--events", type=Path, default=here / "sample_events.csv")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--engine", choices=("local-stream", "distributed-stream"), default="local-stream")
    parser.add_argument("--max-offsets", type=int, default=1000)
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    topic = f"telecom.verify.v2.{suffix}"
    checkpoint = (args.out_dir or Path(tempfile.gettempdir())) / f"telecom-verify-v2-{suffix}"
    admin = KafkaAdminClient(bootstrap_servers=args.bootstrap)
    try:
        admin.create_topics([NewTopic(topic, num_partitions=4, replication_factor=1)])
    finally:
        admin.close()
    frame = pd.read_csv(
        args.events, dtype={key: str for key in ("caller", "callee", "imei", "cell_id", "event_id")}
    ).sort_values(["ts", "event_id"])
    rows = [json_safe(row) for row in frame.to_dict("records")]
    producer = KafkaProducer(
        bootstrap_servers=args.bootstrap,
        acks="all",
        retries=3,
        max_in_flight_requests_per_connection=1,
        key_serializer=lambda value: str(value).encode(),
        value_serializer=lambda value: (
            value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
        ),
    )
    lateness = max(1.0, rows[-1]["ts"] - rows[0]["ts"] + 1.0)
    command = [
        sys.executable,
        str(here / "spark_features.py"),
        "--mode",
        "stream",
        "--kafka",
        args.bootstrap,
        "--topic",
        topic,
        "--checkpoint",
        str(checkpoint),
        "--engine",
        args.engine,
        "--partitions",
        "4",
        "--max-offsets",
        str(args.max_offsets),
        "--allowed-lateness-seconds",
        str(lateness),
        "--dedup-horizon-seconds",
        str(max(86400.0, lateness)),
        "--available-now",
    ]
    midpoint = len(rows) // 2
    try:
        for chunk in (rows[:midpoint], rows[midpoint:]):
            for row in chunk:
                producer.send(topic, key=row["caller"], value=row).get(timeout=30)
            producer.flush()
            subprocess.run(command, check=True)
        producer.send(topic, key=rows[0]["caller"], value=rows[0]).get(timeout=30)
        producer.send(topic, key="invalid", value=b"{bad-json").get(timeout=30)
        producer.flush()
        subprocess.run(command, check=True)
    finally:
        producer.close()
    if args.engine == "distributed-stream":
        from distributed_stream import DistributedCallStream

        processor_class = DistributedCallStream
    else:
        processor_class = StatefulCallStream
    files = processor_class.output_files(checkpoint)
    actual = pd.concat([pd.read_parquet(path) for path in files]).set_index("event_id").sort_index()
    expected = reference_features(frame).set_index("event_id").sort_index()
    assert actual.index.is_unique
    pd.testing.assert_frame_equal(actual[CALL_FEATURES], expected[CALL_FEATURES].astype(float), atol=1e-9, rtol=1e-9)
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))["metrics"]
        for path in (
            checkpoint / ("distributed_call_features_v1" if args.engine == "distributed-stream" else "call_features_v2")
        ).glob("batch-*/receipt.json")
    ]
    assert sum(metrics["duplicates"] for metrics in receipts) == 1
    assert sum(metrics["quarantined"] for metrics in receipts) == 1
    print(
        f"PASS: engine={args.engine}, {len(actual)} events, 27/27 features, four partitions, restart, dedup, quarantine"
    )
    print(f"TOPIC={topic} CHECKPOINT={checkpoint}")


if __name__ == "__main__":
    main()
