"""Replay validated CALLs to an explicitly provisioned topic, in causal order."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from event_contract import json_safe, validate_call

HERE = Path(__file__).resolve().parent


def replay(rows, producer, topic, *, rate=0.0, speed=0.0, clock=time.perf_counter, sleep=time.sleep):
    """Wait before sending; acknowledgements must succeed before counting a send."""
    started = clock()
    first_ts = rows[0]["ts"] if rows else 0
    for index, row in enumerate(rows):
        target = (row["ts"] - first_ts) / speed if speed > 0 else index / rate if rate > 0 else 0
        wait = started + target - clock()
        if wait > 0:
            sleep(wait)
        producer.send(topic, key=row["caller"], value=row).get(timeout=30)
    return len(rows)


def main():
    import json

    import pandas as pd
    from kafka import KafkaProducer

    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, default=HERE / "sample_events.csv")
    parser.add_argument("--topic", default="telecom.events.ordered.v2")
    parser.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP", "localhost:29092"))
    parser.add_argument("--rate", type=float, default=0)
    parser.add_argument("--speed", type=float, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--expected-partitions", type=int, default=1)
    args = parser.parse_args()
    if min(args.rate, args.speed, args.limit) < 0 or args.expected_partitions < 1:
        parser.error("rate/speed/limit must be nonnegative; partitions must be positive")
    if args.rate and args.speed:
        parser.error("choose either rate or speed")
    frame = pd.read_csv(args.events, dtype={key: str for key in ("caller", "callee", "imei", "cell_id", "event_id")})
    frame = frame.sort_values(["ts", "event_id"], kind="stable")
    if args.limit:
        frame = frame.head(args.limit)
    rows = [validate_call(json_safe(row)) for row in frame.to_dict("records")]
    rows = [row for row in rows if row is not None]
    if len({row["event_id"] for row in rows}) != len(rows):
        raise ValueError("Replay input has duplicate event_id")
    producer = KafkaProducer(
        bootstrap_servers=args.bootstrap,
        value_serializer=lambda row: json.dumps(row, allow_nan=False).encode(),
        key_serializer=lambda key: str(key).encode(),
        acks="all",
        retries=3,
        max_in_flight_requests_per_connection=1,
        linger_ms=0,
    )
    started = time.perf_counter()
    try:
        partitions = producer.partitions_for(args.topic)
        if not partitions or len(partitions) != args.expected_partitions:
            raise ValueError("Provision a topic with the requested partition count first")
        sent = replay(rows, producer, args.topic, rate=args.rate, speed=args.speed)
        producer.flush()
    finally:
        producer.close()
    elapsed = time.perf_counter() - started
    print(f"acknowledged={sent} topic={args.topic} elapsed_s={elapsed:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
