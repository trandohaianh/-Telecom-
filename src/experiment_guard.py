"""Does the ingestion guard actually matter? Measure it.

A queue that redelivers after a retry, and a feed that arrives slightly out of order, are not
hypothetical: they are the normal behaviour of every at-least-once transport. This script takes
the same 50,000-event test window used for the headline numbers and runs it three ways:

  A  clean      - the stream exactly as generated (the baseline every reported number comes from)
  B  dirty      - 5 % of events redelivered, 2 % delivered late, guard OFF
  C  dirty      - the same corrupted stream, guard ON

and compares B and C against A, decision by decision.

Run:  python src/experiment_guard.py   ->  data/guard_experiment.json
"""

# Nạp bộ thư viện và thành phần để tạo stream nhiễu, chạy detector và đánh giá guard.
from __future__ import annotations

import json
import random
from collections import Counter

from build_dataset import WINDOWS
from config import DATA_DIR
from detector import Detector
from events_io import load_events
from ingest_guard import IngestGuard

RANK = {"ALLOW": 0, "FLAG": 1, "BLOCK": 2}
DUP_RATE = 0.05  # share of events redelivered once
LATE_RATE = 0.02  # share of events held back and delivered out of order
LATE_DELAY_S = 300.0  # how far behind the stream a held-back event reappears
SEED = 20261008


def corrupt(events, dup_rate=DUP_RATE, late_rate=LATE_RATE, delay_s=LATE_DELAY_S, seed=SEED):
    """Turn an exactly-once, ordered stream into an at-least-once, out-of-order one.

    A redelivered event is byte-identical to the original, including its event_id: that is what a
    consumer retry looks like. A late event keeps its own timestamp but is handed over later, which
    is what a delayed partition looks like.
    """
    # Dùng seed cố định để tái lập nhiễu; held chứa sự kiện trễ còn out là luồng giao.
    rng = random.Random(seed)
    held, out = [], []
    n_dup = n_late = 0
    for ev in events:
        # Giao các sự kiện đang chờ khi thời điểm tái xuất đã tới.
        ts = float(ev["ts"])
        while held and held[0][0] <= ts:
            out.append(held.pop(0)[1])
        # Giữ một tỷ lệ sự kiện lại để giao trễ hoặc giao ngay và có thể tạo bản retry.
        if rng.random() < late_rate:
            held.append((ts + delay_s, ev))
            held.sort(key=lambda x: x[0])
            n_late += 1
            continue
        out.append(ev)
        if rng.random() < dup_rate:
            out.append(dict(ev))  # the retry: same event_id, same content
            n_dup += 1
    # Cuối luồng, giao các sự kiện còn giữ và trả thống kê số bản nhiễu.
    out.extend(ev for _, ev in held)
    return out, {"duplicates_injected": n_dup, "late_injected": n_late, "stream_length": len(out)}


def run(events, warmup, guard=None, batch=256):
    # Khởi tạo detector với cấu hình guard, nạp warm-up và xử lý stream theo batch.
    det = Detector(guard=guard)
    det.prime(warmup)
    results = []
    for i in range(0, len(events), batch):
        # Nối kết quả xử lý lô hiện tại vào danh sách theo thứ tự stream.
        results += det.process(events[i : i + batch])
    return det, results


def strongest_by_event(events, results):
    """One outcome per real event: the strongest decision that would actually have been executed.

    Without the guard a redelivered event is scored twice and both decisions reach the network, so
    the strongest one is what the subscriber experiences.
    """
    # Giữ lại quyết định thực sự được thi hành và gộp mỗi event_id về quyết định mạnh nhất.
    out = {}
    for ev, r in zip(events, results):
        if not r.get("enforced", False) and r["decision"] != "ALLOW":
            d = "ALLOW"  # scored, but never executed (late, duplicate, or shadow mode)
        else:
            d = r["decision"]
        k = ev["event_id"]
        if k not in out or RANK[d] > RANK[out[k]]:
            out[k] = d
    return out


def compare(base, other, labels):
    # Xác định thay đổi so với baseline và tách ảnh hưởng lên mẫu hợp lệ/gian lận.
    changed = [k for k in base if base[k] != other.get(k, base[k])]
    legit_newly_blocked = [
        k for k in changed if labels.get(k) == 0 and RANK[other[k]] > RANK[base[k]] and other[k] == "BLOCK"
    ]
    legit_newly_actioned = [k for k in changed if labels.get(k) == 0 and RANK[other[k]] > RANK[base[k]]]
    fraud_now_missed = [k for k in changed if labels.get(k) == 1 and RANK[other[k]] < RANK[base[k]]]
    # Đếm tổng mẫu hợp lệ/gian lận, số hợp lệ bị chặn và số gian lận được bắt.
    n_legit = sum(1 for v in labels.values() if v == 0)
    n_fraud = sum(1 for v in labels.values() if v == 1)
    blocked_legit = sum(1 for k, v in other.items() if labels.get(k) == 0 and v == "BLOCK")
    caught = sum(1 for k, v in other.items() if labels.get(k) == 1 and v != "ALLOW")
    # Trả số thay đổi, ảnh hưởng sai và tỷ lệ FPR/thu hồi để so sánh hai lần chạy.
    return {
        "decisions_changed": len(changed),
        "legitimate_newly_blocked": len(legit_newly_blocked),
        "legitimate_newly_actioned": len(legit_newly_actioned),
        "fraud_newly_missed": len(fraud_now_missed),
        "block_fpr_pct": round(100.0 * blocked_legit / max(n_legit, 1), 6),
        "fraud_caught_pct": round(100.0 * caught / max(n_fraud, 1), 4),
        "examples_legitimate_newly_blocked": legit_newly_blocked[:5],
    }


def main():
    # Đọc test/warm-up, tạo bảng nhãn gốc và in quy mô cửa sổ sạch.
    events = load_events(DATA_DIR / "test_events_50k.csv")
    warmup = load_events(DATA_DIR / "test_warmup_24h_events.csv.gz")
    labels = {e["event_id"]: int(e["label"]) for e in events}
    print(f"clean stream: {len(events):,} events, warm-up {len(warmup):,}")

    # Chạy baseline sạch để có một quyết định mạnh nhất cho từng sự kiện thực.
    _, res_a = run(events, warmup)
    base = strongest_by_event(events, res_a)

    # Tạo luồng trùng/lệch thời gian và in thống kê mức độ hỏng dữ liệu.
    dirty, info = corrupt(events)
    print(
        f'dirty stream: {info["stream_length"]:,} deliveries '
        f'({info["duplicates_injected"]:,} redelivered, {info["late_injected"]:,} late)'
    )

    # Chạy luồng nhiễu khi guard tắt để đo mức thiệt hại không được kiểm soát.
    _, res_b = run(dirty, warmup)
    off = strongest_by_event(dirty, res_b)

    # Chạy cùng luồng với guard bật và thu quyết định thực thi cùng bộ đếm ingest.
    guard = IngestGuard()
    det_c, res_c = run(dirty, warmup, guard=guard)
    on = strongest_by_event(dirty, res_c)

    # Tập hợp cấu hình, thông tin nhiễu và các phép so sánh vào kết quả chính.
    out = {
        "window": {"seed": WINDOWS["test"][0], "events": len(events)},
        "injection": {"duplicate_rate": DUP_RATE, "late_rate": LATE_RATE, "late_delay_s": LATE_DELAY_S, **info},
        "guard_config": guard.state(),
        "A_clean_baseline": {
            "block_fpr_pct": compare(base, base, labels)["block_fpr_pct"],
            "fraud_caught_pct": compare(base, base, labels)["fraud_caught_pct"],
        },
        "B_dirty_guard_off": compare(base, off, labels),
        "C_dirty_guard_on": compare(base, on, labels),
        "ingest_counters": {k: v for k, v in guard.state().items() if k in ("admitted", "duplicate", "late")},
        "status_counts_guard_on": dict(Counter(r.get("ingest", "ADMITTED") for r in res_c)),
    }
    # How the damage grows with the redelivery rate, and that the guard does not care.
    # Lặp lại thí nghiệm ở hai tỷ lệ trùng cao hơn để quan sát xu hướng ảnh hưởng.
    sweep = []
    for rate in (0.10, 0.20):
        # Với mỗi tỷ lệ, chạy luồng nhiễu cả khi tắt và bật guard.
        d2, i2 = corrupt(events, dup_rate=rate)
        _, rb = run(d2, warmup)
        _, rc = run(d2, warmup, guard=IngestGuard())
        off2, on2 = strongest_by_event(d2, rb), strongest_by_event(d2, rc)
        # Lưu kết quả đối chiếu và in nhanh số quyết định thay đổi.
        sweep.append(
            {
                "duplicate_rate": rate,
                "deliveries": i2["stream_length"],
                "guard_off": compare(base, off2, labels),
                "guard_on": compare(base, on2, labels),
            }
        )
        print(
            f'  dup {rate:.0%}: guard off changed {sweep[-1]["guard_off"]["decisions_changed"]}, '
            f'on changed {sweep[-1]["guard_on"]["decisions_changed"]}'
        )
    out["sweep_duplicate_rate"] = sweep

    # Ghi JSON đầy đủ và in các phần chính của so sánh.
    (DATA_DIR / "guard_experiment.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(
        json.dumps(
            {k: out[k] for k in ("B_dirty_guard_off", "C_dirty_guard_on", "ingest_counters")},
            indent=1,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
