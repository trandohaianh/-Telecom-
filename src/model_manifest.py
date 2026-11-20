"""Artifact/provenance manifests. Checksums detect mismatch; they do not authenticate pickles."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from feature_store import AUDIO_FEATURES, CALL_FEATURES, SMS_META_FEATURES, FeatureStore

MODEL_ARTIFACTS = (
    "tier1_call_lgbm.txt",
    "tier2_audio_lgbm.txt",
    "tier1_zero_day_iforest.joblib",
    "sms_text_lr.joblib",
    "thresholds.json",
    "training_report.json",
)
TRAIN_FEATURES = tuple(
    f"{split}_{kind}_features.parquet" for split in ("train", "val", "cal") for kind in ("call", "sms", "audio")
)


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def feature_schema() -> dict:
    return {
        "contract": FeatureStore.FEATURE_CONTRACT,
        "call": list(CALL_FEATURES),
        "sms_meta": list(SMS_META_FEATURES),
        "audio": list(AUDIO_FEATURES),
    }


def _write_atomic(path, content):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(content, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def write_dataset_manifest(data_dir, windows, artifacts) -> dict:
    data_dir = Path(data_dir).resolve()
    files = {}
    for artifact in artifacts:
        path = Path(artifact).resolve()
        name = path.relative_to(data_dir).as_posix()
        files[name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    source_dir = Path(__file__).resolve().parent
    manifest = {
        "version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_kind": "synthetic_simulator",
        "feature_schema": feature_schema(),
        "windows": {name: list(config) for name, config in windows.items()},
        "files": files,
        "source_sha256": {
            name: sha256_file(source_dir / name)
            for name in ("feature_store.py", "data_generator.py", "build_dataset.py")
        },
    }
    _write_atomic(data_dir / "dataset_manifest.json", manifest)
    return manifest


def validate_dataset_manifest(data_dir) -> dict:
    """Verify train/validation/calibration features only; test labels/scores are not consumed."""
    data_dir = Path(data_dir)
    path = data_dir / "dataset_manifest.json"
    if not path.exists():
        return {
            "status": "legacy_unverified",
            "reason": "dataset_manifest.json is absent",
            "feature_contract": "unknown",
            "input_sha256": {
                name: sha256_file(data_dir / name) for name in TRAIN_FEATURES if (data_dir / name).is_file()
            },
        }
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("feature_schema") != feature_schema():
        raise ValueError("dataset feature contract/schema mismatch; rebuild features before training")
    hashes = {}
    for name in TRAIN_FEATURES:
        expected = manifest.get("files", {}).get(name, {}).get("sha256")
        if not expected or not (data_dir / name).is_file():
            raise ValueError(f"dataset manifest is missing training input {name}")
        actual = sha256_file(data_dir / name)
        if actual != expected:
            raise ValueError(f"dataset checksum mismatch: {name}")
        hashes[name] = actual
    return {
        "status": "verified",
        "feature_contract": FeatureStore.FEATURE_CONTRACT,
        "manifest_sha256": sha256_file(path),
        "input_sha256": hashes,
        "dataset_kind": manifest.get("dataset_kind", "unknown"),
    }


def write_model_manifest(model_dir, model_version: str, dataset_provenance: dict) -> dict:
    model_dir = Path(model_dir)
    manifest = {
        "version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": model_version,
        "feature_schema": feature_schema(),
        "dataset_provenance": dataset_provenance,
        "probabilities_calibrated": False,
        "drift_baseline_requires_rebuild": True,
        "artifacts": {
            name: {"sha256": sha256_file(model_dir / name), "bytes": (model_dir / name).stat().st_size}
            for name in MODEL_ARTIFACTS
        },
    }
    _write_atomic(model_dir / "manifest.json", manifest)
    return manifest


def validate_model_manifest(model_dir, *, require_manifest=False) -> dict:
    """Check the complete declared model bundle before loading any serialized model."""
    model_dir = Path(model_dir)
    path = model_dir / "manifest.json"
    if not path.exists():
        if require_manifest:
            raise ValueError("model manifest is required; legacy artifacts are unverified")
        return {"status": "legacy_unverified", "reason": "manifest.json is absent"}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("feature_schema") != feature_schema():
        raise ValueError("model feature contract/schema mismatch")
    if set(manifest.get("artifacts", {})) != set(MODEL_ARTIFACTS):
        raise ValueError("model manifest must describe the complete expected bundle")
    for name in MODEL_ARTIFACTS:
        spec = manifest["artifacts"][name]
        artifact = model_dir / name
        if (
            not artifact.is_file()
            or sha256_file(artifact) != spec.get("sha256")
            or artifact.stat().st_size != spec.get("bytes")
        ):
            raise ValueError(f"model artifact checksum/size mismatch: {name}")
    thresholds = json.loads((model_dir / "thresholds.json").read_text(encoding="utf-8"))
    if manifest.get("model_version") != thresholds.get("model_version"):
        raise ValueError("manifest and thresholds model_version mismatch")
    provenance_status = manifest.get("dataset_provenance", {}).get("status", "legacy_unverified")
    if require_manifest and provenance_status != "verified":
        raise ValueError("strict manifest mode requires verified dataset provenance")
    return {
        "status": "verified" if provenance_status == "verified" else "integrity_verified_dataset_unverified",
        "manifest_sha256": sha256_file(path),
        "model_version": manifest["model_version"],
        "dataset_status": provenance_status,
    }
