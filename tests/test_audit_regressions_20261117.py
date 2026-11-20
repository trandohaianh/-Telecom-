"""Hồi quy cho các lỗi sửa trong đợt audit 17/11/2026 (mỗi test thất bại trên mã trước khi sửa).

Run: python -B -m pytest -q tests/test_audit_regressions_20261117.py
"""

import json
import time
import types
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import accounts
import security
from test_portal_accounts import _login_op, _register, env  # noqa: F401  (fixture dùng lại)

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------- OTP: lần nhập sai phải được đếm bền
def test_otp_wrong_attempts_are_persisted_and_lock_the_code(env, monkeypatch):  # noqa: F811
    c, eng, _ = env
    eng.demo_mode = False
    sent = {}

    class Fake(accounts.OtpProvider):
        name, cost_usd = "fake-sms", 0.0

        def send(self, phone, code):
            sent[phone] = code

    monkeypatch.setattr(accounts, "otp_provider", lambda: Fake())
    h = _register(c, "brute@example.com")
    assert c.post("/api/app/phone/otp/start", headers=h, json={"phone": "0912345678"}).status_code == 200
    code = sent["84912345678"]
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    codes = [
        c.post("/api/app/phone/otp/verify", headers=h, json={"phone": "0912345678", "code": wrong}).status_code
        for _ in range(accounts.OTP_MAX_ATTEMPTS)
    ]
    assert codes == [400] * accounts.OTP_MAX_ATTEMPTS
    # Trước khi sửa: ROLLBACK xoá lần đếm, nên mã đúng vẫn dùng được sau vô số lần đoán sai.
    late = c.post("/api/app/phone/otp/verify", headers=h, json={"phone": "0912345678", "code": code})
    assert late.status_code in (400, 429)
    assert c.get("/api/app/auth/me", headers=h).json()["msisdn"] is None


# --------------------------------------------------------------- Google: chiếm chỗ email trước
def test_google_link_removes_preregistered_password_and_sessions(env, monkeypatch):  # noqa: F811
    c, _, _ = env
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    attacker = _register(c, "victim@gmail.com", "mat-khau-ke-gian")
    monkeypatch.setattr(
        accounts,
        "verify_google_token",
        lambda cred: accounts.identity_from_claims(
            {"iss": "accounts.google.com", "sub": "g-victim", "email": "victim@gmail.com", "email_verified": True}
        ),
    )
    r = c.post("/api/app/auth/google", json={"credential": "x" * 40})
    assert r.status_code == 200 and r.json()["is_new_user"] is False  # vẫn liên kết như trước
    assert c.get("/api/app/auth/me", headers=attacker).status_code == 401  # phiên của người đặt mật khẩu bị huỷ
    again = c.post("/api/app/auth/login", json={"email": "victim@gmail.com", "password": "mat-khau-ke-gian"})
    assert again.status_code == 401


# --------------------------------------------------------------- hạ quyền có hiệu lực ngay
def test_demoted_operator_loses_admin_on_existing_session(env):  # noqa: F811
    c, _, users = env
    h = _login_op(c, "t-boss")
    assert c.get("/api/audit", headers=h).status_code == 200
    users["boss"]["role"] = "viewer"  # admin khác hạ quyền tài khoản này
    assert c.get("/api/audit", headers=h).status_code == 403
    assert c.get("/api/whoami", headers=h).json()["role"] == "viewer"


def test_self_demotion_guard_normalises_the_name(env):  # noqa: F811
    c, _, _ = env
    h = _login_op(c, "t-boss")
    r = c.post("/api/admin/operators", headers=h, json={"name": "BOSS", "role": "viewer"})
    assert r.status_code == 400


def test_rate_limit_key_ignores_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("FRAUD_RATE_LIMIT", "1")
    lim = accounts.RateLimiter()
    for name in ("admin", " admin", "admin ", "ADMIN"):
        lim.hit("op_login_user", name, 4, 900)
    with pytest.raises(Exception) as exc:
        lim.hit("op_login_user", "\tadmin", 4, 900)
    assert getattr(exc.value, "status_code", None) == 429


# --------------------------------------------------------------- gia đình: phải có lời mời
def test_family_accept_requires_a_pending_invite(env):  # noqa: F811
    c, _, _ = env
    a = _register(c, "fam@example.com")
    c.post("/api/app/phone/claim-demo", headers=a)
    r = c.post("/api/app/family/respond?guardian=anything-at-all&accept=true", headers=a)
    assert r.status_code == 404
    assert c.get("/api/app/family", headers=a).json()["guardians"] == []


# --------------------------------------------------------------- mốc thời gian đầu độc watermark
def test_millisecond_or_huge_timestamps_are_rejected():
    from backend_runtime import validate_event

    ev = {
        "event_id": "T1",
        "event_type": "CALL",
        "ts": 1.7e12,
        "caller": "84961234567",
        "callee": "84351234567",
        "cell_id": "HN-0001",
        "imei": "35391110000001",
        "origin_trunk": "LOCAL",
        "network": "4G",
    }
    for ts in (1.7e12, 1e308):
        with pytest.raises(ValueError, match="Unix seconds"):
            validate_event({**ev, "ts": ts})
    assert validate_event({**ev, "ts": 1_763_000_000.0})["ts"] == 1_763_000_000.0
    assert validate_event({**ev, "ts": 0.0})["ts"] == 0.0  # đồng hồ giả lập vẫn hợp lệ


def test_network_link_rejects_unix_timestamp_far_in_future(monkeypatch):
    import server
    from test_network_link import CALL, KEY, signed

    monkeypatch.setattr(security, "network_key", lambda: KEY)
    client = TestClient(server.app)
    body = json.dumps({**CALL, "event_id": "FUT1", "ts": time.time() + 30 * 86400}).encode()
    r = client.post("/api/v1/verdict", content=body, headers=signed(body))
    assert r.status_code == 400 and "future" in r.json()["detail"]
    nested = b"[" * 100000 + b"]" * 100000
    assert client.post("/api/v1/verdict", content=nested[:60000], headers=signed(nested[:60000])).status_code == 400


# --------------------------------------------------------------- số tổng đài được bảo vệ
def test_harvest_memory_never_blocks_a_protected_hotline():
    from data_generator import PROTECTED_CLI
    from detector import Detector

    det = Detector()
    hotline = sorted(PROTECTED_CLI)[0]

    def audio(eid, callee, ts):
        return {
            "event_id": eid,
            "event_type": "AUDIO",
            "ts": ts,
            "caller": hotline,
            "callee": callee,
            "codec": "EVS-VoLTE",
            "vocoder_artifact": 0.2,
            "pitch_jitter": 0.024,
            "spectral_flatness": 0.3,
            "speaker_drift": 0.2,
            "caller_speech_ratio": 0.02,
            "callee_speech_s": 4.0,
            "transcript": "",
        }

    det.score_batch([audio("PA1", "84351110001", 100.0)])
    det.score_batch([audio("PA2", "84351110002", 200.0)])
    genuine = {
        "event_id": "PC3",
        "event_type": "CALL",
        "ts": 300.0,
        "caller": hotline,
        "callee": "84351110003",
        "cell_id": "TRUNK",
        "imei": "",
        "origin_trunk": "BANK_TRUNK",
        "network": "4G",
        "caller_age_days": 3000,
    }
    r = det.score_batch([genuine])[0]
    assert all(x["code"] != "A108" for x in r["reasons"])
    assert hotline not in det.harvest_hits


# --------------------------------------------------------------- nén state khi checkpoint
def test_periodic_compaction_drops_expired_window_state_but_respects_rescore_horizon():
    from backend_runtime import InferenceRuntime
    from feature_store import FeatureStore

    fs = FeatureStore()
    base = {
        "event_type": "CALL",
        "cell_id": "HN-0001",
        "imei": "35391110000001",
        "origin_trunk": "LOCAL",
        "network": "4G",
        "callee": "84351234567",
    }
    for i in range(50):
        fs.call_features({**base, "event_id": f"Q{i}", "ts": float(i), "caller": f"849600000{i:02d}"})
    assert len(fs.oc1h) == 50
    stub = types.SimpleNamespace(
        guard=types.SimpleNamespace(max_ts=200000.0),
        g=types.SimpleNamespace(rescore_queue=[{"ts": 10.0}], det=types.SimpleNamespace(fs=fs)),
        stats=__import__("collections").Counter(),
    )
    InferenceRuntime._compact_state(stub)
    assert len(fs.oc1h) == 50  # sự kiện chờ chấm lại ở ts=10 vẫn cần cửa sổ đó
    stub.g.rescore_queue = []
    InferenceRuntime._compact_state(stub)
    assert not fs.oc1h and not fs.in_calls and stub.stats["state_compactions"] == 2
    assert len(fs.first_seen) >= 50  # metadata giữ nguyên (không áp TTL)


# --------------------------------------------------------------- giọng nói stereo không rõ kênh
def test_stereo_audio_without_caller_channel_abstains():
    import io
    import wave

    from voice_model import RealVoiceModel as VoiceModel
    from voice_pipeline import analyze_wav

    t = np.arange(16000) / 16000
    data = np.column_stack([0.15 * np.sin(2 * np.pi * 200 * t), 0.15 * np.sin(2 * np.pi * 150 * t)])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes((data * 32767).astype("<i2").tobytes())
    obs = analyze_wav(buf.getvalue())
    assert "caller_channel_required" in obs["quality_warnings"]

    class Never:
        def predict_proba(self, _):
            raise AssertionError("must abstain before scoring")

    model = VoiceModel.__new__(VoiceModel)
    model.bundle = {"estimator": Never(), "policies": {"block": {}, "flag": {}}}
    model.positive_index = 1
    assert model.predict(obs)["reason"] == "party_attribution_abstention"


# --------------------------------------------------------------- giao diện vận hành: XSS
def test_operator_alert_card_escapes_network_supplied_fields():
    from _pages import page_source

    html = page_source("static/admin/index.html")
    card = html[html.index("function card(a)") : html.index("function render(s)")]
    for raw in ("${a.party", "${a.counterparty", 'data-id="${a.event_id}"', "${a.time}", "${r.code}"):
        assert raw not in card, raw
    for safe in ("esc(a.party)", "esc(a.counterparty)", "esc(a.event_id)", "esc(a.time)", "esc(r.code)"):
        assert safe in card, safe


# --------------------------------------------------------------- đóng gói
def test_docker_contexts_exclude_keys_and_env_files():
    grpc = (ROOT / "telecom_grpc" / "Dockerfile.dockerignore").read_text().splitlines()
    assert "telecom_grpc/secrets" in grpc and "telecom_grpc/tls" in grpc and "**/*.key" in grpc
    root = (ROOT / ".dockerignore").read_text().splitlines()
    assert "**/.env" in root and "**/secrets" in root


def test_verifier_image_installs_every_import_of_the_server():
    req = (ROOT / "docker" / "backend" / "requirements-linux.txt").read_text().lower()
    for package in ("prometheus-client", "pyjwt", "pyarrow"):
        assert package in req, package


def test_stale_pipeline_alerts_fire_when_job_never_succeeded():
    rules = (ROOT / "monitoring" / "alerts" / "telecom-rules.yml").read_text(encoding="utf-8")
    for task in ("backup", "kpi_snapshot"):
        assert f'absent(tfs_pipeline_last_success_timestamp_seconds{{task="{task}"}})' in rules


# --------------------------------------------------------------- PSI: đặc trưng lệch về 0
def test_zero_inflated_feature_does_not_report_drift_against_itself():
    from drift import PSI_WATCH, _edges, null_psi, psi

    rng = np.random.default_rng(7)

    def sample(n):
        x = np.zeros(n)
        idx = rng.random(n) < 0.05
        x[idx] = rng.exponential(30.0, idx.sum())
        return x

    ref, cur = sample(10000), sample(10000)
    assert _edges(ref).size <= 13  # trước khi sửa: ~500 cạnh
    assert psi(ref, cur) < PSI_WATCH
    assert null_psi({"velocity_kmh": ref})["velocity_kmh"] < PSI_WATCH
    shifted = ref.copy()
    shifted[rng.random(ref.size) < 0.3] = 50.0  # dịch chuyển thật vẫn bị bắt
    assert psi(ref, shifted) > 0.25
