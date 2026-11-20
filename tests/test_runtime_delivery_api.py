import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import security


@pytest.mark.parametrize("mode,duplicate,actionable,expected", [
    ("enforce", False, True, "BLOCK"),
    ("enforce", True, True, "NONE"),
    ("enforce", False, False, "NONE"),
    ("shadow", False, True, "NONE"),
])
def test_public_recommendation_requires_fresh_actionable_delivery(monkeypatch, mode, duplicate, actionable, expected):
    from fastapi.testclient import TestClient

    import server
    key = bytes.fromhex("b7" * 32)
    result = dict(decision="BLOCK", ingest="ADMITTED", duplicate=duplicate,
                  actionable=actionable, scored=True, scored_before_deadline=actionable and not duplicate,
                  durable=True, latency_ms=4.0, commit_ms=1.0, queue_ms=.2)
    async def submit(event):
        return result
    engine = SimpleNamespace(submit_event=submit, network_mode=mode,
                             runtime=SimpleNamespace(model_version="test", model_identity={"id": "test"}))
    monkeypatch.setattr(server, "engine", engine)
    monkeypatch.setattr(security, "network_key", lambda: key)
    event = dict(event_type="CALL", event_id="ADVICE", ts=100.0, caller="A", callee="B",
                 cell_id="HN-0001", imei="", origin_trunk="LOCAL", network="4G")
    body = json.dumps(event).encode()
    timestamp = security.network_timestamp()
    response = TestClient(server.app).post("/api/v1/verdict", content=body, headers={
        "x-timestamp": timestamp, "x-signature": security.sign_network(body, timestamp, key)})
    assert response.status_code == 200
    data = response.json()
    assert data["recommended_action"] == expected
    assert data["durable"] and data["commit_ms"] == 1.0 and data["queue_ms"] == .2

def test_readiness_detects_stalled_owner_without_waiting_for_sqlite_lock(monkeypatch, tmp_path):
    import asyncio
    import threading

    from fastapi.testclient import TestClient

    import server
    from test_backend_runtime import FakeDetector, call, make_runtime
    gate, started = threading.Event(), threading.Event()
    runtime, state, _ = make_runtime(tmp_path, FakeDetector(gate=gate, started=started),
                                     owner_stall_after_s=.03)
    engine = SimpleNamespace(runtime=runtime, g=runtime.g, network_mode="shadow", demo_mode=False)
    monkeypatch.setattr(server, "engine", engine)
    try:
        response = asyncio.run(runtime.submit("VERDICT", call(), budget_ms=10))
        assert response["pending"]
        assert started.wait(timeout=.5)
        time.sleep(.04)
        begin = time.perf_counter()
        health = TestClient(server.app).get("/health/ready")
        assert health.status_code == 503 and time.perf_counter() - begin < .5
        assert runtime.progress()["stalled"]
        gate.set()
        runtime.queue.join()
        assert TestClient(server.app).get("/health/ready").status_code == 200
    finally:
        gate.set()
        runtime.close()
        state.close()


@pytest.mark.parametrize("maintenance_error", [False, True])
def test_readiness_detects_stalled_rescore_and_recovers(monkeypatch, tmp_path, maintenance_error):
    import threading

    from fastapi.testclient import TestClient

    import server
    from test_backend_runtime import make_runtime
    gate, started = threading.Event(), threading.Event()
    runtime, state, _ = make_runtime(tmp_path, start=False, owner_stall_after_s=.03)
    def blocked_rescore(**kwargs):
        started.set()
        if not gate.wait(timeout=3):
            raise RuntimeError("test gate was not released")
        if maintenance_error:
            raise RuntimeError("rescore fixture failure")
    monkeypatch.setattr(runtime.g, "drain_rescore", blocked_rescore)
    monkeypatch.setattr(server, "engine", SimpleNamespace(runtime=runtime, g=runtime.g,
                        network_mode="shadow", demo_mode=False))
    try:
        runtime.start()
        assert started.wait(timeout=1)
        time.sleep(.04)
        begin = time.perf_counter()
        health = TestClient(server.app).get("/health/ready")
        assert health.status_code == 503 and time.perf_counter() - begin < .5
        assert runtime.progress()["stalled"]
        gate.set()
        until = time.perf_counter() + 1
        while runtime.progress()["stalled"] and time.perf_counter() < until:
            time.sleep(.001)
        assert TestClient(server.app).get("/health/ready").status_code == 200
        if maintenance_error:
            assert runtime.stats["maintenance_errors"] >= 1
    finally:
        gate.set()
        runtime.close()
        state.close()
