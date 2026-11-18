"""Kafka demo pipeline (src/kafka_pipeline.py) with in-memory fakes: no broker needed.

Checks the properties the demo's measurements rely on: one durable commit per micro-batch, offsets
committed only after the journal, replay deduplication, event-time order inside a batch, end-to-end
latency measured from the producer send time, and that scoring is the existing ShardedEngine.
"""
import json
from pathlib import Path

import pytest
import yaml

import kafka_pipeline as kp
from backend_state import BackendState

ROOT = Path(__file__).resolve().parents[1]


class Msg:
    def __init__(self, key, value, err=None):
        self._k, self._v, self._e = key, value, err

    def key(self):
        return self._k

    def value(self):
        return self._v

    def error(self):
        return self._e


class FakeConsumer:
    def __init__(self, messages, log=None):
        self.queue, self.log, self.commits = list(messages), log if log is not None else [], 0

    def consume(self, num_messages=1, timeout=0):
        out, self.queue = self.queue[:num_messages], self.queue[num_messages:]
        return out

    def commit(self, asynchronous=True):
        self.commits += 1
        self.log.append("offset-commit")


class FakeProducer:
    def __init__(self):
        self.sent = []

    def produce(self, topic, key=None, value=None):
        self.sent.append((topic, key, value))

    def poll(self, t=0):
        return 0

    def flush(self, t=0):
        return 0


class FakeEngine:
    """Stands in for ShardedEngine: same process(list) -> list contract."""

    def __init__(self):
        self.calls = []

    def process(self, events):
        self.calls.append([e["event_id"] for e in events])
        return [{"decision": "FLAG" if e["caller"].endswith("9") else "ALLOW", "risk": 0.5, "scored": True,
                 "reasons": [{"code": "R101"}]} for e in events]


def ev(i, ts=None, caller=None):
    return {"event_id": f"E{i}", "event_type": "CALL", "ts": float(ts if ts is not None else 1000 + i),
            "caller": caller or f"84900000{i % 10}", "callee": "84911111111", "cell_id": "HN-0001",
            "imei": "353911100000001", "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": 300}


def msgs(events, sent_at=1.0):
    return [Msg(*kp.encode_event(e, sent_at)) for e in events]


def count_commits(state, log):
    state.db.set_trace_callback(lambda sql: log.append("sql-commit") if sql.strip().upper() == "COMMIT" else None)


def test_message_contract_key_is_the_caller_and_send_time_round_trips():
    e = ev(1)
    key, value = kp.encode_event(e, 123.5)
    assert key == e["caller"].encode()
    back, sent = kp.decode_event(value)
    assert back == e and sent == 123.5
    with pytest.raises(ValueError):
        kp.decode_event(b'{"no_id": 1}')


def test_one_durable_commit_per_micro_batch_then_offsets(tmp_path):
    log = []
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    count_commits(state, log)
    consumer, producer, engine = FakeConsumer(msgs([ev(i) for i in range(250)]), log), FakeProducer(), FakeEngine()
    pipe = kp.VerdictPipeline(consumer, producer, engine, state, max_batch=100, max_wait_s=0)
    pipe.run(250, idle_timeout_s=1)
    assert pipe.stats["batches"] == 3 and log.count("sql-commit") == 3          # 100 + 100 + 50, one fsync each
    assert log == ["sql-commit", "offset-commit"] * 3                            # offsets only after the journal
    assert len(producer.sent) == 250 and all(t == kp.OUT_TOPIC for t, _, _ in producer.sent)
    v = json.loads(producer.sent[0][2])
    assert v[kp.SENT_AT] == 1.0 and v["decision"] in ("ALLOW", "FLAG") and v["journal_seq"] >= 1
    assert state.synchronous == "FULL"
    state.close()


def test_redelivered_messages_are_not_scored_twice(tmp_path):
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    batch = msgs([ev(i) for i in range(20)])
    engine = FakeEngine()
    pipe = kp.VerdictPipeline(FakeConsumer(batch), FakeProducer(), engine, state, max_batch=50, max_wait_s=0)
    pipe.run(20, idle_timeout_s=1)
    first = dict(pipe.results)
    producer = FakeProducer()
    again = kp.VerdictPipeline(FakeConsumer(batch), producer, engine, state, max_batch=50, max_wait_s=0)
    again.run(20, idle_timeout_s=1)              # at-least-once redelivery after a crash before the offset commit
    assert len(engine.calls) == 1 and again.stats["duplicates"] == 20
    assert {k: v["decision"] for k, v in again.results.items()} == {k: v["decision"] for k, v in first.items()}
    assert len(producer.sent) == 20               # verdicts are re-published from the journal
    state.close()


def test_batch_is_scored_in_event_time_order(tmp_path):
    state = BackendState(tmp_path / "j.sqlite3", background_checkpoint=False)
    events = [ev(1, ts=30), ev(2, ts=10), ev(3, ts=20)]       # interleaved partitions
    engine = FakeEngine()
    kp.VerdictPipeline(FakeConsumer(msgs(events)), FakeProducer(), engine, state, max_batch=10, max_wait_s=0).run(3, 1)
    assert engine.calls == [["E2", "E3", "E1"]]
    state.close()


def test_micro_batcher_waits_after_the_first_message_but_not_forever():
    now = [0.0]
    consumer = FakeConsumer(msgs([ev(i) for i in range(5)]))
    batcher = kp.MicroBatcher(consumer, max_batch=3, max_wait_s=0.01, clock=lambda: now[0])
    assert len(batcher.next_batch()) == 3
    assert len(batcher.next_batch()) == 2
    assert batcher.next_batch() == []
    bad = FakeConsumer([Msg(b"k", b"v", err="broker error")] + msgs([ev(9)]))
    assert len(kp.MicroBatcher(bad, max_batch=5, max_wait_s=0).next_batch()) == 1   # errors are not events
    with pytest.raises(ValueError):
        kp.MicroBatcher(consumer, max_batch=0)


def test_sink_measures_end_to_end_latency_and_ignores_replayed_verdicts():
    vals = [json.dumps({"event_id": "A", "decision": "ALLOW", kp.SENT_AT: 10.000}).encode(),
            json.dumps({"event_id": "B", "decision": "FLAG", kp.SENT_AT: 10.010}).encode(),
            json.dumps({"event_id": "A", "decision": "ALLOW", kp.SENT_AT: 10.000}).encode()]
    sink = kp.VerdictSink(FakeConsumer([Msg(b"k", v) for v in vals]), clock=lambda: 10.030)
    sink.run(2, timeout_s=1)
    assert sink.decisions == {"A": "ALLOW", "B": "FLAG"} and [round(x, 3) for x in sink.latency_ms] == [30.0, 20.0]
    assert kp.percentiles([1, 2, 3, 4, 100])["p99"] == 100 and kp.percentiles([])["p50"] is None


def test_paced_producer_keeps_an_absolute_schedule_and_retries_a_full_queue():
    t = [0.0]

    class Slow(FakeProducer):
        def __init__(self):
            super().__init__()
            self.full = 1

        def produce(self, topic, key=None, value=None):
            if self.full:
                self.full -= 1
                raise BufferError("queue full")
            t[0] += 0.0001
            super().produce(topic, key, value)
    p = Slow()
    out = kp.produce_paced(p, [ev(i) for i in range(100)], rate=1000, clock=lambda: t[0],
                           sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert out["sent"] == 100 and len(p.sent) == 100 and 900 <= out["achieved_rate"] <= 1100
    sent_times = [json.loads(v)[kp.SENT_AT] for _, _, v in p.sent]
    assert sent_times == sorted(sent_times) and sent_times[-1] >= 0.099
    with pytest.raises(ValueError):
        kp.produce_paced(p, [], rate=0)


def test_scoring_reuses_the_existing_sharded_engine_not_a_new_detector():
    src = (ROOT / "src" / "kafka_pipeline.py").read_text(encoding="utf-8")
    assert "from scaleout import ShardedEngine" in src and "Detector(" not in src
    assert "from scaleout import partition_key" in src


def test_demo_stack_is_separate_pinned_and_loopback_only():
    compose = yaml.safe_load((ROOT / "deploy" / "kafka-demo" / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["name"] == "telecom-kafka-demo"
    k = compose["services"]["kafka"]
    assert k["image"] == "apache/kafka:3.8.0" and all(p.startswith("127.0.0.1:") for p in k["ports"])
    assert compose["services"]["demo"]["environment"]["FRAUD_SQLITE_SYNCHRONOUS"] == "FULL"
    assert "confluent-kafka==" in (ROOT / "deploy" / "kafka-demo" / "requirements-kafka.txt").read_text(encoding="utf-8")


def test_shard_workers_warm_models_before_reporting_ready():
    src = (ROOT / "src" / "scaleout.py").read_text(encoding="utf-8")
    worker = src.split("def _worker(", 1)[1].split("\ndef ", 1)[0].split("\nclass ", 1)[0]
    assert worker.index("det.warm_up()") < worker.index('conn.send(("ready", index))')
