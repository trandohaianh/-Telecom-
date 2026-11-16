# Spark validation — 2026-10-21

Feature contract: `causal-v2-exact-24h-completion-order`.
Existing CSV/reference parquet, logs and checkpoint directories were preserved.

| Check | Result |
|---|---|
| Final host regression suite | 13 passed (2026-10-21) |
| Real Spark bounded distributed replay | 8,939 events; 27/27 features matched at atol/rtol 1e-9 |
| Persistent distributed state across independent processes | 8,939 events; 27/27 features matched; two Python/Spark process starts; lateness-tail recovery and exact retry checks |
| Local Kafka/Spark engine | 8,932 events; four partitions; 11 data micro-batches; three Spark process starts; 27/27 features matched |
| Distributed Kafka/Spark actual CLI | 8,932 events; four partitions; three data micro-batches plus two tail-drain commits; three Spark process starts; 27/27 features matched (2026-10-21) |
| Kafka duplicate/malformed input | Both engines skipped one duplicate and quarantined one malformed record |
| Persistent-state diagnostics | Two exact duplicates skipped; conflicting, late and malformed records quarantined; changed retry input rejected |
| Topic provisioning and producer | Idempotent provisioning; 10 acknowledged events consumed in causal order |
| Compose and source validation | Compose configuration valid; 14 Python source files parsed |

The distributed replay/state checks include seven additional events covering
timestamp ties, cross-subscriber incoming calls, shared IMEI/cell history, self
calls, overlapping call completion and exact 24-hour boundaries. The regression
suite additionally covers manifest/configuration binding, retry input iteration
order, buffer-capacity failures, snapshot retention, legacy checkpoint refusal,
optional metadata TTL, idle-state expiry and producer pacing.

The final distributed Kafka CLI reported:

```text
PASS: engine=distributed-stream, 8932 events, 27/27 features, four partitions, restart, dedup, quarantine
```

Its final checkpoint contained 8,192 subscriber states, 3,799 IMEI states and
356 cell states. The driver received scalar control/metrics; per-key transitions,
validation, buffering, dedup and joins ran on Spark workers. Source was frozen
inside the test container during process restarts.

Integration used the existing pinned `spark-spark:latest` image (PySpark 4.2.0)
and Kafka 3.9.0. Source was mounted read-only. Kafka used separate test networks
and unique topics; outputs/checkpoints were under container `/tmp`.
No existing workspace checkpoint or user topic was used. Test containers/networks
were cleaned after their verdicts; pre-existing containers were preserved.

Persistent distributed streaming is implemented through versioned per-key
Parquet snapshots and atomic directory commits. It is a full-snapshot
implementation, with state read/shuffle/write work each batch, rather than an
incremental state-store API. Single-host tests used Spark workers; multi-host
execution requires the same shared POSIX filesystem path on every worker and
driver. These checks do not establish multi-host throughput, high availability
or a 30 ms latency guarantee.

Reproduce:

```bash
python -m unittest test_stateful_stream test_distributed_stream -v
python verify_distributed_batch.py --out-dir /tmp/telecom-batch-check
python verify_distributed_stream.py --out-dir /tmp/telecom-state-check
python verify_kafka_stream.py --engine local-stream --bootstrap kafka:9092 --out-dir /tmp
python verify_kafka_stream.py --engine distributed-stream --max-offsets 10000 --bootstrap kafka:9092 --out-dir /tmp
```

README.md documents checkpoint separation, accepted-data/event-time completeness,
bounded dedup, quarantine, retention and filesystem requirements.

## Contract/recovery verification, updated 2026-10-25

The current host regression includes 21 Spark tests plus 50 subtests across
test_stateful_stream.py, test_distributed_stream.py and test_stream_contracts.py.
The latest complete project run passed 426 tests plus 50 subtests; see
[the final SLA optimization report](../docs/SLA_TARGET_20261025.md).

[Real Spark contract verification](../data/spark_contract_verification_20261022.json)
ran two separate Spark processes and resumed actual Structured Streaming offsets
and commits: five accepted events, three quarantined records, 27/27 features
matching Python. Valid CSV was accepted; an extra header column and a checkpoint
from the other engine were rejected. The recorded Spark source hashes still
match the current source. This targeted run did not rerun the historical Kafka
volume studies above and does not establish production throughput.
