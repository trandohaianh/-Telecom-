"""Bộ eval Telecom Fraud Shield: chất lượng mô hình + SLA phục vụ thật qua HTTP, có cổng chặn hồi quy.

  python evals/run_evals.py                      # đủ 2 phần (≈1 phút)
  python evals/run_evals.py --skip-serving       # chỉ chất lượng mô hình (≈3 phút: test + 10 cửa sổ lặp)
  python evals/run_evals.py --windows 0          # nhanh: chỉ cửa sổ test (không dùng làm cổng phát hành)
  python evals/run_evals.py --update-baseline    # chốt mốc mới sau thay đổi có chủ đích

Phần 1 — chất lượng (cửa sổ test độc lập 50k sự kiện, warm-up 24h, đúng bundle đang phục vụ):
  recall/precision/FPR của BLOCK và BLOCK∪FLAG kèm khoảng tin cậy Wilson 95%, theo họ lừa đảo,
  zero-day (Wangiri chưa thấy khi huấn luyện), theo phiên cuộc gọi trước khi đổ chuông.
Phần 2 — phục vụ (tiến trình uvicorn thật, yêu cầu ký HMAC như tổng đài gửi):
  % quyết định chấm xong trong 30 ms, p50/p95/p99, fail-open, khuyến nghị enforce ngoài ý muốn,
  chuỗi bằng chứng toàn vẹn sau khởi động lại.

GIỚI HẠN: dữ liệu là mô phỏng; số liệu đo đường ống và hồi quy, không chứng nhận hiệu quả trên mạng thật.
Mã thoát: 0 đạt · 1 không đạt ngưỡng/hồi quy · 2 thiếu bundle.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
sys.path.insert(0, str(ROOT / "src"))

HIGHER = ["block_recall_pct", "any_recall_pct", "block_precision_pct", "family_min_detected_pct",
          "zero_day_caught_pct", "session_pre_ring_block_recall_pct", "serving_scored_within_30ms_pct",
          "any_recall_min_window_pct"]
LOWER = ["block_fpr_ci_hi_pct", "any_fpr_pct", "serving_p99_ms", "serving_fail_open", "serving_unexpected_enforce"]


def _bundle() -> Path:
    candidate = Path(os.environ.get("FRAUD_ARTIFACT_DIR", ROOT / "runtime" / "artifacts_v22"))
    if not (candidate / "models" / "manifest.json").is_file():
        raise SystemExit(2)
    os.environ.setdefault("FRAUD_MODEL_DIR", str(candidate / "models"))
    os.environ.setdefault("FRAUD_DATA_DIR", str(candidate / "data"))
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ.setdefault(name, "1")
    return candidate


def pool_windows(window_metrics: list) -> dict:
    """M2: pool confusion counts over several independent windows instead of gating on one seed.

    One 50k window holds ~15 distinct fraud parties, so its point estimates move by tens of points
    between seeds (seed 204: block recall 42 %; seed 206: block FPR upper bound 0.020 %).
    """
    from statistical_reporting import wilson
    out, fam, zd = {}, {}, {"events": 0, "missed": 0}
    for key in ("block", "block_or_flag"):
        tp = sum(m[key]["TP"] for m in window_metrics); fp = sum(m[key]["FP"] for m in window_metrics)
        fn = sum(m[key]["FN"] for m in window_metrics); tn = sum(m[key]["TN"] for m in window_metrics)
        out[key] = {"TP": tp, "FP": fp, "FN": fn, "TN": tn,
                    "recall_pct": round(100 * tp / max(tp + fn, 1), 2),
                    "precision_pct": round(100 * tp / max(tp + fp, 1), 2),
                    "fpr_pct": round(100 * fp / max(fp + tn, 1), 4),
                    "fpr_95ci_pct": [round(100 * x, 4) for x in wilson(fp, fp + tn)]}
    for m in window_metrics:
        for name, v in m["by_family"].items():
            f = fam.setdefault(name, {"events": 0, "blocked": 0, "flagged": 0, "missed": 0})
            for k in f:
                f[k] += v[k]
        zd["events"] += m["zero_day"]["events"]; zd["missed"] += m["zero_day"]["missed"]
    for v in fam.values():
        v["detected_pct"] = round(100 * (v["blocked"] + v["flagged"]) / max(v["events"], 1), 1)
    out["by_family"], out["zero_day"] = fam, zd
    out["any_recall_min_window_pct"] = min(m["block_or_flag"]["recall_pct"] for m in window_metrics)
    from statistical_reporting import cluster_bootstrap_recall
    actors = [v for i, m in enumerate(window_metrics) for v in (m.get("by_actor") or {}).values()]
    out["actor_level"] = {"any": cluster_bootstrap_recall(actors, "detected"),
                          "block": cluster_bootstrap_recall(actors, "blocked")}
    out["windows"] = len(window_metrics)
    return out


def model_quality(windows: int = 10) -> dict:
    import evaluate
    from build_dataset import make_window
    from config import DATA_DIR
    from events_io import load_events

    events = load_events(DATA_DIR / "test_events_50k.csv")
    warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    t0 = time.perf_counter()
    results = evaluate.score_stream(events, warmup=warm)
    score_s = time.perf_counter() - t0
    m = evaluate.metrics(events, results)
    per_window = [m]
    # Replicate windows at local start hours spread over the day (M7), regenerated deterministically.
    for i in range(windows):
        rwarm, rev = make_window("rep", 201 + i, 4.3, evaluate.replicate_start_hour(i, windows), "test",
                                 limit=50_000, warmup_hours=24)
        per_window.append(evaluate.metrics(rev, evaluate.score_stream(rev, warmup=rwarm)))
    pooled = pool_windows(per_window)
    fam = pooled["by_family"]
    zd = pooled["zero_day"]
    sess = m["call_sessions"]["pre_ring_block"]
    return {
        "headline": {
            "block_recall_pct": pooled["block"]["recall_pct"],
            "block_precision_pct": pooled["block"]["precision_pct"],
            "block_fpr_pct": pooled["block"]["fpr_pct"],
            "block_fpr_ci_hi_pct": pooled["block"]["fpr_95ci_pct"][1],
            "any_recall_pct": pooled["block_or_flag"]["recall_pct"],
            "any_fpr_pct": pooled["block_or_flag"]["fpr_pct"],
            "any_recall_min_window_pct": pooled["any_recall_min_window_pct"],
            "pooled_windows": pooled["windows"],
            "family_min_detected_pct": min(v["detected_pct"] for v in fam.values()) if fam else None,
            "zero_day_caught_pct": round(100 * (zd["events"] - zd["missed"]) / zd["events"], 1) if zd["events"] else None,
            "session_pre_ring_block_recall_pct": (round(sess["recall_pct"], 2)
                                                  if sess.get("recall_pct") is not None else None),
            "scored_events": m["scored_events"],
            "throughput_eps_offline": round(len(events) / score_s),
        },
        "by_family": fam,
        "block": pooled["block"], "block_or_flag": pooled["block_or_flag"], "zero_day": zd,
        "test_window": {"block": m["block"], "block_or_flag": m["block_or_flag"], "by_family": m["by_family"]},
        "actor_level_recall": pooled.get("actor_level"),
        "call_sessions_pre_ring_block": sess,
    }


def serving(rates: tuple[float, ...], seconds: float) -> dict:
    from verify_live_http import verify_stack

    out = verify_stack(rates=rates, seconds=seconds, model_dir=Path(os.environ["FRAUD_MODEL_DIR"]).resolve())
    runs = out["runs"]
    worst = max(runs, key=lambda r: r["latency_ms"]["p99"])
    within = min(r.get("scored_within_http_30ms_pct", 0.0) for r in runs)
    return {
        "headline": {
            "serving_scored_within_30ms_pct": within,
            "serving_p99_ms": worst["latency_ms"]["p99"],
            "serving_fail_open": sum(r["fail_open"] for r in runs),
            "serving_unexpected_enforce": sum(r.get("unexpected_enforce", 0) for r in runs),
            "evidence_chain_valid": bool(out.get("evidence_chain_valid")),
            "smoke_passed": all(v is True or v == "ok" for v in (out.get("smoke") or {}).values()
                                if isinstance(v, (bool, str))),
        },
        "runs": [{k: r.get(k) for k in ("offered_rate_eps", "completed_eps", "latency_ms", "fail_open", "client_shed",
                                         "scored_within_http_30ms_pct", "decisions", "http_status")} for r in runs],
    }


def gates(metrics: dict, thresholds: dict) -> list[str]:
    fails = []
    for k, v in thresholds.get("min", {}).items():
        if metrics.get(k) is None or metrics[k] < v:
            fails.append(f"{k}={metrics.get(k)} < {v}")
    for k, v in thresholds.get("max", {}).items():
        if metrics.get(k) is None or metrics[k] > v:
            fails.append(f"{k}={metrics.get(k)} > {v}")
    for k in thresholds.get("must_be_true", []):
        if metrics.get(k) is not True:
            fails.append(f"{k} phải là true")
    return fails


def regressions(metrics: dict, baseline: dict, tol: float) -> tuple[list[str], dict]:
    out, deltas = [], {}
    for k in HIGHER + LOWER:
        a, b = metrics.get(k), baseline.get(k)
        if a is None or b is None:
            continue
        deltas[k] = round(a - b, 4)
        # tol là phần trăm điểm cho chỉ số %; với ms/đếm dùng cùng biên tương đối 25%.
        if k in HIGHER and a < b - tol:
            out.append(f"{k}: {b} → {a}")
        if k in LOWER and k.endswith("_ms") and a > b * 1.25 + 2:
            out.append(f"{k}: {b} → {a}")
        elif k in LOWER and "fpr" in k:
            # FPR có mục tiêu 0,01-0,2 %: dung sai 3 điểm % không bao giờ kích hoạt. Dùng biên tương đối
            # 50 % cộng 0,005 điểm % (một vài sự kiện trên 50k) để còn bắt được hồi quy thật.
            if a > b * 1.5 + 0.005:
                out.append(f"{k}: {b} → {a}")
        elif k in LOWER and not k.endswith("_ms") and a > b + (tol if k.endswith("_pct") else 0):
            out.append(f"{k}: {b} → {a}")
    return out, deltas


def markdown(report: dict, fails: list[str], regs: list[str], deltas: dict) -> str:
    h = report["metrics"]
    gates_info = report.get("gates") or {}
    ok = gates_info.get("passed", not fails and not regs)
    lines = [f"# Telecom Fraud Shield eval — {'✅ ĐẠT' if ok else '❌ KHÔNG ĐẠT'}", "",
             f"- Thời điểm {report['started_at']} · bundle `{report['bundle']}` · {report['duration_s']} s",
             "- Dữ liệu mô phỏng: đo hồi quy đường ống, không chứng nhận hiệu quả trên mạng thật.", "",
             "| Chỉ số | Giá trị | Δ baseline |", "|---|---:|---:|"]
    for k, v in h.items():
        d = deltas.get(k)
        lines.append(f"| {k} | {v} | {'' if d is None else f'{d:+}'} |")
    if "quality" in report:
        lines += ["", "## Theo họ lừa đảo", "", "| Họ | Sự kiện | Chặn | Gắn cờ | Bỏ sót | Phát hiện % |", "|---|---:|---:|---:|---:|---:|"]
        for fam, v in report["quality"]["by_family"].items():
            lines.append(f"| {fam} | {v['events']} | {v['blocked']} | {v['flagged']} | {v['missed']} | {v['detected_pct']} |")
    if "serving" in report:
        lines += ["", "## Phục vụ HTTP (tiến trình thật, yêu cầu ký HMAC)", "",
                  "| EPS chào | EPS xong | p50 | p95 | p99 | ≤30ms % | fail-open | client shed |", "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in report["serving"]["runs"]:
            lat = r["latency_ms"]
            lines.append(f"| {r['offered_rate_eps']} | {r['completed_eps']} | {lat['p50']} | {lat['p95']} | {lat['p99']} | "
                         f"{r['scored_within_http_30ms_pct']} | {r['fail_open']} | {r['client_shed']} |")
    if gates_info.get("advisory_misses"):
        lines += ["", "## Chỉ tiêu chất lượng chưa đạt (cảnh báo, không chặn)", ""] + [f"- {f}" for f in gates_info["advisory_misses"]]
    blocking = gates_info.get("blocking_failures")
    if blocking:
        lines += ["", "## Cổng AN TOÀN không đạt (chặn)", ""] + [f"- {f}" for f in blocking]
    elif fails and "blocking_failures" not in gates_info:
        lines += ["", "## Không đạt ngưỡng", ""] + [f"- {f}" for f in fails]
    if regs:
        lines += ["", "## Hồi quy", ""] + [f"- {r}" for r in regs]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-serving", action="store_true")
    ap.add_argument("--windows", type=int, default=10,
                    help="replicate windows pooled with the test window for the quality gates (0 = test only)")
    ap.add_argument("--rates", default="50,100")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "evals")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--tolerance", type=float, default=3.0, help="điểm phần trăm cho chỉ số %%")
    args = ap.parse_args(argv)

    bundle = _bundle()
    started = time.perf_counter()
    report = {"suite": "telecom-fraud-eval", "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "bundle": str(bundle.relative_to(ROOT)) if bundle.is_relative_to(ROOT) else str(bundle)}
    report["quality"] = model_quality(args.windows)
    metrics = dict(report["quality"]["headline"])
    mode = "quality"
    if not args.skip_serving:
        report["serving"] = serving(tuple(float(x) for x in args.rates.split(",")), args.seconds)
        metrics.update(report["serving"]["headline"])
        mode = "full"
    report["duration_s"] = round(time.perf_counter() - started, 1)
    report["metrics"] = metrics

    thresholds = json.loads((EVALS / "thresholds.json").read_text(encoding="utf-8"))
    th = {k: {kk: vv for kk, vv in v.items() if mode == "full" or not kk.startswith("serving_")}
          if isinstance(v, dict) else [x for x in v if mode == "full" or not x.startswith(("serving_", "evidence_", "smoke_"))]
          for k, v in thresholds.items() if not k.startswith("_")}
    fails = gates(metrics, th)
    base_path = EVALS / "baselines" / f"{mode}.json"
    regs, deltas = ([], {})
    if base_path.exists() and not args.update_baseline:
        regs, deltas = regressions(metrics, json.loads(base_path.read_text(encoding="utf-8"))["metrics"], args.tolerance)
    # Owner decision 2026-11-17: safety gates block CI/deploy; quality targets stay unchanged but are
    # advisory (reported red, never silently lowered).
    advisory = set(thresholds.get("advisory", []))
    name_of = lambda line: line.replace(" phải là true", "").split("=")[0].split(":")[0].strip()
    blocking_fails = [f for f in fails if name_of(f) not in advisory]
    blocking_regs = [r for r in regs if name_of(r) not in advisory]
    advisory_misses = [x for x in fails + regs if name_of(x) in advisory]
    passed = not blocking_fails and not blocking_regs
    report["gates"] = {"passed": passed, "threshold_failures": fails, "regressions": regs,
                       "blocking_failures": blocking_fails + blocking_regs, "advisory_misses": advisory_misses}

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{mode}"
    md = markdown(report, fails, regs, deltas)
    stem.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    stem.with_suffix(".md").write_text(md, encoding="utf-8")
    (args.output_dir / "latest.md").write_text(md, encoding="utf-8")
    with (args.output_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": report["started_at"], "mode": mode, "passed": passed, **metrics}) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(md)
    if args.update_baseline:
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(json.dumps({"created_at": report["started_at"], "bundle": report["bundle"],
                                         "metrics": metrics}, indent=1) + "\n", encoding="utf-8")
        print(f"Đã cập nhật baseline {base_path}")
    print(json.dumps(metrics, ensure_ascii=False))
    for line in fails + regs:
        print("  ✗", line)
    print(f"Báo cáo: {stem.with_suffix('.md')}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
