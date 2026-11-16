"""Run the existing real-HTTP verifier in an isolated Linux container."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sys

PACKAGES = ("numpy", "pandas", "scikit-learn", "lightgbm", "scipy", "numba",
            "fastapi", "uvicorn", "joblib", "httpx")


def kernel_limit(name):
    path = Path("/sys/fs/cgroup") / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", default="10,100,500")
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--model-dir", type=Path, default=Path("/artifacts/models"))
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("this entrypoint is for the isolated Linux backend image")
    # Set all mutable config paths before importing the application-owned verifier modules.
    os.environ.update(FRAUD_DEMO="0", FRAUD_REAL_TRAFFIC_VALIDATED="0",
                      FRAUD_MODEL_DIR=str(args.model_dir), FRAUD_REQUIRE_MANIFEST="1",
                      FRAUD_DATA_DIR="/tmp/telecom/data", FRAUD_LOG_DIR="/tmp/telecom/logs")
    sys.path.insert(0, "/workspace/src")
    from verify_live_http import verify_stack
    try:
        result = verify_stack(rates=tuple(float(v) for v in args.rates.split(",")),
                              seconds=args.seconds, model_dir=args.model_dir.resolve())
    except Exception:
        # Only the disposable verifier's process logs; credentials and keys are never printed.
        for path in sorted(Path("/tmp").glob("telecom_live_http_*/server.log")):
            print("Disposable server log: " + str(path), file=sys.stderr)
            print(path.read_text(encoding="utf-8")[-10000:], file=sys.stderr)
        raise
    result.update(
        platform={"system": platform.system(), "release": platform.release(),
                  "machine": platform.machine(), "python": platform.python_version()},
        packages={name: version(name) for name in PACKAGES},
        container_limits={"cpu_max": kernel_limit("cpu.max"), "memory_max": kernel_limit("memory.max")},
        runtime_configuration={"state_owners": 1, "sqlite_journal_mode": "WAL",
                               "sqlite_synchronous": "FULL", "call_deadline_ms": 30,
                               "mode": "shadow", "public_ports": False},
        source_sha256={name: hashlib.sha256((Path("/workspace/src") / name).read_bytes()).hexdigest()
                       for name in ("server.py", "backend_runtime.py", "backend_state.py",
                                    "http_loadtest.py", "verify_live_http.py")},
        artifact_retention="container scratch state/logs are removed with this verifier container",
    )
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
