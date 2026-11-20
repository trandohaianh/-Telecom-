"""Carrier bridge: mapping, signing, retry/dead-letter policy, Kafka at-least-once, end-to-end into the API."""

import asyncio
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import carrier_bridge as cb  # noqa: E402
import security  # noqa: E402

KEY = bytes.fromhex("ab" * 32)
CALL = {
    "event_type": "CALL",
    "event_id": "B1",
    "ts": 100.0,
    "caller": "84961234567",
    "callee": "84351234567",
    "cell_id": "HN-0001",
    "imei": "35391110000001",
    "origin_trunk": "LOCAL",
    "network": "4G",
}


async def no_sleep(_s):
    await asyncio.sleep(0)


def bridge(handler, tmp_path, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://api")
    return cb.Bridge(
        "http://api", KEY, client=client, dead_letter=cb.DeadLetter(tmp_path / "dlq.jsonl"), sleep=no_sleep, **kw
    )


def dlq(tmp_path):
    path = tmp_path / "dlq.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ---------------------------------------------------------------- pure functions
def test_mapping_renames_fields_and_converts_milliseconds():
    m = cb.normalize_mapping(
        {
            "calling_number": "caller",
            "called_number": "callee",
            "setup_time": "ts",
            "ts_unit": "ms",
            "defaults": {"network": "4G"},
            "drop": ["vendor_seq"],
            "values": {"event_type": {"MOC": "CALL"}},
        }
    )
    out = cb.map_event(
        {
            "event_type": "MOC",
            "event_id": "X",
            "calling_number": "84961234567",
            "called_number": "84351234567",
            "setup_time": 1731830400123,
            "vendor_seq": 7,
        },
        m,
    )
    assert out == {
        "network": "4G",
        "event_type": "CALL",
        "event_id": "X",
        "caller": "84961234567",
        "callee": "84351234567",
        "ts": pytest.approx(1731830400.123),
    }
    nested = cb.normalize_mapping({"fields": {"t": "ts"}, "ts_unit": "ms", "duration_unit": "ms"})
    assert cb.map_event({"t": "5000", "duration_s": 12000}, nested) == {"ts": 5.0, "duration_s": 12.0}
    iso = cb.normalize_mapping({"ts_unit": "iso"})
    assert (
        cb.map_event({"ts": "2026-11-17T07:00:00+07:00"}, iso)["ts"]
        == datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp()
    )
    with pytest.raises(cb.RecordError):
        cb.map_event({"ts": "2026-11-17T07:00:00"}, iso)  # no offset: ambiguous local time
    with pytest.raises(cb.RecordError):
        cb.map_event({"ts": "soon"}, m)
    with pytest.raises(ValueError):
        cb.normalize_mapping({"ts_unit": "minutes"})


def test_kind_inference_and_envelope():
    m = cb.normalize_mapping(None)
    assert cb.prepare(json.dumps(CALL), m)[0] == "VERDICT"
    kind, event, body = cb.prepare({**CALL, "duration_s": 30}, m)
    assert kind == "CDR" and json.loads(body) == event
    kind, event, _ = cb.prepare({"kind": "cdr", "event": {**CALL, "duration_s": 3}}, m)
    assert kind == "CDR" and "kind" not in event
    dur = cb.normalize_mapping({"call_duration": "duration_s"})
    assert cb.prepare({**CALL, "call_duration": 9}, dur)[0] == "CDR"  # inferred after mapping
    for bad in (b"{not json", "[1]", {"kind": "FAX", "event": CALL}, {**CALL, "event_id": ""}):
        with pytest.raises(cb.RecordError):
            cb.prepare(bad, m)


def test_signed_headers_verify_with_server_security_module():
    body = json.dumps(CALL).encode()
    h = cb.signed_headers(body, KEY)
    assert security.verify_network(body, h["x-timestamp"], h["x-signature"], KEY) is None
    assert cb.sign(body, h["x-timestamp"], KEY) == security.sign_network(body, h["x-timestamp"], KEY)
    h2 = cb.signed_headers(body, KEY)
    assert h2["x-timestamp"] != h["x-timestamp"]  # retries are not replays


def test_key_file_formats_and_no_key_in_repr(tmp_path):
    (tmp_path / "hex.key").write_text("ab" * 32 + "\n")
    (tmp_path / "raw.key").write_bytes(KEY)
    (tmp_path / "bad.key").write_text("secret-value")
    assert cb.load_key(tmp_path / "hex.key") == KEY == cb.load_key(tmp_path / "raw.key")
    with pytest.raises(ValueError) as err:
        cb.load_key(tmp_path / "bad.key")
    assert "secret-value" not in str(err.value)
    b = cb.Bridge("http://api", KEY, client=httpx.AsyncClient())
    assert "ab" * 8 not in repr(b)


def test_status_classification():
    assert cb.classify(200, {"decision": "ALLOW"}) == "ok"
    assert cb.classify(202, {"pending": True, "retryable": True}) == "ok"
    assert cb.classify(200, {"retryable": True, "durable": False}) == "retry"  # fail-open overload
    for s in (401, 429, 500, 503):
        assert cb.classify(s) == "retry"
    for s in (400, 404, 409, 413, 422):
        assert cb.classify(s) == "dead"
    assert cb.percentile([5, 1, 3, 2, 4], 0.5) == 3 and cb.percentile([], 0.5) is None


# ---------------------------------------------------------------- delivery policy
def test_retry_on_503_then_success(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers["x-timestamp"])
        assert (
            security.verify_network(
                request.content, request.headers["x-timestamp"], request.headers["x-signature"], KEY
            )
            is None
        )
        if len(seen) < 3:
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"event_id": "B1", "decision": "ALLOW"})

    b = bridge(handler, tmp_path)
    assert asyncio.run(b.deliver(CALL)) == "ok"
    assert len(seen) == 3 and len(set(seen)) == 3
    assert b.metrics.counts["retried"] == 2 and b.metrics.counts["ok"] == 1 and dlq(tmp_path) == []


def test_transport_errors_retry_and_max_retries_dead_letters(tmp_path):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    b = bridge(handler, tmp_path, max_retries=2)
    assert asyncio.run(b.deliver(CALL)) == "dead"
    assert b.metrics.counts["sent"] == 3 and "retries exhausted" in dlq(tmp_path)[0]["reason"]


@pytest.mark.parametrize("status,detail", [(400, "missing fields: ['imei']"), (409, "conflicting duplicate")])
def test_dead_letter_on_400_and_409(tmp_path, status, detail):
    b = bridge(lambda r: httpx.Response(status, json={"detail": detail}), tmp_path)
    assert asyncio.run(b.deliver(json.dumps(CALL))) == "dead"
    [line] = dlq(tmp_path)
    assert line["status"] == status and detail in line["reason"] and json.loads(line["record"]) == CALL
    assert b.metrics.counts["sent"] == 1 and b.metrics.counts["dead_lettered"] == 1


def test_invalid_records_dead_letter_without_calling_api(tmp_path):
    calls = []
    b = bridge(lambda r: calls.append(r) or httpx.Response(200, json={}), tmp_path)
    src = tmp_path / "in.jsonl"
    src.write_text("{oops\n\n" + json.dumps(CALL) + "\n", encoding="utf-8")
    out = io.StringIO()
    assert asyncio.run(b.run(cb.FileSource(str(src)), metrics_every=0, out=out)) == 0
    assert len(calls) == 1 and dlq(tmp_path)[0]["reason"] == "invalid JSON"
    final = json.loads(out.getvalue().splitlines()[-1])
    assert final["ok"] == 1 and final["dead_lettered"] == 1 and final["received"] == 2
    assert "84961234567" not in out.getvalue()  # metrics carry no subscriber numbers


def test_backpressure_bounds_inflight(tmp_path):
    peak, live = [0], [0]

    async def handler(request):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        await asyncio.sleep(0.01)
        live[0] -= 1
        return httpx.Response(200, json={})

    src = tmp_path / "in.jsonl"
    src.write_text("".join(json.dumps({**CALL, "event_id": f"E{i}"}) + "\n" for i in range(40)))
    b = bridge(handler, tmp_path, concurrency=4)
    asyncio.run(b.run(cb.FileSource(str(src)), metrics_every=0, out=io.StringIO()))
    assert b.metrics.counts["ok"] == 40 and 1 < peak[0] <= 4


# ---------------------------------------------------------------- Kafka at-least-once
class FakeMsg:
    def __init__(self, partition, offset, value):
        self._p, self._o, self._v = partition, offset, value

    def error(self):
        return None

    def topic(self):
        return "carrier"

    def partition(self):
        return self._p

    def offset(self):
        return self._o

    def value(self):
        return self._v


class FakeConsumer:
    def __init__(self, messages):
        self.messages, self.commits, self.closed, self.subscribed = list(messages), [], False, None

    def subscribe(self, topics, on_revoke=None):
        self.subscribed = topics

    def poll(self, timeout):
        if self.messages:
            return self.messages.pop(0)
        time.sleep(0.005)
        return None

    def commit(self, offsets, asynchronous=True):
        assert asynchronous is False
        self.commits.extend((tp.partition, tp.offset) for tp in offsets)

    def close(self):
        self.closed = True


def test_kafka_commits_only_contiguous_acknowledged_offsets(tmp_path):
    def ev(i):
        return json.dumps({**CALL, "event_id": f"K{i}"}).encode()

    consumer = FakeConsumer(
        [FakeMsg(0, i, ev(i)) for i in range(3)] + [FakeMsg(0, 3, b"{bad")] + [FakeMsg(1, 10, ev(10))]
    )

    async def scenario():
        gate, done = asyncio.Event(), set()

        async def handler(request):
            eid = json.loads(request.content)["event_id"]
            if eid == "K0":
                await gate.wait()  # the oldest offset is slow
            done.add(eid)
            return httpx.Response(200, json={"event_id": eid})

        b = bridge(handler, tmp_path, concurrency=8)
        source = cb.KafkaSource(consumer, "carrier", poll_s=0.01)
        task = asyncio.create_task(b.run(source, metrics_every=0, out=io.StringIO()))

        async def until(cond):
            for _ in range(400):
                if cond():
                    return
                await asyncio.sleep(0.005)
            raise AssertionError("timeout")

        await until(lambda: {"K1", "K2", "K10"} <= done and dlq(tmp_path))
        await until(lambda: (1, 11) in consumer.commits)
        await asyncio.sleep(0.05)
        assert all(p != 0 for p, _ in consumer.commits)  # K1/K2/bad done but K0 not: no commit
        gate.set()
        await until(lambda: (0, 4) in consumer.commits)
        b.stop.set()
        assert await task == 0

    asyncio.run(scenario())
    assert consumer.subscribed == ["carrier"] and consumer.closed
    assert [c for c in consumer.commits if c[0] == 0] == [(0, 4)]  # one commit past the dead-lettered record
    assert len(dlq(tmp_path)) == 1


def test_offset_tracker_restore_and_revoke():
    t = cb.OffsetTracker()
    for o in (5, 6, 7):
        t.add("t", 0, o)
    t.done("t", 0, 6)
    assert t.take_ready() == {}
    t.done("t", 0, 5)
    ready = t.take_ready()
    assert ready == {("t", 0): 7}
    t.restore(ready)
    assert t.take_ready() == {("t", 0): 7}
    t.revoke([("t", 0)])
    t.done("t", 0, 7)
    assert t.take_ready() == {}


def test_kafka_connect_forces_manual_commit(monkeypatch):
    pytest.importorskip("confluent_kafka")
    import confluent_kafka

    captured = {}
    monkeypatch.setattr(confluent_kafka, "Consumer", lambda conf: captured.update(conf) or FakeConsumer([]))
    cb.KafkaSource.connect("b:9092", "t", "g", extra={"enable.auto.commit": True})
    assert captured["enable.auto.commit"] is False and captured["group.id"] == "g"


# ---------------------------------------------------------------- end to end into the real API app
@pytest.fixture()
def live_api(monkeypatch, tmp_path):
    import server
    from test_backend_runtime import FakeDetector

    engine = server.LiveEngine(
        demo_mode=False,
        state_path=tmp_path / "state.sqlite3",
        evidence_path=tmp_path / "evidence.jsonl",
        detector_factory=lambda: FakeDetector(),
        start_workers=False,
    )
    monkeypatch.setattr(server, "engine", engine)
    monkeypatch.setattr(security, "network_key", lambda: KEY)
    engine.runtime.start()
    try:
        yield server.app
    finally:
        engine.close()


def test_end_to_end_file_feed_through_fastapi_app(live_api, tmp_path):
    mapping = tmp_path / "map.json"
    mapping.write_text(
        json.dumps({"calling_number": "caller", "called_number": "callee", "setup_time": "ts", "ts_unit": "ms"}),
        encoding="utf-8",
    )
    carrier = {
        "event_type": "CALL",
        "event_id": "E2E-1",
        "calling_number": "84961234567",
        "called_number": "84351234567",
        "setup_time": 100000,
        "cell_id": "HN-0001",
        "imei": "35391110000001",
        "origin_trunk": "LOCAL",
        "network": "4G",
    }
    lines = [
        carrier,
        carrier,  # duplicate: API dedups, still ok
        {
            "kind": "CDR",
            "event": {
                "event_type": "CALL",
                "event_id": "E2E-1",
                "calling_number": "84961234567",
                "setup_time": 100000,
                "duration_s": 42,
            },
        },
        {**carrier, "origin_trunk": "INTL"},  # same id, other content -> 409
        {k: v for k, v in {**carrier, "event_id": "E2E-2"}.items() if k != "imei"},
    ]  # 400
    feed = tmp_path / "feed.jsonl"
    feed.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    statuses = []

    async def scenario():
        async def log_response(response):
            statuses.append(response.status_code)

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=live_api), base_url="http://api", event_hooks={"response": [log_response]}
        )
        b = cb.Bridge(
            "http://api",
            KEY,
            client=client,
            mapping=cb.load_mapping(mapping),
            concurrency=1,
            dead_letter=cb.DeadLetter(tmp_path / "dlq.jsonl"),
            sleep=no_sleep,
            max_retries=3,
        )
        code = await b.run(cb.FileSource(str(feed)), metrics_every=0, out=io.StringIO())
        await client.aclose()
        return code, b.metrics.counts

    code, counts = asyncio.run(scenario())
    assert code == 0
    assert statuses[:3] == [200, 200, 200] and statuses[3:] == [409, 400]
    assert counts["ok"] == 3 and counts["duplicates"] == 1 and counts["dead_lettered"] == 2
    reasons = [x["status"] for x in dlq(tmp_path)]
    assert reasons == [409, 400]
