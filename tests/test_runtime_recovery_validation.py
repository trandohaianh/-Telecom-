"""Invalid inputs cannot poison the inference owner; failed rollback requires recovery."""
import asyncio
import json
from pathlib import Path
import sys
import threading
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backend_runtime import validate_event
from test_backend_runtime import call, make_runtime


@pytest.mark.parametrize("field", ["ts", "caller_age_days", "pitch_jitter"])
def test_numeric_overflow_is_a_validation_error(field):
    with pytest.raises(ValueError, match="finite"):
        validate_event(dict(call(), **{field: 10 ** 400}))


@pytest.mark.parametrize("cdr", [
    dict(ts=1e308, duration_s=1e308),
    dict(ts=100., duration_s=10., end_ts=99.),
])
def test_invalid_derived_cdr_completion_is_rejected(cdr):
    with pytest.raises(ValueError, match="completion"):
        validate_event(dict(event_type="CALL", event_id="CDR-BAD", caller="A", **cdr), "CDR")


def test_invalid_operation_is_rejected_before_enqueue():
    with pytest.raises(ValueError, match="operation"):
        validate_event(call(), "unknown")


def test_signed_overflow_returns_400_before_engine(monkeypatch):
    import security
    import server
    from fastapi.testclient import TestClient
    key = bytes.fromhex("5a" * 32)
    monkeypatch.setattr(security, "network_key", lambda: key)
    monkeypatch.setattr(server, "engine", None)
    body = json.dumps(dict(call(), ts=10 ** 400)).encode()
    stamp = security.network_timestamp()
    response = TestClient(server.app).post("/api/v1/verdict", content=body, headers={
        "x-timestamp": stamp, "x-signature": security.sign_network(body, stamp, key)})
    assert response.status_code == 400 and "finite" in response.json()["detail"]


def test_failed_rescore_restore_stops_owner_and_resolves_queued_job(monkeypatch, tmp_path):
    from resilience import RescoreStateRestoreError
    gate, entered = threading.Event(), threading.Event()
    runtime, state, detector = make_runtime(tmp_path, start=False)
    original_checkpoint = state.get("checkpoint")
    def broken_maintenance(**kwargs):
        entered.set()
        assert gate.wait(timeout=3)
        raise RescoreStateRestoreError("fixture restore failed")
    monkeypatch.setattr(runtime.g, "drain_rescore", broken_maintenance)
    try:
        runtime.start()
        assert entered.wait(timeout=1)
        async def exercise():
            request = asyncio.create_task(runtime.submit("VERDICT", call(), budget_ms=1000))
            await asyncio.sleep(.01)
            assert runtime.queue.qsize() == 1
            gate.set()
            result = await request
            assert result["ingest"] == "RECOVERY_REQUIRED"
            assert result["retryable"] and not result["durable"]
            assert not runtime.progress()["ready"] and runtime._fatal_error is not None
            assert runtime.stats["admission_inflight"] == 0
        asyncio.run(exercise())
        runtime.close()
        assert state.get("checkpoint") == original_checkpoint
        assert detector.fs.calls == 0
    finally:
        gate.set()
        runtime.close()
        state.close()


@pytest.mark.parametrize("changes", [
    {"caller": "different"}, {"ts": 101.}, {"call_id": "different"}, {"callee": "different"}
])
def test_linked_cdr_identity_conflict_has_no_side_effects(tmp_path, changes):
    from backend_state import EventConflict
    runtime, state, detector = make_runtime(tmp_path)
    setup = dict(call(), call_id="SESSION")
    cdr = {k: setup[k] for k in ("event_id", "event_type", "caller", "callee", "call_id", "ts")}
    cdr["duration_s"] = 5.
    try:
        asyncio.run(runtime.submit("VERDICT", setup, budget_ms=1000))
        with pytest.raises(EventConflict, match="linked CALL"):
            asyncio.run(runtime.submit("CDR", dict(cdr, **changes), budget_ms=1000))
        assert state.db.execute("SELECT count(*) FROM events WHERE kind='CDR'").fetchone()[0] == 0
        assert detector.fs.cdrs == 0 and runtime.progress()["ready"]
        result = asyncio.run(runtime.submit("CDR", cdr, budget_ms=1000))
        assert result["ok"] and detector.fs.cdrs == 1
    finally:
        runtime.close()
        state.close()


def test_standalone_cdr_is_still_accepted(tmp_path):
    runtime, state, detector = make_runtime(tmp_path)
    try:
        cdr = dict(event_type="CALL", event_id="standalone", caller="A", ts=100., duration_s=5.)
        result = asyncio.run(runtime.submit("CDR", cdr, budget_ms=1000))
        assert result["ok"] and detector.fs.cdrs == 1
    finally:
        runtime.close()
        state.close()


def test_retro_callback_failure_applies_backpressure_before_rescore(tmp_path):
    from collections import deque
    published, attempts, remaining = [], [], deque("ABCD")
    done = threading.Event()
    def publish(results):
        attempts.append([r["event_id"] for r in results])
        if len(attempts) <= 2:
            raise RuntimeError("temporary downstream failure")
        published.extend(r["event_id"] for r in results)
        if len(published) == 4:
            done.set()
    runtime, state, _ = make_runtime(tmp_path, start=False, on_retro=publish)
    runtime.g.retro = deque(maxlen=2)
    def drain(*, max_n):
        for _ in range(min(max_n, len(remaining))):
            runtime.g.retro.append({"event_id": remaining.popleft()})
            runtime.g.stats["retro_alerts"] += 1
    runtime.g.drain_rescore = drain
    try:
        runtime.start()
        assert done.wait(timeout=3)
        assert published == list("ABCD")
        assert attempts[:3] == [["A", "B"]] * 3
        assert runtime.stats["maintenance_errors"] == 2
        assert runtime._published_retro == 4
    finally:
        runtime.close()
        state.close()


def test_replay_publishes_in_chunks_before_bounded_retro_overflows(tmp_path):
    from collections import deque
    from backend_runtime import InferenceRuntime
    from backend_state import BackendState
    from resilience import GuardedDetector
    from test_runtime_atomic import CountedDetector, create_runtime, event
    runtime, state, _ = create_runtime(tmp_path)
    try:
        for index in range(5):
            asyncio.run(runtime.submit("VERDICT", event(str(index), 100.+index), budget_ms=2000))
    finally:
        runtime.close()
    state.set("checkpoint", None)  # crash after durable journal, before checkpoint
    state.close()
    state = BackendState(tmp_path / "state.sqlite3")
    guarded = GuardedDetector(CountedDetector(), budget_ms=1000)
    guarded.retro = deque(maxlen=2)
    published = []
    restored = None
    try:
        restored = InferenceRuntime(guarded, state, start=False,
            on_retro=lambda results: published.extend(r["event_id"] for r in results))
        assert published == [str(index) for index in range(5)]
        assert len(guarded.retro) == 2 and restored._published_retro == 5
    finally:
        if restored is not None:
            restored.close()
        state.close()


def test_voice_schema_upgrade_preserves_exact_payload_idempotency(tmp_path):
    from backend_state import EventConflict
    runtime, state, detector = make_runtime(tmp_path, start=False)
    legacy = dict(event_type="VOICE_OBSERVATION", event_id="LEGACY-WAV", call_id="SESSION",
                  caller="A", callee="B", ts=100, audio_sha256="11"*32,
                  caller_channel=None, schema_version="voice-acoustic-v1")
    raw = dict(event_id=legacy["event_id"], status="COMPLETE", decision="OBSERVE",
               observation={"schema_version":"voice-acoustic-v1"}, recommended_action="NONE")
    try:
        asyncio.run(runtime.record_observation(legacy, raw))
        before = state.db.total_changes
        current = dict(legacy, schema_version="voice-acoustic-v2")
        previous = asyncio.run(runtime.lookup_observation(current))
        assert previous["duplicate"] and previous["observation"]["schema_version"] == "voice-acoustic-v1"
        assert state.db.total_changes == before and detector.fs.calls == 0
        for changed in (dict(current, audio_sha256="22"*32), dict(current, ts=100.0)):
            with pytest.raises(EventConflict):
                asyncio.run(runtime.lookup_observation(changed))
        assert state.db.total_changes == before
    finally:
        runtime.close()
        state.close()
