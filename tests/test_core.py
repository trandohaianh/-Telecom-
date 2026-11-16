"""Core guarantees of the MVP. Run: python -m pytest -q tests"""
# Nạp công cụ kiểm tra, thêm mã nguồn vào đường dẫn import và lấy các thành phần cần test.
import inspect
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import feature_store  # noqa: E402
from evidence import EvidenceLog, verify  # noqa: E402
from resilience import CircuitBreaker, GuardedDetector  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_feature_store_never_reads_labels_or_generator_counters():
    # Đảm bảo FeatureStore không truy cập nhãn gian lận hoặc bộ đếm chỉ biết khi sinh dữ liệu.
    src = inspect.getsource(feature_store.FeatureStore)
    for forbidden in ("label", "fraud_type", "actor", "calls_past_hour", "is_fraud"):
        # Cấm cả dạng chuỗi nháy kép và nháy đơn để tránh rò rỉ nhãn qua tên trường.
        assert f'"{forbidden}"' not in src and f"'{forbidden}'" not in src


def test_cell_map_does_not_depend_on_python_hash_seed():
    # Tọa độ trạm phải giống nhau ở mọi lần chạy (trước đây dùng hash() nên đổi theo PYTHONHASHSEED).
    import subprocess
    code = "import data_generator as d; print(sorted(d.REGISTERED_CELLS.items())[:3])"
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT / "src",
                           env={**__import__("os").environ, "PYTHONHASHSEED": s}).stdout for s in ("1", "2")}
    assert len(outs) == 1 and outs.pop().strip()


def test_cdr_duration_only_visible_after_call_ends():
    # Kiểm tra thời lượng CDR chỉ đi vào đặc trưng sau thời điểm cuộc gọi kết thúc.
    fs = feature_store.FeatureStore()
    # Tạo một cuộc gọi nền rồi truy vấn đặc trưng trước/sau thời điểm hai cuộc hoàn tất.
    base = dict(event_type="CALL", caller="84900000001", cell_id="HN-0001", imei="35391110000001",
                origin_trunk="LOCAL", network="4G", caller_age_days=500)
    f1 = fs.call_features(dict(base, ts=0.0, callee="1", duration_s=100.0)); fs.commit_cdr(dict(base, ts=0.0, duration_s=100.0))
    f2 = fs.call_features(dict(base, ts=50.0, callee="2", duration_s=3.0)); fs.commit_cdr(dict(base, ts=50.0, duration_s=3.0))
    f3 = fs.call_features(dict(base, ts=200.0, callee="3", duration_s=3.0))
    # Cuộc đầu chưa được tính ở lúc bắt đầu cuộc đầu tiên.
    assert f1["completed_calls_seen"] == 0
    # Ở t=50 cuộc đầu vẫn đang diễn ra, nên chưa có cuộc hoàn tất nào được thấy.
    assert f2["completed_calls_seen"] == 0          # first call still in progress at t=50
    # Tới t=200 cả hai cuộc trước đó đã kết thúc và được ghi nhận.
    assert f3["completed_calls_seen"] == 2


def test_circuit_breaker_opens_and_recovers():
    # Dùng đồng hồ giả để kiểm tra breaker mở sau lỗi và hồi phục sau cooldown.
    t = [0.0]
    cb = CircuitBreaker(failure_threshold=3, cooldown_s=1.0, clock=lambda: t[0])
    # Đạt ngưỡng ba lỗi thì mạch mở và từ chối request.
    for _ in range(3):
        cb.record_failure("timeout")
    assert cb.state == cb.OPEN and not cb.allow_request()
    # Sau cooldown, request được phép ở trạng thái HALF_OPEN; thành công sẽ đóng mạch.
    t[0] = 1.5
    assert cb.allow_request() and cb.state == cb.HALF_OPEN
    cb.record_success()
    assert cb.state == cb.CLOSED


def test_guarded_detector_fails_open_on_error():
    # Mô phỏng model ném lỗi và xác nhận lớp bảo vệ trả ALLOW kèm mã lỗi fail-open.
    class Boom:
        def score_batch(self, evs):
            raise RuntimeError("model crashed")
    # Chấm một event qua detector lỗi và kiểm tra quyết định an toàn cùng lý do.
    g = GuardedDetector(Boom(), budget_ms=30)
    ev = {"event_id": "E1", "event_type": "CALL", "ts": 0.0, "caller": "1", "callee": "2"}
    r = g.score_batch([ev])[0]
    assert r["decision"] == "ALLOW" and r["fail_open"] and r["reasons"][0]["code"] == "F001"


def test_guarded_detector_fails_open_on_timeout():
    # Mô phỏng detector chạy lâu hơn ngân sách và bảo đảm timeout không chặn cuộc gọi.
    class Slow:
        def score_batch(self, evs):
            import time
            time.sleep(0.05)
            return [{"decision": "BLOCK"} for _ in evs]
    # Chấm event với ngân sách ngắn rồi xác nhận kết quả được mở an toàn.
    g = GuardedDetector(Slow(), budget_ms=10)
    r = g.score_batch([{"event_id": "E1", "event_type": "CALL", "ts": 0.0, "caller": "1", "callee": "2"}])[0]
    assert r["decision"] == "ALLOW" and r["fail_open"]


def test_fail_open_events_are_counted_in_full_but_summarised(tmp_path):
    # Mọi sự kiện fail-open đều được đếm (bản tóm tắt), chỉ 1 % được ghi chi tiết; chặn / cờ luôn ghi từng bản.
    p = tmp_path / "ev.jsonl"
    log = EvidenceLog(p, sample_fail_open_every=100, fail_open_summary_every=1000)
    for i in range(2500):
        log.submit({"event_id": f"F{i}", "decision": "ALLOW", "fail_open": True, "ts": float(i),
                    "reasons": [{"en": "Fail-open: detector timed out [circuit OPEN]"}]})
    log.submit({"event_id": "B1", "decision": "BLOCK", "fail_open": True, "ts": 9999.0, "reasons": []})
    log.close()
    recs = [json.loads(x)["record"] for x in p.read_text(encoding="utf-8").splitlines()]
    summaries = [r for r in recs if r.get("record_type") == "fail_open_summary"]
    assert sum(s["count"] for s in summaries) == 2500 and summaries[0]["reasons"] == {"circuit OPEN": 1000}
    assert sum(1 for r in recs if r.get("event_id", "").startswith("F")) == 25          # 1 % sample
    assert any(r.get("event_id") == "B1" for r in recs)                                 # decisions always kept
    assert verify(p)[0]


def test_evidence_log_hash_chain_detects_tampering(tmp_path):
    # Kiểm tra log bằng chứng xác minh được ban đầu và phát hiện sửa nội dung một bản ghi.
    p = tmp_path / "ev.jsonl"
    # Ghi 20 quyết định, đóng log và xác nhận toàn bộ chuỗi hash hợp lệ.
    log = EvidenceLog(p, sample_allow_every=1)
    for i in range(20):
        log.submit({"event_id": f"E{i}", "decision": "BLOCK", "risk": 0.9})
    log.close()
    assert verify(p) == (True, 20, None)
    # Sửa quyết định tại dòng thứ tám, rồi xác nhận verifier báo lỗi đúng chỉ số bản ghi.
    lines = p.read_text().splitlines()
    row = json.loads(lines[7]); row["record"]["decision"] = "ALLOW"
    lines[7] = json.dumps(row)
    p.write_text("\n".join(lines) + "\n")
    ok, n, bad = verify(p)
    assert not ok and bad == 8


# Đặc trưng validation: bản của build_dataset.py, hoặc bản cache của compare_models.py (cùng dữ liệu, cùng seed).
VAL_CALL = next((p for p in (ROOT / "data" / "val_call_features.parquet", ROOT / "data" / "compare_cache" / "val_call.parquet")
                 if p.exists()), None)


@pytest.mark.skipif(VAL_CALL is None, reason="run src/build_dataset.py first")
def test_fast_iforest_matches_sklearn():
    # Khi dữ liệu/model sẵn có, so sánh scorer tối ưu với IsolationForest chuẩn.
    import joblib
    import pandas as pd
    from fast_iforest import FastIForest
    # Tải model đã lưu và mẫu validation để kiểm tra nhiều điểm đầu vào thực tế.
    z = joblib.load(ROOT / "models" / "tier1_zero_day_iforest.joblib")
    X = pd.read_parquet(VAL_CALL)[z["features"]].values[:3000].astype(float)
    for iso in z["iso"].values():
        # Hai cách tính score phải trùng trong sai số số học rất nhỏ.
        assert np.allclose(FastIForest(iso).score(X), -iso.score_samples(X), atol=1e-12)


@pytest.mark.skipif(not (ROOT / "models" / "thresholds.json").exists(), reason="run src/train.py first")
def test_detector_scores_real_events_end_to_end():
    # Chạy mô hình thật trên sự kiện thật: bắt lỗi kiểu method bị lùi lề ra khỏi class mà test đơn vị bỏ sót.
    from detector import Detector
    from events_io import load_events
    evs = load_events(ROOT / "data" / "test_events_50k.csv")[:300]
    res = Detector().score_batch(evs)
    assert len(res) == 300 and {r["decision"] for r in res} <= {"ALLOW", "FLAG", "BLOCK"}
    assert all(r["event_id"] == e["event_id"] for r, e in zip(res, evs))


@pytest.mark.skipif(not (ROOT / "models" / "thresholds.json").exists(), reason="run src/train.py first")
def test_silent_caller_is_remembered_after_two_victims():
    # Một cuộc gọi im lặng đơn lẻ không bị phạt (có thể là bấm nhầm); im lặng với người thứ 2 thì ngắt cuộc gọi,
    # và cuộc gọi tiếp theo của số đó bị chặn trước khi đổ chuông.
    from detector import Detector
    det = Detector()

    def audio(eid, callee, ts):
        return {"event_id": eid, "event_type": "AUDIO", "ts": ts, "caller": "84961112222", "callee": callee,
                "codec": "EVS-VoLTE", "vocoder_artifact": 0.2, "pitch_jitter": 0.024, "spectral_flatness": 0.3,
                "speaker_drift": 0.2, "caller_speech_ratio": 0.02, "callee_speech_s": 4.0, "transcript": ""}
    first = det.score_batch([audio("A1", "84351110001", 100.0)])[0]
    assert all(r["code"] != "A108" for r in first["reasons"])
    second = det.score_batch([audio("A2", "84351110002", 200.0)])[0]
    assert second["decision"] == "BLOCK" and second["reasons"][0]["code"] == "A108"
    call = {"event_id": "C3", "event_type": "CALL", "ts": 300.0, "caller": "84961112222", "callee": "84351110003",
            "cell_id": "HN-0001", "imei": "35391110000001", "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": 900}
    third = det.score_batch([call])[0]
    assert third["decision"] == "BLOCK" and third["reasons"][0]["code"] == "A108"
    # sau 1 giờ không tái phạm thì trí nhớ hết hạn
    later = det.score_batch([dict(call, event_id="C4", ts=300.0 + 3 * 3600)])[0]
    assert all(r["code"] != "A108" for r in later["reasons"])


@pytest.mark.skipif(not (ROOT / "data" / "benchmark_results.json").exists(), reason="benchmark not run")
def test_benchmark_meets_block_fpr_target():
    # Khi benchmark có sẵn, xác nhận quy mô/rate dữ liệu và ngân sách FPR được đáp ứng.
    b = json.loads((ROOT / "data" / "benchmark_results.json").read_text())
    # Kiểm tra dataset đúng 50.000 sự kiện và tỷ lệ gian lận không vượt 0,1%.
    assert b["dataset"]["events"] == 50_000
    # Mục tiêu tỉ lệ lừa đảo thực tế ~0,1 %; một cửa sổ 50k chỉ có vài chục sự kiện lừa đảo nên dao động +-15 sự kiện.
    assert b["dataset"]["fraud_rate_pct"] <= 0.15
    # FPR block trên 10 cửa sổ (500.000 sự kiện) phải thấp hơn 0,01%.
    assert b["replicates_10x50k"]["block_fpr_pct"] < 0.01          # < 0.01 % over 500k events
    for et, v in b["latency_single_event"].items():
        # Feature store phải đạt p99 dưới 10 ms và quyết định nằm trong SLA theo loại sự kiện.
        assert v["feature_store_ms"]["p99"] < 10.0
        assert v["decision_ms"]["p99"] < v["budget_ms"]
