"""Session attribution and uncertainty regression tests; no model training required."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from evaluation_contracts import call_session_metrics, session_outcomes
from statistical_reporting import fpr_report, wilson


def event(kind, ts, call_id=None, duration=50, **extra):
    ev = dict(event_type=kind, ts=ts, caller="A", callee="B", label=1, fraud_type="TEST")
    if call_id is not None:
        ev["call_id"] = call_id
    if kind == "CALL" and duration is not None:
        ev["duration_s"] = duration
    ev.update(extra)
    return ev


def decisions(*values):
    return [dict(decision=v) for v in values]


def test_later_call_audio_cannot_relabel_previous_call_with_same_pair():
    events = [event("CALL", 0, "first"), event("CALL", 100, "second"), event("AUDIO", 110, "second")]
    final, links, report = session_outcomes(events, decisions("ALLOW", "ALLOW", "BLOCK"))
    assert final == ["ALLOW", "BLOCK", "BLOCK"]
    assert links == {2: 1}
    assert report["reasons"]["explicit_call_id"] == 1


def test_legacy_audio_is_bound_to_latest_preceding_call_within_its_duration():
    events = [event("CALL", 0), event("CALL", 100), event("AUDIO", 110)]
    final, links, report = session_outcomes(events, decisions("ALLOW", "ALLOW", "BLOCK"))
    assert final[0] == "ALLOW" and links == {2: 1}
    assert report["legacy_linkage_used"]


@pytest.mark.parametrize(
    "audio,reason",
    [
        (event("AUDIO", 10, "unknown"), "unknown_call_id"),
        (event("AUDIO", -1, "first"), "audio_before_setup"),
        (event("AUDIO", 100, "first"), "audio_after_call_end"),
        (event("AUDIO", 10, "first", caller="C"), "parties_mismatch"),
    ],
)
def test_invalid_linkage_never_upgrades_a_call(audio, reason):
    final, links, report = session_outcomes([event("CALL", 0, "first"), audio], decisions("ALLOW", "BLOCK"))
    assert final[0] == "ALLOW" and not links
    assert report["reasons"][reason] == 1


def test_session_metrics_exclude_chunks_and_report_pre_ring_separately():
    events = [event("CALL", 0, "first"), event("AUDIO", 2, "first"), event("AUDIO", 4, "first")]
    report = call_session_metrics(events, decisions("ALLOW", "FLAG", "BLOCK"))
    assert report["calls"] == 1 and report["fraud_calls"] == 1
    assert report["pre_ring_block"]["TP"] == 0
    assert report["final_block"]["TP"] == 1
    assert report["fraud_time_to_first_action_s"]["median"] == 2
    assert report["final_block"]["fpr"]["empirical_fpr"] is None


def test_unknown_sample_size_never_reports_zero_uncertainty():
    assert wilson(0, 0) == (0.0, 1.0)
    report = fpr_report(0, 3199)
    assert report["empirical_fpr"] == 0
    assert report["fpr_95ci"][1] > 0.001
    assert not report["upper_95ci_within_target"]


def test_rejects_duplicate_session_identity_and_unaligned_results():
    with pytest.raises(ValueError, match="duplicate CALL"):
        session_outcomes([event("CALL", 0, "same"), event("CALL", 100, "same")], decisions("ALLOW", "ALLOW"))
    with pytest.raises(ValueError, match="same length"):
        session_outcomes([event("CALL", 0)], [])


def test_same_timestamp_audio_before_setup_does_not_gain_future_evidence():
    events = [event("AUDIO", 0, "same"), event("CALL", 0, "same")]
    final, links, report = session_outcomes(events, decisions("BLOCK", "ALLOW"))
    assert final[1] == "ALLOW" and links == {}
    assert report["reasons"]["audio_before_setup"] == 1


def test_unscored_rejections_are_excluded_from_headline_metrics():
    from evaluate import metrics

    events = [event("CALL", 0, "scored", label=0), event("CALL", 100, "late", label=0)]
    results = [dict(decision="ALLOW", scored=True), dict(decision="ALLOW", scored=False, ingest="LATE")]
    report = metrics(events, results)
    assert report["unscored_exclusions"] == {"LATE": 1}
    assert report["scored_events"] == 1 and report["block"]["TN"] == 1
