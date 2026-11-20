"""Tạo bộ dữ liệu mẫu cố định + bảng đặc trưng chuẩn (reference) để đối chiếu với Spark.

Hai file sinh ra trong thư mục này:

    sample_events.csv            các sự kiện CALL của một cửa sổ mô phỏng, đã sắp theo ts
    reference_call_features.parquet   27 đặc trưng, tính bằng chính FeatureStore của dự án

File thứ hai là "đáp án". Mọi thứ viết bằng Spark đều phải ra đúng bằng nó; compare_features.py
là bài kiểm tra.

Mặc định lấy seed 101 giống cửa sổ test của build_dataset.py, nhưng ngắn hơn (1 giờ) để chạy
nhanh. Không có warm-up: cả bản Python lẫn bản Spark đều bắt đầu từ trạng thái rỗng, nên hai bên
công bằng với nhau. Điều đó có nghĩa các đặc trưng dựa trên lịch sử dài (24 giờ) sẽ nhỏ hơn thực
tế — không sao, việc ở đây là hai cách tính có khớp nhau không, chứ không phải con số đẹp.

    python make_sample.py --hours 1 --seed 101
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

import pandas as pd  # noqa: E402
from event_contract import json_safe, validate_call

from data_generator import TrafficSimulator  # noqa: E402
from feature_store import CALL_FEATURES, FeatureStore  # noqa: E402

# Các cột của sự kiện CALL mà Spark sẽ đọc. Giữ đúng tên như generator sinh ra.
EVENT_COLS = [
    "event_id",
    "event_type",
    "ts",
    "caller",
    "callee",
    "cell_id",
    "imei",
    "origin_trunk",
    "network",
    "caller_age_days",
    "duration_s",
    "label",
    "fraud_type",
]


def build(seed: int, hours: float, start_hour: int) -> pd.DataFrame:
    events = TrafficSimulator(seed, window="test").build(hours, start_hour=start_hour, warmup_hours=0.0)
    calls = [e for e in events if e["event_type"] == "CALL" and not e.get("warmup")]
    calls.sort(key=lambda e: (e["ts"], e["event_id"]))
    return pd.DataFrame(calls)


def reference_features(calls: pd.DataFrame) -> pd.DataFrame:
    """Chạy đúng FeatureStore của dự án, tuần tự theo thời gian — đây là đáp án."""
    fs = FeatureStore()
    rows = []
    for ev in calls.sort_values(["ts", "event_id"], kind="stable").to_dict("records"):
        ev = validate_call(json_safe(ev))
        if ev is None:
            continue
        f = fs.call_features(ev)
        fs.commit_cdr(ev)
        f["event_id"] = ev["event_id"]
        rows.append(f)
    return pd.DataFrame(rows)[["event_id"] + CALL_FEATURES]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=101)
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--start-hour", type=int, default=8)
    ap.add_argument("--out-dir", type=Path, default=HERE / "_sample_v2")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    calls = build(args.seed, args.hours, args.start_hour)
    missing = [c for c in EVENT_COLS if c not in calls.columns]
    if missing:
        raise SystemExit(f"generator không sinh các cột: {missing}")

    out_events = args.out_dir / "sample_events.csv"
    calls[EVENT_COLS].to_csv(out_events, index=False)

    feats = reference_features(calls)
    out_feats = args.out_dir / "reference_call_features.parquet"
    feats.to_parquet(out_feats, index=False)
    (args.out_dir / "feature_contract.json").write_text(
        json.dumps(
            {"feature_contract": FeatureStore.FEATURE_CONTRACT, "order": ["ts", "event_id"], "features": CALL_FEATURES}
        ),
        encoding="utf-8",
    )

    print(f"{len(calls):,} sự kiện CALL  ->  {out_events.name}")
    print(f"{len(feats):,} dòng × {len(CALL_FEATURES)} đặc trưng  ->  {out_feats.name}")
    print(
        f"khoảng thời gian: ts {calls['ts'].min():.0f} .. {calls['ts'].max():.0f}"
        f"  ({(calls['ts'].max() - calls['ts'].min()) / 3600:.2f} giờ)"
    )
    print(f"số thuê bao gọi đi khác nhau: {calls['caller'].nunique():,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
