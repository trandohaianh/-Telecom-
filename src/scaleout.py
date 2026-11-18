"""Scale-out: chia tải theo số chủ gọi trên nhiều tiến trình (mỗi tiến trình một lõi CPU).

Kiến trúc (giống Kafka Streams / Flink keyed state, nhưng tự chứa để chạy được trên 1 máy):

    luồng sự kiện từ nhà mạng (lô nhỏ, VD 64 sự kiện ≈ 1-2 ms ở 50k TPS)
        └─ Router: hash(chủ gọi) -> shard sở hữu
             ├─ shard k nhận: các sự kiện nó SỞ HỮU (chấm mô hình đầy đủ)
             └─ mọi shard nhận: "side update" rút gọn của sự kiện còn lại (observe_foreign:
                cuộc gọi đến, IMEI<->SIM, SIM mới trên cell) — vài thao tác dict, không chấm mô hình

Mỗi shard áp dụng side update của lô TRƯỚC KHI chấm sự kiện của mình trong lô đó, nên đặc trưng
chéo khoá có thể "thấy trước" tối đa một lô (vài mili-giây) — không đáng kể với cửa sổ 1 giờ /
24 giờ, và được đo lại bằng eval (bench_scaleout.py so chỉ số với chạy 1 tiến trình).

Trạng thái chính (cửa sổ theo chủ gọi, mô hình) không chia sẻ giữa tiến trình nên phần nặng nhất
tăng tuyến tính theo số lõi. Đây là tầng xử lý luồng cho tích hợp lưu lượng lớn; API HTTP từng
sự kiện (server.py) vẫn dùng cho demo/app và tích hợp nhỏ.
"""
from __future__ import annotations

import hashlib
import multiprocessing as mp
import os
import time
from typing import Iterable

_SIDE_FIELDS = ("event_type", "ts", "caller", "callee", "imei", "cell_id", "caller_age_days", "sender", "recipient")


def partition_key(ev: dict) -> str:
    return ev.get("caller") or ev.get("sender") or ev.get("msisdn") or ev.get("event_id", "")


def shard_of_key(key: str, n: int) -> int:
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=4).digest(), "big") % n


def shard_of(ev: dict, n: int) -> int:
    return shard_of_key(partition_key(ev), n)


def side_update(ev: dict) -> dict:
    return {k: ev[k] for k in _SIDE_FIELDS if k in ev}


def _worker(index: int, n: int, conn, warm_events: list[dict], threads: str) -> None:
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ[name] = threads
    from build_dataset import prime
    from detector import Detector

    det = Detector()
    owns = lambda msisdn: shard_of_key(msisdn, n) == index      # noqa: E731
    for ev in warm_events:
        if shard_of(ev, n) == index:
            prime(det.fs, [ev])
        else:
            det.fs.observe_foreign(ev, owns)
    # Same warm-up as the HTTP server (models + Numba JIT on zero rows, no feature-store state): without it
    # the first scored batch of every shard also paid model initialisation and compilation.
    det.warm_up()
    conn.send(("ready", index))
    while True:
        message = conn.recv()
        if message is None:
            break
        owned, foreign = message
        t0 = time.perf_counter()
        for ev in foreign:
            det.fs.observe_foreign(ev, owns)
        results = det.score_batch(owned) if owned else []
        slim = [{"decision": r.get("decision"), "risk": r.get("risk"), "scored": r.get("scored", True),
                 "model_decision": r.get("model_decision"), "tier": r.get("tier"), "zero_day": r.get("zero_day"),
                 "reasons": [{"code": x.get("code")} for x in r.get("reasons", [])][:3]} for r in results]
        conn.send((slim, (time.perf_counter() - t0) * 1000.0))
    conn.close()


class ShardedEngine:
    """N tiến trình chấm điểm + router trong tiến trình gọi. Dùng: with ShardedEngine(4, warm) as e: e.process(batch)."""

    def __init__(self, shards: int, warmup: Iterable[dict] = (), threads_per_worker: str = "1"):
        self.n = shards
        ctx = mp.get_context("spawn")
        self.conns, self.procs = [], []
        warm = list(warmup)
        for i in range(shards):
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=_worker, args=(i, shards, child, warm, threads_per_worker), daemon=True)
            proc.start()
            self.conns.append(parent)
            self.procs.append(proc)
        for conn in self.conns:
            if conn.recv()[0] != "ready":
                raise RuntimeError("shard failed to start")
        self.worker_ms = [0.0] * shards

    def process(self, events: list[dict]) -> list[dict]:
        owners = [shard_of(ev, self.n) for ev in events]
        sides = [side_update(ev) for ev in events] if self.n > 1 else []
        for s, conn in enumerate(self.conns):
            owned = [ev for ev, o in zip(events, owners) if o == s]
            foreign = [sd for sd, o in zip(sides, owners) if o != s]
            conn.send((owned, foreign))
        per_shard = []
        for s, conn in enumerate(self.conns):
            results, ms = conn.recv()
            self.worker_ms[s] += ms
            per_shard.append(iter(results))
        return [next(per_shard[o]) for o in owners]

    def close(self) -> None:
        for conn in self.conns:
            try:
                conn.send(None)
            except (BrokenPipeError, OSError):
                pass
        for proc in self.procs:
            proc.join(timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
