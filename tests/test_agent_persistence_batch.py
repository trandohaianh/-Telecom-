"""A burst of proposals must not rewrite the full SQLite snapshot per proposal."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import security
from agent import OpsAgent
from backend_state import BackendState
from test_agent_privacy import USERS, FakeEngine, hit


class CountingState(BackendState):
    def __init__(self, path):
        super().__init__(path)
        self.agent_writes = 0

    def set(self, key, value):
        if key == "agent_state":
            self.agent_writes += 1
        return super().set(key, value)


def create_agent(tmp_path):
    state = CountingState(tmp_path / "state.sqlite3")
    engine = FakeEngine()
    log = security.AuditLog(tmp_path / "audit.jsonl", user_db=USERS)
    for index in range(64):
        engine.recent_hits.append(hit(f"849{index:08d}", f"35391110{index:07d}", 1000.0, age=500))
    return OpsAgent(engine, log, clock=lambda: 1000.0, state_store=state), state, engine, log


def test_scan_persists_all_proposals_once_and_preserves_audit_and_restart(tmp_path):
    agent, state, engine, log = create_agent(tmp_path)
    try:
        agent.scan()
        assert len(agent.proposals) == 64
        assert state.agent_writes == 1
        assert len(state.get("agent_state")["proposals"]) == 64
        audit = security.verify_audit(log.path, user_db=USERS)
        assert audit["valid"] and audit["records"] == 64
        restored = OpsAgent(engine, log, clock=lambda: 1000.0, state_store=state)
        assert list(restored.proposals) == list(agent.proposals)
        assert restored._open == agent._open
    finally:
        state.close()


def test_failed_scan_flushes_created_proposals_and_restores_immediate_decision_persistence(tmp_path, monkeypatch):
    agent, state, _, _ = create_agent(tmp_path)

    def fail():
        raise RuntimeError("later watcher failed")

    monkeypatch.setattr(agent, "_watch_reports", fail)
    try:
        with pytest.raises(RuntimeError, match="later watcher failed"):
            agent.scan()
        assert state.agent_writes == 1
        assert len(state.get("agent_state")["proposals"]) == 64
        pid = next(iter(agent.proposals))
        agent.decide(pid, "boss", approve=False)
        assert state.agent_writes == 2
        assert state.get("agent_state")["proposals"][0]["status"] == "rejected"
    finally:
        state.close()


def test_failed_audit_never_persists_an_unaudited_proposal(tmp_path, monkeypatch):
    agent, state, _, log = create_agent(tmp_path)
    original = log.record

    def record(actor, action, **params):
        if params.get("title") == "bad":
            raise OSError("audit unavailable")
        return original(actor, action, **params)

    monkeypatch.setattr(log, "record", record)

    def proposals():
        for key in ("good", "bad"):
            agent.propose(key, "investigate", "medium", key, [], {})

    monkeypatch.setattr(agent, "_watch_groups", proposals)
    try:
        with pytest.raises(OSError, match="audit unavailable"):
            agent.scan()
        assert [p["title"] for p in state.get("agent_state")["proposals"]] == ["good"]
        assert "bad" not in agent._open
        assert len(agent.proposals) == 1
    finally:
        state.close()
