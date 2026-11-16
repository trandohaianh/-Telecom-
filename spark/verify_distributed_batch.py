"""Real Spark shuffle parity check; writes only below an isolated output directory."""
import argparse
from pathlib import Path
import sys
import tempfile

import pandas as pd
from pyspark.sql import SparkSession

from spark_features import HERE, SRC, run_batch
from make_sample import reference_features
from test_stateful_stream import event
from compare_features import compare, load
from event_contract import json_safe, validate_call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, default=HERE / "sample_events.csv")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    out = args.out_dir or Path(tempfile.mkdtemp(prefix="telecom-distributed-v2-"))
    out.mkdir(parents=True, exist_ok=True)
    frame = pd.read_csv(args.events, dtype={key: str for key in
        ("caller", "callee", "imei", "cell_id", "event_id")})
    from data_generator import REGISTERED_CELLS
    cell = next(iter(REGISTERED_CELLS))
    edge = [event("EDGE-a", 0., duration_s=100., cell_id=cell),
            event("EDGE-b", 50., duration_s=3., cell_id=cell),
            event("EDGE-c", 60., caller="B", callee="A", cell_id=cell),
            event("EDGE-d", 60., caller="A", callee="C", cell_id=cell),
            event("EDGE-e", 60., caller="A", callee="A", imei=""),
            event("EDGE-f", 86400., caller="A", callee="B", cell_id=cell),
            event("EDGE-g", 86459., caller="A", callee="B", cell_id=cell)]
    rows = [validate_call(json_safe(row)) for row in frame.to_dict("records")] + edge
    frame = pd.DataFrame(rows).sort_values(["ts", "event_id"], kind="stable")
    input_path, ref_path, result_path = out / "events.csv", out / "reference.parquet", out / "distributed.parquet"
    frame.to_csv(input_path, index=False)
    reference_features(frame).to_parquet(ref_path, index=False)
    spark = (SparkSession.builder.master("local[2]").appName("telecom-v2-distributed-verification")
        .config("spark.sql.shuffle.partitions", "4").config("spark.ui.enabled", "false").getOrCreate())
    spark.sparkContext.setLogLevel("ERROR")
    for source in (SRC / "feature_store.py", SRC / "data_generator.py",
                   HERE / "distributed_features.py", HERE / "event_contract.py"):
        spark.sparkContext.addPyFile(str(source))
    try:
        run_batch(spark, input_path, result_path, engine="distributed", partitions=4)
        result = compare(load(ref_path), load(result_path))
        if result:
            raise SystemExit(result)
        print(f"PASS: {len(frame)} events, 27/27 features, real Spark shuffles, tied times and cross-key edges")
        print(f"ARTIFACTS={out}")
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
