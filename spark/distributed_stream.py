"""Persistent distributed CALL micro-batches on versioned per-entity snapshots.

Workers own subscriber/IMEI/cell state. The driver handles scalar watermarks,
metrics and an atomic directory commit only. Snapshots use a shared local/POSIX
filesystem; this is distributed full-snapshot state, not an incremental state API.
"""
from __future__ import annotations

import json
import math
import shutil
import tempfile
import time
from collections import deque
from functools import partial
from itertools import groupby
from pathlib import Path

from event_contract import (
    EVENT_FIELDS,
    fsync_directory,
    json_safe,
    order_key,
    payload_hash,
    require_checkpoint_namespace,
    validate_call,
)


def classify_record(record):
    raw = json_safe(record.asDict(recursive=True))
    payload = json.dumps(raw, sort_keys=True, allow_nan=False)
    try:
        row = validate_call(raw)
        if row is None:
            return (None,) * len(EVENT_FIELDS) + (None, None, True, payload)
        values = [row.get(key) for key in EVENT_FIELDS]
        label = row.get("label")
        values[EVENT_FIELDS.index("label")] = float(label) if isinstance(label, (int, float)) else None
        return tuple(values) + (payload_hash(row), None, False, payload)
    except ValueError as error:
        values = [None] * len(EVENT_FIELDS)
        values[0] = str(raw["event_id"]) if raw.get("event_id") is not None else None
        return tuple(values) + (None, str(error), False, payload)


def state_records(record):
    return ((record["key"], 0, 0., "", 0), ("state", record["payload"]))


def event_records(record, entity):
    row = record.asDict(recursive=True)
    ts, event_id = order_key(row)
    if entity == "subscriber":
        yield ((row["caller"], 1, ts, event_id, 1), ("event", ("out", row)))
        if row["callee"] != row["caller"]:
            yield ((row["callee"], 1, ts, event_id, 0), ("event", ("in", row)))
    else:
        key = (row.get("imei") or "") if entity == "imei" else row["cell_id"]
        yield ((key, 1, ts, event_id, 0), ("event", ("out", row)))


def entity_partition(records, *, entity, watermark, metadata_ttl_s):
    from data_generator import REGISTERED_CELLS
    from feature_store import FarmCellWindow, FeatureStore
    for key, group in groupby(records, key=lambda record: record[0][0]):
        store = FeatureStore() if entity == "subscriber" else None
        links = {}
        farm = deque()      # cell: (ts, caller, callee) of fresh SIMs in the last hour (farm_cell_sims_1h)
        window = None
        for _, (kind, payload) in group:
            if kind == "state":
                restored = json.loads(payload)
                if store is not None:
                    store.load_state_dict(restored)
                elif entity == "cell" and set(restored) == {"links", "farm"}:
                    links = restored["links"]
                    farm = deque(tuple(x) for x in restored["farm"])
                else:
                    links = restored
                continue
            role, row = payload
            ts = row["ts"]
            if entity == "subscriber":
                if role == "in":
                    store.first_seen.setdefault(key, ts)
                    store.last_seen[key] = ts
                    incoming = store.in_calls[key]
                    while incoming and incoming[0] < ts - 86400.:
                        incoming.popleft()
                    incoming.append(ts)
                    continue
                features = store.call_features(row)
                store.commit_cdr(row)
                yield (key, "feature", row["event_id"], json.dumps(features, allow_nan=False))
            elif entity == "imei":
                links = {party: seen for party, seen in links.items() if seen >= ts - 86400.}
                if key:
                    links[row["caller"]] = ts
                yield (key, "feature", row["event_id"], json.dumps(
                    {"msisdn_per_imei_24h": float(len(links)) if key else 0.}))
            else:
                enabled = key in REGISTERED_CELLS or key.startswith("X-")
                links = {party: seen for party, seen in links.items() if seen >= ts - 3600.}
                age = row["caller_age_days"]
                fresh = enabled and (0 <= age < 60 or (age >= 0 and row["active_hours_on_network"] < 6.))
                if window is None or window.q is not farm:
                    window = FarmCellWindow(farm)
                if fresh:
                    links[row["caller"]] = ts
                    window.add((ts, row["caller"], row["callee"]))
                window.expire(ts - 3600.)
                yield (key, "feature", row["event_id"], json.dumps(
                    {"new_sims_on_cell_1h": float(len(links)) if enabled else 0.,
                     # the caller's own profile (no incoming, all-new callees) is applied at the join
                     "farm_cell_candidates": float(window.count_for(row["caller"], ts)) if enabled else 0.}))
        if store is not None:
            # Foreign-recipient state belongs to its own subscriber partition.
            for name in ("first_seen", "last_seen", "in_calls"):
                mapping = getattr(store, name)
                for other in list(mapping):
                    if other != key:
                        del mapping[other]
            store.msisdn_by_imei.clear()
            store.new_sims_cell.clear()
            if watermark is not None:
                store.prune_state(watermark, metadata_ttl_s=metadata_ttl_s)
            if store.first_seen or store.pending_cdr:
                yield (key, "state", None, json.dumps(store.state_dict(), allow_nan=False))
        else:
            horizon = 86400. if entity == "imei" else 3600.
            if watermark is not None:
                links = {party: seen for party, seen in links.items() if seen >= watermark - horizon}
                while farm and farm[0][0] < watermark - 3600.:
                    farm.popleft()
            if entity == "cell" and (links or farm):
                yield (key, "state", None, json.dumps({"links": links, "farm": list(farm)}, allow_nan=False))
            elif links:
                yield (key, "state", None, json.dumps(links, allow_nan=False))


class DistributedCallStream:
    VERSION = 1
    NAMESPACE = "distributed_call_features_v1"

    def __init__(self, spark, checkpoint, source, *, allowed_lateness_s=0.,
                 dedup_horizon_s=86400., partitions=8, max_buffered_events=100000,
                 metadata_ttl_s=None, retain_snapshots=3, late_policy="quarantine"):
        from feature_store import FeatureStore
        if not all(math.isfinite(value) for value in (allowed_lateness_s, dedup_horizon_s)):
            raise ValueError("event-time configuration must be finite")
        if allowed_lateness_s < 0 or dedup_horizon_s < max(1., allowed_lateness_s):
            raise ValueError("dedup horizon must be >= max(1, allowed lateness)")
        if metadata_ttl_s is not None and (not math.isfinite(metadata_ttl_s) or metadata_ttl_s < 86400.):
            raise ValueError("metadata TTL must be >=24h")
        if late_policy not in ("quarantine", "fail"):
            raise ValueError("invalid late policy")
        if any(type(value) is not int or value < 1
               for value in (partitions, max_buffered_events, retain_snapshots)):
            raise ValueError("partitions, buffer capacity, and snapshot retention must be positive integers")
        self.spark, self.partitions = spark, partitions
        self.allowed_lateness_s, self.dedup_horizon_s = float(allowed_lateness_s), float(dedup_horizon_s)
        self.metadata_ttl_s, self.max_buffered_events = metadata_ttl_s, max_buffered_events
        self.retain_snapshots, self.late_policy = retain_snapshots, late_policy
        self.root = require_checkpoint_namespace(checkpoint, self.NAMESPACE)
        self.root.mkdir(parents=True, exist_ok=True)
        expected = {"version": self.VERSION, "feature_contract": FeatureStore.FEATURE_CONTRACT,
                    "source": source, "allowed_lateness_s": self.allowed_lateness_s,
                    "dedup_horizon_s": self.dedup_horizon_s, "partitions": partitions,
                    "max_buffered_events": max_buffered_events, "metadata_ttl_s": metadata_ttl_s,
                    "late_policy": late_policy}
        manifest = self.root / "source.json"
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")) != expected:
                raise ValueError("Distributed checkpoint source or semantic configuration changed")
        else:
            pending = manifest.with_suffix(".pending")   # atomic: a torn source.json would brick the checkpoint
            pending.write_text(json.dumps(expected, allow_nan=False), encoding="utf-8")
            pending.replace(manifest)
            fsync_directory(manifest.parent)
        self.latest, self.last_batch, self.max_ts, self.watermark, self.last_key = None, -1, None, None, None
        self.last_metrics = {}
        self.buffered_rows = 0
        commits = [path for path in self.root.glob("*-*/receipt.json") if
                   path.parent.name.startswith(("batch-", "flush-"))]
        if commits:
            latest = max(commits, key=lambda path: self._commit_order(path.parent))
            self._adopt(latest.parent, json.loads(latest.read_text(encoding="utf-8")))

    @staticmethod
    def _commit_order(path):
        return (int(path.name.split("-")[1]), path.name.startswith("flush-"))

    @staticmethod
    def output_files(checkpoint):
        root = Path(checkpoint) / DistributedCallStream.NAMESPACE
        return sorted([*root.glob("batch-*/features"), *root.glob("flush-*/features")])

    @staticmethod
    def _event_schema():
        from pyspark.sql import types as T
        return T.StructType([T.StructField(key, T.DoubleType() if key in
            ("ts", "caller_age_days", "duration_s", "label") else T.StringType()) for key in EVENT_FIELDS])

    @staticmethod
    def _feature_schema():
        from pyspark.sql import types as T

        from feature_store import CALL_FEATURES
        return T.StructType([T.StructField(key, T.DoubleType()) for key in CALL_FEATURES])

    def _empty(self, schema):
        return self.spark.createDataFrame([], schema)

    def _prior(self, relative, schema):
        return self.spark.read.parquet(str(self.latest / relative)) if self.latest else self._empty(schema)

    def _entity(self, old_state, events, entity, watermark):
        from pyspark.rdd import portable_hash
        records = old_state.rdd.map(state_records).union(
            events.rdd.flatMap(partial(event_records, entity=entity)))
        records = records.repartitionAndSortWithinPartitions(
            numPartitions=self.partitions, partitionFunc=lambda key: portable_hash(key[0]))
        return self.spark.createDataFrame(records.mapPartitions(partial(
            entity_partition, entity=entity, watermark=watermark, metadata_ttl_s=self.metadata_ttl_s)),
            "key string, kind string, event_id string, payload string")

    def _adopt(self, target, receipt):
        self.latest = target
        state = receipt["control"]
        self.last_batch, self.max_ts, self.watermark = state["last_batch"], state["max_ts"], state["watermark"]
        self.last_key = tuple(state["last_key"]) if state["last_key"] else None
        self.last_metrics = receipt["metrics"]
        self.buffered_rows = receipt["metrics"]["buffered_rows"]

    def process(self, frame, batch_id):
        return self._process(frame, int(batch_id), flush=False)

    def flush(self):
        if self.latest is None or not self.buffered_rows:
            schema = "event_id string, caller string, ts double, " + ", ".join(
                f"{field.name} double" for field in self._feature_schema())
            return self._empty(schema)
        frame = self.spark.read.parquet(str(self.latest / "input")).limit(0)
        return self._process(frame, self.last_batch, flush=True)

    def _process(self, frame, batch_id, *, flush):
        from pyspark import StorageLevel
        from pyspark.sql import functions as F
        from pyspark.sql import types as T

        from feature_store import CALL_FEATURES
        started = time.perf_counter()
        target = self.root / f"{'flush' if flush else 'batch'}-{batch_id:020d}"
        if target.exists():
            old_input = self.spark.read.parquet(str(target / "input"))
            if set(old_input.columns) != set(frame.columns):
                raise ValueError("Batch replay schema changed")
            ordered = frame.select(*old_input.columns)
            if old_input.exceptAll(ordered).limit(1).count() or ordered.exceptAll(old_input).limit(1).count():
                raise ValueError("Batch replay has different input")
            self.last_metrics = json.loads((target / "receipt.json").read_text(encoding="utf-8"))["metrics"]
            return self.spark.read.parquet(str(target / "features"))
        if not flush and batch_id <= self.last_batch:
            raise ValueError("Batch is older than committed distributed state")
        temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.root))
        cached = []
        def cache(data):
            cached.append(data.persist(StorageLevel.MEMORY_AND_DISK))
            return cached[-1]
        try:
            frame = cache(frame)
            input_count = frame.count()
            # Retained input is also the exact, row-order-independent retry receipt.
            frame.write.parquet(str(temporary / "input"))
            classification_schema = T.StructType([*self._event_schema().fields,
                T.StructField("_payload_hash", T.StringType()), T.StructField("_reason", T.StringType()),
                T.StructField("_ignored", T.BooleanType()), T.StructField("_payload", T.StringType())])
            classified = cache(self.spark.createDataFrame(frame.rdd.map(classify_record), classification_schema))
            ignored = classified.filter("_ignored").count()
            quarantine = classified.filter("_reason is not null").select(
                "event_id", F.col("_reason").alias("reason"), F.col("_payload").alias("payload"))
            valid = classified.filter("_reason is null and not _ignored")
            variants = valid.groupBy("event_id").agg(F.countDistinct("_payload_hash").alias("_variants"),
                F.count("*").alias("_copies"))
            duplicate_within = variants.filter("_variants = 1").agg(
                F.sum(F.col("_copies") - 1).alias("n")).first()["n"] or 0
            conflicts = valid.join(variants.filter("_variants > 1").select("event_id"), "event_id")
            quarantine = quarantine.unionByName(conflicts.select("event_id",
                F.lit("duplicate event_id with conflicting payload").alias("reason"),
                F.col("_payload").alias("payload")))
            unique = valid.join(variants.filter("_variants = 1").select("event_id"), "event_id").dropDuplicates(["event_id"])
            previous_dedup = self._prior("dedup", "event_id string, ts double, _payload_hash string")
            matched = cache(unique.join(previous_dedup.select("event_id",
                F.col("_payload_hash").alias("_old_hash")), "event_id", "left"))
            duplicate_old = matched.filter(F.col("_old_hash") == F.col("_payload_hash")).count()
            conflict_old = matched.filter(F.col("_old_hash").isNotNull() &
                                          (F.col("_old_hash") != F.col("_payload_hash")))
            quarantine = quarantine.unionByName(conflict_old.select("event_id",
                F.lit("duplicate event_id with conflicting payload").alias("reason"),
                F.col("_payload").alias("payload")))
            unseen = matched.filter("_old_hash is null").drop("_old_hash")
            late_condition = F.lit(False)
            if self.watermark is not None:
                late_condition = late_condition | (F.col("ts") < self.watermark)
            if self.last_key:
                late_condition = late_condition | (F.col("ts") < self.last_key[0]) | (
                    (F.col("ts") == self.last_key[0]) & (F.col("event_id") <= self.last_key[1]))
            late = unseen.filter(late_condition)
            if self.late_policy == "fail" and late.limit(1).count():
                raise ValueError("Late CALL behind distributed watermark/order")
            quarantine = quarantine.unionByName(late.select("event_id",
                F.lit("late event behind committed watermark/order").alias("reason"),
                F.col("_payload").alias("payload")))
            accepted = cache(unseen.filter(~late_condition))
            summary = accepted.agg(F.count("*").alias("n"), F.max("ts").alias("max_ts")).first()
            max_ts = self.max_ts
            if summary["max_ts"] is not None:
                max_ts = summary["max_ts"] if max_ts is None else max(max_ts, summary["max_ts"])
            watermark = (max_ts if flush else max_ts - self.allowed_lateness_s) if max_ts is not None else None
            if self.watermark is not None:
                watermark = max(self.watermark, watermark) if watermark is not None else self.watermark
            pending = self._prior("buffer", self._event_schema()).unionByName(accepted.select(*EVENT_FIELDS))
            emit = cache(pending.filter(F.col("ts") <= watermark) if watermark is not None else pending.limit(0))
            buffered = cache(pending.filter(F.col("ts") > watermark) if watermark is not None else pending)
            buffered_count = buffered.count()
            if buffered_count > self.max_buffered_events:
                raise ValueError("Distributed event-time buffer capacity exceeded")
            emitted_count = emit.count()
            last = emit.agg(F.max(F.struct("ts", "event_id")).alias("key")).first()["key"]
            last_key = (float(last["ts"]), last["event_id"]) if last else self.last_key
            state_schema = "key string, payload string"
            if emitted_count or (self.latest is not None and watermark is not None
                                 and watermark != self.watermark):
                core_rows = cache(self._entity(self._prior("state/subscriber", state_schema), emit,
                                                "subscriber", watermark))
                core = core_rows.filter("kind = 'feature'").select("event_id",
                    F.from_json("payload", self._feature_schema()).alias("features")).select("event_id", "features.*")
                imei_rows = cache(self._entity(self._prior("state/imei", state_schema), emit, "imei", watermark))
                imei = imei_rows.filter("kind = 'feature'").select("event_id",
                    F.get_json_object("payload", "$.msisdn_per_imei_24h").cast("double").alias("shared_imei"))
                enriched = emit.join(core.select("event_id", "active_hours_on_network"), "event_id")
                cell_rows = cache(self._entity(self._prior("state/cell", state_schema), enriched, "cell", watermark))
                cell = cell_rows.filter("kind = 'feature'").select("event_id",
                    F.get_json_object("payload", "$.new_sims_on_cell_1h").cast("double").alias("shared_cell"),
                    F.get_json_object("payload", "$.farm_cell_candidates").cast("double").alias("shared_farm"))
                for entity, rows in (("subscriber", core_rows), ("imei", imei_rows), ("cell", cell_rows)):
                    rows.filter("kind = 'state'").select("key", "payload").write.parquet(
                        str(temporary / "state" / entity))
                features = (core.withColumn("farm_cell_sims_1h_self", F.col("farm_cell_sims_1h"))
                    .drop("msisdn_per_imei_24h", "new_sims_on_cell_1h", "farm_cell_sims_1h")
                    .join(imei, "event_id").join(cell, "event_id")
                    .withColumnRenamed("shared_imei", "msisdn_per_imei_24h")
                    .withColumnRenamed("shared_cell", "new_sims_on_cell_1h")
                    # subscriber partition sees only its own calls on the cell: > 0 means "profile matches"
                    .withColumn("farm_cell_sims_1h", F.when(F.col("farm_cell_sims_1h_self") > 0,
                                                            F.col("shared_farm")).otherwise(F.lit(0.)))
                    .join(emit.select("event_id", "caller", "ts"), "event_id")
                    .select("event_id", "caller", "ts", *CALL_FEATURES))
            else:
                for entity in ("subscriber", "imei", "cell"):
                    self._prior(f"state/{entity}", state_schema).write.parquet(str(temporary / "state" / entity))
                features = self._empty("event_id string, caller string, ts double, " + ", ".join(
                    f"{name} double" for name in CALL_FEATURES))
            features.write.parquet(str(temporary / "features"))
            actual_count = self.spark.read.parquet(str(temporary / "features")).count()
            if actual_count != emitted_count:
                raise ValueError("Distributed feature join lost or multiplied events")
            buffered.write.parquet(str(temporary / "buffer"))
            dedup = previous_dedup.unionByName(accepted.select("event_id", "ts", "_payload_hash"))
            if max_ts is not None:
                dedup = dedup.filter(F.col("ts") >= max_ts - self.dedup_horizon_s)
            dedup.write.parquet(str(temporary / "dedup"))
            quarantine = cache(quarantine)
            quarantined_count = quarantine.count()
            quarantine.write.parquet(str(temporary / "quarantine"))
            state_counts = {entity: self.spark.read.parquet(str(temporary / "state" / entity)).count()
                            for entity in ("subscriber", "imei", "cell")}
            metrics = {"engine": "distributed-stream", "batch_id": batch_id, "flush": flush,
                "input_rows": input_count, "accepted_rows": summary["n"], "ignored_rows": ignored,
                "duplicates": duplicate_within + duplicate_old, "quarantined": quarantined_count,
                "emitted_rows": emitted_count, "buffered_rows": buffered_count, "watermark": watermark,
                "state_entities": state_counts, "dedup_keys": self.spark.read.parquet(str(temporary / "dedup")).count(),
                "processing_ms": round((time.perf_counter() - started) * 1000, 3)}
            receipt = {"version": self.VERSION, "control": {
                "last_batch": self.last_batch if flush else batch_id, "max_ts": max_ts,
                "watermark": watermark, "last_key": last_key}, "metrics": metrics}
            with (temporary / "receipt.json").open("w", encoding="utf-8") as handle:
                json.dump(receipt, handle, allow_nan=False)
                handle.flush()
                import os
                os.fsync(handle.fileno())
            temporary.rename(target)
            fsync_directory(target.parent)
            self._adopt(target, receipt)
            self._retain()
            return self.spark.read.parquet(str(target / "features"))
        finally:
            for data in reversed(cached):
                data.unpersist()
            if temporary.exists():
                shutil.rmtree(temporary)

    def _retain(self):
        commits = [path.parent for path in self.root.glob("*-*/receipt.json")
                   if path.parent.name.startswith(("batch-", "flush-"))]
        commits.sort(key=self._commit_order)
        snapshots = [path for path in commits if (path / "state").exists()]
        for old in snapshots[:-self.retain_snapshots]:
            for name in ("state", "buffer", "dedup"):
                target = old / name
                if target.exists() and target.resolve().parent.parent == self.root.resolve():
                    shutil.rmtree(target)
