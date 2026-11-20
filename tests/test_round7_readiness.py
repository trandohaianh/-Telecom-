"""Rà soát sẵn sàng production (17/11/2026, reports/06). Mỗi test thất bại trên mã trước bản sửa.

Run: python -B -m pytest -q tests/test_round7_readiness.py
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import accounts
import portal
import security
from appdb import AppDB
from test_portal_accounts import _register, env  # noqa: F401  (fixture dùng lại)

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    from _pages import page_source  # HTML + the page's local .js files (scripts moved out for the CSP)

    return page_source(rel)


# =========================================================================== backend
def test_push_key_returns_503_not_500_when_key_dir_is_read_only(env, monkeypatch, tmp_path):  # noqa: F811
    import push

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(push, "KEY_FILE", blocker / "secrets" / "vapid.pem")  # parent is a file -> OSError
    c, _, _ = env
    r = c.get("/api/app/push/key")
    assert r.status_code == 503  # disabled with a message; was an unhandled PermissionError (500)


def test_container_keeps_the_vapid_key_on_the_state_volume_and_requires_a_manifest():
    docker = read("Dockerfile")
    assert "FRAUD_VAPID_PRIVATE_KEY_FILE=/srv/state/secrets/vapid_private.pem" in docker
    assert "FRAUD_REQUIRE_MANIFEST=1" in docker


def test_misconfigured_otp_provider_fails_fast_and_never_500s_the_login_page(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FRAUD_OTP_PROVIDER", "sms-http")
    monkeypatch.delenv("FRAUD_SMS_HTTP_URL", raising=False)
    with pytest.raises(accounts.OtpConfigError, match="FRAUD_SMS_HTTP_URL"):
        accounts.otp_provider()
    monkeypatch.setenv("FRAUD_OTP_PROVIDER", "typo")
    with pytest.raises(accounts.OtpConfigError, match="unknown"):
        accounts.otp_provider()
    c, eng, _ = env
    eng.demo_mode = False
    r = c.get("/api/auth/config")
    assert r.status_code == 200 and r.json()["phone_verification"] == "unavailable"
    src = read("src/portal.py").split("def startup", 1)[1].split("\ndef ", 1)[0]
    assert "real_otp_provider(demo=False)" in src  # validated once at start-up


def test_empty_compose_values_mean_default_for_sms_template(monkeypatch):
    monkeypatch.setenv("FRAUD_SMS_HTTP_URL", "https://sms.example.vn/send")
    monkeypatch.setenv("FRAUD_SMS_HTTP_BODY", "")
    monkeypatch.setenv("FRAUD_SMS_BRANDNAME", "")
    p = accounts.HttpSmsOtp()
    assert p.brandname == "LACHAN" and p.render("84912345678", "123456")["to"] == "84912345678"
    monkeypatch.setenv("FRAUD_SMS_HTTP_BODY", "{not json")
    monkeypatch.setenv("FRAUD_OTP_PROVIDER", "sms-http")
    with pytest.raises(accounts.OtpConfigError):
        accounts.otp_provider()


def test_wrong_passwords_from_another_ip_cannot_lock_the_admin_out(env, monkeypatch):  # noqa: F811
    import server

    _, _, users = env
    name = next(iter(users))
    monkeypatch.setattr(
        security, "login_password", lambda u, p: ("s", {"name": u, "role": "admin"}, 0) if p == "right" else None
    )
    attacker = TestClient(server.app, client=("203.0.113.9", 4000))
    owner = TestClient(server.app, client=("198.51.100.7", 4000))
    for _ in range(12):
        attacker.post("/api/login", json={"username": name, "password": "wrong"})
    assert attacker.post("/api/login", json={"username": name, "password": "wrong"}).status_code == 429
    assert owner.post("/api/login", json={"username": name, "password": "right"}).status_code == 200
    # a correct login is not counted as a failure
    for _ in range(12):
        assert owner.post("/api/login", json={"username": name, "password": "right"}).status_code == 200


def test_subscriber_lockout_counts_only_failures_per_ip(env):  # noqa: F811
    import server

    c, _, _ = env
    _register(c, "lock@example.com", "dung-mat-khau-1")
    attacker = TestClient(server.app, client=("203.0.113.10", 4000))
    for _ in range(11):
        attacker.post("/api/app/auth/login", json={"email": "lock@example.com", "password": "sai-mat-khau-x"})
    victim = TestClient(server.app, client=("198.51.100.8", 4000))
    assert (
        victim.post(
            "/api/app/auth/login", json={"email": "lock@example.com", "password": "dung-mat-khau-1"}
        ).status_code
        == 200
    )


def test_oversized_bodies_are_refused_before_any_route_reads_them(env):  # noqa: F811
    c, _, _ = env
    big = b"x" * (1024 * 1024 + 10)
    r = c.post("/api/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.headers["x-frame-options"] == "DENY"

    def chunks():
        for _ in range(40):
            yield b"y" * 65536

    assert (
        c.post("/api/app/auth/login", content=chunks(), headers={"content-type": "application/json"}).status_code == 413
    )
    import http_hardening

    mw = http_hardening.BodyLimitMiddleware(None, max_bytes=4096, large_bytes=10 << 20)
    assert mw._limit("/api/v1/audio") == 10 << 20 and mw._limit("/api/login") == 4096


def test_invalid_body_limit_setting_is_rejected(monkeypatch):
    import http_hardening

    for bad in ("abc", "10", str(1 << 30)):
        monkeypatch.setenv("FRAUD_MAX_BODY_BYTES", bad)
        with pytest.raises(ValueError):
            http_hardening.max_body_bytes()


def test_security_headers_on_normal_and_unhandled_error_responses(env, monkeypatch):  # noqa: F811
    import server

    c, _, _ = env
    r = c.get("/health/live")
    assert r.headers["referrer-policy"] == "same-origin" and "object-src 'none'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers  # plain http
    hs = TestClient(server.app, base_url="https://testserver").get("/health/live")
    assert hs.headers["strict-transport-security"].startswith("max-age=")

    def boom():
        raise RuntimeError("secret internal detail")

    server.app.add_api_route("/__test_boom", boom)
    try:
        r = TestClient(server.app, raise_server_exceptions=False).get("/__test_boom")
    finally:
        server.app.router.routes = [x for x in server.app.router.routes if getattr(x, "path", "") != "/__test_boom"]
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error"}
    assert "secret internal detail" not in r.text and r.headers["x-content-type-options"] == "nosniff"


def test_declining_unknown_guardians_creates_no_state(env):  # noqa: F811
    c, eng, _ = env
    h = _register(c, "fam7@example.com")
    c.post("/api/app/phone/claim-demo", headers=h)
    before = len(eng.inbox)
    for i in range(20):
        r = c.post(f"/api/app/family/respond?guardian=random-{i}&accept=false", headers=h)
        assert r.status_code == 404
    assert len(eng.inbox) == before


def test_non_ascii_credentials_are_a_401_not_a_500(env):  # noqa: F811
    c, _, _ = env
    assert security._same_secret("abc", "ábc") is False and security._same_secret("abc", "abc") is True
    r = c.post("/api/login", headers={"Authorization": "Bearer tést".encode("latin-1")})
    assert r.status_code == 401


def test_audit_page_size_is_bounded():
    src = read("src/server.py")
    assert "n: int = Query(default=20, ge=1, le=500)" in src.split("def api_audit(", 1)[1].split(")", 1)[0] + ")"


def test_evidence_tail_skips_a_torn_last_line(tmp_path):
    import server

    p = tmp_path / "ev.jsonl"
    rows = [json.dumps({"prev": "0", "hash": str(i), "record": {"i": i}}) for i in range(300)]
    p.write_text("\n".join(rows) + "\n" + '{"prev": "x", "hash"', encoding="utf-8")  # writer mid-line
    got = server._tail_evidence(p, 5, chunk=256)
    assert [r["record"]["i"] for r in got] == [295, 296, 297, 298, 299]
    assert server._tail_evidence(tmp_path / "missing.jsonl", 5) == []


def test_malformed_pipeline_schedule_fails_at_start(monkeypatch):
    import ops_pipeline

    assert ops_pipeline.parse_daily_at("02:30") == (2, 30)
    for bad in ("2:61", "25:00", "noon", "1"):
        with pytest.raises(ValueError):
            ops_pipeline.parse_daily_at(bad)
    monkeypatch.setenv("FRAUD_PIPELINE_DAILY_AT", "bad")
    with pytest.raises(ValueError):
        ops_pipeline.Scheduler(AppDB(":memory:"), lambda: None)


def test_app_db_migrations_add_indexes_to_existing_databases(tmp_path):
    from appdb import MIGRATIONS

    path = tmp_path / "old.sqlite3"
    AppDB(path)
    con = sqlite3.connect(path)
    con.execute("DROP INDEX ix_push_sub")
    con.execute("PRAGMA user_version=0")  # an old database created before migrations existed
    con.commit()
    con.close()
    AppDB(path)
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_push_sub", "ix_guardians_guardian", "ix_invites_guardian", "ix_usage_sub"} <= names
    con.close()


# =========================================================================== ops / deploy
def test_compose_forwards_documented_settings_and_rotates_logs():
    compose = yaml.safe_load(read("deploy/cloud/docker-compose.yml"))
    env_ = compose["services"]["telecom"]["environment"]
    for name in (
        "FRAUD_REAL_TRAFFIC_VALIDATED",
        "FRAUD_SMS_HTTP_URL",
        "FRAUD_SMS_HTTP_TOKEN_FILE",
        "FRAUD_SMS_HTTP_BODY",
        "FRAUD_SMS_BRANDNAME",
        "FRAUD_PUSH",
        "FRAUD_VAPID_SUBJECT",
        "FRAUD_SQLITE_SYNCHRONOUS",
        "LOG_LEVEL",
    ):
        assert name in env_, name
    assert env_["FRAUD_SQLITE_SYNCHRONOUS"].endswith(":-FULL}")
    assert compose["services"]["telecom"]["logging"]["options"]["max-size"] == "10m"
    example = read("deploy/cloud/.env.example")
    for name in ("FRAUD_SMS_HTTP_URL", "FRAUD_VAPID_SUBJECT", "FRAUD_REAL_TRAFFIC_VALIDATED", "FORWARDED_ALLOW_IPS"):
        assert re.search(rf"^{name}=", example, re.M), name


def test_standalone_caddy_uses_the_hardened_caddyfile():
    compose = yaml.safe_load(read("deploy/cloud/docker-compose.yml"))
    caddy = compose["services"]["caddy"]
    assert "command" not in caddy and any("standalone.Caddyfile:/etc/caddy/Caddyfile" in v for v in caddy["volumes"])
    cf = read("deploy/cloud/standalone.Caddyfile")
    assert "{$TELECOM_DOMAIN}" in cf and "Strict-Transport-Security" in cf and "max_size 16MB" in cf


def test_workflows_are_least_privilege_and_manual_deploy_needs_a_tested_sha():
    deploy = yaml.safe_load(read(".github/workflows/deploy.yml"))
    ci = yaml.safe_load(read(".github/workflows/ci.yml"))
    assert deploy["permissions"] == {"contents": "read"} and ci["permissions"] == {"contents": "read"}
    on = deploy.get("on", deploy.get(True))
    assert on["workflow_dispatch"]["inputs"]["sha"]["required"] is True
    assert deploy["jobs"]["deploy"]["environment"] == "production"
    assert "inputs.sha" in read(".github/workflows/deploy.yml")


def test_setup_creates_env_with_private_permissions_and_image_skips_test_logs():
    setup = read("deploy/cloud/setup.sh")
    assert setup.index("umask 077") < setup.index("cp .env.example .env")
    assert "runtime/artifacts_v22/logs" in read(".dockerignore")
    assert "os.environ.get('PORT','8080')" in read("Dockerfile")


def test_direct_imports_are_pinned():
    serve = read("requirements-serve.txt")
    assert re.search(r"^py-vapid==", serve, re.M) and re.search(r"^cryptography==", serve, re.M)
    assert "pywebpush" in read("requirements.txt")


# =========================================================================== frontend
def test_admin_api_helper_rejects_every_non_2xx_and_polling_pauses_when_hidden():
    page = read("static/admin/index.html")
    api = page.split("async function api(url, opts = {}){", 1)[1].split("\n}", 1)[0]
    assert "if (!r.ok) throw" in api
    assert "document.hidden" in page.split("async function poll()", 1)[1].split("\n}", 1)[0]
    assert "if (!document.hidden) pollAgent()" in page
    assert "netFail()" in page.split("$('#lform').onsubmit", 1)[1].split("};", 1)[0]
    assert "(T[a.event_type] || [a.event_type, a.event_type])" in page


def test_subscriber_app_never_ends_on_a_blank_screen_and_escapes_family_numbers():
    page = read("static/app/index.html")
    after = page.split("async function afterLogin(me){", 1)[1].split("\n}", 1)[0]
    assert "catch(e){ return; }" not in after and "show('login')" in after
    assert ".map(pretty)" not in page


def test_service_worker_offline_deep_link_and_notification_navigation():
    sw = read("static/app/sw.js")
    assert "e.request.mode === 'navigate' ? caches.match('index.html')" in sw
    assert "c.navigate(target)" in sw and int(re.search(r"la-chan-v(\d+)", sw).group(1)) >= 5


def test_manage_page_escapes_and_encodes_and_never_fails_silently():
    page = read("static/admin/manage.html")
    assert "encodeURIComponent(b.dataset.k)" in page and "encodeURIComponent(b.dataset.j)" in page
    assert 'data-r="${esc(o.role)}"' in page and "catch(e){ fail(e); return; }" in page
    assert "overflow-x:auto" in page


# =========================================================================== quyết định chủ dự án 17/11/2026
def test_demo_mode_insider_lockout_survives_a_restart(env, monkeypatch):  # noqa: F811
    _, _, users = env
    name = next(n for n, u in users.items() if u.get("role") in security.ROLES)
    monkeypatch.setattr(security, "_session_store", None)  # demo: no durable session store
    security.suspend(name)
    try:
        saved = json.loads(Path(security.USERS_FILE).read_text(encoding="utf-8"))
        assert saved[name]["disabled"] is True  # persisted, not only in RAM
    finally:
        security.suspended.discard(name)


def test_console_otp_is_refused_outside_demo_mode(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FRAUD_OTP_PROVIDER", "console")
    assert portal.real_otp_provider(demo=True).name == "console"
    with pytest.raises(accounts.OtpConfigError, match="FRAUD_DEMO=1"):
        portal.real_otp_provider(demo=False)
    c, eng, _ = env
    eng.demo_mode = False
    assert c.get("/api/auth/config").json()["phone_verification"] == "unavailable"


def test_android_release_cannot_be_pointed_at_another_server():
    kt = read("android/app/src/main/java/vn/lachan/fraudshield/MainActivity.kt")
    assert "if (!BuildConfig.DEBUG) BuildConfig.DEFAULT_SERVER" in kt
    assert "visibility = android.view.View.GONE" in kt and "if (!BuildConfig.DEBUG) return" in kt


# =========================================================================== dry run Docker thật (17/11/2026)
def test_demo_evidence_rotates_and_both_chains_stay_valid(tmp_path):
    import time as _t

    from backend_evidence import DurableEvidenceLog
    from backend_state import BackendState
    from evidence import verify

    path = tmp_path / "evidence_demo.jsonl"
    log = DurableEvidenceLog(path, BackendState(), rotate_bytes=1 << 20)
    pad = "x" * 900
    for i in range(1600):  # ~1.6 MB of FLAG records
        log.submit({"event_id": f"E{i}", "decision": "FLAG", "pad": pad})
        if i % 200 == 0:
            _t.sleep(0.05)
    deadline = _t.time() + 10
    while log.rotations == 0 and _t.time() < deadline:
        _t.sleep(0.05)
    log.close()
    assert log.rotations >= 1 and log.rotated_path().exists()
    assert path.stat().st_size < (1 << 20) + 200_000
    for f in (path, log.rotated_path()):
        ok, n, bad = verify(f)
        assert ok and n > 0 and bad is None
    with pytest.raises(ValueError):
        DurableEvidenceLog(tmp_path / "x.jsonl", BackendState(), rotate_bytes=1000)


def test_only_demo_mode_rotates_evidence(monkeypatch):
    import server

    src = read("src/server.py")
    assert "rotate_bytes=_demo_evidence_cap() if self.demo_mode else None" in src
    assert server._demo_evidence_cap() == 256 << 20
    for bad in ("1", "abc", "999999"):
        monkeypatch.setenv("FRAUD_DEMO_EVIDENCE_MAX_MB", bad)
        with pytest.raises(ValueError):
            server._demo_evidence_cap()
    compose = yaml.safe_load(read("deploy/cloud/docker-compose.yml"))
    assert "FRAUD_DEMO_EVIDENCE_MAX_MB" in compose["services"]["telecom"]["environment"]
