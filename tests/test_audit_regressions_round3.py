"""Hồi quy vòng 3 (17/11/2026): các mục còn mở không cần quyết định của chủ dự án.

Run: python -B -m pytest -q tests/test_audit_regressions_round3.py
"""
import json
import re
from pathlib import Path

import security
import observability

ROOT = Path(__file__).resolve().parents[1]
USERS = {"boss": {"role": "admin", "token": "t", "sign_key": "aa" * 32}}


def test_metrics_without_token_are_closed_outside_demo(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    monkeypatch.delenv("FRAUD_PUBLIC", raising=False)
    assert observability.metrics_authorized("", demo_mode=True) is True       # demo cục bộ vẫn mở
    assert observability.metrics_authorized("", demo_mode=False) is False     # run_live.py: trước đây mở


def test_external_anchor_detects_truncated_audit_tail(tmp_path):
    log = security.AuditLog(tmp_path / "audit.jsonl", user_db=USERS)
    for i in range(3):
        log.record("boss", "act", i=i)
    head = security.verify_audit(log.path, USERS)["head"]
    assert head["records"] == 3
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")       # xoá dòng cuối
    plain = security.verify_audit(log.path, USERS)
    assert plain["valid"] is True                                             # chuỗi không thấy được
    anchored = security.verify_audit(log.path, USERS, anchor=head)
    assert anchored["valid"] is False and "anchor" in anchored["problem"]
    assert security.verify_audit(log.path, USERS, anchor={"records": 2, "hash": json.loads(lines[1])["hash"]})["valid"]


def test_down_alert_does_not_depend_on_scrape_job_name():
    rules = (ROOT / "monitoring" / "alerts" / "telecom-rules.yml").read_text(encoding="utf-8")
    assert "absent_over_time(tfs_uptime_seconds[5m])" in rules


def test_duckdns_token_is_not_written_into_crontab():
    setup = (ROOT / "deploy" / "cloud" / "setup.sh").read_text(encoding="utf-8")
    assert "token=${DUCKDNS_TOKEN}" not in setup and "curl -K" in setup and "umask 077" in setup


def test_workflow_actions_are_pinned_to_commit_shas():
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for ref in re.findall(r"uses:\s*(\S+)", wf.read_text(encoding="utf-8")):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), (wf.name, ref)


# =========================================================================== D1: frontier theo từng bên gọi
def _call(ts, caller, eid, callee="84900000099"):
    return {"event_id": eid, "event_type": "CALL", "ts": float(ts), "caller": caller, "callee": callee,
            "cell_id": "HN-0001", "imei": "353325093735030", "origin_trunk": "LOCAL", "network": "VoLTE",
            "caller_age_days": 900.0}


def test_reordering_between_two_trunks_is_no_longer_unscored():
    from ingest_guard import ADMITTED, LATE, IngestGuard
    g = IngestGuard(allowed_lateness_s=120.0)
    assert g.admit(_call(2000.000, "84900000001", "A")) == ADMITTED
    assert g.admit(_call(1999.999, "84900000002", "B")) == ADMITTED     # trước: LATE (watermark toàn cục)
    assert g.admit(_call(1999.000, "84900000001", "C")) == LATE         # chính bên gọi đó lùi thời gian
    assert g.admit(_call(1850.000, "84900000003", "D")) == LATE         # quá 120 s sau mốc toàn cục


def test_party_frontiers_survive_checkpoint_and_v1_checkpoints_stay_strict():
    import json as _json
    from ingest_guard import ADMITTED, LATE, IngestGuard
    g = IngestGuard(allowed_lateness_s=120.0)
    g.admit(_call(2000, "84900000001", "A"))
    restored = IngestGuard()
    restored.load_state_dict(_json.loads(_json.dumps(g.state_dict())))
    assert restored.admit(_call(1999, "84900000001", "B")) == LATE
    assert restored.admit(_call(1999, "84900000002", "C")) == ADMITTED
    legacy = {"version": 1, "policy": "strict_event_time_frontier", "seen": [], "max_ts": 2000.0,
              "dedup_ttl_s": 900.0, "allowed_lateness_s": 120.0, "stats": {}}
    old = IngestGuard()
    old.load_state_dict(legacy)
    assert old.admit(_call(1999, "84900000002", "D")) == LATE
    assert old.admit(_call(2001, "84900000002", "E")) == ADMITTED


def test_feature_store_stays_causal_and_restorable_under_bounded_disorder():
    import json as _json
    from feature_store import FeatureStore
    fs = FeatureStore()
    victim = "84900000099"
    fs.call_features(_call(2000, "84900000001", "A", callee=victim))
    fs.call_features(_call(1990, "84900000002", "B", callee=victim))     # bên khác, đến muộn 10 s
    assert list(fs.in_calls[victim]) == [1990.0, 2000.0]
    # Cuộc gọi đi của nạn nhân ở ts=1995 chỉ thấy cuộc gọi đến tại 1990, không thấy 2000 (tương lai).
    f = fs.call_features(_call(1995, victim, "C", callee="84900000050"))
    assert f["incoming_24h"] == 1
    assert fs.last_seen["84900000001"] == 2000.0
    clone = FeatureStore()
    clone.load_state_dict(_json.loads(_json.dumps(fs.state_dict())))     # thứ tự deque vẫn hợp lệ


# =========================================================================== D6: giờ địa phương thống nhất
def test_hour_of_day_is_local_time_for_live_unix_timestamps():
    from feature_store import FeatureStore
    # 2026-11-17 02:00 UTC = 09:00 giờ Việt Nam
    ts = 1_794_880_800.0
    f = FeatureStore().call_features(_call(ts, "84900000001", "H1"))
    assert abs(f["hour_of_day"] - 9.0) < 1e-9          # trước: 2.0 (giờ UTC), lệch 7 giờ so với dữ liệu huấn luyện


def test_simulator_windows_start_at_their_local_hour():
    from config import local_hour
    from data_generator import TrafficSimulator, day_profile
    events = TrafficSimulator(5, n_personal=300, n_callcentres=2, window="test").build(1.0, start_hour=8)
    measured = [e for e in events if not e["warmup"]]
    assert measured and all(8.0 <= local_hour(e["ts"]) < 9.0 for e in measured)
    assert min(e["ts"] for e in events) >= 0                                   # mốc kiểu Unix không âm
    assert day_profile(3 * 3600 - 7 * 3600 + 86400) == 0.12                    # 03:00 địa phương: đêm


# =========================================================================== D7: tham chiếu drift và PSI vận hành
def test_drift_reference_prefers_the_realistic_window():
    src = (ROOT / "src" / "make_drift_baseline.py").read_text(encoding="utf-8")
    assert '("ref", "cal", "val")' in src
    import build_dataset
    assert build_dataset.WINDOWS["ref"][3] == "reference"


def test_ops_decision_psi_uses_recent_call_decisions_only(monkeypatch):
    import ops_pipeline
    monkeypatch.setattr(ops_pipeline, "_LAST_CALL_DECISIONS", {})
    first = ops_pipeline._call_decision_window({"call_decisions": {"ALLOW": 990, "FLAG": 9, "BLOCK": 1}})
    assert first == {"ALLOW": 990, "FLAG": 9, "BLOCK": 1}
    # 1 000 cuộc gọi mới toàn BLOCK: cửa sổ chỉ chứa chúng, không bị pha loãng bởi lịch sử
    second = ops_pipeline._call_decision_window({"call_decisions": {"ALLOW": 990, "FLAG": 9, "BLOCK": 1001}})
    assert second == {"ALLOW": 0, "FLAG": 0, "BLOCK": 1000}
    assert ops_pipeline._decision_psi(second, {"ALLOW": 0.99, "FLAG": 0.009, "BLOCK": 0.001}) > 0.25
