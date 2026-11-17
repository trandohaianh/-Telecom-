"""Carrier feed -> main fraud API bridge (the only supported way to feed operator data in).

Decision 17/11/2026: the main FastAPI API (src/server.py) is the single source of fraud decisions.
This adapter reads a carrier feed and forwards each event, HMAC-signed, to
POST /api/v1/verdict (call set-up / SMS / audio) or POST /api/v1/cdr (completed call).

Sources
  * newline-delimited JSON from a file (optionally tailed with --follow) or stdin ("-")
  * a Kafka topic (confluent_kafka, imported only when --kafka-bootstrap is given)

Delivery is at-least-once: a file line is done / a Kafka offset is committed only after the API
acknowledged it (HTTP 200/202) or the record was durably written to the dead-letter file.
The API deduplicates by event_id, so re-sending after a crash or rebalance is safe.

Usage
  python src/carrier_bridge.py --api https://host --key-file secrets/network_link.key \
      --input events.jsonl [--follow] [--mapping map.json] [--dead-letter dlq.jsonl] [--concurrency 32]
  python src/carrier_bridge.py --api https://host --key-file secrets/network_link.key \
      --kafka-bootstrap broker:9092 --kafka-topic cdr --kafka-group fraud-bridge

The signing key is never printed. Metrics lines never contain subscriber numbers.
Dependencies: httpx (+ confluent-kafka for the Kafka source). No import of the server code.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import random
import signal
import sys
import threading
import time
from typing import Any, AsyncIterator, Callable

import httpx

log = logging.getLogger("carrier_bridge")

KINDS = ("VERDICT", "CDR")
PATHS = {"VERDICT": "/api/v1/verdict", "CDR": "/api/v1/cdr"}
MAX_BODY = 65536  # server.MAX_NETWORK_BODY
TIME_FIELDS = ("ts", "end_ts")
_RESERVED = {"fields", "ts_unit", "duration_unit", "defaults", "drop", "values", "kind_field"}


class RecordError(ValueError):
    """A record that can never be delivered as-is (goes to the dead-letter file)."""


# ====================================================================== pure functions
def load_key(path) -> bytes:
    """Read the network link key: 64 hex characters (security.network_key format) or 32 raw bytes.
    Errors never include the file content."""
    raw = Path(path).read_bytes()
    text = raw.strip()
    try:
        if len(text) == 64:
            return bytes.fromhex(text.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        pass
    if len(raw) == 32:
        return raw
    raise ValueError(f"{path}: network key must be 64 hexadecimal characters or 32 raw bytes")


_ts_lock = threading.Lock()
_last_ts_ns = 0


def fresh_timestamp() -> str:
    """Strictly increasing signing timestamp (same format as security.network_timestamp)."""
    global _last_ts_ns
    with _ts_lock:
        _last_ts_ns = max(time.time_ns(), _last_ts_ns + 1)
        now = _last_ts_ns
    seconds, fraction = divmod(now, 1_000_000_000)
    return f"{seconds}.{fraction:09d}"


def sign(body: bytes, ts: str, key: bytes) -> str:
    """HMAC-SHA256 over ts + "." + body: identical to security.sign_network."""
    return hmac.new(key, ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, key: bytes, ts: str | None = None) -> dict:
    ts = ts or fresh_timestamp()
    return {"content-type": "application/json", "x-timestamp": ts, "x-signature": sign(body, ts, key)}


def normalize_mapping(spec: dict | None) -> dict:
    """Accept {"fields": {...}, "ts_unit": "ms", ...} or the flat form {"calling_number": "caller", ...}."""
    spec = dict(spec or {})
    if not isinstance(spec, dict):
        raise ValueError("mapping must be a JSON object")
    fields = dict(spec.get("fields") or {})
    fields.update({k: v for k, v in spec.items() if k not in _RESERVED})
    for k, v in fields.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("mapping fields must map carrier name -> API name (strings)")
    units = {"ts_unit": spec.get("ts_unit", "s"), "duration_unit": spec.get("duration_unit", "s")}
    if units["ts_unit"] not in ("s", "ms", "iso") or units["duration_unit"] not in ("s", "ms"):
        raise ValueError("ts_unit must be s|ms|iso and duration_unit s|ms")
    values = spec.get("values") or {}
    if not isinstance(values, dict) or not all(isinstance(v, dict) for v in values.values()):
        raise ValueError("values must be {api_field: {carrier_value: api_value}}")
    return {"fields": fields, **units, "defaults": dict(spec.get("defaults") or {}),
            "drop": set(spec.get("drop") or ()), "values": values, "kind_field": spec.get("kind_field", "kind")}


def load_mapping(path) -> dict:
    if not path:
        return normalize_mapping(None)
    return normalize_mapping(json.loads(Path(path).read_text(encoding="utf-8")))


def _number(name, value):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise RecordError(f"{name} is not a number") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RecordError(f"{name} is not a finite number")
    return value


def _seconds(name, value, unit):
    if unit == "iso":
        if not isinstance(value, str):
            return _number(name, value)          # already numeric: assume seconds
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise RecordError(f"{name} is not ISO-8601") from None
        if parsed.tzinfo is None:
            raise RecordError(f"{name} has no UTC offset")
        return parsed.timestamp()
    value = _number(name, value)
    return value / 1000.0 if unit == "ms" else value


def map_event(event: dict, mapping: dict) -> dict:
    """Rename carrier fields to API fields, translate values, convert units. Returns a new dict."""
    if not isinstance(event, dict):
        raise RecordError("event must be a JSON object")
    out = dict(mapping["defaults"])
    for name, value in event.items():
        if name in mapping["drop"]:
            continue
        out[mapping["fields"].get(name, name)] = value
    for name, table in mapping["values"].items():
        if name in out and isinstance(out[name], str) and out[name] in table:
            out[name] = table[out[name]]
    for name in TIME_FIELDS:
        if name in out:
            out[name] = _seconds(name, out[name], mapping["ts_unit"])
    if "duration_s" in out:
        out["duration_s"] = _seconds("duration_s", out["duration_s"], mapping["duration_unit"])
    return out


def infer_kind(event: dict) -> str:
    return "CDR" if "duration_s" in event else "VERDICT"


def prepare(record: Any, mapping: dict) -> tuple[str, dict, bytes]:
    """Record (envelope {"kind", "event"} or bare event; bytes/str/dict) -> (kind, api_event, body)."""
    if isinstance(record, (bytes, bytearray)):
        record = record.decode("utf-8", errors="strict")
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except ValueError:
            raise RecordError("invalid JSON") from None
    if not isinstance(record, dict):
        raise RecordError("record must be a JSON object")
    kind_field = mapping["kind_field"]
    if isinstance(record.get("event"), dict):
        kind, raw = record.get(kind_field), record["event"]
    else:
        kind, raw = record.get(kind_field), {k: v for k, v in record.items() if k != kind_field}
    event = map_event(raw, mapping)
    if kind is None:
        kind = infer_kind(event)
    if not isinstance(kind, str) or kind.upper() not in KINDS:
        raise RecordError("kind must be VERDICT or CDR")
    kind = kind.upper()
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        raise RecordError("event_id missing")
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY:
        raise RecordError("event body is too large")
    return kind, event, body


def classify(status: int, payload: Any = None) -> str:
    """HTTP outcome -> "ok" | "retry" | "dead".

    401 is retried (clock skew or key rotation in progress; a wrong key must not empty the feed into the
    dead-letter file). 200 with retryable=true and not durable is the API's fail-open overload reply:
    the event was not admitted, so it is retried (the API dedups by event_id)."""
    if status in (200, 202):
        if status == 200 and isinstance(payload, dict) and payload.get("retryable") and not payload.get("durable"):
            return "retry"
        return "ok"
    if status in (401, 408, 425, 429) or status >= 500:
        return "retry"
    return "dead"                                   # other 4xx incl. 409 conflicting duplicate


def backoff_delay(attempt: int, base: float, cap: float, retry_after: str | None = None, rng=random.random) -> float:
    if retry_after:
        try:
            return min(cap, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(cap, base * (2 ** attempt)) * (0.5 + rng() / 2)


def percentile(values: list, q: float):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))], 3)


# ====================================================================== metrics / dead letters
@dataclass
class Metrics:
    counts: Counter = field(default_factory=Counter)
    latencies: list = field(default_factory=list)
    inflight: int = 0

    def snapshot(self, reset_window: bool = True) -> dict:
        lat, c = self.latencies, self.counts
        if reset_window:
            self.latencies = []
        return {"ts": round(time.time(), 3), "received": c["received"], "sent": c["sent"], "ok": c["ok"],
                "duplicates": c["duplicates"], "retried": c["retried"], "dead_lettered": c["dead_lettered"],
                "inflight": self.inflight, "window_requests": len(lat),
                "latency_p50_ms": percentile(lat, .50), "latency_p99_ms": percentile(lat, .99),
                "status": {k[7:]: v for k, v in sorted(c.items()) if k.startswith("status_")}}


class DeadLetter:
    """Append-only JSONL; each line is flushed and fsynced before the record counts as done."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._fh = None

    def write(self, reason: str, record: Any, kind: str | None = None, status: int | None = None):
        if self.path is None:
            log.error("dropping undeliverable record (no --dead-letter): %s", reason)
            return
        if isinstance(record, (bytes, bytearray)):
            record = record.decode("utf-8", errors="replace")
        line = json.dumps({"at": round(time.time(), 3), "reason": reason[:500], "status": status, "kind": kind,
                           "record": record}, ensure_ascii=False) + "\n"
        with self._lock:
            if self._fh is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(line)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self):
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


# ====================================================================== delivery
@dataclass
class Item:
    record: Any
    ack: Callable[[], None] | None = None


class Bridge:
    def __init__(self, api: str, key: bytes, *, mapping: dict | None = None, client: httpx.AsyncClient | None = None,
                 concurrency: int = 32, timeout: float = 5.0, dead_letter: DeadLetter | None = None,
                 max_retries: int = 0, backoff_base: float = 0.2, backoff_max: float = 10.0,
                 sleep=asyncio.sleep, drain_timeout: float = 30.0):
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.api = api.rstrip("/")
        self._key = key
        self.mapping = mapping or normalize_mapping(None)
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency))
        self._own_client = client is None
        self.concurrency = concurrency
        self.dead = dead_letter or DeadLetter(None)
        self.max_retries = max_retries               # 0 = retry until acknowledged (back-pressure)
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.sleep = sleep
        self.drain_timeout = drain_timeout
        self.metrics = Metrics()
        self.stop = asyncio.Event()
        self.fatal: BaseException | None = None

    def __repr__(self):                              # never expose the key
        return f"Bridge(api={self.api!r}, concurrency={self.concurrency})"

    async def deliver(self, record: Any) -> str:
        """Deliver one record. Returns "ok" or "dead" once it is safe to ack the source."""
        try:
            kind, event, body = prepare(record, self.mapping)
        except RecordError as exc:
            self._dead(str(exc), record)
            return "dead"
        url, attempt = self.api + PATHS[kind], 0
        while True:
            headers = signed_headers(body, self._key)  # fresh timestamp per attempt (server replay guard)
            self.metrics.counts["sent"] += 1
            started = time.perf_counter()
            status, payload, retry_after, reason = None, None, None, ""
            try:
                response = await self.client.post(url, content=body, headers=headers)
                status = response.status_code
                self.metrics.latencies.append((time.perf_counter() - started) * 1000)
                self.metrics.counts[f"status_{status}"] += 1
                retry_after = response.headers.get("retry-after")
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                outcome = classify(status, payload)
                if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                    reason = payload["detail"]
                elif outcome == "retry" and status in (200, 202):
                    reason = "not admitted (retryable)"
            except httpx.TransportError as exc:
                outcome, reason = "retry", type(exc).__name__
                self.metrics.counts["status_transport_error"] += 1
            if outcome == "ok":
                self.metrics.counts["ok"] += 1
                if isinstance(payload, dict) and payload.get("duplicate"):
                    self.metrics.counts["duplicates"] += 1
                return "ok"
            if outcome == "dead":
                self._dead(f"HTTP {status}: {reason}", record, kind, status)
                return "dead"
            if status == 401:
                log.error("API refused the signature (%s); check key file and clock", reason or "401")
            if self.max_retries and attempt >= self.max_retries:
                self._dead(f"retries exhausted, last: {status or reason}", record, kind, status)
                return "dead"
            self.metrics.counts["retried"] += 1
            await self.sleep(backoff_delay(attempt, self.backoff_base, self.backoff_max, retry_after))
            attempt += 1

    def _dead(self, reason, record, kind=None, status=None):
        self.dead.write(reason, record, kind, status)  # raises on IO error -> caller does not ack
        self.metrics.counts["dead_lettered"] += 1

    async def _one(self, item: Item, sem: asyncio.Semaphore):
        self.metrics.inflight += 1
        try:
            await self.deliver(item.record)
            if item.ack:
                item.ack()
        except asyncio.CancelledError:
            raise
        except Exception as exc:                     # e.g. dead-letter disk full: stop, do not ack
            log.error("fatal delivery error: %s", type(exc).__name__)
            self.fatal = exc
            self.stop.set()
        finally:
            self.metrics.inflight -= 1
            sem.release()

    async def run(self, source, metrics_every: float = 10.0, out=None) -> int:
        """Pump the source until it ends or stop is set, then drain in-flight deliveries."""
        out = out or sys.stdout
        sem, tasks = asyncio.Semaphore(self.concurrency), set()
        reporter = asyncio.create_task(self._report(metrics_every, out)) if metrics_every > 0 else None
        items = source.items(self.stop)
        try:
            async for item in items:
                self.metrics.counts["received"] += 1
                await sem.acquire()                  # back-pressure: bounded in-flight
                task = asyncio.create_task(self._one(item, sem))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                if self.stop.is_set():
                    break
        finally:
            await items.aclose()
            if tasks:
                done, pending = await asyncio.wait(set(tasks), timeout=self.drain_timeout)
                for task in pending:                 # not acked -> redelivered next run (at-least-once)
                    task.cancel()
                if pending:
                    log.warning("drain timeout: %d deliveries left unacknowledged", len(pending))
                    await asyncio.gather(*pending, return_exceptions=True)
            await source.finish()
            if reporter:
                reporter.cancel()
            print(json.dumps({"final": True, **self.metrics.snapshot()}), file=out, flush=True)
            self.dead.close()
            if self._own_client:
                await self.client.aclose()
        return 2 if self.fatal else 0

    async def _report(self, every, out):
        while True:
            await asyncio.sleep(every)
            print(json.dumps(self.metrics.snapshot()), file=out, flush=True)


# ====================================================================== sources
class FileSource:
    """NDJSON file or stdin ("-"). With follow=True keeps tailing (handles truncation/rotation)."""

    def __init__(self, path: str, follow: bool = False, poll_s: float = 0.2):
        self.path, self.follow, self.poll_s = path, follow, poll_s

    async def items(self, stop: asyncio.Event) -> AsyncIterator[Item]:
        if self.path == "-":
            async for item in self._stdin(stop):
                yield item
            return
        fh = open(self.path, "rb")
        ident = os.fstat(fh.fileno())
        partial = b""
        try:
            while not stop.is_set():
                line = fh.readline()
                if line:
                    if not line.endswith(b"\n") and self.follow:
                        partial += line              # writer has not finished the line yet
                        continue
                    line, partial = partial + line, b""
                    if line.strip():
                        yield Item(line.strip())
                    continue
                if not self.follow:
                    if partial.strip():
                        yield Item(partial.strip())
                    return
                await asyncio.sleep(self.poll_s)
                try:
                    now = os.stat(self.path)
                except FileNotFoundError:
                    continue
                if (now.st_ino, now.st_dev) != (ident.st_ino, ident.st_dev) or now.st_size < fh.tell():
                    fh.close()                       # rotated or truncated: start the new file from the top
                    fh = open(self.path, "rb")
                    ident, partial = os.fstat(fh.fileno()), b""
        finally:
            fh.close()

    async def _stdin(self, stop):
        reader = sys.stdin.buffer
        waiter = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                read = asyncio.ensure_future(asyncio.to_thread(reader.readline))
                await asyncio.wait({read, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if not read.done():
                    return
                line = read.result()
                if not line:
                    return
                if line.strip():
                    yield Item(line.strip())
        finally:
            waiter.cancel()

    async def finish(self):
        return None


class OffsetTracker:
    """Per-partition contiguous completion: offset N is committable only when every offset before it
    (that this consumer received) is done. Thread-safe (rebalance callbacks run inside poll)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict = {}    # (topic, partition) -> list of offsets in arrival order
        self._done: dict = {}       # (topic, partition) -> set of finished offsets
        self._ready: dict = {}      # (topic, partition) -> next offset to commit

    def add(self, topic, partition, offset):
        with self._lock:
            self._pending.setdefault((topic, partition), []).append(offset)
            self._done.setdefault((topic, partition), set())

    def done(self, topic, partition, offset):
        with self._lock:
            key = (topic, partition)
            if key not in self._pending:
                return                               # partition was revoked meanwhile
            self._done[key].add(offset)
            pending, finished = self._pending[key], self._done[key]
            advanced = None
            while pending and pending[0] in finished:
                advanced = pending.pop(0)
                finished.discard(advanced)
            if advanced is not None:
                self._ready[key] = advanced + 1

    def take_ready(self) -> dict:
        with self._lock:
            ready, self._ready = self._ready, {}
            return ready

    def restore(self, ready: dict):
        """Put back offsets whose commit failed (unless a newer one is ready)."""
        with self._lock:
            for key, off in ready.items():
                if key in self._pending and off > self._ready.get(key, -1):
                    self._ready[key] = off

    def revoke(self, keys):
        with self._lock:
            for key in keys:
                self._pending.pop(key, None)
                self._done.pop(key, None)
                self._ready.pop(key, None)


class KafkaSource:
    """At-least-once Kafka consumer: enable.auto.commit=False; offsets committed only after the API ack.
    All consumer calls run on one dedicated thread (confluent_kafka consumers are not thread-safe)."""

    def __init__(self, consumer, topic: str, poll_s: float = 0.5, topic_partition=None):
        from concurrent.futures import ThreadPoolExecutor
        self.consumer, self.topic, self.poll_s = consumer, topic, poll_s
        self.tracker = OffsetTracker()
        self._tp = topic_partition
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kafka")
        self.committed: list = []                    # (topic, partition, offset) history, for tests/ops

    @classmethod
    def connect(cls, bootstrap, topic, group, offset_reset="earliest", extra: dict | None = None):
        from confluent_kafka import Consumer, TopicPartition
        conf = {"bootstrap.servers": bootstrap, "group.id": group, "enable.auto.commit": False,
                "enable.auto.offset.store": False, "auto.offset.reset": offset_reset, **(extra or {})}
        conf["enable.auto.commit"] = False           # never overridable: at-least-once depends on it
        return cls(Consumer(conf), topic, topic_partition=TopicPartition)

    def _subscribe(self):
        def on_revoke(_consumer, partitions):
            self._commit_ready()                     # commit what is done before losing the partitions
            self.tracker.revoke([(p.topic, p.partition) for p in partitions])
        self.consumer.subscribe([self.topic], on_revoke=on_revoke)

    def _commit_ready(self):
        ready = self.tracker.take_ready()
        if not ready:
            return
        tp = self._tp or _SimpleTP
        offsets = [tp(t, p, off) for (t, p), off in sorted(ready.items())]
        try:
            self.consumer.commit(offsets=offsets, asynchronous=False)
        except Exception as exc:                     # e.g. rebalance in progress: retry next round
            log.warning("offset commit failed: %s", type(exc).__name__)
            self.tracker.restore(ready)
            return
        self.committed.extend((t, p, off) for (t, p), off in sorted(ready.items()))

    async def _call(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._exec, fn, *args)

    async def items(self, stop):
        await self._call(self._subscribe)
        while not stop.is_set():
            await self._call(self._commit_ready)
            msg = await self._call(self.consumer.poll, self.poll_s)
            if msg is None:
                continue
            err = msg.error()
            if err:
                if not _is_partition_eof(err):
                    log.warning("kafka error: %s", err)
                continue
            topic, partition, offset = msg.topic(), msg.partition(), msg.offset()
            self.tracker.add(topic, partition, offset)
            yield Item(msg.value(), ack=lambda t=topic, p=partition, o=offset: self.tracker.done(t, p, o))

    async def finish(self):
        try:
            await self._call(self._commit_ready)
            await self._call(self.consumer.close)
        finally:
            self._exec.shutdown(wait=True)


@dataclass(frozen=True)
class _SimpleTP:
    topic: str
    partition: int
    offset: int


def _is_partition_eof(err) -> bool:
    try:
        from confluent_kafka import KafkaError
        return err.code() == KafkaError._PARTITION_EOF
    except Exception:
        return False


# ====================================================================== CLI
def build_parser():
    p = argparse.ArgumentParser(description="Forward a carrier event feed to the main fraud API (signed).")
    p.add_argument("--api", required=True, help="base URL of the main API, e.g. https://fraud.example")
    p.add_argument("--key-file", required=True, help="network link key (64 hex chars or 32 raw bytes)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help='NDJSON file, or "-" for stdin')
    src.add_argument("--kafka-bootstrap", help="Kafka bootstrap servers")
    p.add_argument("--follow", action="store_true", help="keep tailing --input as it grows")
    p.add_argument("--kafka-topic")
    p.add_argument("--kafka-group")
    p.add_argument("--kafka-offset-reset", default="earliest", choices=("earliest", "latest"))
    p.add_argument("--kafka-config", action="append", default=[], metavar="KEY=VALUE",
                   help="extra librdkafka setting (e.g. security.protocol=SASL_SSL); repeatable")
    p.add_argument("--mapping", help="JSON field-mapping file")
    p.add_argument("--dead-letter", help="JSONL file for undeliverable records")
    p.add_argument("--concurrency", type=int, default=32)
    p.add_argument("--timeout", type=float, default=5.0, help="per-request timeout, seconds")
    p.add_argument("--max-retries", type=int, default=0, help="0 = retry until acknowledged")
    p.add_argument("--backoff-base", type=float, default=0.2)
    p.add_argument("--backoff-max", type=float, default=10.0)
    p.add_argument("--metrics-every", type=float, default=10.0, help="seconds between metric lines (0 = off)")
    p.add_argument("--drain-timeout", type=float, default=30.0)
    p.add_argument("--verify", default="true", help='TLS: "true", "false" or a CA bundle path')
    return p


def _install_signals(stop: asyncio.Event):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):   # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def main_async(args) -> int:
    key = load_key(args.key_file)
    mapping = load_mapping(args.mapping)
    verify = {"true": True, "false": False}.get(args.verify.lower(), args.verify)
    client = httpx.AsyncClient(timeout=httpx.Timeout(args.timeout), verify=verify,
                               limits=httpx.Limits(max_connections=args.concurrency,
                                                   max_keepalive_connections=args.concurrency))
    if args.kafka_bootstrap:
        if not (args.kafka_topic and args.kafka_group):
            raise SystemExit("--kafka-topic and --kafka-group are required with --kafka-bootstrap")
        extra = dict(item.split("=", 1) for item in args.kafka_config)
        source = KafkaSource.connect(args.kafka_bootstrap, args.kafka_topic, args.kafka_group,
                                     args.kafka_offset_reset, extra)
    else:
        source = FileSource(args.input, follow=args.follow)
    bridge = Bridge(args.api, key, mapping=mapping, client=client, concurrency=args.concurrency,
                    dead_letter=DeadLetter(args.dead_letter), max_retries=args.max_retries,
                    backoff_base=args.backoff_base, backoff_max=args.backoff_max, drain_timeout=args.drain_timeout)
    _install_signals(bridge.stop)
    try:
        return await bridge.run(source, metrics_every=args.metrics_every)
    finally:
        await client.aclose()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
