"""Drift monitoring: feature distributions, score distribution, decision rates.

Customer behaviour, network configuration and fraud tactics all move, so a threshold fitted in
October is not necessarily the right threshold in January. This module answers one question:
*has the input or the output moved far enough from the reference that a human should look?*

It deliberately does **not** retrain anything. A drift alert is an input to a decision, not a
trigger for an automatic release: the measured history of this project is that an automatically
promoted model is a model nobody reviewed.

Metric: Population Stability Index, computed on quantile bins taken from the reference window.

Two things were measured before trusting it, and both changed the design:

* **Noise floor.** Splitting the reference window at random and comparing the halves gives a PSI
  of about 0.01 on every watched feature, so anything above 0.10 is signal rather than sampling
  noise. That calibration is stored in the baseline as ``null_psi``.
* **PSI on the raw risk score is useless here.** The score is extremely concentrated (median
  2.3e-6, with the fraud mass at ~1.0), so quantile bins collapse and even two halves of the *same*
  validation window score a PSI of 0.27 — above the "large shift" line, with nothing having moved.
  Score movement is therefore tracked as the share of traffic falling in each decision band, which
  is both stable and the number an operator actually cares about.

    PSI = sum over bins of (actual_share - expected_share) * ln(actual_share / expected_share)

The conventional reading, which this module uses:
    PSI < 0.10            no meaningful shift
    0.10 <= PSI < 0.25    moderate shift, worth watching
    PSI >= 0.25           large shift, investigate before trusting the model
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# PSI_WATCH là ngưỡng cảnh báo: lệch đủ đáng chú ý nhưng chưa phải sự cố lớn.
PSI_WATCH = 0.10
# PSI_ALERT là ngưỡng cảnh báo nặng: feature hoặc decision band đã lệch rõ so với baseline.
PSI_ALERT = 0.25
N_BINS = 10
EPS = 1e-6
MIN_MONITOR_EVENTS = 100

NO_ACTION = "NO_ACTION"
WATCH = "WATCH"
INVESTIGATE = "INVESTIGATE"
RETRAIN_RECOMMENDED = "RETRAIN_RECOMMENDED"


# _edges tính biên quantile để chia mẫu tham chiếu thành bucket ổn định cho PSI.
def _edges(ref: np.ndarray, n_bins: int = N_BINS):
    """Quantile edges of the reference sample, de-duplicated for near-constant features."""
    ref = np.asarray(ref, dtype=float)
    ref = ref[np.isfinite(ref)]
    if ref.size == 0:
        return None
    qs = np.linspace(0.0, 1.0, n_bins + 1)
    e = np.unique(np.quantile(ref, qs))
    if e.size < 3:                      # constant or binary: compare the two values directly
        e = np.unique(ref)
        if e.size < 2:
            # Isolate a constant value so a new nonzero population can still trigger drift.
            return np.unique(np.array([-np.inf, e[0], np.nextafter(e[0], np.inf), np.inf]))
        e = np.concatenate([[-np.inf], (e[:-1] + e[1:]) / 2.0, [np.inf]])
    else:
        e = np.concatenate([[-np.inf], e[1:-1], [np.inf]])
    return e


# _shares chuyển histogram thành phần trăm thực tế của từng bucket để so sánh baseline.
def _shares(x: np.ndarray, edges: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    counts = np.histogram(x, bins=edges)[0].astype(float)
    total = counts.sum()
    if total == 0:
        return np.full(counts.size, EPS)
    return np.maximum(counts / total, EPS)


# psi đo mức lệch của cửa sổ hiện tại so với tham chiếu theo công thức PSI; None nghĩa không có tín hiệu.
def psi(reference, current, n_bins: int = N_BINS) -> float | None:
    """PSI of ``current`` against ``reference``. None when the reference carries no information."""
    edges = _edges(reference, n_bins)
    if edges is None:
        return None
    e_share = _shares(reference, edges)
    a_share = _shares(current, edges)
    return float(np.sum((a_share - e_share) * np.log(a_share / e_share)))


# band_shares tính tỷ lệ lưu lượng trong các dải BLOCK / FLAG / ALLOW, ổn định hơn raw score.
def band_shares(scores, thresholds) -> dict:
    """Share of traffic in each decision band. Stable where a PSI on the raw score is not."""
    s = np.asarray(scores, dtype=float)
    s = s[np.isfinite(s)]
    if not s.size:
        return {"BLOCK": 0.0, "FLAG": 0.0, "ALLOW": 0.0}
    n = s.size
    block = float((s >= thresholds["block"]).sum())
    flag = float(((s >= thresholds["flag"]) & (s < thresholds["block"])).sum())
    return {"BLOCK": block / n, "FLAG": flag / n, "ALLOW": (n - block - flag) / n}


# null_psi ước lượng "nhiễu nền" bằng cách chia cùng mẫu thành 2 nửa ngẫu nhiên rồi đo PSI.
def null_psi(features: dict, seed: int = 0, n_bins: int = N_BINS) -> dict:
    """Noise floor: PSI of the reference against itself, split at random.

    Anything a monitor reports should be read against this, otherwise a threshold is just a guess.
    """
    rng = np.random.default_rng(seed)
    out = {}
    for name, sample in features.items():
        x = np.asarray(sample, dtype=float)
        x = x[np.isfinite(x)]
        if x.size < 100:
            continue
        idx = rng.permutation(x.size)
        v = psi(x[idx[: x.size // 2]], x[idx[x.size // 2:]], n_bins)
        if v is not None:
            out[name] = round(v, 4)
    return out


# build_baseline lưu reference distributions một lần cho so sánh tương lai mà không lưu dữ liệu người dùng.
def build_baseline(features: dict, scores=None, decisions=None, thresholds=None, *, segments=None) -> dict:
    """Record the reference distributions. Called once, on the validation window used for fitting.

    ``features`` maps a feature name to its reference sample. Only quantile edges and shares are
    stored, never the raw rows, so the baseline carries no subscriber data.
    """
    out = {"version": 2, "features": {}, "n_bins": N_BINS,
           "quality": {name: _sample_quality(sample) for name, sample in features.items()}}
    for name, sample in features.items():
        edges = _edges(sample)
        if edges is None:
            continue
        out["features"][name] = {"edges": _dump_edges(edges), "shares": _shares(sample, edges).tolist()}
    out["null_psi"] = null_psi(features)
    if scores is not None and thresholds is not None:
        out["band_shares"] = band_shares(scores, thresholds)
        out["thresholds"] = {k: float(v) for k, v in thresholds.items() if k in ("block", "flag")}
    if decisions is not None:
        n = max(len(decisions), 1)
        out["decision_rates"] = {d: decisions.count(d) / n for d in set(decisions)}
    if segments is not None:
        labels = np.asarray(segments, dtype=str)
        if any(len(sample) != len(labels) for sample in features.values()):
            raise ValueError("segment labels must align with every feature sample")
        if scores is not None and len(scores) != len(labels):
            raise ValueError("segment labels must align with scores")
        if decisions is not None and len(decisions) != len(labels):
            raise ValueError("segment labels must align with decisions")
        out["segments"] = {}
        for name in sorted(set(labels)):
            mask = labels == name
            out["segments"][name] = build_baseline(
                {k: np.asarray(v)[mask] for k, v in features.items()},
                np.asarray(scores)[mask] if scores is not None else None,
                np.asarray(decisions)[mask].tolist() if decisions is not None else None,
                thresholds)
    return out


def _sample_quality(sample) -> dict:
    x = np.asarray(sample, dtype=float)
    n = int(x.size)
    finite = int(np.isfinite(x).sum())
    return {"observations": n, "finite": finite, "nonfinite_rate": (n - finite) / n if n else None}


# _dump_edges chuyển biên mở thành chuỗi JSON an toàn để lưu vào baseline file.
def _dump_edges(edges) -> list:
    """JSON has no infinity, so the open ends travel as strings and keep their sign."""
    return ["-inf" if v == -np.inf else ("inf" if v == np.inf else float(v)) for v in edges]


# _load_edges khôi phục JSON baseline về NumPy array để tính PSI lại từ spec đã lưu.
def _load_edges(spec) -> np.ndarray:
    return np.array([{"-inf": -np.inf, "inf": np.inf}.get(v, v) for v in spec["edges"]], dtype=float)


# _psi_from_spec tính PSI trực tiếp từ spec đã lưu mà không cần đọc hết mẫu gốc.
def _psi_from_spec(spec, current) -> float:
    edges = _load_edges(spec)
    e_share = np.maximum(np.array(spec["shares"], dtype=float), EPS)
    a_share = _shares(current, edges)
    return float(np.sum((a_share - e_share) * np.log(a_share / e_share)))


# evaluate_drift so sánh cửa sổ hiện tại với baseline và trả về báo cáo drift không có side effect.
def evaluate_drift(baseline: dict, features: dict, scores=None, decisions=None, *,
                   segments=None, event_times=None, as_of_ts=None, max_age_s=300.0) -> dict:
    """Compare a current window against the baseline. Returns one report, no side effects."""
    per_feature, quality = {}, {}
    expected = set(baseline.get("features", {})) | set(baseline.get("quality", {}))
    missing = sorted(expected - set(features))
    insufficient, quality_alert = [], []
    for name in sorted(expected & set(features)):
        quality[name] = q = _sample_quality(features[name])
        ref = baseline.get("quality", {}).get(name, {})
        if q["finite"] < MIN_MONITOR_EVENTS:
            insufficient.append(name)
        if q["finite"] == 0 or (q["nonfinite_rate"] or 0.0) > max(0.01, (ref.get("nonfinite_rate") or 0.0) + 0.01):
            quality_alert.append(name)
        if name in baseline.get("features", {}) and q["finite"] >= MIN_MONITOR_EVENTS:
            per_feature[name] = round(_psi_from_spec(baseline["features"][name], features[name]), 4)

    report = {"features": per_feature,
              "watch": sorted([k for k, v in per_feature.items() if PSI_WATCH <= v < PSI_ALERT]),
              "alert": sorted([k for k, v in per_feature.items() if v >= PSI_ALERT])}
    report.update(quality=quality, missing_features=missing, insufficient_features=insufficient,
                  quality_alert=quality_alert, min_monitor_events=MIN_MONITOR_EVENTS)
    report["max_feature_psi"] = max(per_feature.values(), default=0.0)

    report["null_psi_max"] = max(baseline.get("null_psi", {}).values(), default=0.0)

    if scores is not None:
        report["score_quality"] = _sample_quality(scores)
        if report["score_quality"]["finite"] == 0 or (report["score_quality"]["nonfinite_rate"] or 0) > 0.01:
            report["quality_alert"].append("risk_score")

    if scores is not None and "band_shares" in baseline:
        now = band_shares(scores, baseline["thresholds"])
        ref = baseline["band_shares"]
        report["band_shares"] = {k: round(v, 6) for k, v in now.items()}
        report["band_share_ratio"] = {k: (round(now[k] / ref[k], 3) if ref.get(k) else None)
                                      for k in ("BLOCK", "FLAG", "ALLOW")}
        # The block band is the one subscribers feel. Doubling or halving it is a human question,
        # whatever the feature distributions say.
        r = report["band_share_ratio"]["BLOCK"]
        report["block_band_moved"] = bool(r is not None and (r >= 2.0 or r <= 0.5))

    if decisions is not None and "decision_rates" in baseline:
        n = max(len(decisions), 1)
        now = {d: decisions.count(d) / n for d in ("ALLOW", "FLAG", "BLOCK")}
        ref = baseline["decision_rates"]
        report["decision_rates"] = {k: round(v, 6) for k, v in now.items()}
        report["decision_rate_delta"] = {k: round(now.get(k, 0.0) - ref.get(k, 0.0), 6)
                                         for k in ("ALLOW", "FLAG", "BLOCK")}
        # A block rate that moves by more than half of itself is worth a human look even when the
        # feature distributions look stable: it is the number customers feel.
        ref_block = ref.get("BLOCK", 0.0)
        report["block_rate_doubled"] = bool(ref_block > 0 and now.get("BLOCK", 0.0) > 2 * ref_block)

    if event_times is not None:
        times = np.asarray(event_times, dtype=float)
        finite_times = times[np.isfinite(times)]
        if not finite_times.size:
            report["freshness"] = {"status": "NO_FINITE_EVENT_TIMES", "stale": True}
        else:
            as_of = float(as_of_ts) if as_of_ts is not None else float(np.max(finite_times))
            if not np.isfinite(as_of) or not np.isfinite(max_age_s) or max_age_s < 0:
                raise ValueError("freshness bounds must be finite and max_age_s non-negative")
            lag = max(0.0, as_of - float(np.max(finite_times)))
            report["freshness"] = {"event_lag_s": lag, "max_age_s": max_age_s,
                                   "stale": lag > max_age_s,
                                   "as_of_source": "explicit" if as_of_ts is not None else "window_event_time"}
    if segments is not None and baseline.get("segments"):
        labels = np.asarray(segments, dtype=str)
        if any(len(sample) != len(labels) for sample in features.values()):
            raise ValueError("segment labels must align with feature samples")
        if scores is not None and len(scores) != len(labels):
            raise ValueError("segment labels must align with scores")
        if decisions is not None and len(decisions) != len(labels):
            raise ValueError("segment labels must align with decisions")
        report["segments"] = {}
        for name, ref in baseline["segments"].items():
            mask = labels == name
            report["segments"][name] = evaluate_drift(
                ref, {k: np.asarray(v)[mask] for k, v in features.items()},
                np.asarray(scores)[mask] if scores is not None else None,
                np.asarray(decisions)[mask].tolist() if decisions is not None else None)
        report["unknown_segments"] = sorted(set(labels) - set(baseline["segments"]))
    report["recommendation"] = _recommend(report)
    return report


# _recommend đưa ra khuyến nghị thận trọng: không tự động release hay retrain khi chưa có người xem xét.
def _recommend(report: dict) -> str:
    """Deliberately conservative: a recommendation is a request for a human, never a release."""
    if report.get("missing_features") or report.get("quality_alert") or report.get("freshness", {}).get("stale"):
        return INVESTIGATE
    segment_recommendations = [s["recommendation"] for s in report.get("segments", {}).values()]
    if RETRAIN_RECOMMENDED in segment_recommendations:
        return RETRAIN_RECOMMENDED
    if INVESTIGATE in segment_recommendations:
        return INVESTIGATE
    if len(report["alert"]) >= 3:
        # Many features moved at once: the model no longer sees the world it was fitted on.
        # Retraining is the likely fix, but it still goes through evaluation and approval.
        return RETRAIN_RECOMMENDED
    if report["alert"] or report.get("block_band_moved"):
        return INVESTIGATE
    if report["watch"] or report.get("block_rate_doubled") or report.get("insufficient_features") or report.get("unknown_segments") or WATCH in segment_recommendations:
        return WATCH
    return NO_ACTION


# save ghi báo cáo drift dưới dạng JSON UTF-8 để UI hoặc job sau đọc lại.
def save(obj: dict, path) -> None:
    Path(path).write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


# load nạp lại report drift đã lưu cho dashboard hoặc workflow tiếp theo.
def load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
