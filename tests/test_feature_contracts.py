"""Regression contracts for causal features, restarts, late events and micro-batches."""
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from detector import Detector
from feature_store import FeatureStore
from ingest_guard import ADMITTED, DUPLICATE, LATE, IngestGuard, event_key


def call(ts, eid="call", **extra):
    ev = dict(event_type="CALL", event_id=eid, call_id=eid, ts=float(ts), caller="A", callee="B",
              cell_id="HN-0001", imei="35391110000001", origin_trunk="LOCAL", network="4G",
              caller_age_days=500)
    ev.update(extra)
    return ev


def audio(ts, callee="B", eid="audio"):
    return dict(event_type="AUDIO", event_id=eid, call_id=eid, ts=float(ts), caller="A", callee=callee,
                codec="EVS-VoLTE", vocoder_artifact=0.2, pitch_jitter=0.02, spectral_flatness=0.3,
                speaker_drift=0.2, caller_speech_ratio=0.02, callee_speech_s=4.0, transcript="")


def test_short_completed_call_is_visible_while_earlier_long_call_remains_pending():
    fs = FeatureStore()
    fs.call_features(call(0))
    fs.commit_cdr(call(0, duration_s=100))
    fs.call_features(call(50))
    fs.commit_cdr(call(50, duration_s=3))
    f = fs.call_features(call(60))
    assert f["completed_calls_seen"] == 1
    assert f["short_call_ratio"] == 1
    assert fs.call_features(call(101))["completed_calls_seen"] == 2


def test_last_twenty_durations_follow_completion_order_not_setup_order():
    fs = FeatureStore()
    fs.commit_cdr(call(0, duration_s=1000))
    for ts in range(10, 35):
        fs.commit_cdr(call(ts, duration_s=2))
    assert fs.call_features(call(40))["completed_calls_seen"] == 20
    f = fs.call_features(call(1001))
    assert f["short_call_ratio"] == pytest.approx(19 / 20)


def test_exact_twenty_four_hour_boundary_includes_exact_boundary_only():
    fs = FeatureStore()
    assert fs._count_out_24h("A", 0.0) == 1
    assert fs._count_out_24h("A", 86400.0) == 2
    assert fs._count_out_24h("A", 86400.001) == 2
    g = FeatureStore()
    g._count_out_24h("A", 0.0)
    assert g._count_out_24h("A", 86459.0) == 1


def test_v2_checkpoint_restores_heap_and_exact_fractional_boundary():
    fs = FeatureStore()
    fs.call_features(call(0.5))
    fs.commit_cdr(call(0.5, duration_s=100))
    fs.call_features(call(1.5))
    fs.commit_cdr(call(1.5, duration_s=2))
    state = json.loads(json.dumps(fs.state_dict()))
    restored = FeatureStore()
    restored.load_state_dict(state)
    assert restored.call_features(call(60.5)) == fs.call_features(call(60.5))
    assert restored.call_features(call(86401.0)) == fs.call_features(call(86401.0))


def test_ambiguous_v1_minute_buckets_are_rejected_without_mutating_existing_state():
    fs = FeatureStore()
    fs.call_features(call(100))
    before = fs.state_dict()
    with pytest.raises(ValueError, match="replay 24 h"):
        fs.load_state_dict(dict(version=1, out24={"A": [[[0, 2]], 2]}))
    assert fs.state_dict() == before


def test_v1_without_outgoing_buckets_can_restore_unsorted_pending_cdrs():
    fs = FeatureStore()
    fs.load_state_dict(dict(version=1, pending_cdr={"A": [[100, 100], [53, 3]]}))
    assert fs.call_features(call(60))["completed_calls_seen"] == 1


def test_global_compaction_preserves_next_features_and_reclaims_expired_associations():
    fs = FeatureStore()
    for ts in (0.0, 10.0, 90.0):
        fs.call_features(call(ts))
        fs.commit_cdr(call(ts, duration_s=2))
    restored = FeatureStore()
    restored.load_state_dict(copy.deepcopy(fs.state_dict()))
    stats = restored.prune_state(90000)
    assert stats["window_events"] == 0
    assert stats["imei_associations"] == 0
    assert stats["subscribers"] == 2
    assert restored.call_features(call(90000)) == fs.call_features(call(90000))


def test_opt_in_metadata_expiry_makes_cold_start_explicit():
    fs = FeatureStore()
    fs.call_features(call(0))
    stats = fs.prune_state(172801, metadata_ttl_s=86400)
    assert stats["metadata_evicted"] == 2
    assert fs.call_features(call(172801))["active_hours_on_network"] == 0


def test_guard_rejects_every_cross_request_regression_even_inside_external_grace():
    guard = IngestGuard(allowed_lateness_s=120)
    assert guard.admit(call(2000, "one")) == ADMITTED
    assert guard.admit(call(1950, "two")) == LATE
    assert guard.admit(call(1950, "two")) == DUPLICATE
    assert guard.watermark == 1880


def test_guard_batch_sort_and_original_occurrence_alignment():
    guard = IngestGuard()
    newer, older = call(20, "newer"), call(10, "older")
    scored, statuses = guard.filter([newer, older, dict(older)])
    assert [e["event_id"] for e in scored] == ["older", "newer"]
    assert statuses == [ADMITTED, ADMITTED, DUPLICATE]
    restored = IngestGuard()
    restored.load_state_dict(json.loads(json.dumps(guard.state_dict())))
    assert restored.admit(newer) == DUPLICATE
    assert restored.admit(call(15, "late")) == LATE
    assert event_key(dict(newer, event_type="CDR")) != event_key(newer)


class ZeroModel:
    def predict(self, x, **kwargs):
        if kwargs.get("pred_contrib"):
            return np.zeros((len(x), x.shape[1] + 1))
        return np.zeros(len(x))


class ZeroAnomaly:
    def score(self, x):
        return np.zeros(len(x))


def detector(guard=None):
    d = Detector.__new__(Detector)
    d.fs, d.guard, d.shadow, d.ledger, d.keep_features = FeatureStore(), guard, False, None, False
    d.harvest_hits = {}
    d.call_model = d.audio_model = ZeroModel()
    d.iso = {name: ZeroAnomaly() for name in ("domestic", "foreign", "trunk")}
    d.iso_feats, d.iso_med, d.iso_iqr = ["calls_1m"], np.array([0.0]), np.array([1.0])
    d.th = {"call": {"block": 0.9, "flag": 0.5,
                     "anomaly_flag": {name: 1 for name in d.iso}}, "audio": {"block": 0.9, "flag": 0.5}}
    return d


def test_detector_results_stay_in_input_order_and_late_inputs_do_not_mutate_state():
    d = detector(IngestGuard())
    results = d.process([call(20, "twenty"), call(10, "ten")])
    assert [r["event_id"] for r in results] == ["twenty", "ten"]
    assert d.fs.oc1["A"] == __import__("collections").deque([10.0, 20.0])
    before = copy.deepcopy(d.fs.state_dict())
    late = d.process([call(15, "fifteen")])[0]
    assert late["ingest"] == LATE and not late["scored"] and not late["enforced"]
    assert late["risk"] is None
    assert late["suppression_reason"] == "EVENT_TIME_BEHIND_FRONTIER"
    assert d.fs.state_dict() == before


def test_harvest_policy_is_batch_size_invariant_and_never_uses_future_audio():
    events = [call(1, "early"), audio(10, "victim1", "a1"), audio(20, "victim2", "a2"), call(30, "later")]
    together = detector().score_batch(events)
    separate_detector = detector()
    separate = [separate_detector.score_batch([ev])[0] for ev in events]
    assert [r["decision"] for r in together] == [r["decision"] for r in separate]
    assert together[0]["decision"] == "ALLOW"
    assert together[-1]["decision"] == "BLOCK"
    assert together[-1]["call_id"] == "later"


def test_prediction_failure_rolls_back_only_touched_feature_keys_before_retry():
    d = detector()
    d.score_batch([call(1, "prior", caller="other")])
    before = copy.deepcopy(d.fs.state_dict())
    class FailingModel:
        def predict(self, *args, **kwargs):
            raise RuntimeError("inference failed")
    d.call_model = FailingModel()
    with pytest.raises(RuntimeError, match="inference failed"):
        d.score_batch([call(10, "failed", duration_s=3)])
    assert d.fs.state_dict() == before
    d.call_model = ZeroModel()
    result = d.score_batch([call(10, "failed", duration_s=3)])[0]
    assert result["scored"]
    assert len(d.fs.oc1["A"]) == 1


def test_policy_failure_rolls_back_harvest_memory_and_audio_state():
    d = detector()
    before = copy.deepcopy(d.fs.state_dict())
    original_result = d._result
    def broken_result(*args, **kwargs):
        out = original_result(*args, **kwargs)
        out["reasons"] = None  # the second harvesting victim reaches policy, then fails
        return out
    d._result = broken_result
    with pytest.raises(AttributeError):
        d.score_batch([audio(10, "v1", "a1"), audio(20, "v2", "a2")])
    assert d.harvest_hits == {}
    assert d.fs.state_dict() == before


def test_three_real_calls_keep_exact_serving_features_through_feedback_export(tmp_path):
    import csv

    from backend_state import BackendState
    from feature_store import CALL_FEATURES
    from shadow_feedback import build_feedback

    class RecordingModel(ZeroModel):
        def predict(self, x, **kwargs):
            self.last_matrix = x.copy()
            return super().predict(x, **kwargs)

    d = detector(IngestGuard())
    d.keep_features = True
    d.call_model = RecordingModel()
    events = [call(ts, f"same-callee-{ts}") for ts in (1, 2, 3)]
    results = d.process(events)
    serving_value = d.call_model.last_matrix[-1, CALL_FEATURES.index("callee_dispersion_1h")]
    assert serving_value == 1 / 3
    assert results[-1]["evidence_features"]["callee_dispersion_1h"] == serving_value
    assert results[-1]["evidence_features"]["callee_dispersion_1h"] != round(1 / 3, 4)

    database = tmp_path / "actual-scoring-journal.sqlite3"
    state = BackendState(database)
    try:
        for event, result in zip(events, results):
            seq, _ = state.reserve("VERDICT", event)
            state.finish(seq, result)
    finally:
        state.db.close()
    labels = tmp_path / "reviews.csv"
    with labels.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["event_id", "group_id", "label", "reviewer"])
        writer.writeheader()
        writer.writerow(dict(event_id=events[-1]["event_id"], group_id="one-campaign", label=0, reviewer="reviewer"))
    destination = tmp_path / "reviewed-feedback"
    report = build_feedback(database, labels, destination, key=b"k" * 32,
                            train_before=10, calibration_before=20)
    assert report["splits"]["train"]["events"] == 1
    exported = json.loads((destination / "train_reviewed_calls.jsonl").read_text(encoding="utf-8"))
    assert exported["callee_dispersion_1h"] == serving_value


def test_compact_evidence_keeps_existing_four_decimal_precision_for_three_calls():
    d = detector(IngestGuard())
    d.th["call"]["anomaly_flag"]["domestic"] = 0.0
    results = d.process([call(ts, f"compact-{ts}") for ts in (1, 2, 3)])
    assert results[-1]["decision"] == "FLAG"
    assert results[-1]["evidence_features"]["callee_dispersion_1h"] == round(1 / 3, 4)
