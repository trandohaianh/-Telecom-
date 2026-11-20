"""Vòng 6 (17/11/2026): farm SIM box "ẩn" (SB_T2_STEALTH) — đặc trưng đồng vị trí cell + luật sàn FLAG.

Trước vòng này mọi SIM box bị bỏ sót trong eval gộp đều là farm 2 SIM, IMEI riêng, TAC thường,
3-6 cuộc/giờ: không đặc trưng theo-từng-SIM nào tách được. Mỗi test dưới đây thất bại trên mã cũ.
Run: python -B -m pytest -q tests/test_round6_simfarm.py
"""
import json
import random

import numpy as np

import reason_codes as rc
from feature_store import CALL_FEATURES, FeatureStore
from scaleout import shard_of, shard_of_key, side_update

CELL = "HN-0001"


def call(i, ts, caller, callee, age=20.0, cell=CELL):
    return {"event_type": "CALL", "event_id": f"E{i}", "ts": ts, "caller": caller, "callee": callee, "cell_id": cell,
            "imei": "3539111" + caller[-8:], "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": age,
            "duration_s": 40.0}


def farm_stream():
    """Two fresh SIMs on one cell, each dialling a different stranger every ~10 minutes."""
    out = []
    for k in range(4):
        out.append(call(2 * k, 1000.0 + 600 * k, "84900000001", f"8491000{k:04d}"))
        out.append(call(2 * k + 1, 1100.0 + 600 * k, "84900000002", f"8492000{k:04d}"))
    return out


def run(fs, events):
    feats = []
    for ev in events:
        feats.append(fs.call_features(ev))
        fs.commit_cdr(ev)
    return feats


def test_farm_feature_counts_colocated_outgoing_only_fresh_sims():
    assert CALL_FEATURES[-1] == "farm_cell_sims_1h"
    f = run(FeatureStore(), farm_stream())
    assert [x["farm_cell_sims_1h"] for x in f] == [0, 0, 1, 2, 2, 2, 2, 2]


def test_lone_new_subscriber_and_subscriber_with_incoming_calls_are_not_a_farm():
    fs = FeatureStore()
    lone = run(fs, [call(i, 1000.0 + 600 * i, "84900000003", f"8493000{i:04d}") for i in range(4)])
    assert max(x["farm_cell_sims_1h"] for x in lone) == 1          # one SIM is never a farm
    fs = FeatureStore()
    evs = farm_stream()
    evs.insert(0, call(99, 900.0, "84977777777", "84900000002", age=900))   # someone calls SIM 2
    f = run(fs, evs)
    sim2 = [x["farm_cell_sims_1h"] for ev, x in zip(evs, f) if ev["caller"] == "84900000002"]
    assert sim2 == [0, 0, 0, 0]                                    # it received a call: not outgoing-only
    old = run(FeatureStore(), [dict(e, caller_age_days=900) for e in farm_stream()])   # old, long-active SIMs
    assert max(x["farm_cell_sims_1h"] for x in old) == 2           # newly active (< 6 h) still counts as fresh
    trunk = run(FeatureStore(), [dict(e, cell_id="TRUNK") for e in farm_stream()])
    assert max(x["farm_cell_sims_1h"] for x in trunk) == 0         # only radio cells


def test_farm_feature_is_exact_under_scale_out():
    rng = random.Random(3)
    events = farm_stream()
    for i in range(400):
        events.append(call(100 + i, rng.uniform(800, 5000), f"8495{rng.randrange(30):07d}", f"8496{rng.randrange(60):07d}",
                           age=rng.choice([5.0, 30.0, 400.0]), cell=rng.choice([CELL, "HN-0002"])))
    events.sort(key=lambda e: e["ts"])
    expected = run(FeatureStore(), events)
    assert max(x["farm_cell_sims_1h"] for x in expected) >= 2
    for n in (2, 3):
        stores = [FeatureStore() for _ in range(n)]
        owns = [(lambda s: lambda m: shard_of_key(m, n) == s)(s) for s in range(n)]  # noqa: B023
        got = []
        for ev in events:
            owner = shard_of(ev, n)
            for s in range(n):
                if s != owner:
                    stores[s].observe_foreign(side_update(ev), owns[s])
            got.append(run(stores[owner], [ev])[0])
        assert [g["farm_cell_sims_1h"] for g in got] == [x["farm_cell_sims_1h"] for x in expected]


def test_farm_state_survives_checkpoint_and_delta():
    evs = farm_stream()
    fs = FeatureStore()
    run(fs, evs[:5])
    clone = FeatureStore()
    clone.load_state_dict(json.loads(json.dumps(fs.state_dict())))
    assert run(clone, evs[5:]) == run(fs, evs[5:])
    base = FeatureStore()
    with base.transaction(evs[:3]):
        run(base, evs[:3])
    snap = json.loads(json.dumps(base.state_dict()))
    base.take_delta()
    with base.transaction(evs[3:6]):
        run(base, evs[3:6])
    delta = json.loads(json.dumps(base.take_delta()))
    assert "fresh_cell_calls" in delta
    restored = FeatureStore()
    restored.load_state_dict(snap)
    restored.apply_delta(delta)
    assert run(restored, evs[6:]) == run(base, evs[6:])


def test_detector_flags_a_colocated_farm_with_its_own_reason_and_not_as_zero_day():
    from test_feature_contracts import detector
    d = detector()
    out = d.process(farm_stream())
    assert [r["decision"] for r in out][3:] == ["FLAG"] * 5
    codes = [x["code"] for x in out[-1]["reasons"]]
    assert rc.SIM_FARM[0] in codes and out[-1]["zero_day"] is False
    assert out[0]["decision"] == "ALLOW"


def test_three_colocated_farm_sims_count_as_behavioural_evidence():
    from detector import has_behavioural_evidence
    plain = dict.fromkeys(CALL_FEATURES, 0.0)
    assert not has_behavioural_evidence(plain | {"farm_cell_sims_1h": 2})
    assert has_behavioural_evidence(plain | {"farm_cell_sims_1h": 3})


def test_every_call_feature_has_a_distinct_reason_code():
    codes = [rc.CALL_REASONS[f][0] for f in CALL_FEATURES if f in rc.CALL_REASONS]
    assert "new_callee_ratio_24h" in rc.CALL_REASONS and "farm_cell_sims_1h" in rc.CALL_REASONS
    assert len(codes) == len(set(codes))


def test_spark_cell_partition_matches_feature_store_for_a_farm():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spark"))
    from test_distributed_stream import stage
    rows = [dict(e, imei="353911100000001", duration_s=40.0) for e in farm_stream()]
    expected = run(FeatureStore(), [dict(r) for r in rows])
    states = {"subscriber": {}, "cell": {}}
    got = {}
    for batch in (rows[:3], rows[3:]):
        wm = max(r["ts"] for r in batch)
        core, states["subscriber"] = stage(batch, states["subscriber"], "subscriber", wm)
        enriched = [{**r, "active_hours_on_network": core[r["event_id"]]["active_hours_on_network"]} for r in batch]
        cells, states["cell"] = stage(enriched, states["cell"], "cell", wm)
        for r in batch:
            own = core[r["event_id"]]["farm_cell_sims_1h"]
            got[r["event_id"]] = cells[r["event_id"]]["farm_cell_candidates"] if own > 0 else 0.0
        states = json.loads(json.dumps(states))
    assert [got[r["event_id"]] for r in rows] == [float(x["farm_cell_sims_1h"]) for x in expected]
    assert np.isclose(max(got.values()), 2.0)


def _brute_force(events):
    """Reference definition of farm_cell_sims_1h, recomputed from scratch for every event."""
    fs, out = FeatureStore(), []
    seen = []
    for ev in events:
        f = fs.call_features(ev)
        fs.commit_cdr(ev)
        fresh = 0 <= ev["caller_age_days"] < 60 or f["active_hours_on_network"] < 6
        if fresh and ev["cell_id"] in ("HN-0001", "HN-0002"):
            seen.append((ev["ts"], ev["caller"], ev["callee"], ev["cell_id"]))
        profile = (fresh and f["incoming_24h"] == 0 and f["callee_dispersion_1h"] == 1.0
                   and f["new_callee_ratio_24h"] == 1.0)
        calls, dialled = {}, {}
        for t, sim, b, c in seen:
            if c == ev["cell_id"] and ev["ts"] - 3600 <= t <= ev["ts"]:
                calls[sim] = calls.get(sim, 0) + 1
                dialled.setdefault(sim, set()).add(b)
        bursty = {s for s, n in calls.items() if n >= 2 and len(dialled[s]) >= 2}
        out.append(len(bursty) if profile and ev["caller"] in bursty else 0)
    return out


def test_incremental_farm_counts_equal_brute_force_definition():
    rng = random.Random(11)
    events = farm_stream()
    for i in range(600):
        events.append(call(1000 + i, rng.uniform(800, 9000), f"8495{rng.randrange(25):07d}", f"8496{rng.randrange(400):07d}",
                           age=rng.choice([5.0, 30.0, 400.0]), cell=rng.choice([CELL, "HN-0002"])))
    events.sort(key=lambda e: e["ts"])
    got = [x["farm_cell_sims_1h"] for x in run(FeatureStore(), events)]
    assert got == _brute_force(events) and max(got) >= 2


def test_farm_feature_cost_does_not_grow_with_a_busy_cell():
    import time as _t

    def per_call_us(n):
        fs = FeatureStore()
        evs = [call(i, 1000.0 + i * 0.5, f"8497{i % 300:07d}", f"8498{i:07d}") for i in range(n)]
        run(fs, evs[: n - 200])
        t0 = _t.perf_counter()
        last = run(fs, evs[n - 200:])
        assert last[-1]["farm_cell_sims_1h"] >= 2        # the busy cell really is counted
        return (_t.perf_counter() - t0) / 200 * 1e6
    small, big = min(per_call_us(400) for _ in range(3)), min(per_call_us(6400) for _ in range(3))
    assert big < 4 * small, (small, big)      # O(window) per call would be ~16x slower
