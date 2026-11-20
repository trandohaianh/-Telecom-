"""Completed CDR identities survive cross-link validation, cache eviction and restart."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backend_runtime import InferenceRuntime, validate_event
from backend_state import BackendState, EventConflict
from resilience import GuardedDetector
from test_backend_runtime import FakeDetector, call


def create_runtime(directory, **options):
    state = BackendState(directory / "state.sqlite3")
    detector = FakeDetector()
    guarded = GuardedDetector(detector, budget_ms=1000)
    runtime = InferenceRuntime(
        guarded, state=state, checkpoint_every=1_000_000, checkpoint_interval_s=1_000_000, **options
    )
    return runtime, state, detector


def setup():
    return dict(call("LINKED", 100.0), call_id="SESSION-A")


def cdr_for(item=None):
    item = setup() if item is None else item
    return {
        **{name: item[name] for name in ("event_id", "event_type", "caller", "callee", "call_id", "ts")},
        "duration_s": 5.0,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"caller": "different"},
        {"ts": 101.0},
        {"call_id": "different"},
        {"callee": "different"},
    ],
)
def test_standalone_cdr_rejects_conflicting_new_setup_before_insert(tmp_path, changes):
    runtime, state, detector = create_runtime(tmp_path)
    cdr = cdr_for()
    try:
        assert asyncio.run(runtime.submit("CDR", cdr, budget_ms=2000))["ok"]
        before = state.journal_after(0)
        with pytest.raises(EventConflict, match="linked CDR"):
            asyncio.run(runtime.submit("VERDICT", dict(setup(), **changes), budget_ms=2000))
        assert state.journal_after(0) == before
        assert state.event_payload("VERDICT", "LINKED") is None
        assert detector.fs.cdrs == 1 and detector.fs.calls == 0 and runtime.progress()["ready"]
        assert asyncio.run(runtime.submit("CDR", cdr, budget_ms=2000))["duplicate"]
    finally:
        runtime.close()
        state.close()


def test_standalone_cdr_allows_later_matching_setup_and_both_retries(tmp_path):
    runtime, state, detector = create_runtime(tmp_path, cache_capacity=1)
    try:
        assert asyncio.run(runtime.submit("CDR", cdr_for(), budget_ms=2000))["ok"]
        assert asyncio.run(runtime.submit("VERDICT", setup(), budget_ms=2000))["decision"] == "BLOCK"
        for kind, payload in (("CDR", cdr_for()), ("VERDICT", setup()), ("CDR", cdr_for())):
            assert asyncio.run(runtime.submit(kind, payload, budget_ms=2000))["duplicate"]
        assert detector.fs.calls == detector.fs.cdrs == 1
    finally:
        runtime.close()
        state.close()


def seed_completed_legacy_pair(directory):
    runtime, state, _ = create_runtime(directory, start=False)
    original_setup = validate_event(setup())
    conflicting_cdr = validate_event(
        dict(cdr_for(), caller="84999999999", call_id="LEGACY-OTHER-SESSION", ts=90.0), "CDR"
    )
    try:
        with state.transaction():
            seq, _ = state.reserve("VERDICT", original_setup)
            state.finish(
                seq,
                {
                    "event_id": "LINKED",
                    "event_type": "CALL",
                    "ts": 100.0,
                    "party": original_setup["caller"],
                    "counterparty": original_setup["callee"],
                    "decision": "BLOCK",
                    "scored": True,
                    "ingest": "ADMITTED",
                    "reasons": [],
                    "learned_deny_updates": {},
                    "learned_deny_evicted": [],
                },
            )
            seq, _ = state.reserve("CDR", conflicting_cdr)
            state.finish(seq, {"event_id": "LINKED", "ok": True, "scored": False})
    finally:
        # The checkpoint predates both manually seeded legacy rows; restore must replay them.
        runtime.close()
        state.close()
    return original_setup, conflicting_cdr


def test_legacy_contradictory_completed_pairs_retry_from_cache_eviction_and_restart(tmp_path):
    original_setup, conflicting_cdr = seed_completed_legacy_pair(tmp_path)
    for _restart in range(2):
        runtime, state, detector = create_runtime(tmp_path, cache_capacity=1)
        try:
            before = state.journal_after(0)
            assert detector.fs.calls == detector.fs.cdrs == 1
            requests = [
                ("CDR", conflicting_cdr),
                ("CDR", conflicting_cdr),
                ("VERDICT", original_setup),
                ("VERDICT", original_setup),
                ("CDR", conflicting_cdr),
                ("VERDICT", original_setup),
            ]
            for kind, payload in requests:
                result = asyncio.run(runtime.submit(kind, payload, budget_ms=2000))
                assert result["duplicate"] and result["durable"] and not result["actionable"]
                if kind == "CDR":
                    assert result["ok"]
                else:
                    assert result["decision"] == "BLOCK"
            assert state.journal_after(0) == before
            assert detector.fs.calls == detector.fs.cdrs == 1
        finally:
            runtime.close()
            state.close()


def test_completed_own_payload_conflict_is_checked_before_legacy_crosslink(tmp_path):
    _, conflicting_cdr = seed_completed_legacy_pair(tmp_path)
    runtime, state, detector = create_runtime(tmp_path, cache_capacity=1)
    try:
        before = state.journal_after(0)
        with pytest.raises(EventConflict, match="different content"):
            asyncio.run(runtime.submit("CDR", dict(conflicting_cdr, ts=999.0), budget_ms=2000))
        assert state.journal_after(0) == before
        assert detector.fs.calls == detector.fs.cdrs == 1 and runtime.progress()["ready"]
    finally:
        runtime.close()
        state.close()


def test_pending_own_cdr_conflict_preserves_row_then_exact_retry_finishes_once(tmp_path):
    runtime, state, detector = create_runtime(tmp_path, start=False)
    pending = validate_event(cdr_for(), "CDR")
    seq, _ = state.reserve("CDR", pending)
    runtime.start()
    try:
        before = state.journal_after(0)
        with pytest.raises(EventConflict, match="different content"):
            asyncio.run(runtime.submit("CDR", dict(pending, caller="OTHER"), budget_ms=2000))
        assert state.journal_after(0) == before and state.result(seq) is None
        first = asyncio.run(runtime.submit("CDR", pending, budget_ms=2000))
        assert first["ok"] and not first["duplicate"]
        assert asyncio.run(runtime.submit("CDR", pending, budget_ms=2000))["duplicate"]
        assert detector.fs.cdrs == 1 and detector.fs.calls == 0
    finally:
        runtime.close()
        state.close()


def test_pending_cdr_rejects_conflicting_setup_without_finishing_or_inserting(tmp_path):
    runtime, state, detector = create_runtime(tmp_path, start=False)
    pending = validate_event(cdr_for(), "CDR")
    seq, _ = state.reserve("CDR", pending)
    runtime.start()
    try:
        before = state.journal_after(0)
        with pytest.raises(EventConflict, match="linked CDR"):
            asyncio.run(runtime.submit("VERDICT", dict(setup(), caller="OTHER"), budget_ms=2000))
        assert state.journal_after(0) == before and state.result(seq) is None
        assert state.event_payload("VERDICT", "LINKED") is None
        assert detector.fs.calls == detector.fs.cdrs == 0
    finally:
        runtime.close()
        state.close()


@pytest.mark.parametrize("first_kind", ["VERDICT", "CDR"])
@pytest.mark.parametrize("conflicting", [False, True])
def test_same_batch_crosslinks_commit_only_valid_operations_without_ghosts(tmp_path, first_kind, conflicting):
    runtime, state, detector = create_runtime(tmp_path, start=False, capacity=8, batch_size=8)
    original_setup, cdr = setup(), cdr_for()
    first_payload = original_setup if first_kind == "VERDICT" else cdr
    second_kind = "CDR" if first_kind == "VERDICT" else "VERDICT"
    second_payload = cdr if second_kind == "CDR" else original_setup
    if conflicting:
        second_payload = dict(second_payload, caller="OTHER")

    async def exercise():
        first = asyncio.create_task(runtime.submit(first_kind, first_payload, budget_ms=2000))
        second = asyncio.create_task(runtime.submit(second_kind, second_payload, budget_ms=2000))
        for _ in range(1000):
            if runtime.queue.qsize() == 2:
                break
            await asyncio.sleep(0.001)
        assert runtime.queue.qsize() == 2
        runtime.start()
        return await asyncio.gather(first, second, return_exceptions=True)

    try:
        first, second = asyncio.run(exercise())
        assert isinstance(first, dict) and first["durable"]
        if conflicting:
            assert isinstance(second, EventConflict)
            assert state.event_payload(second_kind, "LINKED") is None
            assert len(state.journal_after(0)) == 1
            assert detector.fs.calls == (first_kind == "VERDICT")
            assert detector.fs.cdrs == (first_kind == "CDR")
        else:
            assert isinstance(second, dict) and second["durable"]
            assert len(state.journal_after(0)) == 2
            assert detector.fs.calls == detector.fs.cdrs == 1
        assert runtime.progress()["ready"] and runtime.stats["max_owner_batch"] == 2
    finally:
        runtime.close()
        state.close()


def test_legacy_pending_conflict_fails_startup_without_advancing_past_later_completed_row(tmp_path):
    runtime, state, _ = create_runtime(tmp_path, start=False)
    runtime.close()  # Preserve the pre-journal checkpoint at sequence zero.
    original = validate_event(setup())
    pending = validate_event(dict(cdr_for(), caller="LEGACY-OTHER"), "CDR")
    later = validate_event(call("LATER", 101.0))
    try:
        with state.transaction():
            seq, _ = state.reserve("VERDICT", original)
            state.finish(
                seq,
                {
                    "event_id": original["event_id"],
                    "event_type": "CALL",
                    "ts": original["ts"],
                    "decision": "ALLOW",
                    "scored": True,
                    "ingest": "ADMITTED",
                    "reasons": [],
                    "learned_deny_updates": {},
                    "learned_deny_evicted": [],
                },
            )
            pending_seq, _ = state.reserve("CDR", pending)
            later_seq, _ = state.reserve("VERDICT", later)
            state.finish(
                later_seq,
                {
                    "event_id": later["event_id"],
                    "event_type": "CALL",
                    "ts": later["ts"],
                    "decision": "ALLOW",
                    "scored": True,
                    "ingest": "ADMITTED",
                    "reasons": [],
                    "learned_deny_updates": {},
                    "learned_deny_evicted": [],
                },
            )
        before_rows = state.journal_after(0)
        before_metadata = state.db.execute("SELECT key,value FROM metadata ORDER BY key").fetchall()
        assert state.get("checkpoint")["seq"] == 0 and later_seq > pending_seq
    finally:
        state.close()

    state = BackendState(tmp_path / "state.sqlite3")
    detector = FakeDetector()
    try:
        with pytest.raises(RuntimeError, match="legacy pending journal.*replay failed") as caught:
            InferenceRuntime(GuardedDetector(detector, budget_ms=1000), state=state, start=False)
        assert isinstance(caught.value.__cause__, EventConflict)
        assert "explicit recovery or migration" in str(caught.value)
        assert state.result(pending_seq) is None
        assert state.journal_after(0) == before_rows
        assert state.db.execute("SELECT key,value FROM metadata ORDER BY key").fetchall() == before_metadata
        assert state.get("checkpoint")["seq"] == 0
        assert detector.fs.calls == 1 and detector.fs.cdrs == 0  # Later row was never replayed.
    finally:
        state.close()


@pytest.mark.parametrize("resolution", ["unresolved", "cancelled"])
def test_legacy_pending_future_must_resolve_successfully_before_startup(tmp_path, monkeypatch, resolution):
    runtime, state, _ = create_runtime(tmp_path, start=False)
    runtime.close()
    try:
        seq, _ = state.reserve("CDR", validate_event(cdr_for(), "CDR"))
        before_rows = state.journal_after(0)
        before_checkpoint = state.get("checkpoint")
    finally:
        state.close()

    def incomplete_execution(self, work):
        if resolution == "cancelled":
            work.future.cancel()

    monkeypatch.setattr(InferenceRuntime, "_execute", incomplete_execution)
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        message = "did not resolve" if resolution == "unresolved" else "replay failed"
        with pytest.raises(RuntimeError, match=message):
            InferenceRuntime(GuardedDetector(FakeDetector(), budget_ms=1000), state=state, start=False)
        assert state.result(seq) is None
        assert state.journal_after(0) == before_rows
        assert state.get("checkpoint") == before_checkpoint
    finally:
        state.close()
