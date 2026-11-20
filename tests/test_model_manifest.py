"""Artifact/provenance validation uses temporary dummy files and never fits a model."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import train
from model_manifest import (
    MODEL_ARTIFACTS,
    TRAIN_FEATURES,
    validate_dataset_manifest,
    validate_model_manifest,
    write_dataset_manifest,
    write_model_manifest,
)
from statistical_reporting import threshold_validation
from train import calibration_frame, calibration_report


def bundle(path):
    for name in MODEL_ARTIFACTS:
        (path / name).write_text(json.dumps({"model_version": "test-v1"}) if name == "thresholds.json" else name, encoding="utf-8")


def test_model_checksum_mismatch_fails_before_deserialization(tmp_path):
    bundle(tmp_path)
    write_model_manifest(tmp_path, "test-v1", {"status": "verified"})
    assert validate_model_manifest(tmp_path)["status"] == "verified"
    (tmp_path / "sms_text_lr.joblib").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum/size mismatch"):
        validate_model_manifest(tmp_path)


def test_legacy_model_is_explicit_and_can_be_disallowed(tmp_path):
    assert validate_model_manifest(tmp_path)["status"] == "legacy_unverified"
    with pytest.raises(ValueError, match="required"):
        validate_model_manifest(tmp_path, require_manifest=True)


def test_future_dataset_manifest_verifies_all_train_val_cal_features(tmp_path):
    paths = []
    for name in TRAIN_FEATURES:
        p = tmp_path / name
        p.write_text(name, encoding="utf-8")
        paths.append(p)
    manifest = write_dataset_manifest(tmp_path, {"cal": (33, 24, 0, "val", 12)}, paths)
    assert validate_dataset_manifest(tmp_path)["status"] == "verified"
    assert "cal_call_features.parquet" in manifest["files"]
    manifest["feature_schema"]["contract"] = "old-minute-counter"
    (tmp_path / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="contract/schema mismatch"):
        validate_dataset_manifest(tmp_path)


def test_training_input_tampering_and_legacy_provenance_are_explicit(tmp_path):
    assert validate_dataset_manifest(tmp_path)["status"] == "legacy_unverified"
    paths = []
    for name in TRAIN_FEATURES:
        p = tmp_path / name
        p.write_bytes(b"features")
        paths.append(p)
    write_dataset_manifest(tmp_path, {}, paths)
    paths[-1].write_bytes(b"altered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_dataset_manifest(tmp_path)


def test_calibration_selects_independent_window_and_reports_legacy_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    monkeypatch.setattr(train, "load", lambda name, kind: (name, kind))
    assert calibration_frame("audio") == (("val", "audio"), "val")
    (tmp_path / "cal_audio_features.parquet").write_bytes(b"fixture")
    assert calibration_frame("audio") == (("cal", "audio"), "cal")
    scores, thresholds = [0.0] * 1000, {"block": 0.9, "flag": 0.5}
    independent = calibration_report(scores, thresholds, "cal")
    legacy = calibration_report(scores, thresholds, "val")
    assert independent["independent_of_early_stopping"]
    assert not legacy["independent_of_early_stopping"]
    assert not independent["actions"]["block"]["upper_95ci_within_target"]
    assert threshold_validation(scores, thresholds)["actions"]["flag"]["target_fpr"] == 0.005


def test_strict_mode_rejects_bundle_from_unverified_legacy_features(tmp_path):
    bundle(tmp_path)
    write_model_manifest(tmp_path, "test-v1", {"status": "legacy_unverified"})
    assert validate_model_manifest(tmp_path)["status"] == "integrity_verified_dataset_unverified"
    with pytest.raises(ValueError, match="verified dataset provenance"):
        validate_model_manifest(tmp_path, require_manifest=True)


def test_threshold_never_spends_a_fractional_false_positive_budget():
    import numpy as np
    scores = np.linspace(0.0, 1.0, 1000)
    threshold = train.threshold_for_fpr(scores, 0.0001)
    assert int((scores >= threshold).sum()) == 0
    threshold = train.threshold_for_fpr(scores, 0.01)
    assert int((scores >= threshold).sum()) == 5
    tied = np.array([0.1] * 990 + [0.9] * 10)
    assert int((tied >= train.threshold_for_fpr(tied, 0.01)).sum()) <= 5
