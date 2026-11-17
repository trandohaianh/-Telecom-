# RAM-first policy lookup prototype

Standalone Python >=3.11, independent of the existing live API.

Install in a separate environment:
```powershell
python -m venv .venv-ram
.venv-ram/Scripts/python -m pip install -r ram_online/requirements.txt
.venv-ram/Scripts/python -m ram_online.demo --policy-redis redis://localhost:16379 --analytics-redis redis://localhost:16380 --samples 1000 --out data/ram-demo-new.json
```
Provide two Redis instances at those URLs. Use separate servers for policy and
analytical windows in production. Development Redis can be ephemeral; production
needs TLS/ACLs, primary reads, noeviction, HA, durable policy changelog and a
recovery/readiness gate after replay. The demo provisions only a UUID namespace.

Switch/IMS -> online local immutable Bloom -> ONE Redis Lua round-trip
(version/cardinality check, then exact membership if needed) -> ALLOW/BLOCK.
Switch setup copies + CDR enrichment -> Kafka keyed by caller -> window/graph/ML
workers -> calibrated policy -> durable changelog -> online Redis primary.
SQL, ML, Kafka commits and per-call fsync are outside the lookup path.
The new lookup response is a read-only decision, not a durable audit ACK for
that RPC. The switch/adapter needs an independent reliable event/audit path.

A stale local Bloom may miss newly blocked numbers. Lua atomically validates its
version against Redis. On mismatch it uses the exact set. Snapshot installation
requires unchanged versions before/after SSCAN. UUID epochs avoid reset-counter
ABA; cardinality checks detect loss of the entire exact-set key. Writers must
use the policy API, with direct writes prevented by ACL/operational controls.
This guarantees membership relative to active Redis data, not fraud truth or
zero replication lag. Restore policy to a known changelog watermark before
serving after Redis loss/failover.

This safe mode still makes one Redis RPC per call. Bloom reduces exact-set work,
not RPC count. Zero-RPC local-negative allows need a publication ACK barrier or
an explicit staleness contract. Blind Pub/Sub is insufficient. Redis SISMEMBER
alone can be simpler/faster at small scale.

m=-n*ln(p)/(ln(2)^2) bits; k~=(m/n)*ln(2).
n=1e6,p=1e-4: ~2.40 MB decimal /2.29 MiB, 13 hash positions. O(k) membership,
expected O(1) exact membership. Indexed SQL is not brute force; the main benefit
is removing synchronous persistence/heavy computation from online checks.

The 60s window is event-time (frontier-60000,frontier], dedup by call_id.
Late-in-window events count, older events do not; idle keys expire after 120s.
setup_ts_ms must remain the original setup time even in a later CDR. Historical
backfill older than 5s cannot activate live demo blocks. Future skew >2s fails.
Kafka producer keys MUST be canonical caller. Send setup copies immediately;
waiting only for completed CDR increases detection lag.

```powershell
python -m ram_online.streaming --bootstrap localhost:29092 --policy-redis redis://localhost:16379 --analytics-redis redis://localhost:16380 --threshold 100
```
Provision/restore policy first. Offsets commit after successful Redis operations,
with auto commit/store disabled. This is at-least-once with idempotent updates,
not a Kafka+Redis exactly-once transaction. Recovery/rebalance fencing is still
required in production. Invalid records stop without commit unless a DLQ topic
is set (--dlq-topic / KAFKA_DLQ_TOPIC): then data errors are produced to the DLQ
(acks=all, flushed) before the offset is committed; infrastructure errors never
go to the DLQ. A high rate emits FLAG. Automatic blocking is off by
default; --activate-demo-blocks is an illustrative policy, not a validated fraud
classifier. Blocks remain until explicitly removed; window expiry does not
unblock. Unknown first-time fraud cannot be identified by deny lookup alone.

check_call_allowed is async: True=ALLOW, False=BLOCK; OnlineUnavailable means
unknown/overload/late/unready, never a successful ALLOW. Adapter fail-open uses
the original deadline and must be reported separately. The 5ms budget is not a
hard OS scheduling guarantee. Warm pools, nearby Redis and offered-load HTTP/SIP
tests remain necessary. Demo latency covers sequential function+Redis only.
Build large snapshots in an isolated sidecar/process. At carrier scale, shard
by stable phone hash: per-shard Bloom+manifest+exact set and primary routing.
Keep each manifest/set pair in one Redis Cluster hash slot.


## Local-only demo Redis instances

The following config is for the isolated demo, without production credentials:
```powershell
$cfg = (Resolve-Path ram_online/redis-demo.conf).Path
docker run --rm -d --name tfd-policy-demo -p 127.0.0.1:16379:6379 --mount "type=bind,src=$cfg,dst=/usr/local/etc/redis/redis.conf,readonly" redis:7.4-alpine redis-server /usr/local/etc/redis/redis.conf
docker run --rm -d --name tfd-analytics-demo -p 127.0.0.1:16380:6379 --mount "type=bind,src=$cfg,dst=/usr/local/etc/redis/redis.conf,readonly" redis:7.4-alpine redis-server /usr/local/etc/redis/redis.conf
```
Stop only these demo instances after use. The test run for this contribution used
separate UUID/test namespaces and different automatically allocated ports.

Strict measurements (1000 sequential checks each, small deny set):
- Windows host -> Docker Redis: p50 1.300ms / p99 6.711ms / max 15.720ms;
  985 correct within 5ms, 15 degraded, zero incorrect.
- Linux client on the same internal bridge as Redis: p50 0.409ms / p99 1.024ms /
  max 1.681ms; 1000 correct within 5ms, zero degraded or incorrect.
[Windows strict](../data/ram_online_lookup_strict_20261026.json),
[Linux colocated](../data/ram_online_lookup_linux_20261026.json).
These measure function+Redis, not offered-load HTTP/SIP capacity or carrier SLA.
The initial exploratory result is retained separately. 17 prototype tests pass;
Redis integration is real, Kafka offset-order tests use a fake consumer.
The old live API still uses its existing pipeline until explicitly migrated.
