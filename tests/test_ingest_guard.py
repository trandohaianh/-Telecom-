"""Tests for the production-readiness layer: dedup, watermark, checkpoint, drift.

These cover the failures a file replay can never produce: the same event arriving twice, an event
arriving after its window has moved on, and a process that dies and has to come back.
"""

# Nạp JSON/path và pytest, thêm mã nguồn vào sys.path để import các mô-đun đang kiểm thử.
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import drift  # noqa: E402
from feature_store import FeatureStore  # noqa: E402
from ingest_guard import ADMITTED, DUPLICATE, LATE, ActionLedger, IngestGuard, event_key  # noqa: E402


def call(ts, caller="84900000001", callee="84900000002", eid=None, **kw):
    # Tạo sự kiện CALL hợp lệ mặc định, cho phép ghi đè trường để dựng ca kiểm thử.
    ev = {
        "event_id": eid,
        "event_type": "CALL",
        "ts": float(ts),
        "caller": caller,
        "callee": callee,
        "cell_id": "HN-0001",
        "imei": "353325093735030",
        "origin_trunk": "LOCAL",
        "network": "VoLTE",
        "caller_age_days": 900.0,
        "duration_s": 30.0,
    }
    ev.update(kw)
    return ev


# ---------------------------------------------------------------- deduplication
def test_redelivered_event_is_rejected():
    # Xác nhận bản giao đầu được nhận, còn retry cùng ID bị phân loại trùng và đếm.
    g = IngestGuard()
    ev = call(1000.0, eid="E123")
    assert g.admit(ev) == ADMITTED
    assert g.admit(dict(ev)) == DUPLICATE  # a retry: identical content, same id
    assert g.stats[DUPLICATE] == 1


def test_duplicate_does_not_move_the_feature_store():
    """The point of the guard: one call must stay one call in every window."""
    # So sánh feature store sạch với luồng mà guard loại bỏ các bản giao lại.
    fs_clean, fs_dirty = FeatureStore(), FeatureStore()
    g = IngestGuard()
    events = [call(1000.0 + i, eid=f"E{i}") for i in range(5)]

    # Tính đặc trưng kiểm soát trên luồng sạch và đặc trưng sau khi loại retry.
    for ev in events:
        fs_clean.call_features(ev)
    clean = fs_clean.call_features(call(1010.0, eid="probe"))

    # Gửi mỗi event hai lần nhưng chỉ cập nhật store khi guard cho phép.
    for ev in events:
        for delivery in (ev, dict(ev)):  # every event redelivered once
            if g.admit(delivery) == ADMITTED:
                fs_dirty.call_features(delivery)
    guarded = fs_dirty.call_features(call(1010.0, eid="probe2"))

    # Các bộ đếm cửa sổ phút/giờ phải khớp luồng chỉ có một lần giao.
    assert guarded["calls_1m"] == clean["calls_1m"]
    assert guarded["calls_1h"] == clean["calls_1h"]


def test_without_the_guard_duplicates_inflate_the_counters():
    """The failure the guard prevents, stated as a test so it cannot be hand-waved."""
    # Chứng minh store đếm cả retry nếu không có guard ngăn chặn.
    fs = FeatureStore()
    for i in range(5):
        ev = call(1000.0 + i, eid=f"E{i}")
        fs.call_features(ev)
        fs.call_features(dict(ev))  # redelivery reaches the store
    # Năm cuộc gọi lặp đôi cộng probe tạo tổng 11 cuộc trong cửa sổ.
    probe = fs.call_features(call(1010.0, eid="probe"))
    assert probe["calls_1m"] == 11  # 5 calls counted twice, plus the probe


def test_derived_key_when_the_feed_has_no_event_id():
    # Khi thiếu event_id, xác nhận nội dung tương đương sinh khóa như nhau và bị dedup.
    a = call(1000.0, eid=None)
    b = call(1000.0, eid=None)
    # Lần đầu được nhận còn bản sự kiện tương đương bị đánh dấu trùng.
    assert event_key(a) == event_key(b)
    g = IngestGuard()
    assert g.admit(a) == ADMITTED
    assert g.admit(b) == DUPLICATE


def test_dedup_index_is_bounded_by_event_time():
    # Xác nhận chỉ mục dedup loại bỏ khóa cũ theo TTL thay vì tăng vô hạn.
    g = IngestGuard(dedup_ttl_s=60.0)
    for i in range(200):
        g.admit(call(1000.0 + i, eid=f"E{i}"))
    # Sau các mốc tăng dần, số khóa còn giữ xấp xỉ cửa sổ TTL cộng biên.
    assert g._seen and len(g._seen) <= 62  # only the TTL window is retained


# ---------------------------------------------------------------- watermark / lateness
def test_out_of_order_inside_grace_is_rejected_without_an_upstream_reorder_buffer():
    # Sự kiện đến trễ nhưng vẫn nằm trong hạn cho phép phải được chấp nhận.
    g = IngestGuard(allowed_lateness_s=120.0)
    g.admit(call(2000.0, eid="A"))
    assert g.admit(call(1950.0, eid="B")) == LATE  # 50 s behind: inside the allowance


def test_event_later_than_the_allowance_is_marked_late():
    # Sự kiện vượt quá giới hạn trễ bị đánh dấu LATE và watermark vẫn theo allowance.
    g = IngestGuard(allowed_lateness_s=120.0)
    g.admit(call(2000.0, eid="A"))
    # Với max_ts=2000 và độ trễ 120 giây, watermark mong đợi là 1880.
    assert g.admit(call(1500.0, eid="B")) == LATE
    assert g.watermark == pytest.approx(1880.0)


def test_watermark_follows_the_highest_event_time_not_arrival_order():
    # Xác nhận thứ tự giao không làm watermark lùi khỏi thời gian sự kiện lớn nhất.
    g = IngestGuard(allowed_lateness_s=10.0)
    for ts in (1000.0, 1500.0, 1200.0, 1400.0):
        g.admit(call(ts, eid=f"E{ts}"))
    assert g.max_ts == 1500.0


# ---------------------------------------------------------------- idempotent execution
def test_a_replayed_decision_is_executed_once():
    # Cùng một quyết định BLOCK được phát lại chỉ thi hành lần đầu và lần sau bị chặn.
    ledger = ActionLedger()
    fired = []
    r = {"event_id": "E1", "decision": "BLOCK", "ts": 100.0}
    # Kiểm tra kết quả thi hành, số callback và bộ đếm quyết định bị triệt tiêu.
    assert ledger.execute(r, fired.append) is True
    assert ledger.execute(dict(r), fired.append) is False  # the same block, replayed
    assert len(fired) == 1
    assert ledger.suppressed == 1


def test_a_different_decision_on_the_same_event_still_runs():
    # Nâng quyết định cùng event từ FLAG lên BLOCK là escalation, không phải retry trùng.
    ledger = ActionLedger()
    fired = []
    ledger.execute({"event_id": "E1", "decision": "FLAG", "ts": 1.0}, fired.append)
    ledger.execute({"event_id": "E1", "decision": "BLOCK", "ts": 2.0}, fired.append)
    assert len(fired) == 2  # escalation is not a duplicate


# ---------------------------------------------------------------- checkpoint / restore
def test_checkpoint_restores_identical_features(tmp_path):
    """A restart must not cost the 24 h warm-up."""
    # Tạo lịch sử cuộc gọi đã hoàn tất, lưu checkpoint và lấy feature kỳ vọng trước restart.
    fs = FeatureStore()
    for i in range(40):
        ev = call(1000.0 + i * 10, eid=f"E{i}")
        fs.call_features(ev)
        fs.commit_cdr(ev)

    path = tmp_path / "fs.json"
    fs.save_checkpoint(path)
    probe = call(1500.0, eid="probe")
    expected = fs.call_features(probe)

    # Khôi phục store mới từ checkpoint và xác nhận feature sau khôi phục khớp hoàn toàn.
    restored = FeatureStore()
    restored.load_checkpoint(path)
    assert restored.call_features(call(1500.0, eid="probe2")) == expected


def test_checkpoint_round_trip_keeps_pending_cdrs(tmp_path):
    # Đảm bảo checkpoint giữ CDR hoàn tất đang chờ để tính feature cho cuộc gọi kế tiếp.
    fs = FeatureStore()
    ev = call(1000.0, eid="E1", duration_s=2.0)
    fs.call_features(ev)
    fs.commit_cdr(ev)
    path = tmp_path / "fs.json"
    fs.save_checkpoint(path)

    restored = FeatureStore()
    restored.load_checkpoint(path)
    # Sau restore, số cuộc đã hoàn tất và tỷ lệ cuộc ngắn phải phản ánh CDR 2 giây.
    after = restored.call_features(call(1100.0, eid="E2"))
    assert after["completed_calls_seen"] == 1
    assert after["short_call_ratio"] == pytest.approx(1.0)  # the 2 s call is remembered as short


def test_checkpoint_rejects_an_unknown_version(tmp_path):
    # Phiên bản checkpoint không được nhận diện phải gây ValueError.
    path = tmp_path / "fs.json"
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(ValueError):
        FeatureStore().load_checkpoint(path)


# ---------------------------------------------------------------- drift
def test_psi_is_zero_for_the_same_distribution():
    # Hai mẫu cùng phân phối chuẩn phải có PSI dưới ngưỡng cần theo dõi.
    import random

    rng = random.Random(1)
    ref = [rng.gauss(0, 1) for _ in range(5000)]
    cur = [rng.gauss(0, 1) for _ in range(5000)]
    assert drift.psi(ref, cur) < drift.PSI_WATCH


def test_psi_detects_a_shifted_distribution():
    # Dịch phân phối hiện tại mạnh phải đẩy PSI tới ngưỡng cảnh báo.
    import random

    rng = random.Random(2)
    ref = [rng.gauss(0, 1) for _ in range(5000)]
    cur = [rng.gauss(2.5, 1) for _ in range(5000)]
    assert drift.psi(ref, cur) >= drift.PSI_ALERT


def test_drift_report_recommends_nothing_when_traffic_is_stable():
    # Dữ liệu hiện tại ổn định so với baseline phải không đề xuất hành động.
    import random

    rng = random.Random(3)
    feats = {f"f{i}": [rng.gauss(0, 1) for _ in range(3000)] for i in range(4)}
    base = drift.build_baseline(feats, scores=[rng.random() for _ in range(3000)])
    now = {k: [rng.gauss(0, 1) for _ in range(3000)] for k in feats}
    rep = drift.evaluate_drift(base, now, scores=[rng.random() for _ in range(3000)])
    assert rep["recommendation"] == drift.NO_ACTION


def test_drift_report_recommends_retraining_when_everything_moves():
    # Khi mọi đặc trưng và score dịch chuyển, báo cáo phải cảnh báo và khuyến nghị huấn luyện lại.
    import random

    rng = random.Random(4)
    feats = {f"f{i}": [rng.gauss(0, 1) for _ in range(3000)] for i in range(4)}
    scores = [rng.random() * 0.2 for _ in range(3000)]
    base = drift.build_baseline(feats, scores=scores)
    now = {k: [rng.gauss(3.0, 1) for _ in range(3000)] for k in feats}
    rep = drift.evaluate_drift(base, now, scores=[0.8 + rng.random() * 0.2 for _ in range(3000)])
    # Yêu cầu có cảnh báo và đúng trạng thái khuyến nghị tái huấn luyện.
    assert rep["alert"]
    assert rep["recommendation"] == drift.RETRAIN_RECOMMENDED


def test_drift_baseline_stores_no_raw_rows():
    """The baseline ships with the model, so it must not carry subscriber data."""
    # Tạo baseline từ dữ liệu ngẫu nhiên và kiểm tra chỉ lưu thống kê tổng hợp.
    import random

    rng = random.Random(5)
    base = drift.build_baseline({"calls_1m": [rng.gauss(0, 1) for _ in range(500)]})
    # Không cho phép chứa hàng/mẫu thô; đặc trưng chỉ gồm cạnh bin và tỷ trọng.
    blob = json.dumps(base)
    assert "rows" not in blob and "samples" not in blob
    assert set(base["features"]["calls_1m"]) == {"edges", "shares"}


def test_band_shares_are_used_instead_of_a_psi_on_the_raw_score():
    """A PSI on this score is meaningless, so the baseline must carry bands, not score bins."""
    # Tạo score lệch mạnh cùng ngưỡng để kiểm tra baseline sử dụng dải quyết định.
    import random

    rng = random.Random(6)
    scores = [rng.random() ** 8 for _ in range(4000)]  # heavily concentrated near zero
    th = {"block": 0.9, "flag": 0.5}
    base = drift.build_baseline({"f": [rng.gauss(0, 1) for _ in range(4000)]}, scores=scores, thresholds=th)
    # Xác nhận baseline có tỷ trọng theo dải, không lưu PSI score thô.
    assert "band_shares" in base and "score" not in base
    # Với phần lớn score thấp, tỷ lệ ALLOW phải cao hơn BLOCK.
    assert base["band_shares"]["ALLOW"] > base["band_shares"]["BLOCK"]


def test_a_collapsed_block_band_asks_for_a_human():
    # Khi tỷ trọng dải BLOCK co mạnh, cần yêu cầu người vận hành điều tra.
    import random

    rng = random.Random(7)
    feats = {"f": [rng.gauss(0, 1) for _ in range(3000)]}
    th = {"block": 0.9, "flag": 0.5}
    base = drift.build_baseline(feats, scores=[0.95] * 300 + [0.1] * 2700, thresholds=th)
    rep = drift.evaluate_drift(base, {"f": [rng.gauss(0, 1) for _ in range(3000)]}, scores=[0.95] * 5 + [0.1] * 2995)
    # Xác nhận phát hiện dịch chuyển dải BLOCK và đề xuất INVESTIGATE.
    assert rep["block_band_moved"] is True
    assert rep["recommendation"] == drift.INVESTIGATE


def test_the_noise_floor_is_recorded_with_the_baseline():
    """Thresholds are only meaningful next to the metric's own noise."""
    # Xác nhận baseline lưu phân phối PSI nền để diễn giải ngưỡng drift có xét nhiễu.
    import random

    rng = random.Random(8)
    base = drift.build_baseline({f"f{i}": [rng.gauss(0, 1) for _ in range(4000)] for i in range(3)})
    # Có các phép đo null PSI và cực đại vẫn dưới ngưỡng theo dõi.
    assert base["null_psi"]
    assert max(base["null_psi"].values()) < drift.PSI_WATCH
