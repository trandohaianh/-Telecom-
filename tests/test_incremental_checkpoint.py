"""D14: incremental checkpoints (base + deltas) restore exactly the same state as a full snapshot.

Run: python -B -m pytest -q tests/test_incremental_checkpoint.py
"""
import json
import random


from feature_store import FeatureStore
from ingest_guard import IngestGuard


def _call(i, ts, caller, callee, imei="35391110000001", cell="HN-0001"):
    return {"event_id": f"E{i}", "event_type": "CALL", "ts": float(ts), "caller": caller, "callee": callee,
            "cell_id": cell, "imei": imei, "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": 10,
            "duration_s": 30.0}


def _canon(state):
    return json.loads(json.dumps(state, sort_keys=True, default=str))


def test_feature_store_base_plus_deltas_equals_full_snapshot():
    rng = random.Random(3)
    live = FeatureStore()
    base = None
    deltas = []
    numbers = [f"8490000{i:04d}" for i in range(40)]
    t = 0.0
    for i in range(3000):
        t += rng.uniform(0.5, 120)
        caller, callee = rng.sample(numbers, 2)
        ev = _call(i, t, caller, callee, imei=f"3539111000{rng.randint(0, 30):04d}", cell=f"HN-000{rng.randint(1, 3)}")
        with live.transaction([ev]):
            live.call_features(ev)
        if rng.random() < 0.3:
            live.commit_cdr(ev)
        if i == 400:
            base = live.state_dict()
            live.take_delta()                     # base contains everything so far
        elif i > 400 and i % 250 == 0:
            live.prune_state(t - 600)             # compaction deletes keys: deltas must carry tombstones
            deltas.append(json.loads(json.dumps(live.take_delta())))
    deltas.append(json.loads(json.dumps(live.take_delta())))
    restored = FeatureStore()
    restored.load_state_dict(json.loads(json.dumps(base)))
    for d in deltas:
        restored.apply_delta(d)
    assert _canon(restored.state_dict()) == _canon(live.state_dict())


def test_guard_delta_carries_only_new_keys_and_restores_equal():
    g = IngestGuard(dedup_ttl_s=600, allowed_lateness_s=120)
    for i in range(50):
        g.admit(_call(i, 1000 + i, "84900000001", "84900000002"))
    base = json.loads(json.dumps(g.state_dict()))
    g.take_delta()
    for i in range(50, 80):
        g.admit(_call(i, 1000 + i, f"849000000{i % 7}", "84900000002"))
    delta = json.loads(json.dumps(g.take_delta()))
    assert len(delta["seen_new"]) == 30                   # not the whole 80-key index
    restored = IngestGuard(dedup_ttl_s=600, allowed_lateness_s=120)
    restored.load_state_dict(base)
    restored.apply_delta(delta)
    assert _canon(restored.state_dict()) == _canon(g.state_dict())


def _submit(rt, ev):
    import time as _t
    from concurrent.futures import Future
    from backend_runtime import Work
    seq, _ = rt.state.reserve("VERDICT", ev)
    work = Work(seq, "VERDICT", ev, _t.perf_counter() + 10, _t.perf_counter(), Future())
    rt._execute(work)
    return work.future.result()


def test_runtime_recovers_from_base_plus_deltas(tmp_path):
    """End to end: periodic checkpoints become deltas, and a fresh runtime recovers identical features."""
    from backend_state import BackendState
    from backend_runtime import InferenceRuntime
    from resilience import GuardedDetector

    class Det:
        def __init__(self):
            self.fs, self.harvest_hits, self.keep_features = FeatureStore(), {}, False
            self.model_version = "test"

        def score_batch(self, events):
            with self.fs.transaction(events):
                out = []
                for e in events:
                    self.fs.call_features(e)
                    out.append({"event_id": e["event_id"], "event_type": "CALL", "decision": "ALLOW", "risk": 0.0,
                                "reasons": [], "ts": e["ts"], "scored": True})
                return out

    path = tmp_path / "state.sqlite3"
    rt = InferenceRuntime(GuardedDetector(Det(), budget_ms=10_000), state=BackendState(path),
                          checkpoint_every=1, checkpoint_interval_s=3600, start=False)
    rt.base_every_deltas = 3
    for i in range(10):          # ends after 2 deltas on top of the last base
        ev = _call(i, 1000 + 10 * i, f"849000000{i % 4}", f"849000001{i % 5}")
        result = rt.submit_sync(ev) if hasattr(rt, "submit_sync") else _submit(rt, ev)
        assert result.get("ingest") == "ADMITTED"
    assert rt.stats["checkpoint_deltas"] >= 1 and rt.stats["checkpoint_bases"] >= 1
    assert rt.state.checkpoint_deltas()
    expected = _canon(rt.g.det.fs.state_dict())
    rt.state.close()
    rt2 = InferenceRuntime(GuardedDetector(Det(), budget_ms=10_000), state=BackendState(path),
                           checkpoint_every=1, checkpoint_interval_s=3600, start=False)
    assert _canon(rt2.g.det.fs.state_dict()) == expected
    rt2.state.close()
