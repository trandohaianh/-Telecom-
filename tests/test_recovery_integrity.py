"""Regressions for rescore rollback and acknowledged evidence-tail integrity."""

import json
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backend_evidence import DurableEvidenceLog
from backend_state import BackendState
from evidence import verify
from resilience import CircuitBreaker, GuardedDetector, RescoreStateRestoreError


def event(identity="A", timestamp=100.0):
    return {
        "event_id": identity,
        "event_type": "CALL",
        "ts": timestamp,
        "caller": "84900000001",
        "callee": "84900000002",
    }


class Features:
    def __init__(self):
        self.calls = 0
        self.fail_snapshot = self.fail_restore = False

    def state_dict(self):
        if self.fail_snapshot:
            raise RuntimeError("snapshot fixture failure")
        return {"calls": self.calls}

    def load_state_dict(self, snapshot):
        if self.fail_restore:
            self.calls = 999
            raise RuntimeError("restore fixture failure")
        self.calls = snapshot["calls"]


class Detector:
    def __init__(self):
        self.fs = Features()
        self.harvest_hits = {"old": {"peer": 1}}
        self.raise_score = False

    def score_batch(self, events):
        self.fs.calls += len(events)
        self.harvest_hits["new"] = {"peer": 2}
        if self.raise_score:
            raise RuntimeError("model fixture failure")
        return [dict(item, decision="BLOCK", reasons=[], action_vi="", action_en="") for item in events]


class Registry:
    def __init__(self):
        self.fail_on = None

    def apply(self, ev, result):
        if ev["event_id"] == self.fail_on:
            raise RuntimeError("policy fixture failure")


def guarded_fixture():
    detector, registry = Detector(), Registry()
    guarded = GuardedDetector(detector, budget_ms=1000, registry=registry)
    guarded.rescore_queue.extend([event("A"), event("B", 101)])
    return guarded, detector, registry


def test_rescore_policy_partial_failure_rolls_back_alerts_stats_and_keeps_entire_queue():
    guarded, detector, registry = guarded_fixture()
    existing = {"event_id": "OLDER", "decision": "FLAG"}
    guarded.retro = deque([existing], maxlen=2)
    guarded.stats["retro_alerts"] = 1
    before = dict(guarded.stats)
    registry.fail_on = "B"
    with pytest.raises(RuntimeError, match="policy fixture failure"):
        guarded.drain_rescore(budget_ms=1000)
    assert [item["event_id"] for item in guarded.rescore_queue] == ["A", "B"]
    assert list(guarded.retro) == [existing]  # The failed batch must not evict old alerts.
    assert guarded.stats == before
    assert detector.fs.calls == 0 and detector.harvest_hits == {"old": {"peer": 1}}
    registry.fail_on = None
    assert [item["event_id"] for item in guarded.drain_rescore(budget_ms=1000)] == ["A", "B"]
    assert not guarded.rescore_queue
    assert guarded.stats["rescored"] == 2 and guarded.stats["retro_alerts"] == 3
    assert detector.fs.calls == 0 and not guarded.fallback.deny


def test_rescore_snapshot_failure_never_consumes_queued_events():
    guarded, detector, _ = guarded_fixture()
    detector.fs.fail_snapshot = True
    with pytest.raises(RuntimeError, match="snapshot fixture failure"):
        guarded.drain_rescore()
    assert [item["event_id"] for item in guarded.rescore_queue] == ["A", "B"]
    assert detector.fs.calls == 0 and not guarded.retro
    detector.fs.fail_snapshot = False
    assert len(guarded.drain_rescore(budget_ms=1000)) == 2


@pytest.mark.parametrize("model_failure", [False, True])
def test_rescore_restore_failure_is_explicit_owner_recovery_error_and_keeps_queue(model_failure):
    guarded, detector, _ = guarded_fixture()
    detector.fs.fail_restore = True
    detector.raise_score = model_failure
    with pytest.raises(RescoreStateRestoreError, match="requires recovery") as caught:
        guarded.drain_rescore()
    assert caught.value.requires_recovery is True
    assert "restore fixture failure" in str(caught.value.__cause__)
    assert [item["event_id"] for item in guarded.rescore_queue] == ["A", "B"]
    assert not guarded.retro and guarded.stats["rescored"] == 0
    assert detector.harvest_hits == {"old": {"peer": 1}}


def test_model_failure_restores_state_and_does_not_duplicate_or_drop_queue():
    guarded, detector, _ = guarded_fixture()
    detector.raise_score = True
    assert guarded.drain_rescore() == []
    assert [item["event_id"] for item in guarded.rescore_queue] == ["A", "B"]
    assert detector.fs.calls == 0 and detector.harvest_hits == {"old": {"peer": 1}}
    assert guarded.stats["rescored"] == 0 and not guarded.retro


def test_idle_rescore_can_recover_open_breaker_after_cooldown_without_new_traffic():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=1, clock=lambda: now[0])
    detector, registry = Detector(), Registry()
    guarded = GuardedDetector(detector, budget_ms=1000, breaker=breaker, registry=registry)
    detector.raise_score = True
    guarded.score_batch([event()])
    assert breaker.state == breaker.OPEN and len(guarded.rescore_queue) == 1
    detector.raise_score = False
    assert guarded.drain_rescore() == []
    now[0] = 2.0
    assert len(guarded.drain_rescore(budget_ms=1000)) == 1
    assert breaker.state == breaker.CLOSED and not guarded.rescore_queue
    assert guarded.stats["rescored"] == 1


def add_results(state, items):
    sequences = []
    with state.transaction():
        for index, (kind, result) in enumerate(items):
            seq, _ = state.reserve(kind, {"event_id": f"record-{index}", "ts": index})
            state.finish(seq, dict(result, event_id=f"record-{index}"))
            sequences.append(seq)
    return sequences


def ledger_rows(state):
    return state.db.execute("SELECT seq,payload,result,evidence_exported FROM events ORDER BY seq").fetchall()


@pytest.mark.parametrize("missing_file", [False, True])
def test_acked_missing_or_valid_prefix_truncation_fails_without_rewriting_log_or_ledger(tmp_path, missing_file):
    state = BackendState(tmp_path / "state.sqlite3")
    path = tmp_path / "evidence.jsonl"
    try:
        add_results(state, [("VERDICT", {"decision": "BLOCK"}), ("VERDICT", {"decision": "FLAG"})])
        logger = DurableEvidenceLog(path, state)
        logger.close()
        original = path.read_bytes()
        assert verify(path) == (True, 2, None)
        before = ledger_rows(state)
        metadata_before = state.db.execute("SELECT key,value FROM metadata ORDER BY key").fetchall()
        if missing_file:
            path.unlink()
            retained = None
        else:
            retained = original.splitlines(keepends=True)[0]
            path.write_bytes(retained)
            assert verify(path) == (True, 1, None)  # A valid prefix alone cannot reveal its missing tail.
        with pytest.raises(RuntimeError, match="acknowledged evidence is missing"):
            DurableEvidenceLog(path, state)
        assert (path.read_bytes() if path.exists() else None) == retained
        assert ledger_rows(state) == before
        assert state.db.execute("SELECT key,value FROM metadata ORDER BY key").fetchall() == metadata_before
    finally:
        state.close()


def test_skipped_clean_allow_and_cdr_acks_do_not_require_jsonl_records(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    path = tmp_path / "evidence.jsonl"
    try:
        add_results(state, [("VERDICT", {"decision": "ALLOW"}), ("CDR", {"ok": True})])
        logger = DurableEvidenceLog(path, state, sample_allow_every=2)
        logger.close()
        assert verify(path) == (True, 0, None)
        assert not state.pending_evidence()
        path.unlink()  # No eligible event ever required a JSONL record.
        logger = DurableEvidenceLog(path, state, sample_allow_every=2)
        logger.close()
        assert verify(path) == (True, 0, None)
    finally:
        state.close()


def test_sampled_allow_and_fail_open_are_required_but_skipped_allow_and_cdr_are_not(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    path = tmp_path / "evidence.jsonl"
    try:
        add_results(
            state,
            [
                ("VERDICT", {"decision": "ALLOW"}),
                ("CDR", {"ok": True}),
                ("VERDICT", {"decision": "BLOCK"}),
                ("VERDICT", {"decision": "ALLOW"}),
                ("VERDICT", {"decision": "ALLOW", "fail_open": True}),
            ],
        )
        logger = DurableEvidenceLog(path, state, sample_allow_every=2)
        logger.close()
        assert verify(path) == (True, 3, None)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [row["record"]["event_id"] for row in rows] == ["record-2", "record-3", "record-4"]
        logger = DurableEvidenceLog(path, state, sample_allow_every=2)
        logger.close()
        prefix = path.read_bytes().splitlines(keepends=True)[0]
        path.write_bytes(prefix)
        with pytest.raises(RuntimeError, match="journal sequence 4"):
            DurableEvidenceLog(path, state, sample_allow_every=2)
        assert path.read_bytes() == prefix
    finally:
        state.close()


def test_startup_ack_integrity_check_reads_beyond_first_thousand_rows(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    path = tmp_path / "evidence.jsonl"
    try:
        add_results(state, [("CDR", {"ok": True})] * 1000 + [("VERDICT", {"decision": "BLOCK"})])
        logger = DurableEvidenceLog(path, state)
        logger.close()
        assert verify(path) == (True, 1, None)
        path.write_text("", encoding="utf-8")
        with pytest.raises(RuntimeError, match="journal sequence 1001"):
            DurableEvidenceLog(path, state)
        assert path.read_bytes() == b""
    finally:
        state.close()


def test_sampling_change_requires_explicit_migration_and_preserves_original_log(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    path = tmp_path / "evidence.jsonl"
    try:
        add_results(state, [("VERDICT", {"decision": "ALLOW"})])
        logger = DurableEvidenceLog(path, state, sample_allow_every=2)
        logger.close()
        before = path.read_bytes()
        with pytest.raises(RuntimeError, match="sampling policy changed"):
            DurableEvidenceLog(path, state, sample_allow_every=1)
        assert path.read_bytes() == before
        assert state.get("evidence_export_policy") == {"sample_allow_every": 2}
    finally:
        state.close()
