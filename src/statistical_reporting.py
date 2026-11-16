"""Small, dependency-free statistical reports for validation and held-out evaluation."""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Two-sided binomial interval; no observations means complete uncertainty."""
    if n < 0 or k < 0 or k > n:
        raise ValueError("require 0 <= successes <= observations")
    if not n:
        return 0.0, 1.0
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2.0 * n)
    m = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def fpr_report(false_positives: int, negatives: int, target_fpr: float = 0.0001) -> dict:
    """Report uncertainty, not a zero-FP guarantee. All rates use fractions, not percentages."""
    if not 0.0 < target_fpr < 1.0:
        raise ValueError("target_fpr must lie between zero and one")
    lo, hi = wilson(false_positives, negatives)
    return {"false_positives": int(false_positives), "negative_events": int(negatives),
            "empirical_fpr": false_positives / negatives if negatives else None,
            "fpr_95ci": [lo, hi], "target_fpr": target_fpr,
            "upper_95ci_within_target": bool(negatives and hi <= target_fpr),
            "minimum_negatives_if_zero_fp": math.ceil(1.96 ** 2 * (1.0 - target_fpr) / target_fpr),
            "interval_assumption": "binomial observations; correlated traffic needs cluster validation"}


def threshold_validation(negative_scores, thresholds: dict, target_fpr: float = 0.0001,
                         flag_target_fpr: float = 0.005) -> dict:
    values = [float(x) for x in negative_scores]
    if not values or any(not math.isfinite(x) for x in values):
        raise ValueError("threshold validation requires finite legitimate scores")
    reports = {}
    for action in ("block", "flag"):
        threshold = float(thresholds[action])
        if not math.isfinite(threshold):
            raise ValueError("threshold must be finite")
        reports[action] = fpr_report(sum(x >= threshold for x in values), len(values),
                                     target_fpr if action == "block" else flag_target_fpr)
    return {"scope": "same validation window used for threshold selection; descriptive only",
            "probabilities_calibrated": False, "certifies_deployment_fpr": False,
            "selection_note": "thresholds were selected on these negatives; validate on untouched traffic",
            "actions": reports}
