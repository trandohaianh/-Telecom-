"""Join human reviews to the durable shadow journal without exporting subscriber identities."""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import math
from pathlib import Path
import sqlite3

from feature_store import CALL_FEATURES, FeatureStore


def journal(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        for payload, result in connection.execute(
                "SELECT payload,result FROM events WHERE kind='VERDICT' AND result IS NOT NULL ORDER BY seq"):
            yield json.loads(payload), json.loads(result)
    finally:
        connection.close()


def export_review(database, destination):
    fields = ["event_id", "call_id", "ts", "decision", "ingest", "group_id", "label", "reviewer"]
    with Path(destination).open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        count = 0
        for event, result in journal(database):
            if event["event_type"] != "CALL":
                continue
            writer.writerow({"event_id": event["event_id"], "call_id": event.get("call_id", ""),
                             "ts": event["ts"], "decision": result["decision"], "ingest": result.get("ingest", "")})
            count += 1
    return {"review_rows": count, "identities_exported": False, "raw_content_exported": False}


def build_feedback(database, labels, output_dir, *, key, train_before, calibration_before):
    if len(key) < 32 or not math.isfinite(train_before) or not math.isfinite(calibration_before):
        raise ValueError("32-byte pseudonym key and finite temporal cutoffs are required")
    if train_before >= calibration_before:
        raise ValueError("train cutoff must precede calibration cutoff")
    with Path(labels).open(encoding="utf-8", newline="") as stream:
        reviews = {}
        for row in csv.DictReader(stream):
            if row.get("label", "") == "":
                continue
            if row.get("label") not in ("0", "1") or not row.get("reviewer") or not row.get("group_id"):
                raise ValueError("every completed review requires binary label, reviewer and group_id")
            if row["event_id"] in reviews:
                raise ValueError("duplicate review identity")
            reviews[row["event_id"]] = row
    observations = {}
    total = 0
    for event, result in journal(database):
        total += 1
        identity = event["event_id"]
        if identity in reviews:
            if identity in observations:
                raise ValueError("duplicate observation identity")
            observations[identity] = event, result
    if set(reviews) - set(observations):
        raise ValueError("review references an event absent from the shadow journal")
    def partition(ts):
        return "train" if ts < train_before else ("calibration" if ts < calibration_before else "test")
    groups = {}
    for identity, review in reviews.items():
        event, _ = observations[identity]
        ts = float(event["ts"])
        if not math.isfinite(ts):
            raise ValueError("nonfinite journal timestamp")
        groups.setdefault(review["group_id"], set()).add(partition(ts))
    crossing = {name for name, splits in groups.items() if len(splits) > 1}
    output = {name: [] for name in ("train", "calibration", "test")}
    exclusions = {"group_crosses_temporal_boundary": 0, "not_scored_or_missing_features": 0}
    def pseudonym(value):
        return hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()
    for identity, review in reviews.items():
        event, result = observations[identity]
        if review["group_id"] in crossing:
            exclusions["group_crosses_temporal_boundary"] += 1
            continue
        values = result.get("evidence_features", {})
        if (event["event_type"] != "CALL" or result.get("ingest") != "ADMITTED" or not result.get("scored", True)
                or any(name not in values or not isinstance(values[name], (int, float))
                       or not math.isfinite(values[name]) for name in CALL_FEATURES)):
            exclusions["not_scored_or_missing_features"] += 1
            continue
        row = {name: float(values[name]) for name in CALL_FEATURES}
        row.update(event_id=pseudonym(identity), call_id=pseudonym(event.get("call_id") or identity),
                   group_id=pseudonym(review["group_id"]), reviewer=pseudonym(review["reviewer"]),
                   ts=event["ts"], label=int(review["label"]), decision=result["decision"],
                   model_version=result.get("model_version", "unreported"))
        output[partition(event["ts"])].append(row)
    destination = Path(output_dir)
    if destination.exists():
        raise FileExistsError("use a new output directory to preserve prior feedback datasets")
    destination.mkdir(parents=True)
    files = {}
    for name, rows in output.items():
        path = destination / (name + "_reviewed_calls.jsonl")
        content = "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows)
        path.write_text(content, encoding="utf-8")
        files[path.name] = hashlib.sha256(content.encode()).hexdigest()
    report = {
        "feature_contract": FeatureStore.FEATURE_CONTRACT,
        "provenance": "operator-reviewed shadow API journal; real-world provenance must be established by operator",
        "source_events": total, "reviewed_events": len(reviews), "exclusions": exclusions,
        "splits": {name: {"events": len(rows), "positives": sum(row["label"] for row in rows)}
                   for name, rows in output.items()},
        "train_before": train_before, "calibration_before": calibration_before,
        "files_sha256": files, "subscriber_identities_exported": False,
        "raw_content_exported": False,
        "sampling_note": "label selection can bias metrics; review representative ALLOW controls as well as alerts",
    }
    (destination / "feedback_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-review")
    export.add_argument("--state-db", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    build = sub.add_parser("build")
    build.add_argument("--state-db", type=Path, required=True)
    build.add_argument("--labels", type=Path, required=True)
    build.add_argument("--out-dir", type=Path, required=True)
    build.add_argument("--key-file", type=Path, required=True, help="separate pseudonymization key; do not reuse network HMAC key")
    build.add_argument("--train-before", type=float, required=True)
    build.add_argument("--calibration-before", type=float, required=True)
    args = parser.parse_args()
    if args.command == "export-review":
        report = export_review(args.state_db, args.out)
    else:
        key = bytes.fromhex(args.key_file.read_text(encoding="utf-8").strip())
        report = build_feedback(args.state_db, args.labels, args.out_dir, key=key,
                                train_before=args.train_before, calibration_before=args.calibration_before)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
