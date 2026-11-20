"""Real Spark persistent-state integration across two separate Python processes.

The controller and every keyed state transition run on Spark workers; only the
verification step collects the final bounded feature output for comparison.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
from event_contract import json_safe, validate_call
from make_sample import reference_features
from spark_features import EVENT_SCHEMA, HERE, SRC
from test_stateful_stream import event


def spark_session():
    from pyspark.sql import SparkSession
    spark = (SparkSession.builder.master("local[2]").appName("telecom-distributed-state-check")
             .config("spark.sql.shuffle.partitions", "4").config("spark.ui.enabled", "false")
             .config("spark.sql.adaptive.enabled", "false").getOrCreate())
    spark.sparkContext.setLogLevel("ERROR")
    for source in (SRC / "feature_store.py", SRC / "data_generator.py",
                   HERE / "distributed_stream.py", HERE / "event_contract.py"):
        spark.sparkContext.addPyFile(str(source))
    return spark


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, default=HERE / "sample_events.csv")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--phase", choices=("first", "second"))
    args = parser.parse_args()
    out = args.out_dir or Path(tempfile.mkdtemp(prefix="telecom-distributed-stream-"))
    out.mkdir(parents=True, exist_ok=True)
    if args.phase is None:
        frame = pd.read_csv(args.events, dtype={key: str for key in
            ("caller", "callee", "imei", "cell_id", "event_id")})
        from data_generator import REGISTERED_CELLS
        cell = next(iter(REGISTERED_CELLS))
        edges = [event("EDGE-a", 0., duration_s=100., cell_id=cell),
                 event("EDGE-b", 50., duration_s=3., cell_id=cell),
                 event("EDGE-c", 60., caller="B", callee="A", cell_id=cell),
                 event("EDGE-d", 60., caller="A", callee="C", cell_id=cell),
                 event("EDGE-e", 60., caller="A", callee="A", imei=""),
                 event("EDGE-f", 86400., caller="A", callee="B", cell_id=cell),
                 event("EDGE-g", 86459., caller="A", callee="B", cell_id=cell)]
        rows = [validate_call(json_safe(row)) for row in frame.to_dict("records")] + edges
        frame = pd.DataFrame(rows).sort_values(["ts", "event_id"], kind="stable")
        frame.to_csv(out / "events.csv", index=False)
        reference_features(frame).to_parquet(out / "reference.parquet", index=False)
        for phase in ("first", "second"):
            subprocess.run([sys.executable, __file__, "--out-dir", str(out), "--phase", phase], check=True)
        print(f"PASS: {len(frame)} events,27/27 features,persistent per-key Spark state,two process starts")
        print(f"ARTIFACTS={out}")
        return
    from distributed_stream import DistributedCallStream
    from pyspark.sql import functions as F
    spark = spark_session()
    checkpoint = out / "checkpoint"
    source = {"input": str(out / "events.csv"), "test": "distributed-v1"}
    try:
        frame = pd.read_csv(out / "events.csv", dtype={key: str for key in
            ("caller", "callee", "imei", "cell_id", "event_id")})
        rows = [validate_call(json_safe(row)) for row in frame.to_dict("records")]
        midpoint = len(rows) // 2
        first = spark.createDataFrame(rows[:midpoint], EVENT_SCHEMA).repartition(4)
        second = spark.createDataFrame(rows[midpoint:], EVENT_SCHEMA).repartition(4)
        processor = DistributedCallStream(spark, checkpoint, source, partitions=4,
            allowed_lateness_s=10., retain_snapshots=2)
        if args.phase == "first":
            processor.process(first, 0)
            assert processor.last_metrics["buffered_rows"] > 0
            assert processor.last_metrics["emitted_rows"] > 0
            assert processor.last_metrics["state_entities"]["subscriber"] > 0
            retry = processor.process(first.orderBy(F.desc("event_id")), 0)
            assert retry.count() == processor.last_metrics["emitted_rows"]
            with_failure = first.withColumn("duration_s", F.col("duration_s") + 1.)
            try:
                processor.process(with_failure, 0)
            except ValueError as error:
                assert "different input" in str(error)
            else:
                raise AssertionError("Changed retry must fail")
        else:
            assert processor.last_batch == 0
            processor.process(first, 0)  # Retry after a real process restart.
            processor.process(second, 1)
            assert processor.last_metrics["buffered_rows"] > 0
            processor.flush()
            duplicate = next(row for row in rows if 1000. < row["ts"] < 60000.)
            malformed = {**duplicate, "event_id": "INVALID", "ts": None}
            other = next(row for row in rows if 1000. < row["ts"] < 60000. and row["event_id"] != duplicate["event_id"])
            conflicting = {**other, "duration_s": other["duration_s"] + 1.}
            late = {**duplicate, "event_id": "LATE", "ts": 0.}
            processor.process(spark.createDataFrame([duplicate, duplicate, conflicting, malformed, late], EVENT_SCHEMA), 2)
            assert processor.last_metrics["duplicates"] == 2
            assert processor.last_metrics["quarantined"] == 3
            files = DistributedCallStream.output_files(checkpoint)
            actual = spark.read.parquet(*[str(path) for path in files]).toPandas().set_index("event_id").sort_index()
            expected = pd.read_parquet(out / "reference.parquet").set_index("event_id").sort_index()
            from feature_store import CALL_FEATURES
            assert actual.index.is_unique
            pd.testing.assert_frame_equal(actual[CALL_FEATURES], expected[CALL_FEATURES].astype(float),
                                          atol=1e-9, rtol=1e-9)
            try:
                DistributedCallStream(spark, checkpoint, {**source, "test": "changed"}, partitions=4,
                                      allowed_lateness_s=10., retain_snapshots=2)
            except ValueError:
                pass
            else:
                raise AssertionError("Source contract mismatch must fail")
            print("PASS: exact27 parity after restart,lateness tail,retry,conflicts,late and malformed quarantine")
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
