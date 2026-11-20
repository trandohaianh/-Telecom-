"""Observable atomic-commit, deadline and restart contracts for the live inference owner.

No trained model is needed: counted feature state isolates persistence and concurrency behavior.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backend_runtime import InferenceRuntime
from backend_state import BackendState
from resilience import GuardedDetector


def event(identity="ATOMIC-1", ts=100.0):
    return {"event_id": identity, "call_id": identity, "event_type": "CALL", "ts": float(ts),
            "caller": "84910000001", "callee": "84910000002", "cell_id": "HN-0001",
            "imei": "35391110000001", "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": 500}


class CountedFeatures:
    FEATURE_CONTRACT = "atomic-runtime-test-v1"

    def __init__(self):
        self.calls = 0
        self.cdrs = 0

    def state_dict(self):
        return {"calls": self.calls, "cdrs": self.cdrs}

    def load_state_dict(self, state):
        self.calls, self.cdrs = state["calls"], state["cdrs"]

    def call_features(self, _):
        self.calls += 1
        return {"calls_seen": self.calls}

    sms_features = audio_features = call_features

    def commit_cdr(self, _):
        self.cdrs += 1


class CountedDetector:
    model_version = "atomic-runtime-fixture"

    def __init__(self):
        self.fs = CountedFeatures()
        self.harvest_hits = {}
        self.scored_ids = []

    def score_batch(self, events):
        results = []
        for ev in events:
            values = self.fs.call_features(ev)
            self.scored_ids.append(ev["event_id"])
            results.append({"event_id": ev["event_id"], "event_type": ev["event_type"], "ts": ev["ts"],
                            "party": ev["caller"], "counterparty": ev["callee"], "decision": "BLOCK",
                            "risk": 0.99, "reasons": [], "evidence_features": values,
                            "action_vi": "", "action_en": ""})
        return results


def create_runtime(directory, *, detector=None, **options):
    state = BackendState(directory / "state.sqlite3")
    detector = detector or CountedDetector()
    guarded = GuardedDetector(detector, budget_ms=1000)
    runtime = InferenceRuntime(guarded, state=state, checkpoint_every=1_000_000,
                               checkpoint_interval_s=1_000_000, **options)
    return runtime, state, detector


def observer_for(state):
    return sqlite3.connect(str(state.path), timeout=1, check_same_thread=False, isolation_level=None)


def completed_rows(observer):
    return observer.execute("SELECT event_id,result FROM events WHERE kind='VERDICT' ORDER BY seq").fetchall()


async def until_set(gate, timeout=2.0):
    assert await asyncio.to_thread(gate.wait, timeout), "worker did not reach the expected test gate"


def install_commit_gate(monkeypatch, state):
    reached, release = threading.Event(), threading.Event()
    original = state.transaction

    @contextmanager
    def gated():
        with original() as transaction:
            yield transaction
            reached.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test commit gate was not released")

    monkeypatch.setattr(state, "transaction", gated)
    return reached, release


def test_one_atomic_commit_publishes_neither_partial_row_callback_nor_future(monkeypatch, tmp_path):
    callbacks = []
    runtime, state, detector = create_runtime(tmp_path, start=False,
                                            on_result=lambda ev, result: callbacks.append(dict(result)))
    observer = observer_for(state)
    reached, release = install_commit_gate(monkeypatch, state)
    commits = []
    state.db.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
    try:
        async def exercise():
            task = asyncio.create_task(runtime.submit("VERDICT", event(), budget_ms=2000))
            runtime.start()
            await until_set(reached)
            assert detector.fs.calls == 1
            assert completed_rows(observer) == []  # reserve and result remain invisible together
            assert callbacks == [] and not task.done()
            release.set()
            response = await task
            assert response["decision"] == "BLOCK" and not response.get("fail_open")
            rows = completed_rows(observer)
            assert len(rows) == 1 and json.loads(rows[0][1])["model_decision"] == "BLOCK"
            assert len(commits) == 1
        asyncio.run(exercise())
    finally:
        release.set()
        runtime.close()
        observer.close()
        state.close()


def test_commit_past_http_deadline_never_rearms_cached_block(monkeypatch, tmp_path):
    runtime, state, detector = create_runtime(tmp_path, start=False)
    observer = observer_for(state)
    reached, release = install_commit_gate(monkeypatch, state)
    try:
        async def exercise():
            task = asyncio.create_task(runtime.submit("VERDICT", event(), budget_ms=30))
            runtime.start()
            await until_set(reached)
            response = await task  # commit is still blocked while the HTTP timer expires
            assert response["fail_open"]
            assert completed_rows(observer) == []
            release.set()
            await asyncio.to_thread(runtime.queue.join)
            rows = completed_rows(observer)
            raw = json.loads(rows[0][1])
            assert raw["model_decision"] == "BLOCK"
            assert raw["delivery_status"] == "model_result_only"
            assert raw["latency_phase"] == "before_commit"
            repeated = await runtime.submit("VERDICT", event(), budget_ms=1000)
            assert repeated["duplicate"]
            assert repeated["scored_before_deadline"] is False
            assert detector.fs.calls == 1
        asyncio.run(exercise())
    finally:
        release.set()
        runtime.close()
        observer.close()
        state.close()


def test_aborted_commit_leaves_no_ghost_checkpoint_and_retry_updates_once(monkeypatch, tmp_path):
    runtime, state, _ = create_runtime(tmp_path, start=False)
    observer = observer_for(state)
    original = state.transaction

    @contextmanager
    def abort_before_commit():
        with original() as transaction:
            yield transaction
            raise OSError("simulated storage failure before commit")

    monkeypatch.setattr(state, "transaction", abort_before_commit)
    try:
        runtime.start()
        response = asyncio.run(runtime.submit("VERDICT", event(), budget_ms=1000))
        assert response["fail_open"]
        assert completed_rows(observer) == []
    finally:
        runtime.close()
        observer.close()
        state.close()
    restored, state, detector = create_runtime(tmp_path)
    observer = observer_for(state)
    try:
        assert detector.fs.calls == 0
        first = asyncio.run(restored.submit("VERDICT", event(), budget_ms=1000))
        repeated = asyncio.run(restored.submit("VERDICT", event(), budget_ms=1000))
        assert first["decision"] == "BLOCK" and repeated["duplicate"]
        assert detector.fs.calls == 1 and detector.scored_ids == [event()["event_id"]]
        assert len(completed_rows(observer)) == 1
    finally:
        restored.close()
        observer.close()
        state.close()


def test_ready_backlog_is_drained_in_one_full_transaction(tmp_path):
    runtime, state, detector = create_runtime(tmp_path, start=False, capacity=32,
                                            batch_size=16, batch_wait_ms=0)
    observer = observer_for(state)
    commits = []
    state.db.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
    try:
        async def exercise():
            jobs = [asyncio.create_task(runtime.submit("VERDICT", event(f"BATCH-{i}", 100+i), budget_ms=5000))
                    for i in range(8)]
            for _ in range(1000):
                if runtime.queue.qsize() == len(jobs):
                    break
                await asyncio.sleep(0.001)
            assert runtime.queue.qsize() == 8 and completed_rows(observer) == []
            runtime.start()
            results = await asyncio.gather(*jobs)
            assert all(r["decision"] == "BLOCK" and not r.get("fail_open") for r in results)
            assert detector.fs.calls == 8
            rows = completed_rows(observer)
            assert len(rows) == 8 and all(payload is not None for _, payload in rows)
            assert len(commits) == 1
        asyncio.run(exercise())
    finally:
        runtime.close()
        observer.close()
        state.close()


def test_post_commit_callback_can_take_execution_lock_and_read_store_from_another_thread(tmp_path):
    callback_finished = threading.Event()
    outcomes = []
    holder = {}

    def callback(ev, result):
        read_finished = threading.Event()
        observed = []
        def reader():
            with holder["runtime"].execution_lock:
                observed.extend(holder["state"].journal_after(0))
            read_finished.set()
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        finished = read_finished.wait(timeout=0.5)
        outcomes.append((finished, bool(observed and observed[0][3] is not None)))
        callback_finished.set()

    runtime, state, _ = create_runtime(tmp_path, start=False, on_result=callback)
    holder.update(runtime=runtime, state=state)
    try:
        runtime.start()
        response = asyncio.run(runtime.submit("VERDICT", event(), budget_ms=2000))
        assert response["decision"] == "BLOCK"
        assert callback_finished.wait(timeout=2)
        assert outcomes == [(True, True)]
    finally:
        runtime.close()
        state.close()

def test_deny_learning_deltas_reconstruct_exact_updates_and_evictions():
    from resilience import FallbackRules
    fallback = FallbackRules(max_entries=3)
    replayed = {}
    inputs = [dict(event("A", 100), caller="A", imei="I1"),
              dict(event("B", 101), caller="B", imei="I2"),
              dict(event("A2", 102), caller="A", imei="I1")]
    evictions = []
    for ev in inputs:
        result = {"decision": "BLOCK"}
        fallback.learn(ev, result)
        assert len(result["learned_deny_updates"]) <= 2
        replayed.update(result["learned_deny_updates"])
        for key in result["learned_deny_evicted"]:
            replayed.pop(key, None)
        evictions.extend(result["learned_deny_evicted"])
        assert replayed == fallback.deny
    assert evictions == ["A", "B"]
    old_event = dict(inputs[-1], ts=99)
    no_change = {"decision": "BLOCK"}
    fallback.learn(old_event, no_change)
    assert no_change["learned_deny_updates"] == {} and no_change["learned_deny_evicted"] == []
    assert fallback.deny == replayed


def test_explicit_deny_noops_preserve_allow_protected_and_fail_open_semantics():
    from resilience import FallbackRules
    fallback = FallbackRules()
    ev = event()
    for result in ({"decision": "ALLOW"}, {"decision": "BLOCK", "fail_open": True}):
        fallback.learn(ev, result)
        assert result["learned_deny_updates"] == {} and result["learned_deny_evicted"] == []
    fallback.allow.add(ev["caller"])
    allowed = {"decision": "BLOCK"}
    fallback.learn(ev, allowed)
    assert not fallback.deny and allowed["learned_deny_updates"] == {}
    fallback.allow.clear()
    fallback.protected = {ev["caller"]}
    protected = {"decision": "BLOCK"}
    fallback.learn(ev, protected)
    assert protected["learned_deny_updates"] == {ev["imei"]: ev["ts"]}
    assert ev["caller"] not in fallback.deny


def test_replay_uses_original_deny_delta_after_human_policy_changes(tmp_path):
    runtime, state, detector = create_runtime(tmp_path)
    observer = observer_for(state)
    runtime.checkpoint()
    old_checkpoint = state.get("checkpoint")
    ev = event()
    try:
        response = asyncio.run(runtime.submit("VERDICT", ev, budget_ms=1000))
        assert response["decision"] == "BLOCK"
        raw = json.loads(completed_rows(observer)[0][1])
        assert raw["learned_deny_updates"] == {ev["caller"]: ev["ts"], ev["imei"]: ev["ts"]}
    finally:
        runtime.close()
        observer.close()
    # Recreate a crash after the decision and a later human-policy write, before checkpoint.
    state.set("checkpoint", old_checkpoint)
    state.set("human_policy", {"manual": {}, "allow": [ev["caller"]]})
    state.close()
    restored, state, detector = create_runtime(tmp_path)
    try:
        assert detector.fs.calls == 1
        assert ev["caller"] in restored.g.fallback.allow
        assert restored.g.fallback.deny[ev["imei"]] == ev["ts"]
        other_subscriber_same_device = dict(ev, caller="84910000003", ts=101.0)
        assert restored.g.fallback.check(other_subscriber_same_device)[0] == "BLOCK"
    finally:
        restored.close()
        state.close()
