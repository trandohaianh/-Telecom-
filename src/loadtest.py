"""Open-loop streaming load test.

Events of the 50k test file arrive at a fixed rate (like a Kafka partition replayed at R events/s).
The consumer drains whatever has arrived, up to MAX_BATCH events, scores them as one micro-batch
(through the circuit breaker) and writes evidence. Latency = decision time - arrival time, so
queueing delay is included. An event whose latency exceeds its tier budget would have been
released by the switch without a verdict (fail-open) and is counted as such.

Run:  python loadtest.py                 (single process, several rates)
      python loadtest.py --partitions 2  (throughput scale-out, key-partitioned)
"""
# Nạp thư viện đồng thời, đo thời gian, tính thống kê và xử lý dữ liệu sự kiện.
from __future__ import annotations

import argparse
import gc
import json
import multiprocessing as mp
import os
import shutil
import tempfile
import time
import zlib
from pathlib import Path

import numpy as np

from config import DATA_DIR, SLA_TIER1_MS, SLA_TIER2_MS
from detector import Detector
from events_io import load_events
from evidence import EvidenceLog
from resilience import GuardedDetector

# Lô tối đa phải nằm trong ngân sách: ~65 µs/sự kiện -> 128 sự kiện ~ 8 ms. Với 512 (~33 ms > 30 ms) chỉ cần máy khựng
# một chút là hàng đợi dồn thành lô đầy, lô đầy chắc chắn quá hạn, 5 lần liên tiếp là breaker mở và mọi thứ fail-open.
MAX_BATCH = 128


def run_rate(events, warm, rate, max_batch=MAX_BATCH, log_path=None):
    # Làm ấm model/JIT, tạo detector sạch, nạp trạng thái warm-up và cấu hình GC độ trễ thấp.
    det = Detector()
    det.score_batch(events[:300])
    det = Detector()
    det.prime(warm)
    det.warm_up()
    # Low-latency Python: move the (large, long-lived) feature-store state out of the cyclic GC's
    # reach and collect young objects less often, otherwise full collections cause 100+ ms stalls.
    from gc_tuning import THRESHOLDS
    gc.collect()
    gc.freeze()
    gc.set_threshold(*THRESHOLDS)
    # Bọc detector bằng guard SLA và mở log bằng chứng cho lượt đo.
    g = GuardedDetector(det, budget_ms=SLA_TIER1_MS)
    # Bằng chứng vẫn được ghi (chi phí ghi nằm trong phép đo) nhưng vào thư mục tạm, xóa ngay sau lượt đo:
    # trước đây mỗi lần chạy để lại ~700 MB log không dùng đến.
    tmp_dir = tempfile.mkdtemp(prefix="loadtest_")
    log = EvidenceLog(log_path or (Path(tmp_dir) / f"loadtest_{int(rate)}.jsonl"))
    # Lập thời điểm đến lý tưởng của từng event và khởi tạo bộ đệm đo.
    n = len(events)
    arrivals = np.arange(n) / rate
    lat = np.zeros(n)
    i = 0
    t0 = time.perf_counter()
    batches = 0
    while i < n:
        # Chờ tới thời điểm đến kế tiếp nếu stream cung cấp event nhanh hơn tốc độ đề nghị.
        now = time.perf_counter() - t0
        if arrivals[i] > now:
            time.sleep(min(arrivals[i] - now, 0.0005))
            continue
        # Chọn các event đã tới, giới hạn kích thước lô, chấm điểm và tính độ trễ tính cả hàng đợi.
        j = int(np.searchsorted(arrivals, now, side="right"))
        j = min(j, i + max_batch)
        res = g.score_batch(events[i:j])
        done = time.perf_counter() - t0
        lat[i:j] = (done - arrivals[i:j]) * 1000.0
        # Ghi từng kết quả bằng chứng, tăng số batch và chuyển con trỏ tới lô kế tiếp.
        for r in res:
            log.submit(r)
        batches += 1
        i = j
    # Đóng log, phân loại SLA theo loại sự kiện và chuẩn bị thống kê latency.
    wall = time.perf_counter() - t0
    log.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    types = np.array([e["event_type"] for e in events])
    budget = np.where(types == "CALL", SLA_TIER1_MS, SLA_TIER2_MS)

    def pct(m):
        # Tính các phân vị cùng cực đại độ trễ cho tập con được chọn.
        v = lat[m]
        return {"p50": round(float(np.percentile(v, 50)), 2), "p95": round(float(np.percentile(v, 95)), 2),
                "p99": round(float(np.percentile(v, 99)), 2), "max": round(float(v.max()), 2)}
    # Trả thông lượng, số batch, phân vị latency, tỷ lệ quá SLA và thống kê thành phần.
    return {"offered_rate_eps": rate, "achieved_eps": round(n / wall, 1), "events": n, "batches": batches,
            "mean_batch": round(n / batches, 1),
            "latency_ms": {"CALL": pct(types == "CALL"), "SMS": pct(types == "SMS"), "AUDIO": pct(types == "AUDIO")},
            "over_budget_pct": round(100 * float(np.mean(lat > budget)), 3),
            "breaker": dict(g.cb.stats), "evidence_records": log.written}


def _worker(args):
    # Mỗi tiến trình đọc dữ liệu chung nhưng chỉ nhận các khóa thuê bao thuộc partition của nó.
    part, k, rate = args
    events = load_events(DATA_DIR / "test_events_50k.csv")
    warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    # Chia sự kiện/warm-up bằng CRC32 khóa thuê bao để cùng thuê bao luôn ở một partition.
    key = lambda e: zlib.crc32((e.get("caller") or e.get("sender")).encode()) % k   # noqa: E731
    ev = [e for e in events if key(e) == part]
    wm = [e for e in warm if key(e) == part]
    return run_rate(ev, wm, rate)


if __name__ == "__main__":
    # Đọc tham số dòng lệnh cho số partition và các tốc độ cần benchmark.
    ap = argparse.ArgumentParser()
    ap.add_argument("--partitions", type=int, default=1)
    ap.add_argument("--rates", type=str, default="5000,10000,15000,20000,25000,30000")
    a = ap.parse_args()
    # Chuyển tốc độ sang số thực, nạp kết quả cũ nếu có và ghi nhận thông tin máy.
    rates = [float(x) for x in a.rates.split(",")]
    out_file = DATA_DIR / "loadtest_results.json"
    results = json.loads(out_file.read_text()) if out_file.exists() else {}
    hw = {"cpu_count": os.cpu_count(), "python": os.sys.version.split()[0]}
    if a.partitions == 1:
        # Chạy lần lượt từng tốc độ trong một tiến trình với toàn bộ cửa sổ.
        events = load_events(DATA_DIR / "test_events_50k.csv")
        warm = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
        results["single_process"] = {"hardware": hw, "runs": []}
        for r in rates:
            # Đo tốc độ, in kết quả và nối bản ghi vào báo cáo tiến trình đơn.
            res = run_rate(events, warm, r)
            print(json.dumps(res))
            results["single_process"]["runs"].append(res)
    else:
        # Chạy mỗi tốc độ qua pool tiến trình, mỗi worker sở hữu một nhóm khóa.
        key = f"partitioned_{a.partitions}"
        results[key] = {"hardware": hw, "note": "throughput-only: each partition owns its subscriber keys; "
                        "cross-key features need a shared store (Redis/Aerospike) in production", "runs": []}
        for r in rates:
            # Phân phối tốc độ đều qua các partition, sau đó tổng hợp thông lượng và latency xấu nhất.
            with mp.get_context("spawn").Pool(a.partitions) as pool:
                parts = pool.map(_worker, [(p, a.partitions, r / a.partitions) for p in range(a.partitions)])
            agg = {"offered_rate_eps": r, "achieved_eps": round(sum(p["achieved_eps"] for p in parts), 1),
                   "call_p99_ms": max(p["latency_ms"]["CALL"]["p99"] for p in parts),
                   "over_budget_pct": round(float(np.mean([p["over_budget_pct"] for p in parts])), 3), "partitions": parts}
            # In bản tổng hợp không kèm chi tiết partition và lưu đầy đủ từng lượt chạy.
            print(json.dumps({k: v for k, v in agg.items() if k != "partitions"}))
            results[key]["runs"].append(agg)
    # Ghi toàn bộ kết quả benchmark ra tệp JSON.
    out_file.write_text(json.dumps(results, indent=2))
