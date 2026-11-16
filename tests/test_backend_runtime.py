"""Regressions for live isolation, durable RPC identity and bounded inference deadlines."""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import security
from backend_evidence import DurableEvidenceLog
from backend_runtime import InferenceRuntime, validate_event
from backend_state import BackendState, EventConflict
from commercial_registry import CommercialRegistry
from evidence import verify
from resilience import CircuitBreaker, GuardedDetector


def call(eid="C1", ts=100.0):
    return {"event_id": eid, "event_type": "CALL", "ts": ts, "caller": "84900000001",
            "callee": "84900000002", "cell_id": "HN-0001", "imei": "35391110000001",
            "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": -1}


class MemoryFeatures:
    def __init__(self):
        self.calls = 0
        self.cdrs = 0

    def state_dict(self):
        return {"calls": self.calls, "cdrs": self.cdrs}

    def load_state_dict(self, data):
        self.calls, self.cdrs = data["calls"], data["cdrs"]

    def call_features(self, ev):
        self.calls += 1
        return {"calls_seen": self.calls}

    sms_features = call_features
    audio_features = call_features

    def commit_cdr(self, ev):
        self.cdrs += 1


class FakeDetector:
    def __init__(self, gate=None, started=None, block=True):
        self.fs = MemoryFeatures()
        self.harvest_hits = {}
        self.gate, self.started, self.block = gate, started, block

    def warm_up(self):
        return self

    def score_batch(self, events):
        if self.started:
            self.started.set()
        if self.gate:
            self.gate.wait(timeout=2)
        out = []
        for ev in events:
            self.fs.calls += 1
            out.append({"event_id": ev["event_id"], "event_type": ev["event_type"], "ts": ev["ts"],
                        "party": ev.get("caller"), "counterparty": ev.get("callee"),
                        "decision": "BLOCK" if self.block else "ALLOW", "risk": 0.9,
                        "reasons": [{"code": "R103", "vi": "", "en": "", "feature": "calls_1h"}],
                        "evidence_features": {"calls_1h": 10}, "action_vi": "", "action_en": ""})
        return out


def make_runtime(tmp_path, detector=None, **kwargs):
    state = BackendState(tmp_path / "state.sqlite3")
    detector = detector or FakeDetector()
    guarded = GuardedDetector(detector, budget_ms=1000)
    runtime = InferenceRuntime(guarded, state=state, **kwargs)
    return runtime, state, detector


def test_duplicate_setup_cdr_and_restart_keep_exactly_one_state_update(tmp_path):
    runtime, state, detector = make_runtime(tmp_path)
    event = call()
    cdr = {"event_id": event["event_id"], "event_type": "CALL", "ts": 100.0,
           "caller": event["caller"], "duration_s": 12.0}
    try:
        first = asyncio.run(runtime.submit("VERDICT", event, budget_ms=500))
        repeated = asyncio.run(runtime.submit("VERDICT", event, budget_ms=500))
        assert first["decision"] == repeated["decision"] == "BLOCK"
        assert repeated["duplicate"] and detector.fs.calls == 1
        assert asyncio.run(runtime.submit("CDR", cdr, budget_ms=500))["ok"]
        assert asyncio.run(runtime.submit("CDR", cdr, budget_ms=500))["duplicate"]
        assert detector.fs.cdrs == 1
        with pytest.raises(EventConflict):
            asyncio.run(runtime.submit("VERDICT", dict(event, callee="84999999999"), budget_ms=5000))
    finally:
        runtime.close()
        state.close()
    runtime, state, restored = make_runtime(tmp_path)
    try:
        assert restored.fs.calls == restored.fs.cdrs == 1
        assert asyncio.run(runtime.submit("VERDICT", event, budget_ms=500))["duplicate"]
        assert restored.fs.calls == 1
    finally:
        runtime.close()
        state.close()


def test_deadline_queue_bound_and_event_loop_responsiveness(tmp_path):
    gate, started = threading.Event(), threading.Event()
    runtime, state, detector = make_runtime(tmp_path, FakeDetector(gate=gate, started=started), capacity=1)
    try:
        async def exercise():
            start = time.monotonic()
            one = asyncio.create_task(runtime.submit("VERDICT", call("A", 100), budget_ms=25))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.002)
            assert started.is_set()
            two = asyncio.create_task(runtime.submit("VERDICT", call("B", 101), budget_ms=25))
            for _ in range(100):
                if runtime.queue.qsize() == 1:
                    break
                await asyncio.sleep(0.002)
            three = await runtime.submit("VERDICT", call("C", 102), budget_ms=200)
            first, second = await asyncio.gather(one, two)
            assert first["fail_open"] and first["pending"]
            assert second["fail_open"] and second["pending"]
            assert three["ingest"] == "OVERLOAD" and three["fail_open"]
            assert runtime.queue.maxsize == 1 and runtime.queue.qsize() <= 1
            # Inference is still waiting on gate; HTTP deadlines and this coroutine ran independently.
            assert not gate.is_set() and time.monotonic() - start < 0.5
        asyncio.run(exercise())
    finally:
        gate.set()
        runtime.close()
        state.close()
    assert detector.fs.calls == 2


def test_late_registered_commercial_call_never_learns_a_fraud_denylist():
    detector = FakeDetector()
    registry = CommercialRegistry()
    registry.register("84900000001", "Registered enterprise", 100)
    guarded = GuardedDetector(detector, budget_ms=1, registry=registry,
                              breaker=CircuitBreaker(failure_threshold=1, cooldown_s=60))
    guarded.chaos_delay_ms = 10
    result = guarded.score_batch([call()])[0]
    assert result["fail_open"] and result["decision"] == "ALLOW"
    assert not guarded.retro and not guarded.fallback.deny
    assert guarded.score_batch([call("C2", 101)])[0]["decision"] == "ALLOW"


def test_retrospective_policy_and_features_do_not_rewind_live_state():
    detector = FakeDetector()
    registry = CommercialRegistry()
    registry.register("84900000001", "Registered enterprise", 100)
    guarded = GuardedDetector(detector, budget_ms=1000, registry=registry)
    guarded.chaos_error = True
    guarded.score_batch([call()])  # queued while the model did not see the event
    detector.fs.calls = 12        # live state has since advanced
    guarded.chaos_error = False
    assert guarded.drain_rescore() == []
    assert detector.fs.calls == 12 and not guarded.fallback.deny


def test_single_live_owner_and_pending_journal_recovery(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    state.reserve("VERDICT", call())
    state.close()
    runtime, state, detector = make_runtime(tmp_path)
    other = BackendState(tmp_path / "state.sqlite3")
    try:
        assert detector.fs.calls == 1
        assert asyncio.run(runtime.submit("VERDICT", call(), budget_ms=500))["duplicate"]
        with pytest.raises(RuntimeError, match="inference owner"):
            other.acquire_owner()
    finally:
        other.close()
        runtime.close()
        state.close()


def test_sessions_suspension_and_human_policy_survive_reopen(tmp_path, monkeypatch):
    users = {"ana": {"role": "analyst", "token": "a", "sign_key": "11" * 32},
             "boss": {"role": "admin", "token": "b", "sign_key": "22" * 32}}
    monkeypatch.setattr(security, "_users_cache", users)
    saved_suspensions = set(security.suspended)
    runtime, state, _ = make_runtime(tmp_path)
    try:
        security.configure_store(state)
        sid, *_ = security.login("b")
        security.suspend("ana")
        runtime.g.fallback.block_manual(["84900000009"], "approved-proposal")
        runtime.close()
        security.configure_store(None)
        state.close()
        runtime, state, _ = make_runtime(tmp_path)
        security.configure_store(state)
        assert security.session_user(sid)["name"] == "boss"
        assert "ana" in security.suspended and "ana" in state.suspended_users()
        assert runtime.g.fallback.manual["84900000009"] == "approved-proposal"
    finally:
        security.configure_store(None)
        security.suspended.clear()
        security.suspended.update(saved_suspensions)
        runtime.close()
        state.close()


def test_durable_evidence_drains_outbox_once_and_preserves_existing_chain(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    path = tmp_path / "evidence.jsonl"
    log = DurableEvidenceLog(path, state)
    seq, _ = state.reserve("VERDICT", call())
    state.finish(seq, {"event_id": "C1", "decision": "BLOCK"})
    log.close()
    original = path.read_bytes()
    assert verify(path) == (True, 1, None)
    assert not state.pending_evidence()
    log = DurableEvidenceLog(path, state)
    log.close()
    assert path.read_bytes() == original
    state.close()


def test_live_boot_skips_demo_files_and_appends_live_decision(tmp_path, monkeypatch):
    import server
    def no_demo_data(*args, **kwargs):
        raise AssertionError("live mode must not load demo data")
    monkeypatch.setattr(server, "load_events", no_demo_data)
    path = tmp_path / "live.jsonl"
    engine = server.LiveEngine(demo_mode=False, state_path=tmp_path / "state.sqlite3",
                               evidence_path=path, detector_factory=FakeDetector, start_workers=False)
    try:
        assert engine.events == [] and engine.sim is None and engine._stream_thread is None
        engine.runtime.start()
        result = asyncio.run(engine.submit_event(call(), budget_ms=500))
        assert result["decision"] == "BLOCK"
    finally:
        engine.close()
    assert verify(path)[0] and verify(path)[1] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["record"]["event_id"] == "C1"


@pytest.mark.parametrize("change", [{"ts": float("nan")}, {"ts": "100"}, {"imei": []},
                                    {"label": 1}, {"duration_s": 10}])
def test_network_schema_rejects_unsafe_or_ambiguous_fields(change):
    with pytest.raises(ValueError):
        validate_event(dict(call(), **change))


def test_network_schema_allows_missing_radio_identity_on_trunks():
    event = validate_event(dict(call(), imei="", origin_trunk="BANK_TRUNK"))
    assert event["imei"] == ""


def test_cdr_overload_is_retryable_and_does_not_consume_identity(tmp_path):
    runtime, state, detector = make_runtime(tmp_path, capacity=1, start=False)
    cdr = {"event_id": "CDR1", "event_type": "CALL", "caller": "A", "ts": 1.0, "duration_s": 5.0}
    try:
        runtime.queue.put_nowait(object())
        rejected = asyncio.run(runtime.submit("CDR", cdr, budget_ms=1000))
        assert rejected["retryable"] and rejected["ingest"] == "OVERLOAD"
        assert state.journal_after(0) == []
        runtime.queue.get_nowait()
        runtime.queue.task_done()
        runtime.start()
        assert asyncio.run(runtime.submit("CDR", cdr, budget_ms=1000))["ok"]
        assert asyncio.run(runtime.submit("CDR", cdr, budget_ms=1000))["duplicate"]
        assert detector.fs.cdrs == 1
    finally:
        runtime.close()
        state.close()


def test_pending_voice_observation_recovery_never_enters_synthetic_detector(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    event = {"event_id": "V1", "event_type": "VOICE_OBSERVATION", "call_id": "CALL1",
             "caller": "A", "callee": "B", "ts": 1000.0, "audio_sha256": "aa"}
    state.reserve("OBSERVATION", event)
    state.close()
    runtime, state, detector = make_runtime(tmp_path)
    try:
        assert detector.fs.calls == 0 and runtime.guard.max_ts == float("-inf")
        observed = asyncio.run(runtime.record_observation(event, {"event_id": "V1", "decision": "OBSERVE"}))
        assert observed["decision"] == "OBSERVE"
        assert detector.fs.calls == 0
    finally:
        runtime.close()
        state.close()


def test_noncausal_rescore_never_learns_deny_or_notifies_as_confirmed_fraud(tmp_path):
    from agent import OpsAgent
    from collections import deque, defaultdict, Counter
    from types import SimpleNamespace
    detector = FakeDetector()
    guarded = GuardedDetector(detector, budget_ms=1000)
    guarded.chaos_error = True
    guarded.score_batch([call()])
    guarded.chaos_error = False
    retro = guarded.drain_rescore()
    assert len(retro) == 1 and retro[0]["requires_review"]
    assert retro[0]["retro_context"] == "current_snapshot" and not guarded.fallback.deny
    users = {"ai_agent": {"role": "agent", "sign_key": "11" * 32}}
    engine = SimpleNamespace(g=guarded, lock=threading.RLock(), recent_hits=deque(),
                             reports=defaultdict(set), reputation=defaultdict(Counter))
    audit = security.AuditLog(tmp_path / "agent-audit.jsonl", user_db=users)
    agent = OpsAgent(engine, audit)
    agent.scan()
    assert any(p["kind"] == "investigate" for p in agent.list())
    assert all(p["action"]["kind"] != "notify" for p in agent.list())


@pytest.mark.parametrize("timestamp", ["nan", "NaN", "inf", "-inf"])
def test_signed_nonfinite_network_timestamps_are_rejected(timestamp):
    key = bytes.fromhex("ab" * 32)
    body = b"{}"
    signature = security.sign_network(body, timestamp, key)
    assert security.verify_network(body, timestamp, signature, key) == "stale timestamp"


@pytest.mark.parametrize("number", ["+84901234567", "+84 901 234 567", "849-0123-4567"])
def test_formatted_phone_numbers_remain_masked(number):
    masked = security.mask_number(number)
    assert "****" in masked and "0123" not in masked and masked.endswith("4567")


def test_review_only_retro_does_not_reach_subscribers_or_group_tracking():
    import server
    from collections import deque, defaultdict
    engine = server.LiveEngine.__new__(server.LiveEngine)
    engine.lock = threading.RLock()
    engine.alerts = deque(maxlen=60)
    engine.recent_hits = deque(maxlen=3000)
    engine.inbox = defaultdict(lambda: deque(maxlen=50))
    engine.inbox["B"]
    engine.guardians = defaultdict(set)
    result = {"event_id": "R1", "event_type": "CALL", "decision": "FLAG",
              "party": "A", "counterparty": "B", "requires_review": True, "retro": True}
    engine._alert(result, {"event_type": "CALL"})
    assert engine.alerts[0]["requires_review"]
    assert not engine.inbox["B"] and not engine.recent_hits


def test_outage_observe_only_matches_crash_replay_and_rescore_does_not_count_twice(tmp_path):
    runtime, state, detector = make_runtime(tmp_path)
    runtime.checkpoint()
    before = state.get("checkpoint")
    runtime.g.chaos_error = True
    result = asyncio.run(runtime.submit("VERDICT", call(), budget_ms=1000))
    assert result["observed_without_model"] and result["fail_open"]
    assert detector.fs.calls == 1 and detector.fs.cdrs == 0
    runtime.close()
    state.set("checkpoint", before)  # simulate an old checkpoint plus the durable completed journal
    state.close()
    runtime, state, restored = make_runtime(tmp_path)
    try:
        assert restored.fs.calls == 1 and restored.fs.cdrs == 0
        assert not runtime.g.fallback.deny
        runtime.g.drain_rescore()
        assert restored.fs.calls == 1
        asyncio.run(runtime.submit("VERDICT", call("C2", 101), budget_ms=1000))
        assert restored.fs.calls == 2
    finally:
        runtime.close()
        state.close()


def test_checkpoint_pins_model_before_loading_feature_state(tmp_path):
    first = FakeDetector()
    first.model_version = "bundle-A"
    first.model_manifest = {"status": "verified", "manifest_sha256": "aa"}
    runtime, state, _ = make_runtime(tmp_path, first)
    asyncio.run(runtime.submit("VERDICT", call(), budget_ms=1000))
    runtime.close()
    state.close()
    second = FakeDetector()
    second.model_version = "bundle-B"
    second.model_manifest = {"status": "verified", "manifest_sha256": "bb"}
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(RuntimeError, match="differs from durable state"):
            InferenceRuntime(GuardedDetector(second, budget_ms=1000), state=state)
        assert second.fs.calls == 0
    finally:
        state.close()


def test_fail_open_and_public_api_use_loaded_bundle_provenance(tmp_path, monkeypatch):
    import server
    from fastapi.testclient import TestClient
    detector = FakeDetector()
    detector.model_version = "loaded-new-version"
    detector.model_manifest = {"status": "verified", "manifest_sha256": "1234"}
    engine = server.LiveEngine(demo_mode=False, state_path=tmp_path / "state.sqlite3",
                               evidence_path=tmp_path / "evidence.jsonl",
                               detector_factory=lambda: detector, start_workers=False)
    monkeypatch.setattr(server, "engine", engine)
    key = bytes.fromhex("ab" * 32)
    monkeypatch.setattr(security, "network_key", lambda: key)
    try:
        engine.runtime.start()
        engine.g.chaos_error = True
        client = TestClient(server.app)
        body = json.dumps(call()).encode()
        timestamp = security.network_timestamp()
        response = client.post("/api/v1/verdict", content=body, headers={
            "x-timestamp": timestamp, "x-signature": security.sign_network(body, timestamp, key)})
        assert response.status_code == 200
        assert response.json()["model_version"] == "loaded-new-version"
        assert response.json()["artifact_status"] == "verified"
        assert client.get("/health/ready").json()["model_manifest"]["status"] == "verified"
        assert engine.state()["model_version"] == "loaded-new-version"
    finally:
        engine.close()


def test_bounded_owner_jobs_survive_caller_cancellation_without_blocking_event_loop(tmp_path):
    import sqlite3
    gate, started = threading.Event(), threading.Event()
    runtime, state, detector = make_runtime(tmp_path, FakeDetector(gate=gate, started=started),
                                            capacity=2, admission_capacity=3)
    cdr = {"event_id": "RETRY-CDR", "event_type": "CALL", "caller": "A", "ts": 1.0, "duration_s": 2.0}
    try:
        async def exercise():
            one = asyncio.create_task(runtime.submit("VERDICT", call("KEEP", 1), budget_ms=2000))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.002)
            assert started.is_set()
            one.cancel()
            with pytest.raises(asyncio.CancelledError):
                await one
            two, three = await asyncio.gather(
                runtime.submit("VERDICT", call("B", 2), budget_ms=20),
                runtime.submit("VERDICT", call("C", 3), budget_ms=20))
            assert two["pending"] and three["pending"]
            begin = time.perf_counter()
            replies = await asyncio.gather(*(runtime.submit("VERDICT", call(f"OVER-{i}", 10+i), budget_ms=20)
                                             for i in range(50)))
            assert time.perf_counter() - begin < .5
            assert all(r["ingest"] in ("OVERLOAD", "ADMISSION_OVERLOAD") and not r["durable"] for r in replies)
            assert runtime.stats["admission_peak"] == 3 and runtime.stats["admission_inflight"] == 3
            assert len(runtime._admission_futures) == 3
            # Separate WAL reader stays responsive and cannot see speculative owner state.
            with sqlite3.connect(state.path) as reader:
                assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 0
            denied = await runtime.submit("CDR", cdr, budget_ms=50)
            assert denied["retryable"] and not denied["durable"]
            gate.set()
            await asyncio.to_thread(runtime.queue.join)
            assert runtime.stats["admission_inflight"] == 0 and detector.fs.calls == 3
            assert (await runtime.submit("CDR", cdr, budget_ms=1000))["ok"]
            assert (await runtime.submit("CDR", cdr, budget_ms=1000))["duplicate"]
        asyncio.run(exercise())
    finally:
        gate.set()
        runtime.close()
        state.close()


def test_precise_deadline_retries_an_early_asyncio_timeout(monkeypatch):
    from backend_runtime import await_precise_deadline
    original = asyncio.wait_for
    waits = []
    async def early_once(awaitable, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            awaitable.cancel()  # emulate wait_for cancelling only the shield
            raise asyncio.TimeoutError
        return await original(awaitable, timeout)
    monkeypatch.setattr(asyncio, "wait_for", early_once)
    async def exercise():
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        loop.call_later(0.01, future.set_result, "ready")
        result = await await_precise_deadline(future, time.perf_counter() + 0.05)
        assert result == "ready" and not future.cancelled()
        assert len(waits) >= 2 and waits[1] <= waits[0]
    asyncio.run(exercise())


def test_runtime_uses_full_precise_deadline_without_waiting_for_slow_model(tmp_path):
    class SlowDetector(FakeDetector):
        def score_batch(self, events):
            time.sleep(0.15)
            return super().score_batch(events)
    runtime, state, _ = make_runtime(tmp_path, SlowDetector())
    try:
        begin = time.perf_counter()
        result = asyncio.run(runtime.submit("VERDICT", call(), budget_ms=30))
        elapsed = time.perf_counter() - begin
        assert result["fail_open"] and result["pending"]
        assert 0.028 <= elapsed < 0.12
        assert 28 <= result["latency_ms"] < 120
    finally:
        runtime.close()
        state.close()


def test_completed_work_returns_without_an_extra_loop_turn_before_deadline(tmp_path, monkeypatch):
    import backend_runtime as module
    from concurrent.futures import Future
    runtime, state, _ = make_runtime(tmp_path)
    original_wait = module.await_precise_deadline
    waits = []
    def complete(kind, event, deadline, submitted):
        future = Future()
        future.set_result({"event_id": event["event_id"], "decision": "ALLOW", "fast": True})
        return module.Work(0, kind, event, deadline, submitted, future)
    async def observe_wait(future, deadline):
        waits.append(deadline)
        return await original_wait(future, deadline)
    monkeypatch.setattr(runtime, "_enqueue", complete)
    monkeypatch.setattr(module, "await_precise_deadline", observe_wait)
    try:
        response = asyncio.run(runtime.submit("VERDICT", call(), budget_ms=1000))
        assert response["fast"] and response["decision"] == "ALLOW"
        assert len(waits) == 0  # memory-only admission and a completed result need no event-loop turn
    finally:
        runtime.close()
        state.close()
