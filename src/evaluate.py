"""Independent evaluation on the 50,000-event test window (+ 10 extra windows for confidence).

Run:  python evaluate.py            -> data/benchmark_results.json
"""
# Nạp thư viện thống kê, thời gian, bộ đếm và các thành phần chấm điểm dữ liệu.
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict

import numpy as np

from build_dataset import make_window
from config import DATA_DIR, SLA_FEATURE_STORE_MS, SLA_TIER1_MS, SLA_TIER2_MS
from detector import Detector
from events_io import load_events
from evaluation_contracts import call_session_metrics, session_outcomes
from statistical_reporting import wilson

RANK = {"ALLOW": 0, "FLAG": 1, "BLOCK": 2}


def score_stream(events, batch=256, warmup=()):
    # Khởi tạo detector, nạp trạng thái từ warm-up và chấm sự kiện theo lô.
    det = Detector()
    det.prime(warmup)
    out = []
    for i in range(0, len(events), batch):
        # Nối kết quả của lô hiện tại theo đúng thứ tự đầu vào.
        out += det.score_batch(events[i:i + batch])
    return out


def final_decisions(events, results):
    """Final per-event outcomes using validated session linkage, never whole-window phone pairs."""
    return session_outcomes(events, results)[0]


def metrics(events, results):
    if len(events) != len(results):
        raise ValueError("events and results must have the same length")
    excluded = Counter(r.get("ingest", "UNSCORED") for r in results if not r.get("scored", True))
    scored = [(ev, r) for ev, r in zip(events, results) if r.get("scored", True)]
    events, results = [ev for ev, _ in scored], [r for _, r in scored]
    # Tạo nhãn thật, quyết định cuối cùng và các mặt nạ dự đoán cho từng loại chỉ số.
    y = np.array([int(e["label"]) for e in events])
    final = final_decisions(events, results)
    blk = np.array([d == "BLOCK" for d in final])
    any_ = np.array([d != "ALLOW" for d in final])
    # Tách mẫu hợp lệ/gian lận để tính ma trận nhầm lẫn.
    neg, pos = (y == 0), (y == 1)

    def cm(pred):
        # Đếm TP/FP/FN/TN và tính khoảng tin cậy cho FPR/recall.
        tp, fp = int((pred & pos).sum()), int((pred & neg).sum())
        fn, tn = int((~pred & pos).sum()), int((~pred & neg).sum())
        lo, hi = wilson(fp, fp + tn)
        rlo, rhi = wilson(tp, tp + fn)
        return {"TP": tp, "FP": fp, "FN": fn, "TN": tn,
                "recall_pct": round(100 * tp / max(tp + fn, 1), 2), "recall_95ci_pct": [round(100 * rlo, 1), round(100 * rhi, 1)],
                "precision_pct": round(100 * tp / max(tp + fp, 1), 2),
                "fpr_pct": round(100 * fp / max(fp + tn, 1), 4), "fpr_95ci_pct": [round(100 * lo, 4), round(100 * hi, 4)]}

    fam = defaultdict(lambda: Counter())
    # Đếm kết quả theo họ gian lận để biết số bị chặn, gắn cờ hoặc bỏ sót.
    for e, d in zip(events, final):
        if e["label"]:
            fam[e["fraud_type"]][d] += 1
    family = {k: {"events": sum(v.values()), "blocked": v["BLOCK"], "flagged": v["FLAG"], "missed": v["ALLOW"],
                  "detected_pct": round(100 * (v["BLOCK"] + v["FLAG"]) / sum(v.values()), 1)} for k, v in sorted(fam.items())}

    # Tính số sự kiện gian lận/hợp lệ được xử lý riêng theo từng loại sự kiện.
    tiers = {}
    for et in ("CALL", "SMS", "AUDIO"):
        idx = [i for i, e in enumerate(events) if e["event_type"] == et]
        yy = y[idx]
        dd = np.array([results[i]["decision"] for i in idx])
        tiers[et] = {"events": len(idx), "fraud": int(yy.sum()),
                     "blocked_fraud": int(((dd == "BLOCK") & (yy == 1)).sum()),
                     "flagged_fraud": int(((dd == "FLAG") & (yy == 1)).sum()),
                     "blocked_legit": int(((dd == "BLOCK") & (yy == 0)).sum()),
                     "flagged_legit": int(((dd == "FLAG") & (yy == 0)).sum())}

    # Phân tích riêng các sự kiện zero-day theo nguồn phát hiện và kết quả cuối.
    zd = [r for e, r in zip(events, results) if e["fraud_type"] == "ZERO_DAY_WANGIRI"]
    zero_day = {"events": len(zd), "flagged_by_unsupervised": sum(1 for r in zd if r.get("zero_day")),
                "caught_by_supervised": sum(1 for r in zd if r["decision"] != "ALLOW" and not r.get("zero_day")),
                "missed": sum(1 for r in zd if r["decision"] == "ALLOW")}
    # number-level view: once a number is blocked the operator can bar it, so what matters is
    # whether each fraudulent number is caught and after how many of its events
    # Ghi nhận số lần xuất hiện và lần đầu có hành động cho từng số gian lận.
    first = {}
    for e_, d in zip(events, final):
        if not e_["label"] or e_["event_type"] == "AUDIO":
            continue
        num = e_.get("caller") or e_.get("sender")
        st = first.setdefault((e_["fraud_type"], num), {"events": 0, "first_hit": None})
        st["events"] += 1
        if d != "ALLOW" and st["first_hit"] is None:
            st["first_hit"] = st["events"]
    # Tổng hợp số thuê bao được bắt và trung vị số sự kiện tới lần phát hiện đầu.
    numbers = defaultdict(lambda: {"numbers": 0, "caught": 0, "events_until_first_hit": []})
    for (ft, _), st in first.items():
        n = numbers[ft]
        n["numbers"] += 1
        if st["first_hit"]:
            n["caught"] += 1
            n["events_until_first_hit"].append(st["first_hit"])
    numbers = {k: {"numbers": v["numbers"], "caught": v["caught"],
                   "median_events_until_first_hit": float(np.median(v["events_until_first_hit"])) if v["events_until_first_hit"] else None}
               for k, v in sorted(numbers.items())}
    # Đếm hợp lệ bị gắn cờ bởi mô hình không giám sát và trả toàn bộ báo cáo chỉ số.
    legit_zero_day_flags = sum(1 for e, r in zip(events, results) if e["label"] == 0 and r.get("zero_day"))
    return {"unscored_exclusions": dict(excluded), "scored_events": len(events),
            "block": cm(blk), "block_or_flag": cm(any_), "by_family": family, "by_tier": tiers,
            "zero_day": zero_day, "legit_events_flagged_by_unsupervised": legit_zero_day_flags,
            "by_number": numbers,
            "call_sessions": call_session_metrics(events, results),
            "evaluation_contract": {"version": 2, "headline_unit": "event",
                                    "pre_ring_and_final_separated": True,
                                    "note": "Event metrics include correlated AUDIO chunks; use call_sessions for session outcomes."}}


def latency_profile(events, warm, n=6000):
    """Single-event path (batch of 1): what one call set-up experiences with no queueing."""
    # Chạy lượt khởi động để nạp JIT/cache, rồi tạo detector sạch để đo.
    det = Detector()
    det.score_batch(events[:300])          # JIT / cache warm-up
    det = Detector()
    # Nạp trạng thái warm-up, tạo bộ đệm độ trễ và đo đặc trưng.
    det.prime(warm)
    lat = defaultdict(list)
    fs = defaultdict(list)
    for ev in events[:n]:
        # Đo thời gian chấm một sự kiện và lưu độ trễ của feature store.
        t = time.perf_counter()
        r = det.score_batch([ev])[0]
        lat[ev["event_type"]].append((time.perf_counter() - t) * 1000)
        fs[ev["event_type"]].append(r["feature_store_ms"])

    def pct(v):
        # Tóm tắt các phân vị và cực đại của một dãy thời gian.
        return {"p50": round(float(np.percentile(v, 50)), 3), "p95": round(float(np.percentile(v, 95)), 3),
                "p99": round(float(np.percentile(v, 99)), 3), "max": round(float(np.max(v)), 3), "n": len(v)}
    # Ánh xạ SLA theo loại, rồi xuất phân vị độ trễ và tỷ lệ trong ngân sách.
    budgets = {"CALL": SLA_TIER1_MS, "SMS": SLA_TIER2_MS, "AUDIO": SLA_TIER2_MS}
    return {et: {"decision_ms": pct(lat[et]), "feature_store_ms": pct(fs[et]), "budget_ms": budgets[et],
                 "feature_budget_ms": SLA_FEATURE_STORE_MS,
                 "within_budget_pct": round(100 * float(np.mean(np.array(lat[et]) < budgets[et])), 3)}
            for et in lat}


def replicate_study(seeds=range(201, 211)):
    # Chạy nhiều cửa sổ test độc lập để lượng hóa độ dao động của chỉ số.
    rows = []
    for s in seeds:
        # Sinh cửa sổ với seed riêng, chấm điểm và tính các chỉ số kết quả.
        warm, ev = make_window("rep", s, 4.3, 8, "test", limit=50_000, warmup_hours=24)
        res = score_stream(ev, warmup=warm)
        m = metrics(ev, res)
        rows.append({"seed": s, "fraud_events": int(sum(e["label"] for e in ev)),
                     "recall_any_pct": m["block_or_flag"]["recall_pct"], "recall_block_pct": m["block"]["recall_pct"],
                     "fp_block": m["block"]["FP"], "fp_any": m["block_or_flag"]["FP"], "legit": m["block"]["FP"] + m["block"]["TN"],
                     "zero_day_events": m["zero_day"]["events"], "zero_day_missed": m["zero_day"]["missed"],
                     "by_family": {k: v["detected_pct"] for k, v in m["by_family"].items()},
                     "numbers_caught": {k: f'{v["caught"]}/{v["numbers"]}' for k, v in m["by_number"].items()}})
    # Gộp số hợp lệ và số dương tính giả qua các cửa sổ để tính FPR tổng thể.
    tot_legit = sum(r["legit"] for r in rows)
    fpb, fpa = sum(r["fp_block"] for r in rows), sum(r["fp_any"] for r in rows)
    # Trả các chỉ số trung bình/cực tiểu và khoảng Wilson trên toàn bộ lần lặp.
    return {"windows": len(rows), "events": 50_000 * len(rows), "per_window": rows,
            "recall_any_mean_pct": round(float(np.mean([r["recall_any_pct"] for r in rows])), 2),
            "recall_any_min_pct": min(r["recall_any_pct"] for r in rows),
            "recall_block_mean_pct": round(float(np.mean([r["recall_block_pct"] for r in rows])), 2),
            "block_fpr_pct": round(100 * fpb / tot_legit, 5), "block_fpr_95ci_pct": [round(100 * x, 5) for x in wilson(fpb, tot_legit)],
            "flag_or_block_fpr_pct": round(100 * fpa / tot_legit, 4)}


def new_threats_study(seeds=range(501, 506)):
    """Threats raised in review, measured apart so the headline numbers stay comparable with v2.0:
    SIM-box farms on OLD rented SIMs (no new SIM to spot) and silent voice-harvesting calls."""
    rows, fam, nums = [], defaultdict(list), defaultdict(lambda: [0, 0, []])
    fp_block = legit = 0
    for s in seeds:
        warm, ev = make_window("threats", s, 4.3, 8, "threats", limit=50_000, warmup_hours=24)
        res = score_stream(ev, warmup=warm)
        m = metrics(ev, res)
        fp_block += m["block"]["FP"]
        legit += m["block"]["FP"] + m["block"]["TN"]
        for k, v in m["by_family"].items():
            fam[k].append(v["detected_pct"])
        for k, v in m["by_number"].items():
            n = nums[k]
            n[0] += v["numbers"]
            n[1] += v["caught"]
            if v["median_events_until_first_hit"] is not None:
                n[2].append(v["median_events_until_first_hit"])
        # cuộc gọi thu giọng bị chặn TRƯỚC khi đổ chuông nhờ trí nhớ theo số (mã A108)
        vh_calls = [(e, r) for e, r in zip(ev, res) if e["fraud_type"] == "VOICE_HARVEST" and e["event_type"] == "CALL"]
        rows.append({"seed": s, "fraud_events": int(sum(e["label"] for e in ev)),
                     "harvest_calls": len(vh_calls),
                     "harvest_calls_blocked_pre_ring": sum(1 for _, r in vh_calls if r["decision"] == "BLOCK"),
                     "harvest_blocked_by_memory": sum(1 for _, r in vh_calls
                                                      if any(x["code"] == "A108" for x in r.get("reasons", [])))})
    return {"windows": len(rows), "events": 50_000 * len(rows), "per_window": rows,
            "detected_by_family_mean_pct": {k: round(float(np.mean(v)), 1) for k, v in fam.items()},
            "numbers": {k: {"numbers": v[0], "caught": v[1],
                            "median_events_until_first_hit": float(np.median(v[2])) if v[2] else None}
                        for k, v in nums.items()},
            "block_fpr_pct": round(100 * fp_block / max(legit, 1), 5),
            "block_fpr_95ci_pct": [round(100 * x, 5) for x in wilson(fp_block, legit)]}


if __name__ == "__main__":
    # Bắt đầu đồng hồ và nạp cửa sổ test chính cùng dữ liệu warm-up.
    t0 = time.time()
    events = load_events(DATA_DIR / "test_events_50k.csv")
    warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    # Chấm stream và lập thông tin quy mô, tỷ lệ gian lận và phân bố loại sự kiện.
    res = score_stream(events, warmup=warm)
    out = {"dataset": {"file": "data/test_events_50k.csv", "warmup_file": "data/test_warmup_24h_events.csv.gz",
                       "warmup_events": len(warm), "events": len(events),
                       "fraud_events": int(sum(e["label"] for e in events)),
                       "fraud_rate_pct": round(100 * sum(e["label"] for e in events) / len(events), 3),
                       "by_type": dict(Counter(e["event_type"] for e in events))},
           "accuracy_test_50k": metrics(events, res)}
    # In hai ma trận tổng quát, sau đó đo độ trễ và độ ổn định qua các lần lặp.
    print(json.dumps(out["accuracy_test_50k"]["block"]), json.dumps(out["accuracy_test_50k"]["block_or_flag"]))
    out["latency_single_event"] = latency_profile(events, warm)
    out["replicates_10x50k"] = replicate_study()
    out["new_threats_5x50k"] = new_threats_study()
    # Lưu báo cáo benchmark và từng quyết định (loại bỏ đặc trưng bằng chứng chi tiết).
    out["runtime_s"] = round(time.time() - t0, 1)
    (DATA_DIR / "benchmark_results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    with open(DATA_DIR / "test_decisions_50k.jsonl", "w", encoding="utf-8") as fh:
        for r in res:
            fh.write(json.dumps({k: v for k, v in r.items() if k != "evidence_features"}, ensure_ascii=False) + "\n")
    # In các phần báo cáo chính để xem nhanh kết quả chạy.
    print(json.dumps({k: out[k] for k in ("dataset", "latency_single_event")}, indent=1))
    print(json.dumps(out["accuracy_test_50k"], indent=1))
    print(json.dumps({k: v for k, v in out["replicates_10x50k"].items() if k != "per_window"}, indent=1))
    print(json.dumps(out["new_threats_5x50k"], indent=1))
