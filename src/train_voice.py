"""Train a real-audio baseline from labeled, chronologically separated recordings."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from voice_model import DOMAIN, RealVoiceModel, calibrate, feature_vector, fpr_upper
from voice_pipeline import MAX_WAV_BYTES, REAL_FEATURES, SCHEMA_VERSION, analyze_wav

SPLITS = ("train", "calibration", "test")


def validate_manifest(rows):
    if not rows:
        raise ValueError("empty manifest")
    partitions = {name: [] for name in SPLITS}
    for row in rows:
        if row.get("split") not in partitions:
            raise ValueError("split must be train, calibration or test")
        if not row.get("group_id") or not row.get("call_id"):
            raise ValueError("group_id and call_id are required (speaker/campaign isolation)")
        if str(row.get("label")) not in ("0", "1"):
            raise ValueError("label must be binary 0/1")
        if not np.isfinite(float(row["ts"])):
            raise ValueError("ts must be finite")
        partitions[row["split"]].append(row)
    for split, records in partitions.items():
        if not records or {str(r["label"]) for r in records} != {"0", "1"}:
            raise ValueError(f"{split} requires both classes")
    for field in ("group_id", "call_id", "speaker_id", "campaign_id"):
        seen = {}
        for split, records in partitions.items():
            for row in records:
                value = row.get(field)
                if value:
                    if value in seen and seen[value] != split:
                        raise ValueError(f"{field} leaks across splits")
                    seen[value] = split
    for before, after in zip(SPLITS, SPLITS[1:]):
        if max(float(r["ts"]) for r in partitions[before]) >= min(float(r["ts"]) for r in partitions[after]):
            raise ValueError("splits must be strictly chronological: train < calibration < test")
    return partitions


def independent_groups(rows):
    # Connected components prevent different group labels hiding shared calls/speakers/campaigns.
    parents = list(range(len(rows)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owners = {}
    for index, row in enumerate(rows):
        for field in ("group_id", "call_id", "speaker_id", "campaign_id"):
            value = row.get(field)
            if value:
                token = (field, value)
                if token in owners:
                    a, b = find(index), find(owners[token])
                    parents[a] = b
                else:
                    owners[token] = index
    return [str(find(index)) for index in range(len(rows))]


def train(manifest_path: Path, output_dir: Path):
    manifest_path, output_dir = Path(manifest_path), Path(output_dir)
    with manifest_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    partitions = validate_manifest(rows)
    features, labels, fingerprints = {}, {}, {}
    content_digest = hashlib.sha256(manifest_path.read_bytes())
    for split in SPLITS:
        x, y = [], []
        for row in partitions[split]:
            path = (manifest_path.parent / row["audio_path"]).resolve()
            if path.stat().st_size > MAX_WAV_BYTES:
                raise ValueError(f"WAV exceeds size limit: {path.name}")
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest in fingerprints:
                raise ValueError("duplicate recording in manifest; segments must have distinct content")
            fingerprints[digest] = split
            content_digest.update(bytes.fromhex(digest))
            channel = int(row["caller_channel"]) if row.get("caller_channel") else None
            observation = analyze_wav(payload, caller_channel=channel)
            if set(observation["quality_warnings"]) & {"near_silence", "clipping", "insufficient_speech"}:
                raise ValueError(f"training recording failed quality checks: {path.name}")
            x.append(feature_vector(observation))
            y.append(int(row["label"]))
        features[split], labels[split] = np.array(x), np.array(y)
    # Fit preprocessing and classifier on train only; calibration and test never fit the estimator.
    estimator = make_pipeline(
        SimpleImputer(strategy="median", keep_empty_features=True),
        StandardScaler(),
        LogisticRegression(class_weight="balanced", max_iter=1000, random_state=7),
    )
    estimator.fit(features["train"], labels["train"])
    calibration_scores = estimator.predict_proba(features["calibration"])[:, 1]
    groups = independent_groups(partitions["calibration"])
    policies = {
        "block": calibrate(calibration_scores, labels["calibration"], 0.0001, groups=groups),
        "flag": calibrate(calibration_scores, labels["calibration"], 0.005, groups=groups),
    }
    bundle = {
        "domain": DOMAIN,
        "schema_version": SCHEMA_VERSION,
        "features": REAL_FEATURES,
        "estimator": estimator,
        "policies": policies,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "data_sha256": content_digest.hexdigest(),
        "target": "manifest_fraud_label",
    }
    test_scores = estimator.predict_proba(features["test"])[:, 1]
    report = {
        "domain": DOMAIN,
        "schema_version": SCHEMA_VERSION,
        "splits": {k: {"events": len(labels[k]), "positives": int(labels[k].sum())} for k in SPLITS},
        "test_pr_auc": float(average_precision_score(labels["test"], test_scores)),
        "policies": policies,
        "data_sha256": bundle["data_sha256"],
        "score_semantics": "ranking_score_not_calibrated_probability",
        "raw_audio_stored": False,
        "calibration_unit": "connected call/group/speaker/campaign component; max score per component",
        "deployment_fpr_certified": False,
    }
    for name, policy in policies.items():
        threshold = policy["threshold"]
        if threshold is None or not policy["eligible"]:
            report["test_" + name] = {"enabled": False, "reason": "insufficient_calibration"}
        else:
            predictions = test_scores >= threshold
            positive, negative = labels["test"] == 1, labels["test"] == 0
            report["test_" + name] = {
                "enabled": True,
                "recall": float(predictions[positive].mean()),
                "fpr": float(predictions[negative].mean()),
                "false_positives": int(predictions[negative].sum()),
                "negative_records": int(negative.sum()),
                "fpr_upper_95_assuming_iid_records": fpr_upper(int(predictions[negative].sum()), int(negative.sum())),
                "interval_scope": "record-level binomial; campaign correlation is not certified",
            }
    artifact = output_dir / "real_voice_bundle.joblib"
    metadata = artifact.with_suffix(".json")
    report_path = output_dir / "real_voice_training_report.json"
    if any(p.exists() for p in (artifact, metadata, report_path)):
        raise FileExistsError("output artifacts already exist; use a fresh model directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, artifact)
    metadata.write_text(
        json.dumps(
            {
                "domain": DOMAIN,
                "schema_version": SCHEMA_VERSION,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    RealVoiceModel.load(artifact)  # Verify the exact artifact a serving process will load.
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(train(args.manifest, args.out_dir), indent=2))


if __name__ == "__main__":
    main()
