"""Chia tải theo số chủ gọi + side state nhân bản phải cho ĐÚNG đặc trưng như chạy 1 tiến trình."""
import random

from feature_store import FeatureStore
from scaleout import shard_of, shard_of_key, side_update


def _events(n=3000, seed=7):
    rng = random.Random(seed)
    subs = [f"849{i:08d}" for i in range(60)]
    imeis = [f"86769004{i:07d}" for i in range(8)]                 # vài IMEI dùng chung: kiểu SIM box
    cells = ["HN-0001", "HN-0002", "X-FAKE1", "TRUNK"]
    out, ts = [], 1000.0
    for i in range(n):
        ts += rng.uniform(0.5, 40)
        if rng.random() < 0.8:
            a, b = rng.sample(subs, 2)
            out.append({"event_type": "CALL", "ts": ts, "caller": a, "callee": b, "cell_id": rng.choice(cells),
                        "imei": rng.choice(imeis), "origin_trunk": "LOCAL", "network": "4G",
                        "caller_age_days": rng.choice([3.0, 30.0, 400.0, -1.0]), "duration_s": rng.uniform(1, 200),
                        "event_id": f"E{i}"})
        else:
            out.append({"event_type": "SMS", "ts": ts, "sender": rng.choice(subs), "recipient": rng.choice(subs),
                        "sms_route": "P2P", "text": "xin chao", "cell_id": "SMSC", "event_id": f"E{i}"})
    return out


def _features(fs, ev):
    if ev["event_type"] == "CALL":
        f = fs.call_features(ev)
        fs.commit_cdr(ev)
        return f
    return fs.sms_features(ev)


def test_sharded_features_equal_single_process():
    events = _events()
    single = FeatureStore()
    expected = [_features(single, ev) for ev in events]
    for n in (2, 3, 5):
        stores = [FeatureStore() for _ in range(n)]
        owns = [(lambda s: lambda m: shard_of_key(m, n) == s)(s) for s in range(n)]  # noqa: B023
        got = [None] * len(events)
        for i, ev in enumerate(events):
            owner = shard_of(ev, n)
            for s in range(n):
                if s == owner:
                    got[i] = _features(stores[s], ev)
                else:
                    stores[s].observe_foreign(side_update(ev), owns[s])
        mismatched = [i for i, (a, b) in enumerate(zip(expected, got)) if a != b]
        assert not mismatched, (n, mismatched[:3], expected[mismatched[0]], got[mismatched[0]])


def test_without_side_state_cross_key_features_drift():
    """Đối chứng: bỏ side update thì incoming/IMEI/cell sai -> chứng minh test trên có ý nghĩa."""
    events = _events()
    single = FeatureStore()
    expected = [_features(single, ev) for ev in events]
    stores = [FeatureStore() for _ in range(3)]
    got = [_features(stores[shard_of(ev, 3)], ev) for ev in events]
    assert sum(a != b for a, b in zip(expected, got)) > 100
