"""Benchmark tầng scale-out (src/scaleout.py): thông lượng, độ trễ lô và chất lượng so với 1 tiến trình.

    python evals/bench_scaleout.py --shards 1 2 --batch 64

Phát lại cửa sổ test 50.000 sự kiện theo lô (như luồng Kafka/gRPC từ nhà mạng) qua router +
N tiến trình chấm điểm, đo:
- thông lượng end-to-end (sự kiện/giây);
- độ trễ mỗi lô p50/p99 (thời gian chờ gom lô ở phía nhà mạng cộng thêm vào trễ quyết định);
- chỉ số phát hiện (block recall/precision/FPR) — phải khớp chạy 1 tiến trình;
- CPU-ms mỗi sự kiện của tiến trình bận nhất -> mô hình năng lực cho 50.000 TPS.
Máy có C lõi thì chỉ đo được tới N ≈ C - 1 (router chiếm một lõi); số lõi cho 50k TPS được ngoại suy
từ CPU-ms/sự kiện đo được, ghi rõ là ngoại suy.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src"))
from run_evals import _bundle  # noqa: E402


def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, max(0, math.ceil(q / 100 * len(values)) - 1))], 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--target-tps", type=int, default=50_000)
    parser.add_argument("--out", default=str(ROOT / "reports" / "evals" / "scaleout.json"))
    args = parser.parse_args()
    _bundle()
    import evaluate
    from config import DATA_DIR
    from events_io import load_events
    from scaleout import ShardedEngine

    events = load_events(DATA_DIR / "test_events_50k.csv")
    warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    rows = []
    for n in args.shards:
        with ShardedEngine(n, warm) as engine:
            results, lat = [], []
            t0 = time.perf_counter()
            for i in range(0, len(events), args.batch):
                b0 = time.perf_counter()
                results += engine.process(events[i : i + args.batch])
                lat.append((time.perf_counter() - b0) * 1000.0)
            wall = time.perf_counter() - t0
            busiest_ms = max(engine.worker_ms)
        m = evaluate.metrics(events, results)
        cpu_ms_per_event = busiest_ms / (len(events) / n)  # trên sự kiện SỞ HỮU của shard bận nhất
        row = {
            "shards": n,
            "batch": args.batch,
            "cpu_cores_on_host": os.cpu_count(),
            "throughput_eps": round(len(events) / wall),
            "batch_latency_ms": {"p50": pct(lat, 50), "p99": pct(lat, 99)},
            "busiest_worker_busy_pct": round(100 * busiest_ms / 1000 / wall, 1),
            "worker_cpu_ms_per_owned_event": round(cpu_ms_per_event, 4),
            "block_recall_pct": m["block"]["recall_pct"],
            "block_precision_pct": m["block"]["precision_pct"],
            "block_fpr_pct": m["block"]["fpr_pct"],
            "any_recall_pct": m["block_or_flag"]["recall_pct"],
            "family_detected_pct": {k: v["detected_pct"] for k, v in m["by_family"].items()},
        }
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "family_detected_pct"}, ensure_ascii=False), flush=True)
    base = rows[0]
    for row in rows[1:]:
        row["quality_matches_single"] = all(
            row[k] == base[k] for k in ("block_recall_pct", "block_precision_pct", "block_fpr_pct", "any_recall_pct")
        )
    # Mô hình năng lực. Chi phí side update đo riêng trong 1 tiến trình (khi đo trong N tiến trình
    # trên máy ít lõi, router và các shard tranh CPU nên số đo bị thổi phồng).
    from feature_store import FeatureStore
    from scaleout import side_update

    fs, sides = FeatureStore(), [side_update(e) for e in events]
    t_side = time.perf_counter()
    for sd in sides:
        fs.observe_foreign(sd, lambda m: False)
    side_cost = (time.perf_counter() - t_side) * 1000.0 / len(sides)
    per_owned = min(r["worker_cpu_ms_per_owned_event"] for r in rows if r["shards"] == 1)
    target = args.target_tps
    shards_needed = 1
    while (target / shards_needed) * per_owned + (target - target / shards_needed) * side_cost > 700:
        shards_needed += 1  # giữ mỗi lõi ≤ 70% để còn đệm cho đỉnh tải và p99
        if shards_needed > 512:
            break
    capacity = {
        "target_tps": target,
        "cpu_ms_per_owned_event_single": per_owned,
        "cpu_ms_per_side_update": round(side_cost, 5),
        "shards_needed_at_70pct_cpu": shards_needed,
        "note": (
            "Ngoại suy từ CPU-ms đo được trên máy này. Ở triển khai thật, producer phía nhà mạng "
            "đã chia partition theo số chủ gọi (Kafka key) nên không cần router riêng; mỗi shard "
            "đọc partition của mình + topic side update. Cần đo lại trên phần cứng đích."
        ),
    }
    print(json.dumps(capacity, ensure_ascii=False))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps({"runs": rows, "capacity": capacity}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
