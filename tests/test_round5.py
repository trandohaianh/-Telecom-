"""Vòng 5 (17/11/2026): Web Push, OTP qua SMS HTTP, ML (M6, M10, baseline luật), tách cổng eval, đặc trưng mới.

Mỗi test thất bại trên mã trước vòng 5. Run: python -B -m pytest -q tests/test_round5.py
"""

import json
import types
from pathlib import Path

import numpy as np
import pytest

import accounts
import portal
from test_portal_accounts import _register, env  # noqa: F401  (fixture dùng lại)

ROOT = Path(__file__).resolve().parents[1]
FCM = "https://fcm.googleapis.com/fcm/send/abc123"
KEYS = {"p256dh": "B" + "x" * 86, "auth": "y" * 22}


# =========================================================================== Web Push
def test_push_subscription_validation_blocks_internal_endpoints():
    import push

    assert push.validate_subscription({"endpoint": FCM, "keys": KEYS})["endpoint"] == FCM
    for bad in (
        "http://fcm.googleapis.com/x",
        "https://localhost/x",
        "https://10.0.0.5/x",
        "https://svc.internal/x",
        "https://[::1]/x",
    ):
        with pytest.raises(ValueError):
            push.validate_subscription({"endpoint": bad, "keys": KEYS})


def test_push_payload_carries_no_phone_number_or_sms_text():
    import push

    item = {
        "decision": "BLOCK",
        "action": "Đã chặn cuộc gọi giả danh ngân hàng",
        "caller": "84961234567",
        "text": "Ban da trung thuong, bam link http://x.y",
    }
    data = json.dumps(push.payload_for(item), ensure_ascii=False)
    assert "84961234567" not in data and "trung thuong" not in data and data.count("http") == 0


def test_push_subscribe_and_delivery_with_expiry(env, monkeypatch):  # noqa: F811
    import push

    c, eng, _ = env
    h = _register(c, "push@example.com")
    number = c.post("/api/app/phone/claim-demo", headers=h).json()["msisdn"]
    assert (
        c.post("/api/app/push/subscribe", headers=h, json={"endpoint": "http://evil/x", "keys": KEYS}).status_code
        == 422
    )
    assert c.post("/api/app/push/subscribe", headers=h, json={"endpoint": FCM, "keys": KEYS}).status_code == 200
    sent = []

    class Gone(push.WebPushException):
        def __init__(self):
            Exception.__init__(self, "gone")
            self.response = types.SimpleNamespace(status_code=410)

    sender = push.PushSender.__new__(push.PushSender)
    sender.db, sender._last, sender.stats = (
        portal.db,
        {},
        {"sent": 0, "expired": 0, "failed": 0, "dropped": 0, "throttled": 0},
    )
    sender._send = lambda sub, data: sent.append((sub["endpoint"], data))
    sender.deliver(number, push.payload_for({"decision": "BLOCK", "action": "x"}))
    assert sent and sent[0][0] == FCM and sender.stats["sent"] == 1

    def gone(sub, data):
        raise Gone()

    sender._send, sender._last = gone, {}
    sender.deliver(number, {"title": "t"})
    assert sender.stats["expired"] == 1
    assert not portal.db().query("SELECT 1 FROM push_subscriptions")


def test_service_worker_shows_push_notifications():
    sw = (ROOT / "static" / "app" / "sw.js").read_text(encoding="utf-8")
    assert "addEventListener('push'" in sw and "notificationclick" in sw


# =========================================================================== OTP qua SMS HTTP của nhà mạng
def test_http_sms_otp_provider_renders_template_safely(monkeypatch, tmp_path):
    token = tmp_path / "tok"
    token.write_text("secret-token", encoding="utf-8")
    monkeypatch.setenv("FRAUD_OTP_PROVIDER", "sms-http")
    monkeypatch.setenv("FRAUD_SMS_HTTP_URL", "https://sms.example.vn/api/send")
    monkeypatch.setenv("FRAUD_SMS_HTTP_TOKEN_FILE", str(token))
    monkeypatch.setenv("FRAUD_SMS_HTTP_BODY", '{"msisdn": "{phone}", "brand": "{brandname}", "content": "{message}"}')
    provider = accounts.otp_provider()
    assert isinstance(provider, accounts.HttpSmsOtp)
    body = provider.render("84912345678", "123456")
    assert body["msisdn"] == "84912345678" and "123456" in body["content"] and body["brand"] == "LACHAN"
    calls = []
    import httpx

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return types.SimpleNamespace(status_code=200, json=lambda: {})

    monkeypatch.setattr(httpx, "post", fake_post)
    provider.send("84912345678", "654321")
    assert calls[0][0] == "https://sms.example.vn/api/send" and calls[0][2]["Authorization"].endswith("secret-token")
    monkeypatch.setenv("FRAUD_SMS_HTTP_URL", "http://insecure.example/api")
    with pytest.raises(RuntimeError):
        accounts.HttpSmsOtp()


# =========================================================================== ML
def test_new_callee_ratio_feature_is_causal_and_restorable():
    from feature_store import CALL_FEATURES, FeatureStore

    assert "new_callee_ratio_24h" in CALL_FEATURES
    fs = FeatureStore()
    base = {
        "event_type": "CALL",
        "cell_id": "HN-0001",
        "imei": "35391110000001",
        "origin_trunk": "LOCAL",
        "network": "4G",
        "caller_age_days": 300,
    }
    r = [
        fs.call_features({**base, "event_id": f"E{i}", "ts": 100.0 * i, "caller": "A", "callee": c})
        for i, c in enumerate(["B", "C", "B", "B"])
    ]
    assert [round(x["new_callee_ratio_24h"], 3) for x in r] == [1.0, 1.0, round(2 / 3, 3), 0.5]
    clone = FeatureStore()
    clone.load_state_dict(json.loads(json.dumps(fs.state_dict())))
    assert (
        clone.call_features({**base, "event_id": "E9", "ts": 900.0, "caller": "A", "callee": "D"})[
            "new_callee_ratio_24h"
        ]
        == fs.call_features({**base, "event_id": "E9", "ts": 900.0, "caller": "A", "callee": "D"})[
            "new_callee_ratio_24h"
        ]
    )


def test_cluster_bootstrap_ci_is_wider_than_event_wilson():
    from statistical_reporting import cluster_bootstrap_recall, wilson

    actors = [{"events": 30, "detected": 30}, {"events": 30, "detected": 0}] + [{"events": 1, "detected": 1}] * 6
    out = cluster_bootstrap_recall(actors)
    lo, hi = wilson(36, 66)
    assert out["clusters"] == 8 and (out["ci_pct"][1] - out["ci_pct"][0]) > 100 * (hi - lo)


def test_zero_day_explanations_use_segment_scale():
    src = (ROOT / "src" / "detector.py").read_text(encoding="utf-8")
    assert "iso_seg_scale" in src
    assert '"segment_scale"' in (ROOT / "src" / "train.py").read_text(encoding="utf-8")


def test_rules_only_baseline_scores_behavioural_evidence():
    import compare_models
    from feature_store import CALL_FEATURES

    row = dict.fromkeys(CALL_FEATURES, 0.0) | {"msisdn_per_imei_24h": 3, "active_hours_on_network": 50, "calls_1h": 1}
    plain = dict.fromkeys(CALL_FEATURES, 0.0) | {"active_hours_on_network": 50, "incoming_24h": 3}
    X = np.array([[row[c] for c in CALL_FEATURES], [plain[c] for c in CALL_FEATURES]])
    assert list(compare_models.rules_score(None, X)) == [2.0, 0.0]


def test_eval_quality_targets_are_advisory_but_safety_gates_block():
    th = json.loads((ROOT / "evals" / "thresholds.json").read_text(encoding="utf-8"))
    assert th["min"]["block_recall_pct"] == 45 and th["min"]["family_min_detected_pct"] == 75  # mục tiêu giữ nguyên
    assert "block_recall_pct" in th["advisory"] and "block_fpr_ci_hi_pct" not in th["advisory"]
    src = (ROOT / "evals" / "run_evals.py").read_text(encoding="utf-8")
    assert "blocking_failures" in src and "advisory_misses" in src


def test_subscriber_app_registers_and_drops_web_push():
    from _pages import page_source

    page = page_source("static/app/index.html")
    assert "pushManager.subscribe" in page and "/api/app/push/subscribe" in page and "applicationServerKey" in page
    assert "/api/app/push/unsubscribe" in page and "pushUnsubscribe(TOKEN)" in page
