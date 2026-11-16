"""Session-level evaluation without attributing later calls' audio to earlier calls."""
from __future__ import annotations

import bisect
from collections import Counter, defaultdict
import math

from statistical_reporting import fpr_report, wilson

RANK = {"ALLOW": 0, "FLAG": 1, "BLOCK": 2}


def _time(event):
    value = float(event["ts"])
    if not math.isfinite(value):
        raise ValueError("evaluation requires finite timestamps")
    return value


def session_outcomes(events, results):
    """Return final decisions, AUDIO -> CALL indices and attribution diagnostics.

    Explicit call_id is authoritative and validated against parties, time and known duration.
    Legacy audio without call_id uses the latest preceding legacy CALL for the same pair, within
    its known duration. It never falls back from an unknown explicit identifier to a phone pair.
    Final outcomes may include later evidence from the SAME call; pre-ring metrics remain separate.
    """
    if len(events) != len(results):
        raise ValueError("events and results must have the same length")
    ids, by_pair = {}, defaultdict(list)
    times = [_time(ev) for ev in events]
    for i, ev in enumerate(events):
        if ev["event_type"] != "CALL":
            continue
        call_id = ev.get("call_id")
        if call_id:
            if str(call_id) in ids:
                raise ValueError("duplicate CALL call_id in evaluation")
            ids[str(call_id)] = i
        else:
            by_pair[(ev.get("caller"), ev.get("callee"))].append((times[i], i))
    for calls in by_pair.values():
        calls.sort()
    final = [r["decision"] for r in results]
    links, reasons = {}, Counter()
    for i, (ev, result) in enumerate(zip(events, results)):
        if ev["event_type"] != "AUDIO":
            continue
        if not result.get("scored", True):
            reasons["unscored_audio"] += 1
            continue
        call_id = ev.get("call_id")
        if call_id:
            target = ids.get(str(call_id))
            method = "explicit_call_id"
        else:
            calls = by_pair.get((ev.get("caller"), ev.get("callee")), [])
            j = bisect.bisect_right(calls, (times[i], i)) - 1
            target = calls[j][1] if j >= 0 else None
            method = "legacy_preceding_call"
        if target is None:
            reasons["unknown_call_id" if call_id else "no_preceding_legacy_call"] += 1
            continue
        call = events[target]
        if (ev.get("caller"), ev.get("callee")) != (call.get("caller"), call.get("callee")):
            reasons["parties_mismatch"] += 1
            continue
        if (times[i], i) < (times[target], target):
            reasons["audio_before_setup"] += 1
            continue
        duration = call.get("duration_s")
        if duration is not None:
            duration = float(duration)
            if not math.isfinite(duration) or duration < 0:
                raise ValueError("CALL duration must be finite and non-negative")
            if times[i] > times[target] + duration:
                reasons["audio_after_call_end"] += 1
                continue
        if not results[target].get("scored", True):
            reasons["unscored_call"] += 1
            continue
        links[i] = target
        reasons[method] += 1
        if RANK[result["decision"]] > RANK[final[target]]:
            final[target] = result["decision"]
    linked = len(links)
    audio_count = sum(ev["event_type"] == "AUDIO" for ev in events)
    return final, links, {"audio_events": audio_count, "linked_audio": linked,
                          "unmatched_audio": audio_count - linked, "reasons": dict(reasons),
                          "legacy_linkage_used": bool(reasons["legacy_preceding_call"])}


def _classification(labels, decisions):
    tp = sum(y and d != "ALLOW" for y, d in zip(labels, decisions))
    fp = sum(not y and d != "ALLOW" for y, d in zip(labels, decisions))
    positives = sum(labels)
    negatives = len(labels) - positives
    lo, hi = wilson(tp, positives)
    return {"TP": tp, "FP": fp, "FN": positives - tp, "TN": negatives - fp,
            "recall_pct": 100.0 * tp / positives if positives else None,
            "recall_95ci_pct": [100.0 * lo, 100.0 * hi],
            "precision_pct": 100.0 * tp / (tp + fp) if tp + fp else None,
            "fpr": fpr_report(fp, negatives)}


def call_session_metrics(events, results):
    final, links, linkage = session_outcomes(events, results)
    calls = [i for i, ev in enumerate(events) if ev["event_type"] == "CALL" and results[i].get("scored", True)]
    labels = [int(events[i]["label"]) for i in calls]
    after_audio = [final[i] for i in calls]
    before_audio = [results[i]["decision"] for i in calls]
    first_detection = {i: _time(events[i]) for i in calls if results[i]["decision"] != "ALLOW"}
    for audio, call in links.items():
        if results[audio]["decision"] != "ALLOW":
            first_detection[call] = min(first_detection.get(call, math.inf), _time(events[audio]))
    delays = sorted(first_detection[i] - _time(events[i]) for i in calls if int(events[i]["label"]) and i in first_detection)
    def median(values):
        if not values:
            return None
        n = len(values)
        return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    def block_only(decisions):
        return ["BLOCK" if d == "BLOCK" else "ALLOW" for d in decisions]
    return {"unit": "CALL session, one observation per setup (AUDIO chunks excluded)",
            "calls": len(calls), "fraud_calls": sum(labels),
            "unscored_calls": sum(ev["event_type"] == "CALL" and not r.get("scored", True) for ev, r in zip(events, results)),
            "pre_ring_block": _classification(labels, block_only(before_audio)),
            "pre_ring_block_or_flag": _classification(labels, before_audio),
            "final_block": _classification(labels, block_only(after_audio)),
            "final_block_or_flag": _classification(labels, after_audio),
            "fraud_time_to_first_action_s": {"detected_calls": len(delays), "median": median(delays),
                                             "max": max(delays) if delays else None},
            "linkage": linkage,
            "limitations": ["Binomial intervals do not account for repeated callers/campaign correlation.",
                            "Legacy linkage is approximate; explicit call_id is required for reliable sessions."]}
