"""Generate train / validation / independent test windows and extract features with the runtime feature store."""

import json
import time

import pandas as pd

from config import DATA_DIR
from data_generator import TrafficSimulator
from feature_store import FeatureStore
from model_manifest import write_dataset_manifest

# Định nghĩa các cửa sổ train/val/test và tham số seed, thời lượng, giờ bắt đầu và warm-up.
WINDOWS = {
    # name: (seed, hours, start_hour, window kind)
    # name: (seed, hours, start_hour, kind, warm-up hours)
    # 12 h warm-up like val/test: history features ("active on the network for N hours") are not empty at the start
    "train": (11, 48, 0, "train", 12),
    "val": (22, 24, 0, "val", 12),
    "cal": (33, 24, 0, "val", 12),  # independent threshold calibration, never early stopping
    "test": (101, 4.3, 8, "test", 24),  # the independent 50,000-event evaluation set (after a 24 h warm-up)
    # drift reference (D7): realistic prevalence, known families only; never trains or calibrates.
    # Same local hours and warm-up as the monitored window, so history features are comparable.
    "ref": (55, 4.3, 8, "reference", 24),
}


# Nạp từng event warm-up vào feature store để chuẩn bị state lịch sử trước khi trích xuất feature.
def prime(fs, events):
    for ev in events:
        if ev["event_type"] == "CALL":
            fs.call_features(ev)
            fs.commit_cdr(ev)
        elif ev["event_type"] == "SMS":
            fs.sms_features(ev)
        else:
            fs.audio_features(ev)


# Chạy lại event theo thứ tự thời gian để tạo DataFrame feature tách theo loại CALL/SMS/AUDIO.
def replay(events, warmup=()):
    """Run events through a fresh feature store in time order; returns per-type feature frames."""
    fs = FeatureStore()
    prime(fs, warmup)
    rows = {"CALL": [], "SMS": [], "AUDIO": []}
    for ev in events:
        et = ev["event_type"]
        if et == "CALL":
            f = fs.call_features(ev)
            fs.commit_cdr(ev)
        elif et == "SMS":
            f = fs.sms_features(ev)
            f["text"] = ev["text"]
        else:
            f = fs.audio_features(ev)
        f.update(event_id=ev["event_id"], label=ev["label"], fraud_type=ev["fraud_type"], actor=ev.get("actor", ""))
        rows[et].append(f)
    return {k: pd.DataFrame(v) for k, v in rows.items()}


# Tạo một window dữ liệu và tách phần warm-up ra khỏi phần cần đánh giá để tránh data leakage.
def make_window(name, seed, hours, start, kind, limit=None, warmup_hours=0.0):
    """Returns (warmup_events, events). Warm-up events only prime the feature store."""
    ev = TrafficSimulator(seed, window=kind).build(hours, start_hour=start, warmup_hours=warmup_hours)
    if name != kind:
        # M9: event IDs come from the window KIND, so "val" and "cal" (both kind val) collided ("VA…").
        prefix = name[:3].upper()
        for e in ev:
            e["event_id"] = prefix + e["event_id"][2:]
    warm = [e for e in ev if e["warmup"]]
    ev = [e for e in ev if not e["warmup"]]
    if limit:
        ev = ev[:limit]
    return warm, ev


# Khi chạy trực tiếp, sinh dữ liệu và summary để chuẩn bị dataset cho training và đánh giá.
if __name__ == "__main__":
    summary, artifacts = {}, []
    for name, (seed, hours, start, kind, warm_h) in WINDOWS.items():
        t = time.time()
        warm, ev = make_window(
            name, seed, hours, start, kind, limit=50_000 if name == "test" else None, warmup_hours=warm_h
        )
        raw = pd.DataFrame(ev)
        raw_path = DATA_DIR / (f"{name}_events.csv.gz" if name != "test" else "test_events_50k.csv")
        raw.to_csv(raw_path, index=False)
        artifacts.append(raw_path)
        if name == "test":
            warm_path = DATA_DIR / "test_warmup_24h_events.csv.gz"
            pd.DataFrame(warm).to_csv(warm_path, index=False)
            artifacts.append(warm_path)
        feats = replay(ev, warm)
        for k, df in feats.items():
            feature_path = DATA_DIR / f"{name}_{k.lower()}_features.parquet"
            df.to_parquet(feature_path, index=False)
            artifacts.append(feature_path)
        summary[name] = {
            "events": len(ev),
            "fraud_events": int(raw["label"].sum()),
            "fraud_rate_pct": round(100 * raw["label"].mean(), 4),
            "by_type": raw.groupby("event_type").size().to_dict(),
            "fraud_by_family": raw[raw.label == 1].groupby("fraud_type").size().to_dict(),
            "warmup_events": len(warm),
            "seconds": round(time.time() - t, 1),
        }
        print(name, summary[name])
    summary_path = DATA_DIR / "dataset_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    artifacts.append(summary_path)
    write_dataset_manifest(DATA_DIR, WINDOWS, artifacts)
