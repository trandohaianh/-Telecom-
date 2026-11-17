"""Causal CALL features on Spark workers, with state partitioned by entity.

Three sorted shuffles compute subscriber, shared IMEI and shared cell features.
This is distributed bounded batch replay. It does not provide persistent
distributed streaming state or an online low-latency feature service.
"""
from __future__ import annotations

from itertools import groupby


def event_order(row):
    return (float(row["ts"]), row["event_id"])


def subscriber_records(row):
    order = event_order(row)
    yield ((row["caller"], *order, 1), ("out", row))
    if row["callee"] != row["caller"]:
        yield ((row["callee"], *order, 0), ("in", row))


def subscriber_partition(records):
    # Imports execute on workers; spark_features registers src as pyFiles.
    from feature_store import CALL_FEATURES, FeatureStore
    for subscriber, group in groupby(records, key=lambda item: item[0][0]):
        store = FeatureStore()
        for _, (role, row) in group:
            if role == "in":
                store.first_seen.setdefault(subscriber, row["ts"])
                incoming = store.in_calls[subscriber]
                while incoming and incoming[0] < row["ts"] - 86400.0:
                    incoming.popleft()
                incoming.append(row["ts"])
                continue
            features = store.call_features(row)
            store.commit_cdr(row)
            yield (row["event_id"], *[float(features[name]) for name in CALL_FEATURES])


def imei_entity(row):
    """Partition key for the shared-IMEI feature; rows without IMEI each get their own key."""
    return row.get("imei") or "~no-imei~" + str(row["event_id"])


def imei_partition(records):
    for _, group in groupby(records, key=lambda item: item[0][0]):
        last_seen = {}
        for _, row in group:
            cutoff = row["ts"] - 86400.0
            # Bound associations by the actual 24h feature horizon.
            last_seen = {key: ts for key, ts in last_seen.items() if ts >= cutoff}
            if row.get("imei"):
                last_seen[row["caller"]] = row["ts"]
            yield (row["event_id"], float(len(last_seen)) if row.get("imei") else 0.0)


def cell_partition(records):
    for _, group in groupby(records, key=lambda item: item[0][0]):
        from data_generator import REGISTERED_CELLS
        last_seen = {}
        for _, row in group:
            cell, ts, age = row["cell_id"], row["ts"], row["caller_age_days"]
            enabled = cell in REGISTERED_CELLS or cell.startswith("X-")
            if not enabled:
                yield (row["event_id"], 0.0)
                continue
            last_seen = {key: seen for key, seen in last_seen.items() if seen >= ts - 3600.0}
            if 0 <= age < 60 or (age >= 0 and row["active_hours_on_network"] < 6.0):
                last_seen[row["caller"]] = ts
            yield (row["event_id"], float(len(last_seen)))


def add_distributed_features(frame, partitions: int = 8):
    """Return event_id + all 27 features, preserving causal order (ts,event_id).

    No collect/toPandas/groupByKey: each worker streams one sorted entity at a time.
    The subscriber stage fans incoming calls out to their recipient, so
    incoming_24h and first_seen include calls made by other subscribers.
    """
    from pyspark.rdd import portable_hash
    from pyspark.sql import types as T
    from feature_store import CALL_FEATURES

    def sorted_by_entity(rdd):
        return rdd.repartitionAndSortWithinPartitions(
            numPartitions=partitions, partitionFunc=lambda key: portable_hash(key[0]))

    rows = frame.rdd.map(lambda row: row.asDict(recursive=True))
    schema = T.StructType([T.StructField("event_id", T.StringType(), False)] + [
        T.StructField(name, T.DoubleType(), False) for name in CALL_FEATURES])
    core = frame.sparkSession.createDataFrame(
        sorted_by_entity(rows.flatMap(subscriber_records)).mapPartitions(subscriber_partition), schema)
    imei_schema = "event_id string, shared_imei_count double"
    imei = frame.sparkSession.createDataFrame(
        # Rows without IMEI never share a device (their feature is 0), so key each one by its own
        # event_id: one "" key used to send every such row to a single hot partition.
        sorted_by_entity(rows.map(lambda row: (
            (imei_entity(row), *event_order(row)), row))).mapPartitions(imei_partition),
        imei_schema)
    enriched = frame.join(core.select("event_id", "active_hours_on_network"), "event_id")
    cell_rows = enriched.rdd.map(lambda row: row.asDict(recursive=True))
    cells = frame.sparkSession.createDataFrame(
        sorted_by_entity(cell_rows.map(lambda row: (
            (row["cell_id"], *event_order(row)), row))).mapPartitions(cell_partition),
        "event_id string, shared_cell_count double")
    return (core.join(imei, "event_id").join(cells, "event_id")
            .drop("msisdn_per_imei_24h", "new_sims_on_cell_1h")
            .withColumnRenamed("shared_imei_count", "msisdn_per_imei_24h")
            .withColumnRenamed("shared_cell_count", "new_sims_on_cell_1h")
            .select("event_id", *CALL_FEATURES))
