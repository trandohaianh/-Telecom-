"""Hồi quy vòng 4 (đánh giá đa vai trò, 17/11/2026). Mỗi test thất bại trên mã trước vòng 4.

Run: python -B -m pytest -q tests/test_audit_regressions_round4.py
"""
import collections
import importlib.util
import json
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

import accounts
import portal
import security
from test_portal_accounts import _login_op, _register, env  # noqa: F401  (fixture dùng lại)

ROOT = Path(__file__).resolve().parents[1]
CALL = {"event_type": "CALL", "caller": "84961234567", "callee": "84351234567", "cell_id": "HN-0001",
        "imei": "35391110000001", "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": 500}


def _src(*parts):
    if parts[0] == "static" and parts[-1].endswith(".html"):
        from _pages import page_source  # HTML + the page's local .js files (scripts moved out for the CSP)
        return page_source("/".join(parts))
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


# =========================================================================== bảo mật (N1–N10)
def test_voice_upload_has_a_total_read_deadline(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    import voice_api
    monkeypatch.setattr(voice_api, "UPLOAD_READ_TIMEOUT_S", 0.2)
    router = voice_api.create_voice_router(model=None, asr=None)
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/v1/audio")

    class SlowRequest:                                    # slowloris: one byte every 150 ms, never finishes
        headers = {"x-timestamp": security.network_timestamp(), "x-signature": "00"}

        async def stream(self):
            for _ in range(20):
                await asyncio.sleep(0.15)
                yield b" "

    async def run():
        with pytest.raises(HTTPException) as exc:
            await endpoint(SlowRequest())
        return exc.value.status_code
    assert asyncio.run(run()) == 408                     # trước: đọc mãi (không có hạn), giữ slot upload


def test_metrics_method_label_is_bounded():
    import observability
    req = types.SimpleNamespace(method="AAAA1", scope={}, url=types.SimpleNamespace(path="/x"))
    observability.observe(req, 404, time.perf_counter())
    text = observability.generate_latest().decode() if hasattr(observability, "generate_latest") else ""
    assert 'method="AAAA1"' not in text and 'method="OTHER"' in text


def test_number_history_is_forgotten_on_release_and_demo_assignment(env, monkeypatch):  # noqa: F811
    c, eng, _ = env
    calls = []
    monkeypatch.setattr(portal, "forget_number_history", lambda n: calls.append(n))
    h = _register(c, "rel@example.com")
    number = c.post("/api/app/phone/claim-demo", headers=h).json()["msisdn"]
    assert calls == [number]                           # số giả lập tái cấp không mang lịch sử cũ
    eng.demo_mode = False
    sent = {}

    class Fake(accounts.OtpProvider):
        name, cost_usd = "fake", 0.0

        def send(self, phone, code):
            sent[phone] = code
    monkeypatch.setattr(accounts, "otp_provider", lambda: Fake())
    c.post("/api/app/phone/otp/start", headers=h, json={"phone": "0912000222"})
    c.post("/api/app/phone/otp/verify", headers=h, json={"phone": "0912000222", "code": sent["84912000222"]})
    assert "84912000222" in calls and number in calls  # số cũ được dọn khi nhả


def test_main_app_cannot_be_framed(env):  # noqa: F811
    c, _, _ = env
    r = c.get("/api/auth/config")
    assert r.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")


def test_audit_anchor_parameters_are_validated(env):  # noqa: F811
    c, _, _ = env
    h = _login_op(c, "t-boss")
    assert c.get("/api/audit?anchor_records=-2&anchor_hash=" + "a" * 64, headers=h).status_code == 422
    assert c.get("/api/audit?anchor_records=1", headers=h).status_code == 422
    assert c.get("/api/audit?anchor_hash=" + "a" * 64, headers=h).status_code == 422


def test_lookup_is_rate_limited(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FRAUD_RATE_LIMIT", "1")
    c, eng, _ = env
    eng.reputation = collections.defaultdict(collections.Counter)
    a = _register(c, "look@example.com")
    c.post("/api/app/phone/claim-demo", headers=a)
    codes = [c.get(f"/api/app/lookup?number=0987{i:06d}", headers=a).status_code for i in range(121)]
    assert codes[-1] == 429 and set(codes[:120]) == {200}


def test_service_worker_caches_only_successful_same_origin_responses():
    sw = _src("static", "app", "sw.js")
    assert "url.origin !== self.location.origin" in sw and "if (r.ok)" in sw


def test_deploy_runs_from_a_private_copy_and_compose_hides_redis_password():
    assert "DEPLOY_SH_COPY" in _src("deploy", "cloud", "deploy.sh")
    assert "--pass" not in _src("edu_dss", "docker-compose.yml")


# --- fail-closed (N4/N5/N6, quyết định chủ dự án)
def test_deploy_fails_closed_without_pinned_host_key_or_proxy():
    wf = _src(".github", "workflows", "deploy.yml")
    assert "StrictHostKeyChecking=accept-new" not in wf and "::error::" in wf
    proxy = _src("deploy", "cloud", "proxy_ip.sh")
    assert 'echo "*"' not in proxy and "exit 1" in proxy
    # 20/11/2026: default is now the own Caddy's STATIC IP (fixed subnet), still never "*" (fail-closed)
    assert "FORWARDED_ALLOW_IPS:-${TELECOM_CADDY_IP:-172.29.10.2}" in _src("deploy", "cloud", "docker-compose.yml")
    assert 'os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")' in _src("serve.py")


def test_edudss_requires_audience_outside_demo():
    main = _src("edu_dss", "backend", "app", "main.py")
    assert "JWT_AUDIENCE is required outside demo mode" in main
    assert "JWT_AUDIENCE:" in _src("edu_dss", "docker-compose.yml")


# =========================================================================== staff engineer
def test_future_tolerance_is_below_the_lateness_floor():
    import server
    from ingest_guard import DEFAULT_ALLOWED_LATENESS_S
    assert server.NETWORK_MAX_FUTURE_S < DEFAULT_ALLOWED_LATENESS_S


def test_rescore_copies_only_touched_keys():
    from feature_store import FeatureStore
    from resilience import GuardedDetector
    fs = FeatureStore()

    def boom():
        raise AssertionError("whole-store snapshot")
    fs.state_dict = boom

    class Det:
        def __init__(self):
            self.fs, self.harvest_hits = fs, {}

        def score_batch(self, events):
            out = []
            for e in events:
                fs.call_features(e)
                out.append({"event_id": e["event_id"], "event_type": "CALL", "decision": "ALLOW", "reasons": [], "ts": e["ts"]})
            return out
    fs.call_features({**CALL, "event_id": "LIVE", "ts": 50.0, "caller": "84900000077"})
    before = json.dumps(FeatureStore.state_dict(fs), sort_keys=True, default=str)
    g = GuardedDetector(Det(), budget_ms=1000)
    g.rescore_queue.append({**CALL, "event_id": "R1", "ts": 100.0})
    g.drain_rescore(max_n=8)
    after = json.dumps(FeatureStore.state_dict(fs), sort_keys=True, default=str)
    assert before == after and not g.rescore_queue      # chấm lại không để lại dấu vết, không chụp cả store


def test_torn_audit_tail_does_not_stop_startup(tmp_path):
    users = {"boss": {"role": "admin", "token": "t", "sign_key": "aa" * 32}}
    log = security.AuditLog(tmp_path / "a.jsonl", user_db=users)
    log.record("boss", "x")
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"prev": "abc", "hash": ')            # mất điện giữa lúc ghi
    again = security.AuditLog(log.path, user_db=users)   # trước: JSONDecodeError khi import server
    assert again.torn_lines == 1
    again.record("boss", "y")
    out = security.verify_audit(log.path, users)
    assert out["valid"] is False and "malformed" in out["problem"]


def test_audio_parsing_runs_off_the_event_loop():
    src = _src("src", "voice_api.py")
    assert "asyncio.to_thread(" in src and "_parse_signed_upload" in src


def test_drift_counts_model_decision_when_delivery_expired():
    import server
    eng = types.SimpleNamespace(log=types.SimpleNamespace(submit=lambda r: None), lock=threading.RLock(),
                                network_stats=collections.Counter(), ingest_counts=collections.Counter(),
                                call_decisions=collections.Counter(), processed=0, by_type=collections.Counter(),
                                counts=collections.Counter(), lat=collections.deque(), _track=lambda e, r: None,
                                _alert=lambda r, e: None)
    late = {"event_id": "L", "decision": "ALLOW", "fail_open": True, "model_decision": "BLOCK", "scored": True,
            "ingest": "ADMITTED"}
    server.LiveEngine._record_live_result(eng, {**CALL, "event_id": "L", "ts": 1.0}, late)
    assert eng.call_decisions == {"BLOCK": 1}


def test_kafka_future_timestamp_is_retried_not_dead_lettered():
    import asyncio

    from ram_online.streaming import POISON_ERRORS, EventFromFuture, _is_poison
    assert not issubclass(EventFromFuture, POISON_ERRORS) and not _is_poison(EventFromFuture("x"))
    _ = asyncio


def test_first_seen_and_device_links_stay_causal_under_reordering():
    from feature_store import FeatureStore
    fs = FeatureStore()
    a, b = "84900000001", "84900000002"
    fs.call_features({**CALL, "event_id": "1", "ts": 2000.0, "caller": b, "callee": a, "imei": "35391110000099"})
    f = fs.call_features({**CALL, "event_id": "2", "ts": 1990.0, "caller": a, "callee": "84900000003",
                          "imei": "35391110000099"})
    assert f["active_hours_on_network"] >= 0 and fs.first_seen[a] == 1990.0
    assert f["msisdn_per_imei_24h"] == 1                # link của b (ts 2000) nằm ở "tương lai" của sự kiện này


def test_checkpoint_count_trigger_and_legacy_frontier_validation():
    import inspect

    from backend_runtime import InferenceRuntime
    from ingest_guard import IngestGuard
    assert inspect.signature(InferenceRuntime.__init__).parameters["checkpoint_every"].default >= 100_000
    state = IngestGuard().state_dict() | {"max_ts": 100.0, "legacy_frontier": 1e9}
    with pytest.raises(ValueError, match="legacy"):
        IngestGuard().load_state_dict(state)


# =========================================================================== UX
def test_operator_console_copy_and_accessibility():
    admin = _src("static", "admin", "index.html")
    assert 'id="ver">tfd-v2.0<' not in admin and "mục tiêu &lt;30 ms" in admin
    assert 'aria-label="Mật khẩu"' in admin and 'aria-label="Đóng bằng chứng"' in admin
    assert "--on-red:#1a0007" in admin and "<a href=\"/admin/manage\" class=\"analystonly\"" not in admin
    assert "holding('#feed')" in admin and "holding('#agent')" in admin
    assert "/api/reveal', { method: 'POST'" in admin and "reason=${encodeURIComponent" not in admin
    manage = _src("static", "admin", "manage.html")
    assert 'data-k="${esc(r.key)}"' in manage and "confirm('Xoá khoản chi phí này?')" in manage
    assert "Cần ghi căn cứ" in manage
    app = _src("static", "app", "index.html")
    assert 'aria-live="polite"' in app and 'aria-label="Email"' in app
    assert "demo-only" in app and "visibilityState" in app


def test_reveal_reason_is_accepted_in_the_body(env):  # noqa: F811
    c, eng, _ = env
    eng.alerts = collections.deque([{"event_id": "E1", "party": "84961234567", "counterparty": "84351234567",
                                     "imei": "1"}])
    eng.agent = types.SimpleNamespace(note_reveal=lambda name: None)
    h = _login_op(c, "t-ana")
    r = c.post("/api/reveal", headers=h, json={"event_id": "E1", "reason": "điều tra khiếu nại"})
    assert r.status_code == 200 and r.json()["party"] == "84961234567"


# =========================================================================== ML (M2–M9)
def test_block_threshold_is_bounded_by_clopper_pearson():
    from scipy.stats import beta

    from train import BLOCK_DISABLED, threshold_for_fpr_bound
    rng = np.random.default_rng(0)
    neg = rng.random(105_427)
    th = threshold_for_fpr_bound(neg, 1e-4)
    k = int((neg >= th).sum())
    assert beta.ppf(0.95, k + 1, neg.size - k) <= 1e-4
    assert threshold_for_fpr_bound(rng.random(2965), 1e-4) == BLOCK_DISABLED   # không chứng minh được → tắt BLOCK


def test_ops_drift_detects_a_block_rate_jump_the_psi_misses():
    import ops_pipeline
    window = {"ALLOW": 9900, "FLAG": 70, "BLOCK": 30}
    base = {"ALLOW": 0.9993, "FLAG": 0.0004, "BLOCK": 0.0003}
    assert ops_pipeline._decision_psi(window, base) < 0.25
    assert ops_pipeline._block_rate_pvalue(window, base) < 1e-3


def test_drift_reference_uses_post_policy_decisions():
    assert "has_behavioural_evidence" in _src("src", "make_drift_baseline.py")


def test_replicates_cover_the_whole_day_and_fraud_follows_the_daily_profile():
    import evaluate
    hours = [evaluate.replicate_start_hour(i) for i in range(10)]
    assert min(hours) == 0 and max(hours) >= 20
    assert "profile=False" not in _src("src", "data_generator.py")


def test_gateway_tac_farms_alternate_within_a_window():
    from data_generator import GATEWAY_TACS, TrafficSimulator
    sim = TrafficSimulator(3, n_personal=200, n_callcentres=2, window="train")
    sim.events = []
    for k in range(4):
        sim.simbox(0, 3600, 2, (20, 30), tag=f"G{k}")
    tacs = {e["actor"]: e["imei"][:8] in GATEWAY_TACS for e in sim.events if str(e.get("actor", "")).startswith("G")}
    # luân phiên xác định: farm 1, 3 dùng TAC gateway; farm 2, 4 dùng TAC máy thường
    assert [tacs[f"G{k}"] for k in range(4)] == [True, False, True, False]


def test_clustered_voice_calibration_can_become_eligible():
    from voice_model import calibrate
    rng = np.random.default_rng(1)
    scores = list(rng.random(80_000) * 0.5) + [0.99] * 10
    labels = [0] * 80_000 + [1] * 10
    groups = [f"speaker{i // 2}" for i in range(80_000)] + [f"f{i}" for i in range(10)]   # 2 bản ghi / người nói
    policy = calibrate(scores, labels, 1e-4, groups=groups)
    assert policy["eligible"] and policy["statistical_unit"] == "independent_group_max"


def test_window_name_prefixes_event_ids():
    from build_dataset import make_window
    _, ev = make_window("cal", 7, 0.05, 8, "val", warmup_hours=0)
    assert ev and all(e["event_id"].startswith("CAL") for e in ev)


def test_eval_gates_pool_several_windows():
    spec = importlib.util.spec_from_file_location("run_evals_r4", ROOT / "evals" / "run_evals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    w = {"block": {"TP": 5, "FP": 0, "FN": 5, "TN": 1000}, "block_or_flag": {"TP": 8, "FP": 1, "FN": 2, "TN": 999,
         "recall_pct": 80.0}, "by_family": {"X": {"events": 10, "blocked": 5, "flagged": 3, "missed": 2}},
         "zero_day": {"events": 2, "missed": 1}}
    w2 = json.loads(json.dumps(w)) | {"block_or_flag": {"TP": 6, "FP": 0, "FN": 4, "TN": 1000, "recall_pct": 60.0}}
    pooled = mod.pool_windows([w, w2])
    assert pooled["block"]["TP"] == 10 and pooled["windows"] == 2 and pooled["any_recall_min_window_pct"] == 60.0
    assert pooled["block_or_flag"]["recall_pct"] == 70.0 and pooled["zero_day"] == {"events": 4, "missed": 2}
