"""AI agent (human decides), data-leak controls and family protection. Run: python -m pytest -q tests"""

import collections
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import security  # noqa: E402
from agent import INSIDER_REVEALS, OpsAgent  # noqa: E402
from resilience import CircuitBreaker, GuardedDetector  # noqa: E402

USERS = {
    "boss": {"role": "admin", "token": "t-admin", "sign_key": "11" * 32},
    "ana": {"role": "analyst", "token": "t-analyst", "sign_key": "22" * 32},
    "staff": {"role": "viewer", "token": "t-staff", "sign_key": "33" * 32},
    "ai_agent": {"role": "agent", "token": "t-agent", "sign_key": "44" * 32},
}


def S(tok):
    """Sign in with a personal credential and return the header carrying the resulting session."""
    out = security.login(tok)
    return {"Authorization": "Bearer " + (out[0] if out else "invalid")}


class Model:
    def score_batch(self, evs):
        return [
            {
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "ts": e["ts"],
                "party": e["caller"],
                "decision": "ALLOW",
                "reasons": [],
                "action_vi": "Cho phép",
            }
            for e in evs
        ]


class FakeEngine:
    """The parts of LiveEngine the agent reads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.g = GuardedDetector(Model(), budget_ms=1000, breaker=CircuitBreaker())
        self.recent_hits = collections.deque()
        self.reports, self.reputation = collections.defaultdict(set), collections.defaultdict(collections.Counter)
        self.sent = []

    def notify(self, recipients, message):
        self.sent.append((tuple(recipients), message))
        return len(recipients)


@pytest.fixture()
def agent(tmp_path):
    e = FakeEngine()
    log = security.AuditLog(tmp_path / "audit.jsonl", user_db=USERS)
    t = [1000.0]
    a = OpsAgent(e, log, clock=lambda: t[0])
    return a, e, log, t, tmp_path


def hit(party, imei, t, decision="BLOCK", age=10):
    return {
        "wall": t,
        "type": "CALL",
        "decision": decision,
        "party": party,
        "imei": imei,
        "cell": "HN-0001",
        "event_id": f"E-{party}",
        "caller_age_days": age,
    }


def call(caller, imei, eid="X", ts=0.0):
    return {
        "event_id": eid,
        "event_type": "CALL",
        "ts": ts,
        "caller": caller,
        "callee": "84911111111",
        "imei": imei,
        "origin_trunk": "LOCAL",
    }


def test_agent_proposes_but_never_acts_alone(agent):
    a, e, log, t, _ = agent
    for p in ("84900000001", "84900000002", "84900000003"):
        e.recent_hits.append(hit(p, "86001204000001", t[0]))
    a.scan()
    props = a.list()
    assert len(props) == 1 and props[0]["kind"] == "block_group" and props[0]["status"] == "pending"
    assert all("****" in s for s in props[0]["subjects"])  # numbers / IMEI masked in the proposal
    # nothing changed in the network until a human approves
    assert e.g.fallback.manual == {}
    assert e.g.score_batch([call("84900000009", "86001204000001")])[0]["decision"] == "ALLOW"


def test_numbers_rotating_over_several_devices_are_one_group(agent):
    a, e, log, t, _ = agent
    for p, imei in (
        ("84900000001", "86001204000001"),
        ("84900000002", "86001204000001"),
        ("84900000002", "86001204000002"),
        ("84900000003", "86001204000002"),
    ):
        e.recent_hits.append(hit(p, imei, t[0]))
    a.scan()
    props = a.list()
    assert len(props) == 1  # one farm, one proposal
    assert sorted(props[0]["subjects"])[:3] == sorted(["849****0001", "849****0002", "849****0003"])


def test_incremental_chain_check_only_reads_new_lines(tmp_path):
    import json

    from evidence import EvidenceLog, verify_incremental

    p = tmp_path / "ev.jsonl"
    log = EvidenceLog(p, sample_allow_every=1)
    for i in range(5):
        log.submit({"event_id": f"E{i}", "decision": "BLOCK"})
    log.close()
    st = {}
    assert verify_incremental(p, st) == (True, 5, None)
    log = EvidenceLog(p, sample_allow_every=1)  # appends continue the chain
    for i in range(5, 8):
        log.submit({"event_id": f"E{i}", "decision": "BLOCK"})
    log.close()
    assert verify_incremental(p, dict(st)) == (True, 8, None)
    lines = p.read_text().splitlines()
    row = json.loads(lines[6])
    row["record"]["decision"] = "ALLOW"
    lines[6] = json.dumps(row)
    p.write_text("\n".join(lines) + "\n")
    ok, n, bad = verify_incremental(p, dict(st))
    assert not ok and bad == 7


def test_human_approval_executes_and_is_signed(agent):
    a, e, log, t, tmp = agent
    for p in ("84900000001", "84900000002"):
        e.recent_hits.append(hit(p, "86001204000001", t[0]))
    a.scan()
    pid = a.list()[0]["id"]
    out = a.decide(pid, "boss", True, "SIM box rõ ràng")
    assert out["status"] == "approved" and out["decided_by"] == "boss"
    # a NEW SIM on the same gateway device is now blocked as part of the group
    r = e.g.score_batch([call("84900000099", "86001204000001")])[0]
    assert r["decision"] == "BLOCK" and r["reasons"][0]["code"] == "G001"
    actions = [(x["entry"]["actor"], x["entry"]["action"]) for x in log.tail()]
    assert ("ai_agent", "agent_proposal") in actions and ("boss", "agent_decision") in actions
    assert security.verify_audit(tmp / "audit.jsonl", USERS)["valid"]
    with pytest.raises(ValueError):
        a.decide(pid, "boss", True)  # cannot be decided twice


def test_rejection_changes_nothing_and_is_not_proposed_again(agent):
    a, e, log, t, _ = agent
    for p in ("84900000001", "84900000002"):
        e.recent_hits.append(hit(p, "86001204000001", t[0]))
    a.scan()
    a.decide(a.list()[0]["id"], "boss", False, "cần thêm bằng chứng")
    assert e.g.fallback.manual == {}
    a.scan()
    assert len(a.list()) == 1  # no nagging after a human said no


def test_possible_false_positive_can_be_unblocked_by_a_human(agent):
    a, e, log, t, _ = agent
    e.recent_hits.append(hit("84912345678", "35391110000001", t[0], age=2000))
    a.scan()
    p = a.list()[0]
    assert p["kind"] == "unblock"
    a.decide(p["id"], "boss", True)

    class Blocker(Model):
        def score_batch(self, evs):
            out = super().score_batch(evs)
            for r in out:
                r["decision"] = "BLOCK"
            return out

    e.g.det = Blocker()
    r = e.g.score_batch([call("84912345678", "35391110000001")])[0]
    assert r["decision"] == "FLAG" and r["reasons"][0]["code"] == "G002"  # warned, no longer blocked


def test_user_reports_need_a_human_to_block(agent):
    a, e, *_ = agent
    e.reports["84988888888"] |= {"84900000001", "84900000002"}
    a.scan()
    p = a.list()[0]
    assert p["kind"] == "block_group" and "không bao giờ tự động chặn" in " ".join(p["why"])
    assert "84988888888" not in e.g.fallback.manual


def test_insider_reveals_trigger_a_suspension_proposal(agent):
    a, e, log, t, _ = agent
    for _ in range(INSIDER_REVEALS + 1):
        a.note_reveal("ana")
    a.scan()
    p = a.list()[0]
    assert p["kind"] == "suspend_user" and p["subjects"] == ["ana"]
    try:
        a.decide(p["id"], "boss", True)
        assert "ana" in security.suspended
    finally:
        security.suspended.discard("ana")


def test_mask_imei_keeps_only_the_device_model():
    assert security.mask_imei("86001204123456") == "86001204******"


# ------------------------------------------------------------------ API: masking for everyone, logged reveal
@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import server

    monkeypatch.setattr(security, "_users_cache", USERS)
    monkeypatch.setattr(server, "audit", security.AuditLog(tmp_path / "audit.jsonl", user_db=USERS))
    return TestClient(server.app), server, monkeypatch, tmp_path


def test_agent_token_cannot_log_in(client):
    c, *_ = client
    assert c.get("/api/whoami", headers=S("t-agent")).status_code == 401


def test_only_admin_decides_on_proposals(client):
    c, *_ = client
    for tok in ("t-staff", "t-analyst"):
        assert c.post("/api/agent/P0001/decide?decision=approve", headers=S(tok)).status_code == 403


def test_reveal_needs_analyst_and_a_reason(client):
    c, *_ = client
    assert c.post("/api/reveal?event_id=E1&reason=check", headers=S("t-staff")).status_code == 403
    assert c.post("/api/reveal?event_id=E1&reason=x", headers=S("t-analyst")).status_code == 400


def test_suspended_account_is_locked_out(client):
    c, *_ = client
    security.suspended.add("staff")
    try:
        assert c.get("/api/whoami", headers=S("t-staff")).status_code == 403
    finally:
        security.suspended.discard("staff")
