"""Các mục còn lại của reports/06 §4 sửa ngày 20/11/2026. Mỗi test thất bại trên mã trước bản sửa."""

import re
import time
from pathlib import Path

import pytest
import yaml

from backend_state import BackendState
from test_portal_accounts import env  # noqa: F401  (fixture dùng lại)

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["static/app/index.html", "static/admin/index.html", "static/admin/manage.html"]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# =========================================================================== CSP script-src
def test_pages_have_no_inline_scripts_or_inline_handlers():
    for page in PAGES:
        html = read(page)
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page  # every script has src=
        assert not re.search(r"\son[a-z]+\s*=", html), page  # no onclick= / onload= ...
        assert "javascript:" not in html, page


def test_csp_refuses_inline_scripts_but_api_docs_keep_working(env):  # noqa: F811
    c, _, _ = env
    csp = c.get("/health/live").headers["content-security-policy"]
    assert "script-src 'self' https://accounts.google.com" in csp and "'unsafe-inline'" not in csp
    assert "script-src" not in c.get("/docs").headers["content-security-policy"]  # FastAPI docs exempt
    for asset in (
        "/admin/assets/dashboard.js",
        "/admin/assets/manage.js",
        "/admin/assets/theme-init.js",
        "/app/app.js",
        "/app/theme-init.js",
        "/app/theme-toggle.js",
    ):
        r = c.get(asset)
        assert r.status_code == 200 and "javascript" in r.headers["content-type"], asset


def test_service_worker_caches_the_moved_scripts_for_offline_start():
    sw = read("static/app/sw.js")
    for name in ("app.js", "theme-init.js", "theme-toggle.js"):
        assert f"'{name}'" in sw.split("const SHELL", 1)[1].split("];", 1)[0]


def test_navigation_buttons_use_data_href_with_a_listener():
    assert 'data-href="/admin/manage"' in read("static/admin/index.html")
    assert "querySelectorAll('[data-href]')" in read("static/admin/assets/dashboard.js")
    assert "querySelectorAll('[data-href]')" in read("static/admin/assets/manage.js")


# =========================================================================== Android: Google button
def test_google_button_is_not_offered_inside_the_android_webview():
    js = read("static/app/app.js")
    init = js.split("async function initGoogle(){", 1)[1]
    assert init.index("LaChanAndroid") < init.index("accounts.google.com/gsi/client")
    assert "LaChanAndroid/" in read("android/app/src/main/java/vn/lachan/fraudshield/MainActivity.kt")


# =========================================================================== base images by digest
def test_every_base_image_is_pinned_by_digest():
    for df in ("Dockerfile", "deploy/kafka-demo/Dockerfile", "telecom_grpc/Dockerfile", "spark/Dockerfile"):
        froms = re.findall(r"^FROM\s+(\S+)", read(df), re.M)
        assert froms and all("@sha256:" in f for f in froms), df
    for compose in (
        "deploy/cloud/docker-compose.yml",
        "deploy/kafka-demo/docker-compose.yml",
        "spark/docker-compose.yml",
    ):
        for name, svc in yaml.safe_load(read(compose))["services"].items():
            image = svc.get("image", "")
            if "build" not in svc and image:
                assert "@sha256:" in image, (compose, name)


# =========================================================================== journal retention
def _row(state, eid, received, exported=True, result=True):
    seq, _ = state.reserve("VERDICT", {"event_id": eid, "ts": 1.0})
    if result:
        state.finish(seq, {"decision": "ALLOW"})
    state.db.execute("UPDATE events SET received=?, evidence_exported=? WHERE seq=?", (received, int(exported), seq))
    return seq


def test_retention_deletes_only_rows_that_nothing_needs(tmp_path):
    state = BackendState(tmp_path / "s.sqlite3", background_checkpoint=False)
    old, new = time.time() - 40 * 86400, time.time()
    with state.transaction():
        a = _row(state, "old-done", old)
        _row(state, "old-not-exported", old, exported=False)
        _row(state, "old-pending", old, result=False)
        _row(state, "new-done", new)
        b = _row(state, "old-after-checkpoint", old)
    deleted = state.prune_events(time.time() - 30 * 86400, max_seq=b - 1)
    left = {r[0] for r in state.db.execute("SELECT event_id FROM events")}
    assert deleted == 1 and "old-done" not in left
    assert {"old-not-exported", "old-pending", "new-done", "old-after-checkpoint"} <= left
    assert a < b
    state.close()


def test_runtime_retention_is_hourly_idle_only_and_configurable(monkeypatch):
    src = read("src/backend_runtime.py")
    loop = src.split("def _loop(self):", 1)[1].split("\n    def ", 1)[0]
    idle = loop.split("except queue.Empty:", 1)[1].split("continue", 1)[0]
    assert "self._retention()" in idle  # only when the queue is empty
    assert "now - self._last_retention < 3600" in src
    from backend_runtime import event_retention_days

    assert event_retention_days() == 30
    monkeypatch.setenv("FRAUD_EVENT_RETENTION_DAYS", "0")
    assert event_retention_days() == 0
    for bad in ("forever", "-1", "99999"):
        monkeypatch.setenv("FRAUD_EVENT_RETENTION_DAYS", bad)
        with pytest.raises(ValueError, match="FRAUD_EVENT_RETENTION_DAYS"):
            event_retention_days()
