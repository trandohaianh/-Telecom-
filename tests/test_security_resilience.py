"""Access control, signed audit trail, fallback rules and re-scoring. Run: python -m pytest -q tests"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import security  # noqa: E402
from resilience import CircuitBreaker, FallbackRules, GuardedDetector  # noqa: E402

USERS = {"boss": {"role": "admin", "token": "t-admin", "sign_key": "11" * 32},
         "ana": {"role": "analyst", "token": "t-analyst", "sign_key": "22" * 32},
         "vic": {"role": "viewer", "token": "t-viewer", "sign_key": "33" * 32}}


def S(tok):
    """Sign in with a personal credential and return the header carrying the resulting session."""
    out = security.login(tok)
    return {"Authorization": "Bearer " + (out[0] if out else "invalid")}


# ------------------------------------------------------------------ audit trail
def test_audit_log_proves_who_did_what(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = security.AuditLog(p, user_db=USERS)
    log.record("boss", "fault_drill", mode="latency")
    log.record("ana", "verify_evidence_chain")
    v = security.verify_audit(p, USERS)
    assert v["valid"] and v["records"] == 2
    assert [r["entry"]["actor"] for r in log.tail()] == ["boss", "ana"]


def test_audit_log_detects_edit(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = security.AuditLog(p, user_db=USERS)
    for m in ("latency", "error", "off"):
        log.record("boss", "fault_drill", mode=m)
    lines = p.read_text().splitlines()
    row = json.loads(lines[1]); row["entry"]["params"]["mode"] = "off"
    lines[1] = json.dumps(row)
    p.write_text("\n".join(lines) + "\n")
    v = security.verify_audit(p, USERS)
    assert not v["valid"] and v["first_bad_line"] == 2 and "edited" in v["problem"]


def test_audit_log_detects_forged_actor(tmp_path):
    # Someone rewrites a line in another user's name and even recomputes the hash chain,
    # but cannot produce that user's signature.
    p = tmp_path / "audit.jsonl"
    log = security.AuditLog(p, user_db=USERS)
    log.record("ana", "read_evidence", n=5)
    row = json.loads(p.read_text().splitlines()[0])
    row["entry"]["actor"], row["entry"]["role"] = "boss", "admin"
    payload = json.dumps(row["entry"], ensure_ascii=False, sort_keys=True)
    row["hash"] = security._digest(security.GENESIS, payload)
    p.write_text(json.dumps(row) + "\n")
    v = security.verify_audit(p, USERS)
    assert not v["valid"] and "signature" in v["problem"]


def test_mask_number_hides_the_middle():
    assert security.mask_number("84901234567") == "849****4567"


# ------------------------------------------------------------------ API access control
@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import server
    monkeypatch.setattr(security, "_users_cache", USERS)
    monkeypatch.setattr(server, "audit", security.AuditLog(tmp_path / "audit.jsonl", user_db=USERS))
    return TestClient(server.app), server, monkeypatch    # no `with`: the live engine is not started


def test_api_requires_a_token(client):
    c, _, _ = client
    for method, url in (("get", "/api/state"), ("get", "/api/metrics"), ("post", "/api/chaos?mode=off"),
                        ("post", "/api/inject?type=SIM_BOX"), ("get", "/api/evidence/latest"), ("get", "/api/audit")):
        assert getattr(c, method)(url).status_code == 401, url
    assert c.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_roles_are_enforced(client):
    c, _, _ = client
    viewer, analyst = S("t-viewer"), S("t-analyst")
    assert c.post("/api/chaos?mode=latency", headers=viewer).status_code == 403
    assert c.post("/api/chaos?mode=latency", headers=analyst).status_code == 403
    assert c.get("/api/evidence/latest", headers=viewer).status_code == 403
    assert c.get("/api/audit", headers=analyst).status_code == 403
    assert c.get("/api/whoami", headers=viewer).json()["role"] == "viewer"


def test_personal_credential_only_works_to_sign_in(client):
    c, _, _ = client
    assert c.get("/api/whoami", headers={"Authorization": "Bearer t-viewer"}).status_code == 401
    r = c.post("/api/login", headers={"Authorization": "Bearer t-viewer"})
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    sess = {"Authorization": "Bearer " + r.json()["session"]}
    assert c.get("/api/whoami", headers=sess).status_code == 200
    c.post("/api/logout", headers=sess)
    assert c.get("/api/whoami", headers=sess).status_code == 401
    assert c.post("/api/login", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_sessions_expire_after_idle_time_and_absolute_ttl():
    monkey_users = security._users_cache
    security._users_cache = USERS
    try:
        t = [0.0]
        sid, *_ = security.login("t-analyst", clock=lambda: t[0])
        t[0] = security.SESSION_IDLE_S - 1
        assert security.session_user(sid, clock=lambda: t[0]) is not None       # activity keeps it alive
        t[0] += security.SESSION_IDLE_S + 1
        assert security.session_user(sid, clock=lambda: t[0]) is None           # idle too long
        sid, *_ = security.login("t-analyst", clock=lambda: 0.0)
        for k in range(1, 20):                                                  # active all day...
            assert security.session_user(sid, clock=lambda: k * 1500.0) is not None or k * 1500 > security.SESSION_TTL_S  # noqa: B023
        assert security.session_user(sid, clock=lambda: security.SESSION_TTL_S + 1.0) is None   # ...but not past 8 h
    finally:
        security._users_cache = monkey_users


def test_demo_endpoints_are_off_outside_demo_mode(client):
    c, server, mp = client
    mp.setattr(server, "DEMO_MODE", False)
    r = c.post("/api/chaos?mode=latency", headers=S("t-admin"))
    assert r.status_code == 403 and "demo" in r.json()["detail"]


def test_app_endpoints_need_a_subscriber_session(client):
    c, _, _ = client
    assert c.get("/api/app/inbox").status_code == 401
    # an operator token is not a subscriber session
    assert c.get("/api/app/inbox", headers=S("t-admin")).status_code == 401


# ------------------------------------------------------------------ fallback rules + re-scoring
def call(eid, ts, caller="84900000001", imei="35391110000001", origin="LOCAL"):
    return {"event_id": eid, "event_type": "CALL", "ts": ts, "caller": caller, "callee": "84911111111",
            "imei": imei, "origin_trunk": origin}


class Model:
    """Blocks caller 'bad', can be switched off to simulate an outage."""
    def __init__(self):
        self.down, self.calls = False, 0

    def score_batch(self, evs):
        if self.down:
            raise RuntimeError("down")
        self.calls += len(evs)
        return [{"event_id": e["event_id"], "event_type": e["event_type"], "ts": e["ts"], "party": e["caller"],
                 "decision": "BLOCK" if e["caller"].startswith("bad") else "ALLOW", "reasons": []} for e in evs]


def test_outage_is_not_a_free_pass_for_known_fraud():
    m = Model()
    g = GuardedDetector(m, budget_ms=1000, breaker=CircuitBreaker(failure_threshold=1, cooldown_s=60))
    assert g.score_batch([call("A", 0, caller="bad1", imei="SIMBOX-IMEI")])[0]["decision"] == "BLOCK"
    m.down = True
    g.score_batch([call("B", 1)])                                  # opens the breaker
    out = g.score_batch([call("C", 2, caller="bad1"),               # same number
                         call("D", 3, caller="bad2", imei="SIMBOX-IMEI"),   # new SIM, same gateway device
                         call("E", 4, caller="1900545413"),          # protected hotline from a non-bank trunk
                         call("F", 5, caller="84922222222")])        # ordinary subscriber
    assert [r["decision"] for r in out] == ["BLOCK", "BLOCK", "BLOCK", "ALLOW"]
    assert all(r["fail_open"] and r["degraded"] for r in out)
    assert [r["reasons"][0]["code"] for r in out[:3]] == ["F002", "F002", "F003"]


def test_spoofed_hotline_is_never_deny_listed():
    # The model blocks a call that fakes a bank hotline; during a later outage the bank's genuine
    # call (from its own trunk) must still pass, while another fake (wrong trunk) is still blocked.
    fb = FallbackRules()
    fb.learn(call("S1", 0, caller="1900545413", imei="-1", origin="INTL_GW"), {"decision": "BLOCK"})
    assert "1900545413" not in fb.deny
    assert fb.check(call("S2", 1, caller="1900545413", origin="BANK_TRUNK")) is None
    # M4 (quyết định 17/11/2026): ngân hàng thật cũng gọi qua cổng quốc tế, nên khi mô hình lỗi thì chỉ
    # gắn cờ đường đó; giả mạo qua đường nội địa vẫn bị chặn.
    assert fb.check(call("S3", 2, caller="1900545413", origin="INTL_GW"))[0] == "FLAG"
    assert fb.check(call("S4", 3, caller="1900545413", origin="LOCAL"))[0] == "BLOCK"


def test_events_missed_during_outage_are_rescored_after_recovery():
    m = Model()
    g = GuardedDetector(m, budget_ms=1000, breaker=CircuitBreaker(failure_threshold=1, cooldown_s=0.0))
    m.down = True
    g.score_batch([call("X1", 0, caller="bad9"), call("X2", 1)])
    assert len(g.rescore_queue) == 2 and g.drain_rescore() == []   # not while broken
    m.down = False
    g.score_batch([call("Y", 2)])                                  # healthy again, breaker closes
    retro = g.drain_rescore()
    assert [r["event_id"] for r in retro] == ["X1"] and retro[0]["retro"] and retro[0]["reasons"][0]["code"] == "F010"
    assert len(g.rescore_queue) == 0 and g.stats["rescored"] == 2


def test_rescore_queue_is_bounded_and_counts_drops():
    m = Model(); m.down = True
    g = GuardedDetector(m, budget_ms=1000, rescore_max=3)
    g.score_batch([call(f"E{i}", i) for i in range(5)])
    assert len(g.rescore_queue) == 3 and g.stats["rescore_dropped"] == 2


def test_timeout_keeps_late_verdict_without_rescoring_twice():
    class Slow(Model):
        def score_batch(self, evs):
            import time
            time.sleep(0.03)
            return super().score_batch(evs)
    m = Slow()
    g = GuardedDetector(m, budget_ms=5)
    r = g.score_batch([call("L", 0, caller="bad7")])[0]
    assert r["decision"] == "ALLOW" and r["fail_open"]              # the call already went through
    assert len(g.rescore_queue) == 0 and m.calls == 1               # scored once, not queued again
    assert g.stats["retro_alerts"] == 1 and g.retro[0]["event_id"] == "L"
    assert FallbackRules.check(g.fallback, call("L2", 1, caller="bad7"))[0] == "BLOCK"   # next call from it is caught
