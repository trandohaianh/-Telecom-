> **Trạng thái: thử nghiệm, không triển khai** — quyết định 17/11/2026: API chính là nguồn quyết định duy nhất; dùng `src/carrier_bridge.py` để đưa dữ liệu nhà mạng vào.
> Lý do: Spark chỉ phát lại đặc trưng theo lô/luồng để đối chiếu và huấn luyện; nó không được đưa ra quyết định chặn trực tiếp.

# CALL feature replay: causal contract v2

The default batch engine computes all 27 CALL features on Spark workers.
Kafka streaming has two explicit engines: `local-stream` retains the validated
driver FeatureStore; `distributed-stream` keeps durable per-key subscriber,
IMEI and cell state on Spark workers. Each engine uses a separate checkpoint.

## Verify the engines

Run inside the Spark container, or an environment with its pinned dependencies:

```bash
python make_sample.py --out-dir /tmp/telecom-sample-v2
python spark_features.py --mode batch --engine distributed --events /tmp/telecom-sample-v2/sample_events.csv --out /tmp/telecom-distributed-v2.parquet
python compare_features.py --ref /tmp/telecom-sample-v2/reference_call_features.parquet --got /tmp/telecom-distributed-v2.parquet
python verify_distributed_batch.py --out-dir /tmp/telecom-shuffle-check
python -m unittest test_stateful_stream test_distributed_stream -v
python verify_distributed_stream.py --out-dir /tmp/telecom-persistent-state-check
```

The strict comparator requires identical, unique, non-null event IDs, all 27
feature columns, finite values and numerical parity. `--allow-partial` permits
missing feature columns only, for the optional tutorial.

Batch `--engine reference` explicitly computes on the driver.
`--engine sql-basics` retains the five-feature SQL exercise; its RANGE windows
include all equal-timestamp peers, so it does not claim causal parity for ties.

## Distributed feature calculation

Three sorted shuffles partition state by its actual owner:

1. MSISDN: fan out incoming calls to their recipient, merge incoming and outgoing
   records in `(ts,event_id)` order, and preserve first-seen, completed-call,
   incoming, outgoing, device and movement histories.
2. IMEI: count distinct subscribers sharing each device in the last 24 hours.
3. Cell: use causal subscriber active-hours to count fresh SIMs in the last hour.

Partial feature vectors join by unique `event_id`. The distributed batch engine
and distributed stream do not collect event batches or keyed state onto the
driver. Their state transitions execute in Python workers. The stream driver
receives scalar maxima/counts plus the small commit receipt.

`distributed_features.py` replays a bounded dataset.
`distributed_stream.py` adds durable per-key snapshots, a distributed lateness
buffer and dedup index, quarantine output and idempotent micro-batch commits.

## Persistent distributed stream

Provision a topic first, then select the engine explicitly:

```bash
docker compose up -d --build
docker compose exec spark python provision_topic.py
docker compose exec spark python produce_to_kafka.py --events /work/spark/sample_events.csv --rate 500
docker compose exec spark python spark_features.py --mode stream --engine distributed-stream --kafka kafka:9092 --partitions 4 --allowed-lateness-seconds 10 --available-now
```

The default distributed checkpoint is `_checkpoint_distributed_stream_v1`.
Each committed directory contains:

```text
distributed_call_features_v1/
  source.json
  batch-00000000000000000000/
    input/                  exact input for retry verification
    state/subscriber/       per-MSISDN FeatureStore JSON in Parquet
    state/imei/             per-IMEI active associations
    state/cell/             per-cell fresh-SIM associations
    buffer/                 events beyond the current cutoff
    dedup/                  bounded event_id + payload-hash index
    features/               joined 27-feature output
    quarantine/             rejected records and reasons
    receipt.json            watermark, counts and commit control
```

All entity stages consume the same accepted event set up to the global cutoff.
IMEI/cell results join subscriber results before the batch is committed. Output
cardinality must equal the emitted-event count. The whole directory is renamed
into place only after all state, features, buffer, dedup and quarantine writes
finish. A restart loads the latest committed per-key snapshots and buffer.

A retry compares saved and incoming rows using distributed `exceptAll` in both
directions, so input iteration order does not matter. Changed input fails.
Retained input/output receipts remain usable after old full snapshots expire.

This implementation writes full distributed snapshots each batch. It rereads
and shuffles keyed state; it is not Spark's incremental state-store API.
Use a local filesystem for single-host execution, or the same shared POSIX
filesystem mounted at the same absolute path on every driver and worker.
Object-storage/HDFS commit protocols, concurrent writers and high availability
are outside this implementation. Use one query per checkpoint. Throughput and
hot-key memory still need measurement before a larger deployment.

The default `--mode stream` engine remains `local-stream`, with checkpoint
`_checkpoint_stateful_v2`; it collects a bounded batch on the driver.
Distributed and local checkpoints cannot be interchanged.

## Event time, identity and completeness

- Every accepted CALL uses deterministic `(ts,event_id)` order. Window lower
  bounds are inclusive. Duration history follows call completion time, and
  outgoing 24-hour counts retain exact timestamps.
- `--allowed-lateness-seconds L` defines cutoff `max_accepted_event_ts - L`.
  Data older than the committed cutoff/order is quarantined; `--late-policy fail`
  stops before commit. Once a timestamp peer has been emitted, another peer
  must have a larger event ID.
- All keyed stages finish the same cutoff before commit. Completeness covers
  accepted data within the configured lateness budget. A stalled Kafka partition
  can exceed that budget; four partitions do not guarantee global time order.
  This is an application contract, not Spark's watermark state API.
- `--available-now` drains a finite buffered tail after the query finishes,
  using a separate flush namespace that does not consume Spark batch IDs.
  Continuous mode retains the latest L seconds until event time advances.
- Dedup uses business `event_id` and payload hash for
  `--dedup-horizon-seconds` (default 24 hours, at least L). Exact duplicates are
  skipped; conflicting payloads are quarantined. In distributed mode, all rows
  sharing an ID with conflicting payloads within one batch are quarantined.
- Invalid JSON and invalid CALL fields retain their raw payload and reason in
  quarantine. Other event types are counted as ignored.
- `--max-offsets` bounds each input micro-batch.
  `--max-buffered-events` (100000) rejects an oversized buffer before commit.
  Distributed state remains on workers; a single hot entity must still fit its
  worker's memory.
- Expired window/device/cell associations are pruned. First-seen and completed
  history persist by default. Optional `--metadata-ttl-seconds` (>=24 hours)
  forgets idle metadata during maintenance and changes returning-subscriber
  history semantics; the option is bound to the checkpoint.
- `--retain-snapshots` keeps the latest full snapshots (default 3).
  Local mode expires old state JSON; distributed mode expires old state/buffer/
  dedup directories. Both retain feature outputs, quarantine and retry receipts;
  distributed mode also retains exact raw input for retry comparison.
- Receipts report input/accepted/emitted/ignored/duplicate/quarantine counts,
  buffered rows, dedup keys, watermark and processing time. Local receipts also
  report snapshot bytes and state cardinalities; distributed receipts report
  subscriber/IMEI/cell state-entity counts.

Source, feature contract and semantic configuration are checkpoint-bound.
Old approximate v1 windows require replay into a new checkpoint; exact event
timestamps cannot be reconstructed from minute buckets. Do not remove Spark
offset files independently of feature state. Micro-batches are not the 30 ms
call-blocking path.

## Isolated Kafka integration

The default topic is `telecom.events.ordered.v2`, explicitly provisioned with one
partition. Provisioning verifies rather than modifies an existing partition
count. The producer sorts causally, waits before sending, requires successful
acknowledgements and verifies the expected partition count.

The verifier creates unique four-partition topics and checkpoints, sends two
backlogs, restarts Spark, then adds a duplicate and malformed JSON:

```bash
python verify_kafka_stream.py --engine local-stream --bootstrap kafka:9092 --out-dir /tmp
python verify_kafka_stream.py --engine distributed-stream --max-offsets 10000 --bootstrap kafka:9092 --out-dir /tmp
```

Test topics/checkpoints remain available when running outside an ephemeral
container. `verify_distributed_stream.py` additionally verifies two independent
Spark processes, persistent first-seen/cross-key state, timestamp ties, completion
order, exact 24-hour boundaries, lateness-tail recovery, changed retries and
duplicate/conflict/late/malformed handling.

## Artifacts and dependencies

`make_sample.py` writes new sample/reference files and a feature-contract manifest
under `_sample_v2/` by default. Older reference parquet files can encode old
semantics; regenerate into a fresh directory. Training data and models must use
the matching feature contract.

The image pins PySpark and Python dependencies. The Kafka JVM connector uses the
installed PySpark version and loads from Maven on first use. Compose is a
single-broker development system with a Kafka data volume; use unique projects
for isolated verification. Existing CSV/reference/log/checkpoint artifacts were
preserved.


Checkpoint and replay validation update (2026-10-22): both engines reject foreign
feature namespaces, including unknown versions, and orphan Spark progress without
the matching feature manifest. Resume with the same engine and its intact state;
changing engines requires a fresh checkpoint and replay.

Spark CALL replay uses setup `ts` plus `duration_s` as completion time. Their sum
must remain finite. Live CDR `start_ts`/`end_ts` overrides are unsupported: supplied
values are quarantined, including fields found in the original Kafka JSON after
fixed-schema parsing. Null columns introduced by a pandas frame are removed before
feature computation. Batch CSV headers must match `EVENT_SCHEMA` in names/order;
extra completion columns are rejected rather than silently ignored. Use the
backend CDR endpoint for live completion timestamps.
