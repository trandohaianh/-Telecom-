"""Kafka demo pipeline: carrier.events -> ShardedEngine -> durable journal -> fraud.verdicts.

Not a second engine. Every piece is reused:
- scoring: scaleout.ShardedEngine (router + N scoring processes + cross-key side updates);
- message contract: key = canonical caller, at-least-once (offsets committed only after the batch is
  durable), as in ram_online/streaming.process_kafka_message;
- durability: BackendState (SQLite WAL, synchronous=FULL). A whole micro-batch is journalled inside ONE
  transaction, so one fsync is amortised over the batch (group commit). reserve() deduplicates, so a
  replay after a crash returns the stored verdict instead of scoring the event twice.

What this module adds is only the glue: message encoding, a micro-batcher, the consume -> score ->
journal -> produce -> commit loop, a verdict sink that measures END-TO-END latency (producer send time
-> verdict received from the output topic, including batching wait and Kafka linger), and a paced
producer. The Kafka client is injected (confluent_kafka objects in the demo, fakes in the tests).

Run inside the demo container: python -B src/kafka_pipeline.py bench --partitions 2 --rate 4000
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IN_TOPIC, OUT_TOPIC = "carrier.events", "fraud.verdicts"
SENT_AT = "_sent_at"            # producer wall-clock send time (seconds), carried inside the message


# --------------------------------------------------------------------------- message contract
def message_key(ev: dict) -> bytes:
    """Partition key = canonical caller (same rule as scaleout.partition_key / ram_online)."""
    from scaleout import partition_key
    return partition_key(ev).encode("utf-8")


def encode_event(ev: dict, sent_at: float) -> tuple[bytes, bytes]:
    return message_key(ev), json.dumps({**ev, SENT_AT: sent_at}, separators=(",", ":"),
                                       allow_nan=False).encode("utf-8")


def decode_event(value: bytes) -> tuple[dict, float]:
    ev = json.loads(value)
    if not isinstance(ev, dict) or "event_id" not in ev:
        raise ValueError("event must be a JSON object with event_id")
    sent_at = float(ev.pop(SENT_AT, "nan"))
    return ev, sent_at


def verdict_message(ev: dict, result: dict, sent_at: float, journal_seq: int) -> tuple[bytes, bytes]:
    out = {"event_id": ev["event_id"], "decision": result.get("decision"), "risk": result.get("risk"),
           "scored": result.get("scored", True), "reasons": [r.get("code") for r in result.get("reasons", [])][:3],
           "journal_seq": journal_seq, SENT_AT: sent_at, "verdict_at": time.time()}
    return message_key(ev), json.dumps(out, separators=(",", ":"), allow_nan=False).encode("utf-8")


# --------------------------------------------------------------------------- micro-batching
class MicroBatcher:
    """Collect up to max_batch messages, waiting at most max_wait_s after the FIRST one arrived.

    The wait is part of every event's latency, which is why the sink measures end to end, not per batch.
    """

    def __init__(self, consumer, max_batch: int = 1000, max_wait_s: float = 0.005, clock=time.monotonic):
        if max_batch < 1 or not 0 <= max_wait_s <= 1:
            raise ValueError("max_batch >= 1 and 0 <= max_wait_s <= 1")
        self.consumer, self.max_batch, self.max_wait_s, self.clock = consumer, max_batch, max_wait_s, clock

    def next_batch(self, idle_timeout_s: float = 1.0) -> list:
        # confluent_kafka's consume(n, timeout) blocks until n messages OR the timeout. Asking for the whole
        # batch here made every batch at low load wait the full idle timeout (0.5 s) before processing, a
        # latency floor that had nothing to do with scoring or fsync. Wait for ONE message, drain what is
        # already buffered without waiting, then linger at most max_wait_s.
        first = self.consumer.consume(num_messages=1, timeout=idle_timeout_s)
        batch = [m for m in first if m.error() is None]
        if not first:
            return []
        batch += [m for m in self.consumer.consume(num_messages=self.max_batch - 1, timeout=0) if m.error() is None]
        if not batch:
            return []
        deadline = self.clock() + self.max_wait_s
        while len(batch) < self.max_batch and (left := deadline - self.clock()) > 0:
            more = self.consumer.consume(num_messages=self.max_batch - len(batch), timeout=left)
            if not more:          # consume() already waited the remaining time
                break
            batch += [m for m in more if m.error() is None]
        return batch


# --------------------------------------------------------------------------- consumer
class VerdictPipeline:
    def __init__(self, consumer, producer, engine, state, *, out_topic: str = OUT_TOPIC,
                 max_batch: int = 1000, max_wait_s: float = 0.005):
        self.consumer, self.producer, self.engine, self.state, self.out_topic = consumer, producer, engine, state, out_topic
        self.batcher = MicroBatcher(consumer, max_batch, max_wait_s)
        self.stats = {"batches": 0, "events": 0, "duplicates": 0, "commits": 0, "max_batch": 0,
                      "score_ms": 0.0, "journal_ms": 0.0, "produce_ms": 0.0}
        self.results: dict[str, dict] = {}

    def handle(self, messages) -> int:
        """Score one micro-batch, journal it durably in ONE transaction, publish, then commit offsets."""
        if not messages:
            return 0
        decoded = [decode_event(m.value()) for m in messages]
        # Partitions interleave callers; per-caller order is kept by Kafka. Restore event-time order inside
        # the batch so cross-key features see the same sequence as the in-process replay.
        order = sorted(range(len(decoded)), key=lambda i: (decoded[i][0]["ts"], decoded[i][0]["event_id"]))
        events = [decoded[i][0] for i in order]
        sent = [decoded[i][1] for i in order]
        t0 = time.perf_counter()
        fresh, previous = [], {}
        with self.state.transaction():
            seqs = []
            for ev in events:
                seq, prev = self.state.reserve("VERDICT", ev)
                seqs.append(seq)
                if prev is not None:
                    previous[ev["event_id"]] = prev
                else:
                    fresh.append(ev)
            t1 = time.perf_counter()
            scored = dict(zip((ev["event_id"] for ev in fresh), self.engine.process(fresh))) if fresh else {}
            t2 = time.perf_counter()
            for ev, seq in zip(events, seqs):
                if ev["event_id"] in scored:
                    self.state.finish(seq, scored[ev["event_id"]])
        t3 = time.perf_counter()          # COMMIT (one fsync with synchronous=FULL) happened on exiting the block
        for ev, s, seq in zip(events, sent, seqs):
            result = scored.get(ev["event_id"]) or previous.get(ev["event_id"]) or {}
            self.results[ev["event_id"]] = result
            key, value = verdict_message(ev, result, s, seq)
            self.producer.produce(self.out_topic, key=key, value=value)
        self.producer.poll(0)
        t4 = time.perf_counter()
        # At-least-once: offsets only after the verdicts are durable (a crash replays; reserve() dedupes).
        self.consumer.commit(asynchronous=False)
        self.stats["batches"] += 1
        self.stats["commits"] += 1
        self.stats["events"] += len(events)
        self.stats["duplicates"] += len(previous)
        self.stats["max_batch"] = max(self.stats["max_batch"], len(events))
        self.stats["score_ms"] += (t2 - t1) * 1000
        self.stats["journal_ms"] += ((t1 - t0) + (t3 - t2)) * 1000
        self.stats["produce_ms"] += (t4 - t3) * 1000
        return len(events)

    def run(self, total: int, idle_timeout_s: float = 30.0) -> None:
        seen = 0
        last = time.monotonic()
        while seen < total:
            n = self.handle(self.batcher.next_batch(idle_timeout_s=0.5))
            seen += n
            if n:
                last = time.monotonic()
            elif time.monotonic() - last > idle_timeout_s:
                raise TimeoutError(f"no input for {idle_timeout_s}s after {seen}/{total} events")
        self.producer.flush(30)


# --------------------------------------------------------------------------- A1-A3: pipelined consumer
SIM_ONLY_FIELDS = ("label", "fraud_type", "actor", "warmup")


def journal_event(ev: dict) -> dict:
    """A2: what a carrier event really carries. Simulator labels are not part of it, so they are neither
    scored nor journalled (the detector never reads them; evaluation keeps them from the source file)."""
    return {k: v for k, v in ev.items() if k not in SIM_ONLY_FIELDS}


class AdaptiveBatcher(MicroBatcher):
    """A3: cap the batch at what fits a latency budget; grow it only while there is a sustained backlog.

    A fixed 1000-event cap made every batch full at 50 % load (the cycle was slow, so 1000 events queued
    up), and each event then waited for the whole batch. Here the cap follows the measured cost per event
    (EWMA of score + journal time), so at low load batches stay small; three full batches in a row mean the
    consumer is behind, and the cap doubles (fewer fsyncs per event) up to max_batch.
    """

    def __init__(self, consumer, max_batch=1000, max_wait_s=0.001, budget_ms=10.0, min_batch=16, clock=time.monotonic):
        super().__init__(consumer, max_batch, max_wait_s, clock)
        if budget_ms <= 0 or min_batch < 1 or min_batch > max_batch:
            raise ValueError("budget_ms > 0 and 1 <= min_batch <= max_batch")
        self.cap, self.min_batch, self.budget_ms = max_batch, min_batch, budget_ms
        self.per_event_ms = None
        self.full_streak = 0
        self.limit = max(min_batch, min(max_batch, 64))
        self.limits = []

    def observe(self, n: int, elapsed_ms: float) -> None:
        if n <= 0:
            return
        per = elapsed_ms / n
        self.per_event_ms = per if self.per_event_ms is None else 0.8 * self.per_event_ms + 0.2 * per
        self.full_streak = self.full_streak + 1 if n >= self.limit else 0
        if self.full_streak >= 3:
            self.limit = min(self.cap, self.limit * 2)
        else:
            self.limit = max(self.min_batch, min(self.cap, int(self.budget_ms / max(self.per_event_ms, 1e-6))))
        self.limits.append(self.limit)

    def next_batch(self, idle_timeout_s: float = 1.0) -> list:
        self.max_batch = self.limit
        return super().next_batch(idle_timeout_s)


def _offsets_of(messages):
    """{(topic, partition): next offset} for one batch: exactly what may be committed once it is durable."""
    out = {}
    for m in messages:
        key = (m.topic(), m.partition())
        out[key] = max(out.get(key, -1), m.offset() + 1)
    return out


class PipelinedVerdictPipeline:
    """A1 + A2 (+ A3): score batch N+1 while batch N is being made durable.

    - One writer thread journals batches strictly in order, ONE transaction (one FULL fsync) per batch,
      then publishes that batch's verdicts, then commits exactly that batch's offsets. A bounded queue
      (in_flight batches) gives backpressure.
    - Deduplication checks the in-flight map FIRST and the database (through a separate read connection,
      so it does not wait for the writer's fsync) SECOND: a batch leaves the in-flight map only after its
      commit, so a replayed event is always found in one of the two and is never scored twice.
    - A2: the journal stores one row per event in a single write (record_many), without simulator labels.
    """

    def __init__(self, consumer, producer, engine, state, *, out_topic=OUT_TOPIC, max_batch=1000,
                 max_wait_s=0.001, budget_ms=10.0, adaptive=True, in_flight=1, offset_factory=None):
        self.consumer, self.producer, self.engine, self.state, self.out_topic = consumer, producer, engine, state, out_topic
        self.batcher = (AdaptiveBatcher(consumer, max_batch, max_wait_s, budget_ms) if adaptive
                        else MicroBatcher(consumer, max_batch, max_wait_s))
        if offset_factory is None:
            from confluent_kafka import TopicPartition as offset_factory
        self.offset_factory = offset_factory
        self.reader = (sqlite3.connect(str(state.path), check_same_thread=False, isolation_level=None)
                       if state.path is not None else None)
        self.q: queue.Queue = queue.Queue(maxsize=max(1, in_flight))
        self.inflight: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.results: dict[str, dict] = {}
        self.error = None
        self.last_journal_ms = 0.0
        self.stats = {"batches": 0, "events": 0, "duplicates": 0, "commits": 0, "max_batch": 0,
                      "score_ms": 0.0, "journal_ms": 0.0, "produce_ms": 0.0}
        self.writer = threading.Thread(target=self._write_loop, name="journal-writer", daemon=True)
        self.writer.start()

    def handle(self, messages) -> int:
        if self.error is not None:
            raise RuntimeError("journal writer failed") from self.error
        if not messages:
            return 0
        decoded = [decode_event(m.value()) for m in messages]
        order = sorted(range(len(decoded)), key=lambda i: (decoded[i][0]["ts"], decoded[i][0]["event_id"]))
        events = [journal_event(decoded[i][0]) for i in order]
        sent = [decoded[i][1] for i in order]
        ids = [ev["event_id"] for ev in events]
        with self.lock:                                   # 1) in-flight first ...
            known = {i: self.inflight[i] for i in ids if i in self.inflight}
        rest = [ev for ev in events if ev["event_id"] not in known]
        if rest:                                          # 2) ... then what is durable
            known.update(self.state.lookup_many("VERDICT", rest, db=self.reader))
        fresh, seen = [], set(known)
        for ev in events:
            if ev["event_id"] not in seen:                # also drops a duplicate inside the batch
                seen.add(ev["event_id"])
                fresh.append(ev)
        t1 = time.perf_counter()
        scored = dict(zip((ev["event_id"] for ev in fresh), self.engine.process(fresh))) if fresh else {}
        score_ms = (time.perf_counter() - t1) * 1000
        with self.lock:
            self.inflight.update(scored)
        for i in ids:
            self.results[i] = scored.get(i) or known.get(i) or {}
        if isinstance(self.batcher, AdaptiveBatcher):
            self.batcher.observe(len(events), score_ms + self.last_journal_ms)
        self.stats["batches"] += 1
        self.stats["events"] += len(events)
        self.stats["duplicates"] += len(events) - len(fresh)
        self.stats["max_batch"] = max(self.stats["max_batch"], len(events))
        self.stats["score_ms"] += score_ms
        item = (events, sent, fresh, scored, known, _offsets_of(messages))
        while True:                       # blocks while N-1 is in flight, but never on a dead writer
            try:
                self.q.put(item, timeout=0.1)
                break
            except queue.Full:
                if self.error is not None or not self.writer.is_alive():
                    raise RuntimeError("journal writer failed") from self.error
        return len(events)

    def _write_loop(self) -> None:
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                events, sent, fresh, scored, known, offsets = item
                a = time.perf_counter()
                seqs = {}
                if fresh:
                    with self.state.transaction():         # ONE commit = one fsync (synchronous=FULL)
                        seqs = dict(zip((ev["event_id"] for ev in fresh),
                                        self.state.record_many("VERDICT", fresh, [scored[ev["event_id"]] for ev in fresh])))
                b = time.perf_counter()
                for ev, s_at in zip(events, sent):          # verdicts are published only once durable
                    eid = ev["event_id"]
                    key, value = verdict_message(ev, scored.get(eid) or known.get(eid) or {}, s_at, seqs.get(eid, -1))
                    self.producer.produce(self.out_topic, key=key, value=value)
                self.producer.poll(0)
                self.consumer.commit(offsets=[self.offset_factory(t, p, o) for (t, p), o in sorted(offsets.items())],
                                     asynchronous=False)
                with self.lock:
                    for ev in fresh:
                        self.inflight.pop(ev["event_id"], None)
                c = time.perf_counter()
                self.last_journal_ms = (b - a) * 1000
                self.stats["commits"] += 1
                self.stats["journal_ms"] += (b - a) * 1000
                self.stats["produce_ms"] += (c - b) * 1000
            except BaseException as exc:      # noqa: BLE001 - surfaced to the consumer loop by handle()/run()
                self.error = exc
                return
            finally:
                self.q.task_done()

    def run(self, total: int, idle_timeout_s: float = 30.0) -> None:
        seen, last = 0, time.monotonic()
        try:
            while seen < total:
                n = self.handle(self.batcher.next_batch(idle_timeout_s=0.5))
                seen += n
                if n:
                    last = time.monotonic()
                elif time.monotonic() - last > idle_timeout_s:
                    raise TimeoutError(f"no input for {idle_timeout_s}s after {seen}/{total} events")
            self.q.join()
        finally:
            if self.writer.is_alive():
                self.q.put(None)
                self.writer.join(timeout=30)
            if self.reader is not None:
                self.reader.close()
        if self.error is not None:
            raise RuntimeError("journal writer failed") from self.error
        self.producer.flush(30)


# --------------------------------------------------------------------------- sink (end-to-end latency)
def percentiles(values, qs=(50, 95, 99)) -> dict:
    v = sorted(values)
    if not v:
        return {f"p{q}": None for q in qs}
    return {f"p{q}": round(v[min(len(v) - 1, max(0, math.ceil(q / 100 * len(v)) - 1))], 3) for q in qs}


class VerdictSink:
    """Reads fraud.verdicts; latency = time the verdict is RECEIVED here - time the event was SENT."""

    def __init__(self, consumer, clock=time.time):
        self.consumer, self.clock = consumer, clock
        self.latency_ms: list[float] = []
        self.decisions: dict[str, str] = {}

    def run(self, total: int, timeout_s: float = 300.0) -> None:
        end = time.monotonic() + timeout_s
        while len(self.decisions) < total and time.monotonic() < end:
            # One blocking wait for the first verdict, then a non-blocking drain: consume(5000, 0.2) held
            # every verdict until 5000 had arrived or 200 ms passed, inflating the measured latency.
            got = self.consumer.consume(num_messages=1, timeout=0.2)
            if got:
                got += self.consumer.consume(num_messages=5000, timeout=0)
            for m in got:
                if m.error() is not None:
                    continue
                now = self.clock()
                v = json.loads(m.value())
                if v["event_id"] in self.decisions:
                    continue              # at-least-once: a replayed verdict is not a second sample
                self.decisions[v["event_id"]] = v["decision"]
                self.latency_ms.append((now - v[SENT_AT]) * 1000)


# --------------------------------------------------------------------------- producer
def produce_paced(producer, events, rate: float, topic: str = IN_TOPIC, clock=time.time, sleep=time.sleep) -> dict:
    """Send events at `rate` per second (absolute schedule, so a slow send does not lower the rate)."""
    if rate <= 0 or not math.isfinite(rate):
        raise ValueError("rate must be a positive number")
    start = clock()
    late = 0
    for i, ev in enumerate(events):
        due = start + i / rate
        wait = due - clock()
        if wait > 0:
            sleep(wait)
        elif wait < -0.005:
            late += 1
        key, value = encode_event(ev, clock())
        while True:
            try:
                producer.produce(topic, key=key, value=value)
                break
            except BufferError:           # local queue full: let librdkafka drain, then retry
                producer.poll(0.01)
        if i % 500 == 0:
            producer.poll(0)
    producer.flush(60)
    elapsed = clock() - start
    return {"sent": len(events), "seconds": round(elapsed, 3), "achieved_rate": round(len(events) / max(elapsed, 1e-9)),
            "late_sends": late}


# --------------------------------------------------------------------------- bench (container only)
def _bundle_env():
    """Same bundle selection as evals/run_evals._bundle(), without depending on evals/ (not in the image)."""
    bundle = ROOT / "runtime" / "artifacts_v22"
    os.environ.setdefault("FRAUD_MODEL_DIR", str(bundle / "models"))
    os.environ.setdefault("FRAUD_DATA_DIR", str(bundle / "data"))
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ.setdefault(name, "1")


def _kafka_conf(bootstrap):
    return {"bootstrap.servers": bootstrap}


def _create_topics(bootstrap, partitions):
    from confluent_kafka.admin import AdminClient, NewTopic
    admin = AdminClient(_kafka_conf(bootstrap))
    for t in (IN_TOPIC, OUT_TOPIC):
        if t in admin.list_topics(timeout=10).topics:
            for f in admin.delete_topics([t]).values():
                f.result()
            time.sleep(2)
    futures = admin.create_topics([NewTopic(IN_TOPIC, partitions, 1), NewTopic(OUT_TOPIC, partitions, 1)])
    for f in futures.values():
        f.result()


def _producer_proc(bootstrap, events, rate, out):
    sys.path.insert(0, str(ROOT / "src"))
    from confluent_kafka import Producer
    p = Producer({**_kafka_conf(bootstrap), "linger.ms": 1, "acks": "all", "enable.idempotence": True})
    out.put(produce_paced(p, events, rate))


def bench(args) -> dict:
    import multiprocessing as mp

    from confluent_kafka import Consumer, Producer
    _bundle_env()
    import evaluate
    from backend_state import BackendState
    from config import DATA_DIR
    from events_io import load_events
    from scaleout import ShardedEngine

    events = load_events(DATA_DIR / "test_events_50k.csv")[: args.events]
    warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    _create_topics(args.bootstrap, args.partitions)
    state_path = Path(args.state_dir) / f"journal_p{args.partitions}_{int(time.time())}.sqlite3"
    state = BackendState(state_path)
    group = f"fraud-{args.partitions}-{int(time.time())}"
    consumer = Consumer({**_kafka_conf(args.bootstrap), "group.id": group, "enable.auto.commit": False,
                         "auto.offset.reset": "earliest", "fetch.wait.max.ms": 1})
    consumer.subscribe([IN_TOPIC])
    sink_consumer = Consumer({**_kafka_conf(args.bootstrap), "group.id": group + "-sink",
                              "auto.offset.reset": "earliest", "fetch.wait.max.ms": 1})
    sink_consumer.subscribe([OUT_TOPIC])
    out_producer = Producer({**_kafka_conf(args.bootstrap), "linger.ms": 1, "acks": "all", "enable.idempotence": True})
    lag_samples = []
    with ShardedEngine(args.shards, warm) as engine:
        if args.pipeline == "pipelined":
            pipe = PipelinedVerdictPipeline(consumer, out_producer, engine, state, max_batch=args.batch,
                                            max_wait_s=args.wait_ms / 1000, budget_ms=args.budget_ms,
                                            adaptive=not args.fixed_batch)
        else:
            pipe = VerdictPipeline(consumer, out_producer, engine, state, max_batch=args.batch, max_wait_s=args.wait_ms / 1000)
        sink = VerdictSink(sink_consumer)
        # Wait for both consumer groups to own their partitions before the clock starts.
        for c in (consumer, sink_consumer):
            c.consume(num_messages=1, timeout=5)
        q = mp.get_context("spawn").Queue()
        prod = mp.get_context("spawn").Process(target=_producer_proc, args=(args.bootstrap, events, args.rate, q))
        sink_thread = threading.Thread(target=sink.run, args=(len(events),), daemon=True)
        stop = threading.Event()

        def lag_sampler():
            from confluent_kafka import TopicPartition
            while not stop.wait(0.5):
                try:
                    lag = 0
                    for tp in consumer.assignment():
                        low, high = consumer.get_watermark_offsets(tp, timeout=1, cached=True)
                        pos = consumer.position([TopicPartition(tp.topic, tp.partition)])[0].offset
                        lag += max(0, high - pos) if pos >= 0 else max(0, high - low)
                    lag_samples.append(lag)
                except Exception:      # noqa: BLE001 - sampling must never break the run
                    pass
        lag_thread = threading.Thread(target=lag_sampler, daemon=True)
        sink_thread.start()
        lag_thread.start()
        t0 = time.perf_counter()
        prod.start()
        pipe.run(len(events))
        wall = time.perf_counter() - t0
        sink_thread.join(timeout=120)
        stop.set()
        produced = q.get(timeout=60)
        prod.join(timeout=30)
    state.close()
    consumer.close()
    sink_consumer.close()
    results = [pipe.results[ev["event_id"]] for ev in events]
    m = evaluate.metrics(events, results)
    row = {
        "partitions": args.partitions, "shards": args.shards, "target_rate": args.rate, "events": len(events),
        "pipeline": args.pipeline, "max_batch": args.batch, "wait_ms": args.wait_ms,
        "budget_ms": args.budget_ms if args.pipeline == "pipelined" and not args.fixed_batch else None,
        "producer": produced, "pipeline_throughput_eps": round(len(events) / wall),
        "e2e_latency_ms": percentiles(sink.latency_ms), "e2e_samples": len(sink.latency_ms),
        "batches": pipe.stats["batches"], "durable_commits": pipe.stats["commits"],
        "mean_batch": round(pipe.stats["events"] / max(pipe.stats["batches"], 1), 1), "largest_batch": pipe.stats["max_batch"],
        "journal_ms_per_batch": round(pipe.stats["journal_ms"] / max(pipe.stats["batches"], 1), 2),
        "score_ms_per_batch": round(pipe.stats["score_ms"] / max(pipe.stats["batches"], 1), 2),
        "consumer_lag_max": max(lag_samples) if lag_samples else None,
        "fail_open": sum(1 for r in results if r.get("fail_open")),
        "journal_synchronous": state.synchronous,
        "quality": {"block_recall_pct": m["block"]["recall_pct"], "block_fpr_pct": m["block"]["fpr_pct"],
                    "any_recall_pct": m["block_or_flag"]["recall_pct"], "any_fpr_pct": m["block_or_flag"]["fpr_pct"]},
    }
    if args.reference:
        ref = json.loads(Path(args.reference).read_text(encoding="utf-8"))
        same = sum(1 for ev, r in zip(events, results) if ref["decisions"].get(ev["event_id"]) == r.get("decision"))
        row["decisions_equal_in_process_pct"] = round(100 * same / len(events), 3)
        row["quality_equal_in_process"] = row["quality"] == ref["quality"]
    return row


def reference(args) -> dict:
    """In-process ShardedEngine replay of the same events (no Kafka): the equivalence baseline."""
    _bundle_env()
    import evaluate
    from config import DATA_DIR
    from events_io import load_events
    from scaleout import ShardedEngine
    events = load_events(DATA_DIR / "test_events_50k.csv")[: args.events]
    warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    with ShardedEngine(args.shards, warm) as engine:
        results = []
        for i in range(0, len(events), 64):
            results += engine.process(events[i:i + 64])
    m = evaluate.metrics(events, results)
    return {"decisions": {ev["event_id"]: r.get("decision") for ev, r in zip(events, results)},
            "quality": {"block_recall_pct": m["block"]["recall_pct"], "block_fpr_pct": m["block"]["fpr_pct"],
                        "any_recall_pct": m["block_or_flag"]["recall_pct"], "any_fpr_pct": m["block_or_flag"]["fpr_pct"]}}


def main(argv=None) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["bench", "reference"])
    ap.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092"))
    ap.add_argument("--partitions", type=int, default=1)
    ap.add_argument("--shards", type=int, default=None, help="scoring processes (default = partitions)")
    ap.add_argument("--rate", type=float, default=2000.0)
    ap.add_argument("--events", type=int, default=50_000)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--wait-ms", type=float, default=5.0)
    ap.add_argument("--state-dir", default=os.environ.get("KAFKA_DEMO_STATE", "/srv/state"))
    ap.add_argument("--reference", help="JSON written by the 'reference' mode")
    ap.add_argument("--pipeline", choices=["sequential", "pipelined"], default="pipelined",
                    help="sequential = the first demo (score, journal, publish, commit in turn)")
    ap.add_argument("--budget-ms", type=float, default=10.0, help="A3 latency budget per batch")
    ap.add_argument("--fixed-batch", action="store_true", help="pipelined, but without the adaptive cap")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    args.shards = args.shards or args.partitions
    row = bench(args) if args.mode == "bench" else reference(args)
    text = json.dumps(row, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text if args.mode == "bench" else json.dumps({"quality": row["quality"]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main())
