import csv
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voice_model import RealVoiceModel, calibrate, fpr_upper, feature_vector
from voice_pipeline import analyze_wav
from train_voice import train, validate_manifest
from test_voice_pipeline import tone, wav_bytes


def records():
    rows = []
    for i, split in enumerate(("train", "calibration", "test")):
        for k in range(4):
            rows.append(dict(split=split, label=str(k % 2), group_id=f"group-{i}-{k}",
                             call_id=f"call-{i}-{k}", ts=i * 100 + k,
                             audio_path=f"audio-{i}-{k}.wav"))
    return rows


def test_manifest_rejects_group_leakage_and_time_leakage():
    rows = records()
    validate_manifest(rows)
    rows[4]["group_id"] = rows[0]["group_id"]
    with pytest.raises(ValueError, match="leaks"):
        validate_manifest(rows)
    rows = records()
    rows[4]["ts"] = 0
    with pytest.raises(ValueError, match="chronological"):
        validate_manifest(rows)


def test_zero_errors_with_too_few_negatives_cannot_enable_blocking():
    policy = calibrate([.1] * 1000 + [.9], [0] * 1000 + [1], .0001)
    assert policy["eligible"] is False and policy["threshold"] is None
    assert policy["fpr_upper"] > .0001
    assert fpr_upper(0, 30000) < .0001


def test_calibration_selects_threshold_on_negatives_with_exact_upper_bound():
    scores = np.r_[np.linspace(.01, .2, 1000), .8, .9]
    labels = np.r_[np.zeros(1000, dtype=int), 1, 1]
    policy = calibrate(scores, labels, .005, groups=[str(i) for i in range(len(scores))])
    assert policy["eligible"] and policy["fpr_upper"] <= .005
    assert policy["false_positives"] == int(np.sum(scores[:1000] >= policy["threshold"]))


def test_real_audio_training_artifact_roundtrip_and_domain_gate(tmp_path):
    rows = records()
    for i, row in enumerate(rows):
        frequency = 160 + 180 * int(row["label"]) + i * .4
        (tmp_path / row["audio_path"]).write_bytes(wav_bytes(tone(frequency=frequency)))
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = train(manifest, tmp_path / "model")
    assert report["raw_audio_stored"] is False
    assert report["test_block"]["enabled"] is False
    path = tmp_path / "model" / "real_voice_bundle.joblib"
    model = RealVoiceModel.load(path)
    decision = model.predict(analyze_wav(wav_bytes(tone())))
    assert decision["decision"] == "OBSERVE" and decision["recommended_action"] == "NONE"
    with pytest.raises(ValueError, match="schema"):
        model.predict({"schema_version": "synthetic-audio", "source": "synthetic", "features": {}})
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="integrity"):
        RealVoiceModel.load(path)

def test_correlated_chunks_do_not_arm_block_or_inflate_independent_negatives():
    scores = [.1] * 30000 + [.9]
    labels = [0] * 30000 + [1]
    policy = calibrate(scores, labels, .0001, groups=["one-speaker"] * 30000 + ["fraud"])
    assert not policy["eligible"] and policy["negative_groups"] == 1
    assert policy["negative_records"] == 30000
    assert calibrate(scores, labels, .0001)["eligible"] is False


def test_fractional_labels_and_missing_or_nonfinite_features_are_rejected():
    with pytest.raises(ValueError, match="binary"):
        calibrate([.1, .9], [.4, .9], .005)
    observation = analyze_wav(wav_bytes(tone()))
    missing = {**observation, "features": {}}
    with pytest.raises(ValueError, match="required"):
        feature_vector(missing)
    invalid = {**observation, "features": {**observation["features"], "rms_dbfs": float("inf")}}
    with pytest.raises(ValueError, match="invalid"):
        feature_vector(invalid)
    with pytest.raises(ValueError, match="mixes labels"):
        calibrate([.1, .9], [0, 1], .005, groups=["same", "same"])


def test_speaker_and_campaign_aliases_form_one_calibration_component():
    from train_voice import independent_groups
    groups = independent_groups([
        {"group_id": "g1", "call_id": "c1", "speaker_id": "speaker"},
        {"group_id": "g2", "call_id": "c2", "speaker_id": "speaker"},
        {"group_id": "g3", "call_id": "c3", "speaker_id": "other"},
    ])
    assert groups[0] == groups[1] and groups[0] != groups[2]
