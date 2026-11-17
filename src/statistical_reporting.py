"""Small, dependency-free statistical reports for validation and held-out evaluation."""
from __future__ import annotations

import math


def cluster_bootstrap_recall(clusters, key="detected", n_boot=2000, seed=7, level=0.95):
    """Recall with a percentile CI that resamples whole clusters (actors), not events (M6).

    ``clusters`` is a list of {"events": n, key: k}. Events of one fraud actor are not independent,
    so a Wilson interval over events is far too narrow when 60 events come from 15 actors.
    """
    import numpy as np
    n = np.array([c["events"] for c in clusters], dtype=float)
    k = np.array([c[key] for c in clusters], dtype=float)
    if not len(n) or n.sum() == 0:
        return {"recall_pct": None, "ci_pct": [None, None], "clusters": 0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(n_boot, len(n)))
    boots = k[idx].sum(axis=1) / np.maximum(n[idx].sum(axis=1), 1)
    lo, hi = np.percentile(boots, [100 * (1 - level) / 2, 100 * (1 + level) / 2])
    return {"recall_pct": round(100 * k.sum() / n.sum(), 2), "ci_pct": [round(100 * lo, 1), round(100 * hi, 1)],
            "clusters": int(len(n)), "unit": "fraud actor", "method": "percentile cluster bootstrap"}


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
