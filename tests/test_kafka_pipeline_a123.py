"""A1-A3 of the Kafka demo: pipelining, compact journal, adaptive batch (src/kafka_pipeline.py).

Each test fails on the first (sequential) demo. No broker needed.
"""

import threading
import time

import pytest

import kafka_pipeline as kp
from backend_state import BackendState
from test_kafka_pipeline import FakeConsumer, FakeEngine, FakeProducer, Msg, count_commits, ev


class KMsg(Msg):
    def __init__(self, key, value, offset, partition=0, topic=kp.IN_TOPIC):
        super().__init__(key, value)
        self._o, self._p, self._t = offset, partition, topic

    def offset(self):
        return self._o

    def partition(self):
        return self._p

    def topic(self):
        return self._t


class OffsetConsumer(FakeConsumer):
    def __init__(self, messages, log=None):
        super().__init__(messages, log)
        self.committed = []

    def commit(self, offsets=None, asynchronous=True):
        self.committed.append(sorted(offsets))
        self.log.append("offset-commit")


class LoggingProducer(FakeProducer):
    def __init__(self, log):
        super().__init__()
        self.log = log

    def produce(self, topic, key=None, value=None):
        self.log.append("verdict")
        super().produce(topic, key, value)


def kmsgs(events, partitions=2, sent_at=1.0):
    out, nxt = [], {}
    for e in events:
        p = int(e["caller"][-1]) % partitions
        out.append(KMsg(*kp.encode_event(e, sent_at), offset=nxt.setdefault(p, 0), partition=p))
        nxt[p] += 1
    return out


def pipelined(consumer, producer, engine, state, **kw):
    kw.setdefault("adaptive", False)
    return kp.PipelinedVerdictPipeline(
        consumer, producer, engine, state, max_wait_s=0, offset_factory=lambda t, p, o: (t, p, o), **kw
    )


def test_pipelined_publishes_after_the_journal_and_commits_each_batch_offsets_in_order(tmp_path):
    log = []
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    count_commits(state, log)
    consumer = OffsetConsumer(kmsgs([ev(i) for i in range(30)]), log)
    pipe = pipelined(consumer, LoggingProducer(log), FakeEngine(), state, max_batch=10)
    pipe.run(30, idle_timeout_s=1)
    assert [x for x in log if x != "verdict"] == ["sql-commit", "offset-commit"] * 3
    assert log.index("sql-commit") < log.index("verdict") < log.index("offset-commit")
    # exactly each batch's offsets (next offset per partition), in order: never a later, non-durable batch
    assert consumer.committed[0] == [(kp.IN_TOPIC, 0, 5), (kp.IN_TOPIC, 1, 5)]
    assert consumer.committed[-1] == [(kp.IN_TOPIC, 0, 15), (kp.IN_TOPIC, 1, 15)]
    state.close()


def test_journal_writes_each_event_once_without_simulator_labels(tmp_path):
    sql = []
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    state.db.set_trace_callback(sql.append)
    events = [dict(ev(i), label=1, fraud_type="SIM_BOX", actor="SB") for i in range(12)]
    pipelined(OffsetConsumer(kmsgs(events)), FakeProducer(), FakeEngine(), state, max_batch=50).run(
        12, idle_timeout_s=1
    )
    assert not any(s.strip().upper().startswith("UPDATE EVENTS SET RESULT") for s in sql)  # was one per event
    payloads = [row[0] for row in state.db.execute("SELECT payload FROM events").fetchall()]
    assert len(payloads) == 12
    assert all("label" not in p and "fraud_type" not in p for p in payloads)
    assert all(r is not None for (r,) in state.db.execute("SELECT result FROM events").fetchall())
    state.close()


def test_next_batch_is_scored_while_the_previous_one_is_being_fsynced(tmp_path):
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    marks = {}
    real_tx = state.transaction

    class SlowTx:
        def __enter__(self):
            self.inner = real_tx()
            return self.inner.__enter__()

        def __exit__(self, *exc):
            time.sleep(0.3)  # a slow fsync
            out = self.inner.__exit__(*exc)
            marks.setdefault("first_commit_done", time.perf_counter())
            return out

    state.transaction = SlowTx

    class Timed(FakeEngine):
        def process(self, events):
            if self.calls:
                marks.setdefault("second_score_start", time.perf_counter())
            return super().process(events)

    pipelined(OffsetConsumer(kmsgs([ev(i) for i in range(20)])), FakeProducer(), Timed(), state, max_batch=10).run(
        20, idle_timeout_s=2
    )
    assert marks["second_score_start"] < marks["first_commit_done"]  # real overlap
    state.close()


def test_an_event_replayed_while_its_batch_is_in_flight_is_not_scored_twice(tmp_path):
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    gate = threading.Event()
    real_record = state.record_many

    def held(kind, events, results):
        gate.wait(2)  # batch 1 stays in flight
        return real_record(kind, events, results)

    state.record_many = held
    first = kmsgs([ev(i) for i in range(5)], partitions=1)
    again = kmsgs([ev(i) for i in range(3, 8)], partitions=1)  # E3, E4 redelivered before batch 1 is durable
    engine = FakeEngine()
    timer = threading.Timer(0.3, gate.set)
    timer.start()
    pipe = pipelined(OffsetConsumer(first + again), FakeProducer(), engine, state, max_batch=5)
    pipe.run(10, idle_timeout_s=3)
    scored = sorted(i for call in engine.calls for i in call)
    assert scored == sorted(f"E{i}" for i in range(8)) and pipe.stats["duplicates"] == 2
    state.close()


def test_restart_reads_durable_verdicts_through_the_read_connection(tmp_path):
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    batch = [ev(i) for i in range(6)]
    pipelined(OffsetConsumer(kmsgs(batch)), FakeProducer(), FakeEngine(), state, max_batch=10).run(6, 1)
    engine = FakeEngine()
    again = pipelined(OffsetConsumer(kmsgs(batch)), FakeProducer(), engine, state, max_batch=10)
    again.run(6, 1)
    assert engine.calls == [] and again.stats["duplicates"] == 6
    state.close()


def test_writer_failure_stops_the_consumer_instead_of_committing_offsets(tmp_path):
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)

    def boom(kind, events, results):
        raise OSError("disk full")

    state.record_many = boom
    consumer = OffsetConsumer(kmsgs([ev(i) for i in range(20)]))
    with pytest.raises(RuntimeError, match="journal writer failed"):
        pipelined(consumer, FakeProducer(), FakeEngine(), state, max_batch=5).run(20, idle_timeout_s=1)
    assert consumer.committed == []  # nothing acknowledged that is not durable
    state.close()


def test_adaptive_batch_follows_the_latency_budget_and_grows_under_backlog():
    b = kp.AdaptiveBatcher(FakeConsumer([]), max_batch=1000, budget_ms=10.0, min_batch=16)
    b.observe(20, 20 * 0.1)  # 0.1 ms per event -> 100 events fit 10 ms
    assert b.limit == 100
    b.observe(30, 30 * 1.0)  # much slower: the cap falls (EWMA)
    assert 16 <= b.limit < 100
    before = b.limit
    for _ in range(3):  # three full batches in a row: backlog -> throughput mode
        b.observe(b.limit, b.limit * 1.0)
    assert b.limit > before and b.limit <= 1000
    with pytest.raises(ValueError):
        kp.AdaptiveBatcher(FakeConsumer([]), max_batch=10, min_batch=20)


def test_lookup_many_detects_conflicting_content_and_record_many_returns_real_seqs(tmp_path):
    from backend_state import EventConflict

    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    with state.transaction():
        seqs = state.record_many("VERDICT", [ev(1), ev(2)], [{"decision": "ALLOW"}, {"decision": "FLAG"}])
    assert seqs == [r[0] for r in state.db.execute("SELECT seq FROM events ORDER BY seq").fetchall()]
    assert state.lookup_many("VERDICT", [ev(1), ev(3)]) == {"E1": {"decision": "ALLOW"}}
    with pytest.raises(EventConflict):
        state.lookup_many("VERDICT", [dict(ev(2), callee="84999999999")])
    state.close()


class BlockingConsumer(FakeConsumer):
    """confluent_kafka semantics: consume(n, timeout) blocks until n messages are available OR timeout."""

    def __init__(self, messages, clock):
        super().__init__(messages)
        self.clock = clock

    def consume(self, num_messages=1, timeout=0):
        out = super().consume(num_messages, timeout)
        if len(out) < num_messages:
            self.clock[0] += max(0.0, timeout)  # it waited the whole timeout for the missing ones
        return out


def test_a_lone_message_is_not_held_for_the_idle_timeout():
    clock = [0.0]
    consumer = BlockingConsumer([Msg(*kp.encode_event(ev(1), 0.0))], clock)
    batcher = kp.MicroBatcher(consumer, max_batch=1000, max_wait_s=0.001, clock=lambda: clock[0])
    assert len(batcher.next_batch(idle_timeout_s=0.5)) == 1
    assert clock[0] <= 0.002  # was 0.5 s: consume(1000, 0.5) waited for 999 more


def test_sink_does_not_add_its_own_waiting_to_the_measured_latency():
    clock = [10.0]
    v = kp.json.dumps({"event_id": "A", "decision": "ALLOW", kp.SENT_AT: 10.0}).encode()
    sink = kp.VerdictSink(BlockingConsumer([Msg(b"k", v)], clock), clock=lambda: clock[0])
    sink.run(1, timeout_s=1)
    assert sink.latency_ms == [0.0]  # was 200 ms: consume(5000, 0.2) waited before returning
