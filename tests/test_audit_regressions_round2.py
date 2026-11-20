"""Hồi quy vòng 2 của đợt audit 17/11/2026: các mục trước đó "đã xác nhận, chưa sửa" và các quyết định đã sửa.

Mỗi test thất bại trên mã trước vòng 2. Run: python -B -m pytest -q tests/test_audit_regressions_round2.py
"""
import collections
import importlib.util
import json
import threading
import time
import types
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

import accounts
import portal
import security
from test_portal_accounts import _login_op, _register, env  # noqa: F401  (fixture dùng lại)

ROOT = Path(__file__).resolve().parents[1]

CALL = {"event_type": "CALL", "caller": "84961234567", "callee": "84351234567", "cell_id": "HN-0001",
        "imei": "35391110000001", "origin_trunk": "LOCAL", "network": "4G"}
SMS = {"event_type": "SMS", "sender": "84961234567", "recipient": "84351234567", "sms_route": "P2P",
       "cell_id": "HN-0001", "text": "xin chao"}


# =========================================================================== runtime
class _SlowStateFS:
    def state_dict(self):
        time.sleep(0.05)
        return {}

    def load_state_dict(self, _):
        time.sleep(0.05)


class _Det:
    def __init__(self, delay_s=0.0):
        self.fs, self.harvest_hits, self.delay_s = _SlowStateFS(), {}, delay_s

    def score_batch(self, events):
        time.sleep(self.delay_s)
        return [{"event_id": e["event_id"], "event_type": e["event_type"], "decision": "ALLOW", "risk": 0.0,
                 "reasons": [], "ts": e["ts"]} for e in events]


def test_rescore_timeout_measures_scoring_not_state_copy():
    from resilience import GuardedDetector
    g = GuardedDetector(_Det(), budget_ms=1000)
    g.rescore_queue.append({**CALL, "event_id": "R1", "ts": 1.0})
    g.drain_rescore(max_n=8, budget_ms=20.0)
    # Trước khi sửa: ~100 ms sao chép/khôi phục state bị tính là timeout của mô hình.
    assert g.cb.stats.get("timeouts", 0) == 0 and g.cb.failures == 0
    assert not g.rescore_queue


def test_content_only_batch_uses_tier2_budget():
    from resilience import GuardedDetector
    g = GuardedDetector(_Det(delay_s=0.06), budget_ms=30, content_budget_ms=170)
    g.score_batch([{**SMS, "event_id": "S1", "ts": 1.0}])
    assert g.cb.stats.get("timeouts", 0) == 0          # 60 ms < 170 ms (SMS)
    g.score_batch([{**CALL, "event_id": "C1", "ts": 2.0}])
    assert g.cb.stats.get("timeouts", 0) == 1          # 60 ms > 30 ms (CALL)


def test_busy_owner_drains_rescore_queue_without_idle_gap():
    from backend_runtime import InferenceRuntime
    assert hasattr(InferenceRuntime, "_maintenance")
    src = (ROOT / "src" / "backend_runtime.py").read_text(encoding="utf-8")
    loop = src[src.index("    def _loop(self):"):src.index("    def _maintenance(")]
    # sau mỗi batch (không chỉ khi hàng đợi rỗng 100 ms) owner chạy bảo trì theo nhịp
    assert "busy_maintenance_interval_s" in loop and loop.count("self._maintenance(") == 2


def test_agent_does_not_repropose_restored_retro_alerts():
    from agent import OpsAgent

    class Store:
        def get(self, key, default=None):
            return {"proposals": [], "open": {}, "done_keys": []}

        def set(self, *_):
            pass
    eng = types.SimpleNamespace(g=types.SimpleNamespace(stats={"retro_alerts": 7}, retro=collections.deque()),
                                det_lock=threading.RLock())
    agent = OpsAgent(eng, audit=None, state_store=Store())
    assert agent._retro_seen == 7


def test_late_events_are_counted_separately_and_exported():
    import observability
    import server
    eng = types.SimpleNamespace(log=types.SimpleNamespace(submit=lambda r: None), lock=threading.RLock(),
                                network_stats=collections.Counter(), ingest_counts=collections.Counter(),
                                processed=0, by_type=collections.Counter(), counts=collections.Counter(),
                                lat=collections.deque(), _track=lambda e, r: None, _alert=lambda r, e: None)
    late = {"event_id": "L1", "decision": "ALLOW", "ingest": "LATE", "fail_open": False}
    server.LiveEngine._record_live_result(eng, {**CALL, "event_id": "L1", "ts": 1.0}, late)
    assert eng.ingest_counts["LATE"] == 1
    eng.state = lambda mask=True: {"ingest_unscored": dict(eng.ingest_counts)}
    eng.network_mode = "shadow"
    metrics = {m.name: m for m in observability.EngineCollector(lambda: eng, lambda: None).collect()}
    sample = metrics["tfs_ingest_unscored"].samples[0]
    assert sample.labels == {"status": "LATE"} and sample.value == 1.0
    rules = (ROOT / "monitoring" / "alerts" / "telecom-rules.yml").read_text(encoding="utf-8")
    assert 'tfs_ingest_unscored{status="LATE"}' in rules


def test_demo_injections_are_serialised():
    import server
    assert hasattr(server.LiveEngine, "_inject_unlocked")
    src = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
    assert "% 10_000_000" not in src               # event_id cũ có thể trùng giữa hai lần bấm cùng mili-giây


# =========================================================================== xác thực
def test_password_change_revokes_open_operator_sessions(env, tmp_path):  # noqa: F811
    c, _, users = env
    users["boss"]["password_hash"] = accounts.hash_password("mat-khau-cu-123")
    h = _login_op(c, "t-boss")
    assert c.get("/api/audit", headers=h).status_code == 200
    security.upsert_operator("boss", "admin", password="mat-khau-moi-456")
    assert c.get("/api/audit", headers=h).status_code == 401


def test_durable_store_sessions_revoked_on_disable(tmp_path):
    from backend_state import BackendState
    state = BackendState(tmp_path / "s.sqlite3")
    try:
        state.save_session("tok-1", {"name": "ana", "role": "analyst", "role_label": "x", "created": 1, "last": 1})
        state.save_session("tok-2", {"name": "boss", "role": "admin", "role_label": "x", "created": 1, "last": 1})
        state.delete_sessions_for("ana")
        assert state.session("tok-1") is None and state.session("tok-2") is not None
    finally:
        state.close()


def test_unknown_account_still_pays_for_a_password_check(monkeypatch, env):  # noqa: F811
    c, _, _ = env
    calls = []
    monkeypatch.setattr(accounts, "burn_password_check", lambda pw: calls.append(pw))
    c.post("/api/app/auth/login", json={"email": "nobody@example.com", "password": "khong-ton-tai"})
    assert security.login_password("no-such-operator", "x" * 10) is None
    assert len(calls) == 2


def test_malformed_audit_line_is_reported_not_raised(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"not": "an audit record"\n', encoding="utf-8")
    out = security.verify_audit(path, user_db={})
    assert out["valid"] is False and out["first_bad_line"] == 1


def test_infra_cost_changes_are_audited(env):  # noqa: F811
    import server
    c, _, _ = env
    h = _login_op(c, "t-boss")
    r = c.post("/api/admin/costs/infra", headers=h,
               json={"month": "2026-11", "provider": "oracle", "item": "vm", "amount_usd": 1.5, "note": ""})
    assert r.status_code == 200, r.text
    c.delete(f"/api/admin/costs/infra/{r.json()['id']}", headers=h)
    actions = [json.loads(x)["entry"]["action"] for x in server.audit.path.read_text(encoding="utf-8").splitlines()]
    assert "infra_cost_add" in actions and "infra_cost_delete" in actions


def test_reports_and_invites_validate_numbers_and_are_rate_limited(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FRAUD_RATE_LIMIT", "1")
    c, _, _ = env
    a = _register(c, "rep@example.com")
    c.post("/api/app/phone/claim-demo", headers=a)
    assert c.post("/api/app/report?number=" + "x" * 300, headers=a).status_code == 422
    assert c.post("/api/app/family/invite?number=%3Cscript%3E", headers=a).status_code == 422
    # giới hạn 30/giờ tính cả lần gửi số không hợp lệ ở trên
    codes = [c.post(f"/api/app/report?number=0987{i:06d}", headers=a).status_code for i in range(30)]
    assert codes[:29] == [200] * 29 and codes[29] == 429


def test_public_demo_numbers_are_never_assigned_to_accounts(env):  # noqa: F811
    c, eng, _ = env
    eng.demo_numbers = [s.msisdn for s in eng.sim.subs[:2]]
    a = _register(c, "pool@example.com")
    number = c.post("/api/app/phone/claim-demo", headers=a).json()["msisdn"]
    assert number not in eng.demo_numbers


def test_new_owner_of_a_number_does_not_inherit_history(env, monkeypatch):  # noqa: F811
    c, eng, _ = env
    eng.demo_mode = False
    sent = {}

    class Fake(accounts.OtpProvider):
        name, cost_usd = "fake", 0.0

        def send(self, phone, code):
            sent[phone] = code
    monkeypatch.setattr(accounts, "otp_provider", lambda: Fake())
    phone = "84912000111"
    with portal.db().tx() as d:
        d.execute("INSERT INTO subscriber_alerts(msisdn, created_at, item_json) VALUES (?,?,?)", (phone, 1.0, "{}"))
        d.execute("INSERT INTO guardians VALUES (?,?)", (phone, "84900000099"))
    eng.inbox[phone].append({"old": True})
    eng.guardians[phone].add("84900000099")
    h = _register(c, "newowner@example.com")
    c.post("/api/app/phone/otp/start", headers=h, json={"phone": "0912000111"})
    r = c.post("/api/app/phone/otp/verify", headers=h, json={"phone": "0912000111", "code": sent[phone]})
    assert r.status_code == 200
    assert not list(eng.inbox[phone]) and not eng.guardians.get(phone)
    assert not portal.db().one("SELECT 1 AS x FROM subscriber_alerts WHERE msisdn=?", (phone,))
    assert not portal.db().one("SELECT 1 AS x FROM guardians WHERE protected=?", (phone,))


# =========================================================================== giọng nói
def test_voice_upload_rejects_oversized_declared_length_before_reading():
    from fastapi import FastAPI

    import voice_api
    app = FastAPI()
    app.include_router(voice_api.create_voice_router(model=None, asr=None))
    client = TestClient(app)
    ts = security.network_timestamp()
    r = client.post("/api/v1/audio", content=b"{}", headers={
        "x-timestamp": ts, "x-signature": "00", "content-length": str(voice_api.MAX_BODY_BYTES + 1)})
    assert r.status_code == 413
    stale = client.post("/api/v1/audio", content=b"{}", headers={"x-timestamp": str(time.time() - 3600),
                                                                  "x-signature": "00"})
    assert stale.status_code == 401


# =========================================================================== drift / eval
def _baseline(block_share):
    import drift
    rng = np.random.default_rng(1)
    feats = {"calls_1h": rng.poisson(3, 5000).astype(float)}
    scores = rng.random(5000) * 0.5
    if block_share:
        scores[: int(5000 * block_share)] = 0.99
    return drift.build_baseline(feats, scores=scores, thresholds={"block": 0.9, "flag": 0.6}), feats


def test_block_alarm_fires_when_reference_had_no_blocks():
    import drift
    base, feats = _baseline(0.0)
    scores = np.random.default_rng(2).random(5000) * 0.5
    scores[:50] = 0.99                                  # 1 % BLOCK, tham chiếu 0 %
    rep = drift.evaluate_drift(base, feats, scores)
    assert rep["block_band_moved"] is True and rep["recommendation"] != drift.NO_ACTION


def test_empty_segment_is_not_a_quality_incident():
    import drift
    rng = np.random.default_rng(3)
    ref = rng.poisson(3, 4000).astype(float)
    base = drift.build_baseline({"calls_1h": ref}, segments=np.array(["domestic", "foreign"] * 2000))
    cur = rng.poisson(3, 2000).astype(float)
    rep = drift.evaluate_drift(base, {"calls_1h": cur}, segments=np.array(["domestic"] * 2000))
    assert rep["segments"]["foreign"]["rows"] == 0 and "foreign" in rep["empty_segments"]
    assert rep["recommendation"] == drift.NO_ACTION


def _run_evals():
    spec = importlib.util.spec_from_file_location("run_evals_r2", ROOT / "evals" / "run_evals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fpr_regression_uses_relative_tolerance_and_smoke_is_gated():
    ev = _run_evals()
    regs, _ = ev.regressions({"any_fpr_pct": 0.20, "block_fpr_ci_hi_pct": 0.0077},
                             {"any_fpr_pct": 0.11, "block_fpr_ci_hi_pct": 0.0077}, 3.0)
    assert [r.split(":")[0] for r in regs] == ["any_fpr_pct"]   # trước: +0,09 điểm % < 3 điểm, không bị bắt
    th = json.loads((ROOT / "evals" / "thresholds.json").read_text(encoding="utf-8"))
    assert "smoke_passed" in th["must_be_true"]


def test_shadow_safety_check_is_not_an_assert():
    src = (ROOT / "src" / "shadow_run.py").read_text(encoding="utf-8")
    assert "assert not enforced" not in src and "raise RuntimeError" in src


# =========================================================================== đóng gói / triển khai
def test_cloud_deploy_trusts_only_the_proxy_and_requires_backup():
    compose = (ROOT / "deploy" / "cloud" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS:" in compose
    deploy = (ROOT / "deploy" / "cloud" / "deploy.sh").read_text(encoding="utf-8")
    assert "proxy_ip.sh" in deploy and "['status'])\" || true" not in deploy and "exit 3" in deploy
    smoke = (ROOT / "deploy" / "cloud" / "smoke.sh").read_text(encoding="utf-8")
    assert "curl -ks" not in smoke and 'Bearer $METRICS_TOKEN"' not in smoke
    wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "DEPLOY_KNOWN_HOSTS" in wf and "StrictHostKeyChecking=yes" in wf
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "sha256sum -c" in ci
    assert "FROM python:3.12-slim\n" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dev_compose_files_do_not_expose_unauthenticated_ports_or_passwords():
    spark = (ROOT / "spark" / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:29092:29092"' in spark and "/work/secrets" in spark
    grpc = (ROOT / "telecom_grpc" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "--pass" not in grpc and "REDISCLI_AUTH" in grpc and grpc.count("healthcheck:") == 2


def test_android_keeps_foreign_links_out_of_the_session_webview():
    kt = (ROOT / "android" / "app" / "src" / "main" / "java" / "vn" / "lachan" / "fraudshield"
          / "MainActivity.kt").read_text(encoding="utf-8")
    assert "shouldOverrideUrlLoading" in kt and "ACTION_VIEW" in kt
