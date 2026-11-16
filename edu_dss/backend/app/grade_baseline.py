"""Small, transparent synthetic baseline for the Edu DSS demo.

This module is not trained on institutional or student records. Production
predictions must come from the configured grade-model adapter.
"""
from __future__ import annotations

import math
from statistics import mean

# Synthetic prior-GPA/final-GPA pairs used only to exercise the baseline path.
_DEMO_ROWS = (
    (1.0, 1.3),
    (1.5, 1.7),
    (2.0, 2.1),
    (2.5, 2.4),
    (3.0, 3.0),
    (3.5, 3.4),
    (4.0, 3.8),
)


def _fit_ols(rows: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    if len(rows) < 2:
        raise ValueError("OLS requires at least two training rows")
    xs, ys = [row[0] for row in rows], [row[1] for row in rows]
    if any(not math.isfinite(value) for value in (*xs, *ys)):
        raise ValueError("training values must be finite")
    x_mean, y_mean = mean(xs), mean(ys)
    variance = sum((x - x_mean) ** 2 for x in xs)
    if variance <= 0:
        raise ValueError("training feature must have nonzero variance")
    slope = sum((x - x_mean) * (y - y_mean) for x, y in rows) / variance
    return y_mean - slope * x_mean, slope


def _holdout_metrics() -> dict[str, float | int | str]:
    # Keep the latest two synthetic examples out of fitting for a tiny,
    # chronological demonstration split.
    train, holdout = _DEMO_ROWS[:-2], _DEMO_ROWS[-2:]
    intercept, slope = _fit_ols(train)
    errors = [intercept + slope * x - y for x, y in holdout]
    mae = mean(abs(error) for error in errors)
    rmse = math.sqrt(mean(error * error for error in errors))
    return {
        "split": "last_two_synthetic_rows",
        "samples": len(holdout),
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
    }


def predict_demo_grade(previous_gpa: float) -> dict[str, object]:
    """Return an explicitly synthetic 0-4 GPA demonstration."""
    if not math.isfinite(previous_gpa) or not 0 <= previous_gpa <= 4:
        raise ValueError("previous_gpa must be finite and within 0..4")
    intercept, slope = _fit_ols(_DEMO_ROWS)
    raw_prediction = intercept + slope * previous_gpa
    prediction = min(4.0, max(0.0, raw_prediction))
    return {
        "predicted_grade": round(prediction, 2),
        "scale": "0_to_4",
        "model": "univariate_ordinary_least_squares",
        "mode": "demo_fixture_only",
        "training_rows": len(_DEMO_ROWS),
        "synthetic_holdout_metrics": _holdout_metrics(),
        "limitations": "Synthetic one-feature demo; not validated for academic decisions.",
    }
