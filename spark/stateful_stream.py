"""Local driver stream with durable output/state, bounded lateness and dedup.

This is intentionally not a distributed feature service. New state uses v2;
legacy checkpoint directories are never migrated with invented historical data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from event_contract import (
    fsync_directory,
    json_safe,
    order_key,
    payload_hash,
    require_checkpoint_namespace,
    validate_call,
)

from feature_store import CALL_FEATURES, FeatureStore


class StatefulCallStream:
    VERSION = 2

    def __init__(
        self,
        checkpoint: Path,
        source: dict,
        *,
        allowed_lateness_s: float = 0,
        dedup_horizon_s: float = 86400,
        late_policy: str = "quarantine",
        retain_snapshots: int = 3,
        max_buffered_events: int = 100000,
        metadata_ttl_s: float | None = None,
    ):
        if not all(math.isfinite(value) for value in (allowed_lateness_s, dedup_horizon_s)):
            raise ValueError("lateness and dedup horizon must be finite")
        if metadata_ttl_s is not None and (not math.isfinite(metadata_ttl_s) or metadata_ttl_s < 86400):
            raise ValueError("metadata TTL must be at least 24 hours")
        if allowed_lateness_s < 0 or dedup_horizon_s < max(1, allowed_lateness_s):
            raise ValueError("dedup horizon must be >= max(1, allowed lateness)")
        if late_policy not in ("quarantine", "fail"):
            raise ValueError("invalid late policy")
        if (
            type(retain_snapshots) is not int
            or retain_snapshots < 1
            or type(max_buffered_events) is not int
            or max_buffered_events < 1
        ):
            raise ValueError("snapshot retention and buffered-event capacity must be positive integers")
        self.root = require_checkpoint_namespace(checkpoint, "call_features_v2")
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_lateness_s = float(allowed_lateness_s)
        self.dedup_horizon_s = float(dedup_horizon_s)
        self.late_policy = late_policy
        self.retain_snapshots = retain_snapshots
        self.max_buffered_events = max_buffered_events
        self.metadata_ttl_s = metadata_ttl_s
        expected = {
            "version": self.VERSION,
            "feature_contract": FeatureStore.FEATURE_CONTRACT,
            **source,
            "allowed_lateness_s": self.allowed_lateness_s,
            "dedup_horizon_s": self.dedup_horizon_s,
            "late_policy": late_policy,
            "max_buffered_events": max_buffered_events,
            "metadata_ttl_s": metadata_ttl_s,
        }
        manifest = self.root / "source.json"
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")) != expected:
                raise ValueError("Checkpoint belongs to a different Kafka source or event-time contract")
        else:
            temporary_manifest = manifest.with_suffix(".pending")
            temporary_manifest.write_text(json.dumps(expected), encoding="utf-8")
            temporary_manifest.replace(manifest)
            fsync_directory(manifest.parent)
        self.store = FeatureStore()
        self.last_batch = -1
        self.last_ts = self.max_ts = self.watermark = None
        self.last_key = None
        self.pending_events = []
        self.seen = {}
        self.last_metrics = {}
        snapshots = list(self.root.glob("batch-*/state.json")) + list(self.root.glob("flush-*/state.json"))
        if snapshots:
            latest = max(
                snapshots, key=lambda path: (int(path.parent.name.split("-")[1]), path.parent.name.startswith("flush-"))
            )
            saved = json.loads(latest.read_text(encoding="utf-8"))
            if saved["version"] != self.VERSION:
                raise ValueError("Unsupported stream checkpoint schema")
            self.store.load_state_dict(saved["store"])
            for field in ("last_batch", "last_ts", "max_ts", "watermark", "pending_events", "seen"):
                setattr(self, field, saved[field])
            self.last_key = tuple(saved["last_key"]) if saved["last_key"] else None

    @staticmethod
    def output_files(checkpoint):
        root = Path(checkpoint) / "call_features_v2"
        return sorted([*root.glob("batch-*/features.parquet"), *root.glob("flush-*/features.parquet")])

    def process(self, frame, batch_id):
        return self._process(frame, int(batch_id), flush=False)

    def flush(self):
        """Drain finite replay tail after availableNow stops; preserves Spark batch IDs."""
        return self._process(pd.DataFrame(), self.last_batch, flush=True)

    def _process(self, frame, batch_id, *, flush):
        started = time.perf_counter()
        raw_rows = [json_safe(row) for row in frame.to_dict("records")]
        fingerprint = hashlib.sha256(
            json.dumps(sorted(json.dumps(row, sort_keys=True, allow_nan=False) for row in raw_rows)).encode()
        ).hexdigest()
        target = self.root / f"{'flush' if flush else 'batch'}-{batch_id:020d}"
        if target.exists():
            receipt = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
            if receipt["input_hash"] != fingerprint:
                raise ValueError("Batch replay has different input; use a fresh checkpoint")
            self.last_metrics = receipt["metrics"]
            return pd.read_parquet(target / "features.parquet")
        if not flush and batch_id <= self.last_batch:
            raise ValueError("Batch is older than the latest committed state")
        if flush and self.last_batch < 0:
            return pd.DataFrame(columns=["event_id", "caller", "ts", *CALL_FEATURES])

        candidate = FeatureStore()
        candidate.load_state_dict(self.store.state_dict())
        pending, seen = list(self.pending_events), dict(self.seen)
        max_ts = self.max_ts
        quarantine, duplicates, ignored, accepted = [], 0, 0, 0
        # D13: same rule as the distributed engine. Conflicting copies of one event_id inside a batch
        # are all quarantined; accepting "the first" depended on arrival order.
        batch_hashes = {}
        for raw in raw_rows:
            try:
                probe = validate_call(dict(raw))
            except ValueError:
                continue
            if probe is not None:
                batch_hashes.setdefault(probe["event_id"], set()).add(payload_hash(probe))
        for raw in raw_rows:
            try:
                row = validate_call(raw)
                if row is None:
                    ignored += 1
                    continue
                event_id, digest = row["event_id"], payload_hash(row)
                if len(batch_hashes.get(event_id, ())) > 1:
                    raise ValueError("duplicate event_id with conflicting payload")
                previous = seen.get(event_id)
                if previous:
                    if previous["hash"] != digest:
                        raise ValueError("duplicate event_id with conflicting payload")
                    duplicates += 1
                    continue
                late = (self.watermark is not None and row["ts"] < self.watermark) or (
                    self.last_key is not None and order_key(row) <= self.last_key
                )
                if late:
                    if self.late_policy == "fail":
                        raise RuntimeError(f"Late CALL {event_id}: behind committed event-time order")
                    raise ValueError("late event behind committed watermark/order")
                seen[event_id] = {"ts": row["ts"], "hash": digest}
                pending.append(row)
                max_ts = row["ts"] if max_ts is None else max(max_ts, row["ts"])
                accepted += 1
            except ValueError as error:
                quarantine.append(
                    {
                        "event_id": None if raw.get("event_id") is None else str(raw["event_id"]),
                        "reason": str(error),
                        "payload": json.dumps(raw, sort_keys=True, allow_nan=False),
                    }
                )
        watermark = (max_ts if flush else max_ts - self.allowed_lateness_s) if max_ts is not None else None
        if self.watermark is not None:
            watermark = max(self.watermark, watermark) if watermark is not None else self.watermark
        emit, buffered = [], []
        for row in sorted(pending, key=order_key):
            (emit if flush or row["ts"] <= watermark else buffered).append(row)
        if len(buffered) > self.max_buffered_events:
            raise RuntimeError("Event-time buffer capacity exceeded; increase capacity or reduce lateness")
        output = []
        for row in emit:
            features = candidate.call_features(row)
            candidate.commit_cdr(row)
            output.append(
                {
                    "event_id": row["event_id"],
                    "caller": row["caller"],
                    "ts": row["ts"],
                    **{key: float(features[key]) for key in CALL_FEATURES},
                }
            )
        last_key = order_key(emit[-1]) if emit else self.last_key
        if watermark is not None and hasattr(candidate, "prune_state"):
            # Only prune to the last emitted event time, never ahead of buffered events.
            candidate.prune_state(last_key[0] if last_key else watermark, metadata_ttl_s=self.metadata_ttl_s)
        if max_ts is not None:
            protected = {row["event_id"] for row in buffered}
            seen = {
                key: value
                for key, value in seen.items()
                if value["ts"] >= max_ts - self.dedup_horizon_s or key in protected
            }
        metrics = {
            "batch_id": batch_id,
            "flush": flush,
            "input_rows": len(raw_rows),
            "accepted_rows": accepted,
            "emitted_rows": len(output),
            "ignored_rows": ignored,
            "duplicates": duplicates,
            "quarantined": len(quarantine),
            "buffered_rows": len(buffered),
            "dedup_keys": len(seen),
            "watermark": watermark,
            "processing_ms": round((time.perf_counter() - started) * 1000, 3),
            "state": candidate.state_stats() if hasattr(candidate, "state_stats") else {},
        }
        saved = {
            "version": self.VERSION,
            "last_batch": self.last_batch if flush else batch_id,
            "last_ts": last_key[0] if last_key else self.last_ts,
            "last_key": last_key,
            "max_ts": max_ts,
            "watermark": watermark,
            "pending_events": buffered,
            "seen": seen,
            "store": candidate.state_dict(),
        }
        result = pd.DataFrame(output, columns=["event_id", "caller", "ts", *CALL_FEATURES])
        result = result.astype({key: "float64" for key in ("ts", *CALL_FEATURES)})
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.root))
        try:
            schema = pa.schema(
                [
                    ("event_id", pa.string()),
                    ("caller", pa.string()),
                    *[(key, pa.float64()) for key in ("ts", *CALL_FEATURES)],
                ]
            )
            pq.write_table(
                pa.Table.from_pandas(result, schema=schema, preserve_index=False), temporary / "features.parquet"
            )
            quarantine_schema = pa.schema([(key, pa.string()) for key in ("event_id", "reason", "payload")])
            rejected = pd.DataFrame(quarantine, columns=["event_id", "reason", "payload"])
            pq.write_table(
                pa.Table.from_pandas(rejected, schema=quarantine_schema, preserve_index=False),
                temporary / "quarantine.parquet",
            )
            state_path = temporary / "state.json"
            state_path.write_text(json.dumps(saved, allow_nan=False), encoding="utf-8")
            metrics["snapshot_bytes"] = state_path.stat().st_size
            (temporary / "receipt.json").write_text(
                json.dumps({"input_hash": fingerprint, "metrics": metrics}, allow_nan=False), encoding="utf-8"
            )
            for path in temporary.iterdir():
                with path.open("r+b") as handle:
                    os.fsync(handle.fileno())
            temporary.rename(target)
            fsync_directory(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self.store = candidate
        for field in ("last_batch", "last_ts", "max_ts", "watermark", "pending_events", "seen"):
            setattr(self, field, saved[field])
        self.last_key = tuple(last_key) if last_key else None
        self.last_metrics = metrics
        self._retain_snapshots()
        return result

    def _retain_snapshots(self):
        # Keep all outputs/quarantine/replay receipts. Only old full state is expired.
        snapshots = list(self.root.glob("batch-*/state.json")) + list(self.root.glob("flush-*/state.json"))
        snapshots.sort(key=lambda path: (int(path.parent.name.split("-")[1]), path.parent.name.startswith("flush-")))
        for path in snapshots[: -self.retain_snapshots]:
            if path.resolve().parent.parent == self.root.resolve():
                path.unlink()
