"""Logic cổng chặn của evals/run_evals.py (không chạy mô hình)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("run_evals", Path(__file__).resolve().parents[1] / "evals" / "run_evals.py")
run_evals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_evals)


def test_gates_min_max_and_flags():
    th = {"min": {"block_recall_pct": 45}, "max": {"block_fpr_ci_hi_pct": 0.01}, "must_be_true": ["evidence_chain_valid"]}
    ok = {"block_recall_pct": 50, "block_fpr_ci_hi_pct": 0.008, "evidence_chain_valid": True}
    assert run_evals.gates(ok, th) == []
    bad = {"block_recall_pct": 40, "block_fpr_ci_hi_pct": 0.02, "evidence_chain_valid": False}
    assert len(run_evals.gates(bad, th)) == 3


def test_regressions_respect_direction_and_latency_slack():
    base = {"block_recall_pct": 55.0, "any_fpr_pct": 0.11, "serving_p99_ms": 12.0, "serving_fail_open": 0}
    same, _ = run_evals.regressions(dict(base, serving_p99_ms=15.0), base, 3.0)       # +25% +2ms slack
    assert same == []
    regs, deltas = run_evals.regressions({"block_recall_pct": 50.0, "any_fpr_pct": 3.5, "serving_p99_ms": 30.0,
                                          "serving_fail_open": 1}, base, 3.0)
    assert {r.split(":")[0] for r in regs} == {"block_recall_pct", "any_fpr_pct", "serving_p99_ms", "serving_fail_open"}
    assert deltas["block_recall_pct"] == -5.0


def test_thresholds_file_is_consistent_with_metric_names():
    import json

    th = json.loads((Path(run_evals.EVALS) / "thresholds.json").read_text(encoding="utf-8"))
    known = set(run_evals.HIGHER + run_evals.LOWER) | {"block_fpr_pct", "evidence_chain_valid"}
    for section in ("min", "max"):
        assert set(th[section]) <= known
