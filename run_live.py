"""Start a dedicated live/shadow API with isolated durable state: python run_live.py."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state-dir", type=Path, default=root / "runtime" / "live")
    parser.add_argument("--require-manifest", action="store_true")
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args()
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["FRAUD_DEMO"] = "0"
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ.setdefault(name, "1")
    model_dir = args.model_dir
    if model_dir is None and not os.environ.get("FRAUD_MODEL_DIR"):
        candidate = root / "runtime" / "artifacts_v22" / "models"
        if (candidate / "manifest.json").is_file():
            model_dir = candidate
    if model_dir is not None:
        os.environ["FRAUD_MODEL_DIR"] = str(model_dir.resolve())
        adjacent_data = model_dir.resolve().parent / "data"
        if (adjacent_data / "dataset_manifest.json").is_file():
            os.environ.setdefault("FRAUD_DATA_DIR", str(adjacent_data))
    for variable, filename in (
        ("FRAUD_STATE_DB", "state.sqlite3"), ("FRAUD_EVIDENCE_FILE", "evidence.jsonl"),
        ("FRAUD_AUDIT_FILE", "audit.jsonl"), ("FRAUD_USERS_FILE", "users.json"),
        ("FRAUD_NETWORK_KEY", "network_link.key"),
    ):
        os.environ[variable] = str(state_dir / filename)
    if args.require_manifest:
        os.environ["FRAUD_REQUIRE_MANIFEST"] = "1"
    sys.path.insert(0, str(root / "src"))
    import security
    security.load_users()
    security.network_key()
    import uvicorn
    print(f"Live API starts in shadow mode at http://{args.host}:{args.port}")
    print(f"State and integration credentials: {state_dir}")
    from server import app
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                loop="precise_event_loop:uvicorn_loop_factory")


if __name__ == "__main__":
    main()
