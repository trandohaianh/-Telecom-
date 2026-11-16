"""Đo mất mát chất lượng khi chia tải theo số chủ gọi (partition-by-caller), không chia sẻ trạng thái.

Mỗi shard chỉ thấy sự kiện của các số thuộc về nó (hash ổn định của caller / sender / số gọi
AUDIO), giữ FeatureStore riêng; đặc trưng chéo khoá (cuộc gọi ĐẾN của một số, số SIM trên một
IMEI, SIM mới trên một cell) vì thế chỉ thấy phần cục bộ. Chạy lại toàn bộ cửa sổ test 50.000
sự kiện cho N = 1, 2, 4, 8 shard rồi so chỉ số với N = 1.

    python evals/shard_equivalence.py [--shards 1 2 4 8] [--with-replicated-side-state]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
from run_evals import _bundle  # noqa: E402  (đặt FRAUD_MODEL_DIR/DATA_DIR như bộ eval chính)


def partition_key(ev: dict) -> str:
    return ev.get("caller") or ev.get("sender") or ev.get("msisdn") or ev.get("event_id", "")


def shard_of(ev: dict, n: int) -> int:
    return int.from_bytes(hashlib.blake2b(partition_key(ev).encode(), digest_size=4).digest(), "big") % n


def run_replicated(n: int, events, warm):
    """Mỗi shard chấm sự kiện của mình và nhận side update (observe_foreign) của mọi sự kiện khác,
    đúng thứ tự thời gian — mô phỏng chính xác triển khai N tiến trình + luồng broadcast."""
    import evaluate
    from build_dataset import prime
    from detector import Detector

    dets = [Detector() for _ in range(n)]
    for s, det in enumerate(dets):
        owns = (lambda s: lambda msisdn: shard_of({"caller": msisdn}, n) == s)(s)
        for ev in warm:
            if shard_of(ev, n) == s:
                prime(det.fs, [ev])
            else:
                det.fs.observe_foreign(ev, owns)
    owns_fns = [(lambda s: lambda msisdn: shard_of({"caller": msisdn}, n) == s)(s) for s in range(n)]
    results = [None] * len(events)
    pending = [[] for _ in range(n)]

    def flush(s):
        idxs = pending[s]
        if idxs:
            for i, r in zip(idxs, dets[s].score_batch([events[i] for i in idxs])):
                results[i] = r
            pending[s] = []

    for idx, ev in enumerate(events):
        owner = shard_of(ev, n)
        for s in range(n):
            if s == owner:
                pending[s].append(idx)
            else:
                flush(s)                       # giữ đúng thứ tự: chấm hết việc của shard trước khi nhận side update
                dets[s].fs.observe_foreign(ev, owns_fns[s])
    for s in range(n):
        flush(s)
    return _summary(n, events, results, [sum(1 for e in events if shard_of(e, n) == s) for s in range(n)], None,
                    "partitioned+replicated_side_state")


def _summary(n, events, results, sizes, busiest, mode):
    import evaluate

    m = evaluate.metrics(events, results)
    fam = m["by_family"]
    return {
        "shards": n, "mode": mode,
        "block_recall_pct": m["block"]["recall_pct"], "block_precision_pct": m["block"]["precision_pct"],
        "block_fpr_pct": m["block"]["fpr_pct"], "any_recall_pct": m["block_or_flag"]["recall_pct"],
        "any_fpr_pct": m["block_or_flag"]["fpr_pct"],
        "family_detected_pct": {k: v["detected_pct"] for k, v in fam.items()},
        "largest_shard_events": max(sizes),
        "busiest_shard_seconds": None if busiest is None else round(busiest, 2),
    }


def run(n: int, events, warm):
    import evaluate

    buckets = [[] for _ in range(n)]
    for idx, ev in enumerate(events):
        buckets[shard_of(ev, n)].append(idx)
    warm_b = [[] for _ in range(n)]
    for ev in warm:
        warm_b[shard_of(ev, n)].append(ev)
    results = [None] * len(events)
    busiest = 0.0
    for s in range(n):
        t0 = time.perf_counter()
        out = evaluate.score_stream([events[i] for i in buckets[s]], warmup=warm_b[s])
        busiest = max(busiest, time.perf_counter() - t0)
        for i, r in zip(buckets[s], out):
            results[i] = r
    return _summary(n, events, results, [len(b) for b in buckets], busiest, "partitioned_only")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--replicated", action="store_true", help="kèm side state nhân bản (observe_foreign)")
    parser.add_argument("--out", default=str(ROOT / "reports" / "evals" / "shard_equivalence.json"))
    args = parser.parse_args()
    _bundle()
    sys.path.insert(0, str(ROOT / "src"))
    from config import DATA_DIR
    from events_io import load_events

    events = load_events(DATA_DIR / "test_events_50k.csv")
    warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    rows = [run(n, events, warm) for n in args.shards]
    if args.replicated:
        rows += [run_replicated(n, events, warm) for n in args.shards if n > 1]
    base = rows[0]
    for row in rows:
        row["delta_vs_1"] = {k: round(row[k] - base[k], 4) for k in
                             ("block_recall_pct", "block_precision_pct", "block_fpr_pct", "any_recall_pct", "any_fpr_pct")}
        print(json.dumps({k: v for k, v in row.items() if k not in ("family_detected_pct",)} |
                         {"SIM_BOX": row["family_detected_pct"].get("SIM_BOX")}, ensure_ascii=False))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
