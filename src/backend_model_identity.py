"""Stable serving identity for checkpoints, including legacy bundles without a manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ARTIFACTS = ("tier1_call_lgbm.txt", "tier2_audio_lgbm.txt", "tier1_zero_day_iforest.joblib",
             "sms_text_lr.joblib", "thresholds.json", "training_report.json")


def loaded_model_identity(detector, model_dir=None):
    cached = getattr(detector, "runtime_model_identity", None)
    if cached is not None:
        return dict(cached)
    manifest = getattr(detector, "model_manifest", {})
    bundle = manifest.get("manifest_sha256")
    if bundle is None and model_dir is not None:
        digest = hashlib.sha256()
        for name in ARTIFACTS:
            path = Path(model_dir) / name
            digest.update(name.encode("utf-8") + b"\0")
            if path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(b"missing")
            digest.update(b"\0")
        bundle = "legacy:" + digest.hexdigest()
    return {"version": 1, "model_version": getattr(detector, "model_version", None),
            "bundle_sha256": bundle,
            "feature_contract": getattr(getattr(detector, "fs", None), "FEATURE_CONTRACT", None)}
