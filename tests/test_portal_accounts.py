"""Tài khoản thuê bao thật, gắn số, nhân viên đăng nhập mật khẩu/Google, duyệt báo cáo, chi phí, pipeline, metrics."""
import collections
import threading
import types

import pytest
from fastapi.testclient import TestClient

import accounts
import portal
import security
from appdb import AppDB


class FakeFallback:
    def __init__(self):
        self.manual = {}

    def block_manual(self, keys, ref):
        for k in keys:
            self.manual[k] = ref


class FakeEngine:
    """Đủ thuộc tính cho các endpoint app/admin mà không nạp mô hình."""

    def __init__(self):
        self.demo_mode = True
        self.sim = types.SimpleNamespace(subs=[types.SimpleNamespace(msisdn=f"8490000000{i}") for i in range(3)])
        self.lock, self.det_lock = threading.RLock(), threading.RLock()
        self.inbox = collections.defaultdict(lambda: collections.deque(maxlen=50))
        self.reports = collections.defaultdict(set)
        self.guardians = collections.defaultdict(set)
        self.invites = collections.defaultdict(set)
        self.reputation = collections.defaultdict(collections.Counter)
        self.g = types.SimpleNamespace(fallback=FakeFallback())
        self.processed, self.started = 1000, 0.0
        self.network_mode, self.network_stats = "shadow", collections.Counter(ALLOW=5)
        self.evidence_path = "/nonexistent"
        self.store = types.SimpleNamespace(path=None)

    def state(self, mask=False):
        return {"decisions": {"ALLOW": 900, "FLAG": 60, "BLOCK": 40}, "by_type": {"CALL": 1000}, "processed": 1000,
                "resilience": {"fallback_block": 1, "fallback_flag": 0, "rescored": 2}, "latency_ms": {"p50": 3.0, "p99": 12.0},
                "inference": {"queue_size": 0, "queue_capacity": 1024}, "breaker": {"state": "CLOSED"}, "uptime_s": 60}


USERS = {"boss": {"role": "admin", "token": "t-boss", "sign_key": "aa" * 32},
         "ana": {"role": "analyst", "token": "t-ana", "sign_key": "bb" * 32},
         "vic": {"role": "viewer", "token": "t-vic", "sign_key": "cc" * 32}}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    import server

    users = {k: dict(v) for k, v in USERS.items()}
    monkeypatch.setattr(security, "_users_cache", users)
    monkeypatch.setattr(security, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(server, "audit", security.AuditLog(tmp_path / "audit.jsonl", user_db=users))
    monkeypatch.setattr(portal, "_db", AppDB(":memory:"))
    eng = FakeEngine()
    monkeypatch.setattr(portal, "_engine_getter", lambda: eng)
    monkeypatch.setattr(server, "engine", eng)
    return TestClient(server.app), eng, users


def _login_op(c, token):
    return {"Authorization": "Bearer " + c.post("/api/login", headers={"Authorization": "Bearer " + token}).json()["session"]}


def _register(c, email="an@example.com", pw="mat-khau-123"):
    r = c.post("/api/app/auth/register", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---------------------------------------------------------------- mật khẩu
def test_scrypt_password_roundtrip_and_rejects_short():
    h = accounts.hash_password("correct horse")
    assert accounts.verify_password("correct horse", h) and not accounts.verify_password("wrong", h)
    assert not accounts.verify_password("x", "not-a-hash")
    with pytest.raises(ValueError):
        accounts.hash_password("short")


# ---------------------------------------------------------------- thuê bao
def test_subscriber_register_login_and_claim_unique_demo_numbers(env):
    c, eng, _ = env
    a = _register(c, "a@example.com")
    assert c.post("/api/app/auth/register", json={"email": "a@example.com", "password": "mat-khau-123"}).status_code == 400
    assert c.get("/api/app/inbox", headers=a).status_code == 409          # chưa gắn số
    num_a = c.post("/api/app/phone/claim-demo", headers=a).json()["msisdn"]
    assert c.post("/api/app/phone/claim-demo", headers=a).json()["msisdn"] == num_a   # ổn định
    b = _register(c, "b@example.com")
    num_b = c.post("/api/app/phone/claim-demo", headers=b).json()["msisdn"]
    assert num_a != num_b and {num_a, num_b} <= {s.msisdn for s in eng.sim.subs}
    assert c.get("/api/app/inbox", headers=a).json()["msisdn"] == num_a

    bad = c.post("/api/app/auth/login", json={"email": "a@example.com", "password": "sai-mat-khau"})
    assert bad.status_code == 401
    ok = c.post("/api/app/auth/login", json={"email": "A@example.com", "password": "mat-khau-123"})
    assert ok.status_code == 200 and ok.json()["me"]["msisdn"] == num_a
    c.post("/api/app/auth/logout", headers=a)
    assert c.get("/api/app/auth/me", headers=a).status_code == 401


def test_subscriber_login_is_rate_limited(env):
    c, _, _ = env
    _register(c, "rl@example.com")
    codes = [c.post("/api/app/auth/login", json={"email": "rl@example.com", "password": "nope-nope"}).status_code
             for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_google_login_creates_then_links(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(accounts, "verify_google_token",
                        lambda cred: accounts.identity_from_claims({"iss": "accounts.google.com", "sub": "g1",
                                                                    "email": "g@gmail.com", "email_verified": True}))
    r = c.post("/api/app/auth/google", json={"credential": "x" * 40})
    assert r.status_code == 200 and r.json()["is_new_user"] is True
    assert c.post("/api/app/auth/google", json={"credential": "x" * 40}).json()["is_new_user"] is False
    assert c.get("/api/auth/config").json()["google_client_id"] == "cid.apps.googleusercontent.com"


def test_otp_flow_binds_verified_number_and_records_cost(env, monkeypatch):
    c, eng, _ = env
    eng.demo_mode = False
    sent = {}

    class Fake(accounts.OtpProvider):
        name, cost_usd = "fake-sms", 0.05

        def send(self, phone, code):
            sent[phone] = code

    monkeypatch.setattr(accounts, "otp_provider", lambda: Fake())
    h = _register(c, "otp@example.com")
    assert c.post("/api/app/phone/claim-demo", headers=h).status_code == 409   # không cấp số giả ở chế độ thật
    assert c.post("/api/app/phone/otp/start", headers=h, json={"phone": "0912 345 678"}).status_code == 200
    assert c.post("/api/app/phone/otp/verify", headers=h, json={"phone": "0912345678", "code": "000000"}).status_code in (400,)
    ok = c.post("/api/app/phone/otp/verify", headers=h, json={"phone": "0912345678", "code": sent["84912345678"]})
    assert ok.status_code == 200 and ok.json() == {"msisdn": "84912345678", "kind": "verified"}
    other = _register(c, "other@example.com")
    assert c.post("/api/app/phone/otp/start", headers=other, json={"phone": "0912345678"}).status_code == 409
    boss = _login_op(c, "t-boss")
    usage = c.get("/api/admin/costs", headers=boss).json()["external_usage"]
    assert usage["total_usd"] == pytest.approx(0.05)


def test_reports_and_family_persist_to_database(env):
    c, eng, _ = env
    a = _register(c, "fam1@example.com")
    b = _register(c, "fam2@example.com")
    na = c.post("/api/app/phone/claim-demo", headers=a).json()["msisdn"]
    nb = c.post("/api/app/phone/claim-demo", headers=b).json()["msisdn"]
    assert c.post(f"/api/app/report?number=0987654321&reason=gia%20danh%20cong%20an", headers=a).status_code == 200
    assert c.post(f"/api/app/family/invite?number={nb}", headers=a).status_code == 200
    assert c.post(f"/api/app/family/respond?guardian={na}&accept=true", headers=b).status_code == 200
    db = portal.db()
    assert db.one("SELECT COUNT(*) n FROM number_reports")["n"] == 1
    assert db.one("SELECT protected, guardian FROM guardians")["guardian"] == na
    # Khởi động lại engine: dữ liệu được nạp lại từ CSDL.
    fresh = FakeEngine()
    portal.hydrate_engine(fresh)
    assert na in fresh.guardians[nb] and "84987654321" in fresh.reports and nb in fresh.inbox


# ---------------------------------------------------------------- nhân viên
def test_operator_password_login_and_admin_management(env):
    c, _, users = env
    boss = _login_op(c, "t-boss")
    r = c.post("/api/admin/operators", headers=boss, json={"name": "new.analyst", "role": "analyst",
                                                           "email": "NA@corp.vn", "password": "mat-khau-dai-123"})
    assert r.status_code == 200 and r.json()["has_password"]
    assert c.post("/api/admin/operators", headers=boss, json={"name": "x", "role": "viewer"}).status_code == 422
    login = c.post("/api/login", json={"username": "new.analyst", "password": "mat-khau-dai-123"})
    assert login.status_code == 200 and login.json()["role"] == "analyst"
    assert c.post("/api/login", json={"username": "new.analyst", "password": "wrong"}).status_code == 401
    ana = {"Authorization": "Bearer " + login.json()["session"]}
    assert c.get("/api/admin/operators", headers=ana).status_code == 403
    # Khoá tài khoản: phiên đang mở bị vô hiệu ngay.
    c.post("/api/admin/operators", headers=boss, json={"name": "new.analyst", "role": "analyst", "disabled": True})
    assert c.get("/api/whoami", headers=ana).status_code in (401, 403)
    assert c.post("/api/login", json={"username": "new.analyst", "password": "mat-khau-dai-123"}).status_code == 401
    # Không tự khoá mình.
    assert c.post("/api/admin/operators", headers=boss, json={"name": "boss", "role": "admin", "disabled": True}).status_code == 400


def test_operator_google_login_requires_linked_email(env, monkeypatch):
    c, _, users = env
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    users["ana"]["email"] = "ana@corp.vn"
    ident = {"iss": "accounts.google.com", "sub": "s", "email_verified": True}
    monkeypatch.setattr(accounts, "verify_google_token",
                        lambda cred: accounts.identity_from_claims({**ident, "email": "stranger@gmail.com"}))
    assert c.post("/api/login/google", json={"credential": "x" * 40}).status_code == 403
    monkeypatch.setattr(accounts, "verify_google_token",
                        lambda cred: accounts.identity_from_claims({**ident, "email": "ANA@corp.vn"}))
    r = c.post("/api/login/google", json={"credential": "x" * 40})
    assert r.status_code == 200 and r.json()["name"] == "ana"


def test_bootstrap_admin_password_from_env(env, monkeypatch):
    _, _, users = env
    monkeypatch.setenv("FRAUD_ADMIN_USER", "boss")
    monkeypatch.setenv("FRAUD_ADMIN_PASSWORD", "khoi-tao-123456")
    assert security.bootstrap_admin_password() == "boss"
    assert security.login_password("boss", "khoi-tao-123456") is not None
    assert security.bootstrap_admin_password() is None      # không ghi đè mật khẩu đã có


# ---------------------------------------------------------------- duyệt báo cáo
def test_analyst_confirms_report_and_fallback_blocks_number(env):
    c, eng, _ = env
    a = _register(c, "rep@example.com")
    c.post("/api/app/phone/claim-demo", headers=a)
    c.post("/api/app/report?number=0987000111&reason=lua%20dao", headers=a)
    vic, ana = _login_op(c, "t-vic"), _login_op(c, "t-ana")
    assert c.get("/api/admin/reports", headers=vic).status_code == 403
    rows = c.get("/api/admin/reports", headers=ana).json()
    assert rows[0]["number_masked"] == security.mask_number("84987000111") and "number" not in rows[0]
    r = c.post(f"/api/admin/reports/{rows[0]['key']}/review", headers=ana, json={"decision": "confirm", "note": "đối chiếu CDR"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert eng.g.fallback.manual["84987000111"].startswith("REPORT-")
    assert c.get("/api/admin/reports", headers=ana).json() == []
    assert c.post(f"/api/admin/reports/{rows[0]['key']}/review", headers=ana, json={"decision": "dismiss"}).status_code == 404


# ---------------------------------------------------------------- chi phí, pipeline, metrics
def test_costs_pipeline_and_metrics(env, monkeypatch, tmp_path):
    c, eng, _ = env
    boss = _login_op(c, "t-boss")
    assert c.post("/api/admin/costs/infra", headers=boss, json={"month": "2026-11", "provider": "Oracle", "item": "VM", "amount_usd": 0}).status_code == 200
    summary = c.get("/api/admin/costs", headers=boss).json()
    assert summary["compute"]["processed_decisions"] == 1000 and summary["compute"]["cpu_seconds"] > 0
    monkeypatch.setenv("FRAUD_BACKUP_DIR", str(tmp_path / "bk"))
    for job in ("kpi_snapshot", "backup", "retention"):
        r = c.post(f"/api/admin/pipeline/run?job={job}", headers=boss)
        assert r.status_code == 200 and r.json()["status"] in ("ok", "warn"), r.text
    p = c.get("/api/admin/pipeline", headers=_login_op(c, "t-ana")).json()
    assert p["kpi"][-1]["blocked"] == 40 and p["kpi"][-1]["drift_max_psi"] is not None
    assert (tmp_path / "bk").exists()

    import observability
    observability.register(lambda: eng, lambda: portal._db)
    monkeypatch.setenv("METRICS_TOKEN", "tok")
    assert c.get("/metrics").status_code == 401
    text = c.get("/metrics", headers={"Authorization": "Bearer tok"}).text
    assert 'tfs_decisions{decision="BLOCK"} 40.0' in text
    assert 'tfs_pipeline_last_status{task="backup"} 1.0' in text
    assert "tfs_http_requests_total" in text


def test_admin_and_app_are_separate_pages(env):
    c, _, _ = env
    assert c.get("/", follow_redirects=False).headers["location"] == "/app/"
    assert "Đăng nhập vận hành" in c.get("/admin/").text   # vòng 5: tiêu đề viết hoa đầu câu (R5-UX2)
    assert "TÀI KHOẢN NHÂN VIÊN" in c.get("/admin/manage").text
    assert "Tạo tài khoản" in c.get("/app/").text


def test_subscriber_alerts_survive_restart(env):
    c, eng, _ = env
    h = _register(c, "persist@example.com")
    num = c.post("/api/app/phone/claim-demo", headers=h).json()["msisdn"]
    portal.hydrate_engine(eng)                      # gắn hook lưu bền
    for i in range(3):
        item = {"id": f"E{i}", "type": "CALL", "decision": "BLOCK", "from": "849000", "reasons": []}
        eng.inbox[num].appendleft(item)
        eng.inbox_hook(num, item)
    portal.alert_sink.flush()
    restarted = FakeEngine()
    portal.hydrate_engine(restarted)
    assert [i["id"] for i in restarted.inbox[num]] == ["E2", "E1", "E0"]   # mới nhất đứng đầu


def test_real_engine_pushes_inbox_through_hook():
    import server

    eng = server.LiveEngine.__new__(server.LiveEngine)
    eng.inbox = collections.defaultdict(lambda: collections.deque(maxlen=50))
    seen = []
    eng.inbox_hook = lambda who, item: seen.append((who, item["id"]))
    eng._push_inbox("84900000001", {"id": "X1"})
    assert seen == [("84900000001", "X1")] and eng.inbox["84900000001"][0]["id"] == "X1"


# ---------------------------------------------------------------- chạy thử 1 phút trên app
def test_app_simulate_stream_sends_mixed_events_to_the_subscriber(env, monkeypatch):
    import server

    c, eng, _ = env
    sent = []
    eng.inject = lambda kind, target=None: sent.append((kind, target)) or []
    monkeypatch.setattr(server, "DEMO_MODE", True)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    a = _register(c, "stream@example.com")
    num = c.post("/api/app/phone/claim-demo", headers=a).json()["msisdn"]
    r = c.post("/api/app/simulate-stream", headers=a)
    assert r.status_code == 200 and r.json()["events"] == len(server.APP_STREAM_KINDS)
    for t in threading.enumerate():
        if t.name == "app-stream":
            t.join(5)
    assert sorted(k for k, _ in sent) == sorted(server.APP_STREAM_KINDS)
    assert {t for _, t in sent} == {num}                 # mọi sự kiện nhắm đúng số của người dùng
    assert num not in server._app_streams                # chạy xong thì được chạy lại


def test_app_simulate_stream_is_demo_only_and_not_concurrent(env, monkeypatch):
    import server

    c, eng, _ = env
    a = _register(c, "stream2@example.com")
    num = c.post("/api/app/phone/claim-demo", headers=a).json()["msisdn"]
    monkeypatch.setattr(server, "DEMO_MODE", False)
    assert c.post("/api/app/simulate-stream", headers=a).status_code == 403
    monkeypatch.setattr(server, "DEMO_MODE", True)
    monkeypatch.setitem(server._app_streams, num, server.time.time() + 60)
    assert c.post("/api/app/simulate-stream", headers=a).status_code == 409
