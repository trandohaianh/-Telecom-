"""Shadow mode: score a traffic window, record what the system *would* have done, enforce nothing.

This is the mode a network operator runs before the system is allowed to touch a single call. The
detector reads the stream, computes features, scores, decides and writes evidence exactly as in
production — and the enforcement path is disabled, so no call is dropped and no SMS is held.

The output answers the two questions an operator asks before switching enforcement on:
  1. What would you have done to my traffic? (volumes, by decision and by family)
  2. Has my traffic moved away from what you were calibrated on? (drift against the baseline)

Honesty note: the window here is still the simulated test window, because there is no access to
real CDRs. The mechanism is real and tested; the traffic is not. On real traffic the only change
is the source of the events.

Run:  python src/shadow_run.py   ->  data/shadow_report.json
"""
# Nạp bộ đếm và các thành phần để chấm điểm shadow, chống trùng và kiểm tra drift.
from __future__ import annotations

import json
from collections import Counter, defaultdict

import drift
from config import DATA_DIR, MODEL_DIR
from detector import Detector
from events_io import load_events
from ingest_guard import IngestGuard
from make_drift_baseline import WATCHED


def main(batch=256):
    # Nạp cửa sổ thử nghiệm và dữ liệu lịch sử để làm ấm trạng thái detector.
    events = load_events(DATA_DIR / "test_events_50k.csv")
    warmup = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")

    # Khởi chạy detector ở chế độ shadow, bật guard/giữ đặc trưng rồi nạp warm-up.
    det = Detector(guard=IngestGuard(), shadow=True, keep_features=True)
    det.prime(warmup)

    results = []
    for i in range(0, len(events), batch):
        # Chấm lần lượt từng lô để giới hạn kích thước xử lý trong bộ nhớ.
        results += det.process(events[i:i + batch])

    # Xác nhận shadow chỉ mô phỏng hành động, tuyệt đối không thi hành enforcement.
    enforced = [r for r in results if r.get("enforced")]
    if enforced:  # not an assert: the safety check must survive python -O
        raise RuntimeError(f"shadow mode executed {len(enforced)} actions — that must never happen")

    # Khởi tạo bộ đếm quyết định/đồng thuận và vùng lưu đặc trưng, score để báo cáo.
    scored_pairs = [(ev, r) for ev, r in zip(events, results) if r.get("scored", True)]
    unscored = Counter(r.get("ingest", "UNSCORED") for r in results if not r.get("scored", True))
    would = Counter(r["decision"] for _, r in scored_pairs)
    by_family = defaultdict(Counter)
    agree = Counter()
    feats = defaultdict(list)
    scores, segments, call_times = [], [], []

    for ev, r in scored_pairs:
        # Đối chiếu kết quả dự đoán với nhãn và tổng hợp theo họ gian lận.
        label = int(ev["label"])
        fam = ev.get("fraud_type") or ("CLEAN" if label == 0 else "FRAUD")
        by_family[fam][r["decision"]] += 1
        acted = r["decision"] != "ALLOW"
        agree["true_positive" if (acted and label) else
              "false_positive" if (acted and not label) else
              "false_negative" if (not acted and label) else "true_negative"] += 1
        # Lưu risk/đặc trưng chỉ cho CALL đã chấm điểm để dùng kiểm tra drift.
        if r["event_type"] == "CALL" and r.get("scored", True):
            scores.append(r["risk"])
            call_times.append(float(ev["ts"]))
            ef = r.get("evidence_features", {})
            segments.append("foreign" if ef.get("caller_foreign") == 1 else
                            ("trunk" if ef.get("origin_bank_trunk") == 1 or ef.get("cli_protected") == 1 else "domestic"))
            for f in WATCHED:
                feats[f].append(ef.get(f, float("nan")))


    # Tính quy mô hợp lệ/gian lận từ các nhóm đúng sai đã đếm.
    n_legit = agree["false_positive"] + agree["true_negative"]
    n_fraud = agree["true_positive"] + agree["false_negative"]

    # Tạo báo cáo shadow gồm hành động giả định, tỷ lệ phần trăm và kết quả theo họ.
    report = {
        "mode": "SHADOW",
        "enforced_actions": 0,
        "events": len(events),
        "scored_events": len(scored_pairs),
        "unscored_events": sum(unscored.values()),
        "unscored_by_status": dict(unscored),
        "ingest": det.guard.state(),
        "would_have": dict(would),
        "would_have_pct": {k: round(100.0 * v / max(len(scored_pairs), 1), 4) for k, v in would.items()},
        "would_have_denominator": "scored events only",
        "agreement_with_labels": dict(agree),
        "if_enforced": {
            "legitimate_events": n_legit,
            "legitimate_actioned_pct": round(100.0 * agree["false_positive"] / max(n_legit, 1), 6),
            "fraud_events": n_fraud,
            "fraud_caught_pct": round(100.0 * agree["true_positive"] / max(n_fraud, 1), 4),
        },
        "by_family": {k: dict(v) for k, v in sorted(by_family.items())},
    }

    # Nếu có baseline thì đối chiếu drift, ghi rõ phạm vi dữ liệu được chấm.
    baseline_path = MODEL_DIR / "drift_baseline.json"
    if baseline_path.exists():
        base = drift.load(baseline_path)
        report["drift"] = drift.evaluate_drift(
            base, feats, scores=scores, segments=segments, event_times=call_times,
            as_of_ts=max((float(ev["ts"]) for ev, _ in scored_pairs), default=None))
        report["drift"]["scope"] = "all scored CALL events in the window"

    # Lưu báo cáo và in tóm tắt các quyết định giả định cùng đánh giá drift nếu có.
    (DATA_DIR / "shadow_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ("mode", "enforced_actions", "would_have", "if_enforced")},
                     indent=1, ensure_ascii=False))
    if "drift" in report:
        d = report["drift"]
        print("drift:", d["recommendation"], "| max feature PSI", d["max_feature_psi"],
              "| noise floor", d.get("null_psi_max"), "| bands", d.get("band_shares"))


if __name__ == "__main__":
    main()
