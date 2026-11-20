"""Read event CSVs without pandas mangling identifiers (MSISDN/IMEI must stay strings)."""

import math

import pandas as pd

# Các cột định danh phải giữ dạng string để pandas không ép số MSISDN/IMEI thành kiểu số.
STR_COLS = [
    "call_id",
    "event_id",
    "event_type",
    "caller",
    "callee",
    "cell_id",
    "imei",
    "origin_trunk",
    "network",
    "sender",
    "recipient",
    "sms_route",
    "text",
    "codec",
    "transcript",
    "fraud_type",
    "actor",
]


# Đọc CSV dạng nhiều event và trả dạng dict rời rạc, bỏ các trường không có dữ liệu của event khác.
def load_events(path):
    df = pd.read_csv(path, dtype={c: str for c in STR_COLS}, keep_default_na=False)
    rows = df.to_dict("records")
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if v == "" and k not in STR_COLS:
                continue  # column belongs to another event type
            if isinstance(v, float) and math.isnan(v):
                continue
            clean[k] = v if k in STR_COLS else float(v)
        out.append(clean)
    return out
