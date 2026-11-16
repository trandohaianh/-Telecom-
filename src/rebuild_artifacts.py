"""Rebuild a consistent feature/model bundle in a fresh directory; preserve the original baseline."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--model-version", default="tfd-v2.2")
    parser.add_argument("--skip-evaluation", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume the same verified, partially completed build")
    args = parser.parse_args()
    target = args.out_dir.resolve()
    existing = target.exists()
    if existing and not args.resume:
        parser.error("use a new output directory or --resume for this build")
    if args.resume and not (target / "rebuild_report.json").is_file():
        parser.error("--resume requires an existing rebuild report")
    target.mkdir(parents=True, exist_ok=args.resume)
    data, models, logs = target / "data", target / "models", target / "logs"
    for path in (data, models, logs):
        path.mkdir(exist_ok=args.resume)
    source = Path(__file__).resolve().parent
    env = dict(os.environ, FRAUD_DATA_DIR=str(data), FRAUD_MODEL_DIR=str(models),
               FRAUD_LOG_DIR=str(logs), FRAUD_MODEL_VERSION=args.model_version,
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMBA_NUM_THREADS="1")
    stages = ["build_dataset.py", "train.py"]
    if not args.skip_evaluation:
        stages.append("evaluate.py")
    stages.extend(["make_drift_baseline.py", "shadow_run.py"])
    report = {"model_version": args.model_version, "created_utc": datetime.now(timezone.utc).isoformat(),
              "output_dir": str(target), "baseline_preserved": True, "stages": [],
              "traffic_source": "synthetic_simulator"}
    completed = set()
    if args.resume:
        report = json.loads((target / "rebuild_report.json").read_text(encoding="utf-8"))
        if report.get("model_version") != args.model_version or Path(report["output_dir"]).resolve() != target:
            parser.error("resume target/version differs from the original build")
        completed = {item["script"] for item in report["stages"] if item["exit_code"] == 0}
        checks = []
        if "build_dataset.py" in completed:
            checks.append("from model_manifest import validate_dataset_manifest; from config import DATA_DIR; assert validate_dataset_manifest(DATA_DIR)['status']=='verified'")
        if "train.py" in completed:
            checks.append("from model_manifest import validate_model_manifest; from config import MODEL_DIR; assert validate_model_manifest(MODEL_DIR,require_manifest=True)['status']=='verified'")
        for check in checks:
            subprocess.run([sys.executable, "-B", "-c", check], cwd=source, env=env, check=True)
    for stage in stages:
        if stage in completed:
            print(f"KEEP {stage} (verified completed artifact)", flush=True)
            continue
        print(f"START {stage}", flush=True)
        start = time.perf_counter()
        with (logs / (stage + ".log")).open("w", encoding="utf-8") as stream:
            process = subprocess.run([sys.executable, "-B", str(source / stage)], cwd=source.parent,
                                     env=env, stdout=stream, stderr=subprocess.STDOUT)
        report["stages"].append({"script": stage, "exit_code": process.returncode,
                                "seconds": round(time.perf_counter() - start, 3)})
        (target / "rebuild_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"{stage} failed; inspect {logs / (stage + '.log')}")
        print(f"PASS {stage} ({report['stages'][-1]['seconds']} s)", flush=True)
    code = "from model_manifest import validate_model_manifest; from config import MODEL_DIR; print(validate_model_manifest(MODEL_DIR,require_manifest=True)['status'])"
    subprocess.run([sys.executable, "-B", "-c", code], cwd=source, env=env, check=True)
    report["manifest_verified"] = True
    (target / "rebuild_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"READY {target}", flush=True)


if __name__ == "__main__":
    main()
