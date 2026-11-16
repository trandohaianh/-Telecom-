"""Monitoring must notice missing/stale inputs and movement inside peer groups."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import drift
from make_drift_baseline import WATCHED


def test_top_feature_is_watched_and_constant_baselines_detect_new_values():
    assert "active_hours_on_network" in WATCHED
    baseline = drift.build_baseline({"f": np.zeros(1000)})
    report = drift.evaluate_drift(baseline, {"f": np.ones(1000)})
    assert report["alert"] == ["f"]


def test_missing_or_nonfinite_feature_is_not_silently_healthy():
    baseline = drift.build_baseline({"f": np.arange(1000)})
    missing = drift.evaluate_drift(baseline, {})
    assert missing["missing_features"] == ["f"]
    assert missing["recommendation"] == drift.INVESTIGATE
    invalid = drift.evaluate_drift(baseline, {"f": [float("nan")] * 1000})
    assert invalid["quality_alert"] == ["f"]
    assert invalid["recommendation"] == drift.INVESTIGATE


def test_small_window_is_reported_as_insufficient_and_stale_inputs_investigated():
    baseline = drift.build_baseline({"f": np.arange(1000)})
    small = drift.evaluate_drift(baseline, {"f": [1, 2]})
    assert small["insufficient_features"] == ["f"]
    assert small["recommendation"] == drift.WATCH
    stale = drift.evaluate_drift(baseline, {"f": np.arange(1000)}, event_times=[10, 20], as_of_ts=1000, max_age_s=100)
    assert stale["freshness"]["event_lag_s"] == 980
    assert stale["recommendation"] == drift.INVESTIGATE


def test_peer_group_drift_is_visible_when_aggregate_distribution_is_unchanged():
    groups = ["domestic"] * 1000 + ["foreign"] * 1000
    reference = np.array([0] * 1000 + [1] * 1000)
    baseline = drift.build_baseline({"f": reference}, segments=groups)
    report = drift.evaluate_drift(baseline, {"f": 1 - reference}, segments=groups)
    assert report["features"]["f"] == 0
    assert report["segments"]["foreign"]["alert"] == ["f"]
    assert report["recommendation"] == drift.INVESTIGATE


def test_nonfinite_scores_are_quality_alerts_not_apparent_allow_traffic():
    baseline = drift.build_baseline({"f": np.arange(1000)}, scores=[0.1] * 1000,
                                    thresholds={"block": 0.9, "flag": 0.5})
    report = drift.evaluate_drift(baseline, {"f": np.arange(1000)}, scores=[float("nan")] * 1000)
    assert "risk_score" in report["quality_alert"]
    assert report["band_shares"]["ALLOW"] == 0
    assert report["recommendation"] == drift.INVESTIGATE


def test_shadow_cli_passes_only_scored_call_features_segments_and_event_times(tmp_path, monkeypatch):
    import json
    import shadow_run
    events = [dict(event_id="one", event_type="CALL", ts=100.0, caller="A", callee="B", label=0),
              dict(event_id="late", event_type="CALL", ts=90.0, caller="A", callee="B", label=0)]
    class FakeDetector:
        def __init__(self, **kwargs):
            self.guard = type("Guard", (), {"state": lambda self: {"late": 1}})()
        def prime(self, _):
            pass
        def process(self, rows):
            out = []
            for ev in rows:
                scored = ev["event_id"] == "one"
                out.append(dict(event_type="CALL", decision="ALLOW", risk=0.1 if scored else None,
                                scored=scored, ingest="ADMITTED" if scored else "LATE", enforced=False,
                                evidence_features={name: 0.0 for name in shadow_run.WATCHED}))
            return out
    captured = {}
    def monitor(base, features, **kwargs):
        captured.update(features=features, **kwargs)
        return dict(recommendation=drift.WATCH, max_feature_psi=0.0, null_psi_max=0.0, band_shares={})
    monkeypatch.setattr(shadow_run, "Detector", FakeDetector)
    monkeypatch.setattr(shadow_run, "DATA_DIR", tmp_path)
    monkeypatch.setattr(shadow_run, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(shadow_run, "load_events", lambda path: events if path.name == "test_events_50k.csv" else [])
    monkeypatch.setattr(drift, "load", lambda path: {})
    monkeypatch.setattr(drift, "evaluate_drift", monitor)
    (tmp_path / "drift_baseline.json").write_text("{}", encoding="utf-8")
    shadow_run.main()
    report = json.loads((tmp_path / "shadow_report.json").read_text())
    assert report["unscored_events"] == 1 and report["scored_events"] == 1
    assert report["agreement_with_labels"] == {"true_negative": 1}
    assert captured["event_times"] == [100.0] and captured["segments"] == ["domestic"]
    assert len(captured["features"]["active_hours_on_network"]) == 1
