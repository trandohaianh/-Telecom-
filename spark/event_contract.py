"""Shared CALL validation and deterministic replay ordering."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

EVENT_FIELDS = ("event_id", "event_type", "ts", "caller", "callee", "cell_id", "imei",
                "origin_trunk", "network", "caller_age_days", "duration_s", "label", "fraud_type")


def require_checkpoint_namespace(checkpoint, namespace):
    """Reject shared Spark progress without its own durable feature identity."""
    checkpoint = Path(checkpoint)
    names = {path.name for path in checkpoint.iterdir()} if checkpoint.is_dir() else set()
    foreign = sorted(name for name in names if name != namespace and
                     name.startswith(("call_features_v", "distributed_call_features_v")))
    if foreign:
        if foreign == ["call_features_v1"] and namespace == "call_features_v2":
            raise ValueError("Legacy v1 checkpoint: replay into a fresh checkpoint; v1 buckets are not exact")
        raise ValueError("Checkpoint contains a different feature engine or schema (" +
                         ", ".join(foreign) + "); use a fresh checkpoint")
    root = checkpoint / namespace
    progress = names.intersection(("offsets", "commits", "sources", "state", "metadata"))
    if progress and not (root / "source.json").is_file():
        raise ValueError("Spark checkpoint progress has no matching feature manifest; use a fresh checkpoint")
    return root


def json_safe(row):
    result = {}
    for key, value in row.items():
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        result[key] = value
    return result


def validate_call(row):
    """Return normalized CALL, ignored non-CALL, or raise a quarantine reason."""
    original = row
    row = json_safe(row)
    if row.get("_parse_error"):
        raise ValueError(str(row["_parse_error"]))
    if row.get("event_type") and row["event_type"] != "CALL":
        return None
    for key in ("event_id", "event_type", "caller", "callee", "cell_id", "origin_trunk", "network"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"invalid required string: {key}")
    unsupported = set()
    for key in ("start_ts", "end_ts"):
        value = original.get(key)
        if hasattr(value, "item"):
            value = value.item()
        # A nullable column can be introduced by another row in a pandas frame.
        if value is not None and not (isinstance(value, float) and math.isnan(value)):
            unsupported.add(key)
        row.pop(key, None)
    # The fixed Kafka schema drops unknown fields; inspect the original envelope.
    if isinstance(row.get("_raw"), str):
        try:
            raw = json.loads(row["_raw"])
        except ValueError:
            raw = None
        if isinstance(raw, dict):
            unsupported.update({"start_ts", "end_ts"}.intersection(raw))
    if unsupported:
        raise ValueError("Spark replay CALL uses ts + duration_s; unsupported fields: " +
                         ", ".join(sorted(unsupported)))
    for key in ("ts", "caller_age_days", "duration_s"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"invalid finite number: {key}")
        try:
            number = float(value)
        except OverflowError:
            raise ValueError(f"invalid finite number: {key}") from None
        if not math.isfinite(number):
            raise ValueError(f"invalid finite number: {key}")
        row[key] = number
    if row["duration_s"] < 0:
        raise ValueError("negative duration_s")
    if not math.isfinite(row["ts"] + row["duration_s"]):
        raise ValueError("CALL completion ts + duration_s must be finite")
    if row.get("imei") is not None and not isinstance(row["imei"], str):
        raise ValueError("imei must be a string")
    row["imei"] = row.get("imei") or ""
    return row


def order_key(row):
    return (row["ts"], row["event_id"])


def payload_hash(row):
    # Transport metadata is excluded from business-event identity.
    payload = {key: row.get(key) for key in EVENT_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
