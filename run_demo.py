"""Start the demonstration dashboard. Prefer the rebuilt v2.2 bundle when available."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    artifact_dir = args.artifact_dir
    if artifact_dir is None:
        candidate = root / "runtime" / "artifacts_v22"
        if (candidate / "models" / "manifest.json").is_file() and (candidate / "data" / "dataset_manifest.json").is_file():
            artifact_dir = candidate
    if artifact_dir is not None:
        artifact_dir = artifact_dir.resolve()
        if not (artifact_dir / "models" / "manifest.json").is_file():
            parser.error("artifact directory must contain a model manifest")
        os.environ.setdefault("FRAUD_DATA_DIR", str(artifact_dir / "data"))
        os.environ.setdefault("FRAUD_MODEL_DIR", str(artifact_dir / "models"))
    os.environ["FRAUD_DEMO"] = "1"
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ.setdefault(name, "1")
    sys.path.insert(0, str(root / "src"))
    from security import NETWORK_KEY_FILE, load_users, network_key
    network_key()
    print("Telecom Fraud Shield")
    print(f"  Dashboard: http://localhost:{args.port}")
    print(f"  Subscriber app: http://localhost:{args.port}/app/")
    print(f"  Model bundle: {os.environ.get('FRAUD_MODEL_DIR', str(root / 'models'))}")
    print("  Demo dashboard credentials:")
    for name, user in load_users().items():
        if user["role"] != "agent":
            print(f"    {user['role']:<8} {name:<14} {user['token']}")
    print(f"  Network HMAC key file: {NETWORK_KEY_FILE}")
    # Import native numerical modules before starting the asynchronous server on Windows.
    import uvicorn

    from server import app
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                loop="precise_event_loop:uvicorn_loop_factory")


if __name__ == "__main__":
    main()
