"""Central configuration for the Telecom Fraud Detection MVP (v2)."""

import os
from pathlib import Path

# ROOT là thư mục gốc của dự án v2, dùng làm cơ sở tính các đường dẫn data/models/logs/static.
ROOT = Path(__file__).resolve().parents[1]
# DATA_DIR lưu dữ liệu đầu vào, validation và các feed tạm trong pipeline.
DATA_DIR = Path(os.environ.get("FRAUD_DATA_DIR", ROOT / "data")).resolve()
# MODEL_DIR chứa mô hình tier 1 / tier 2 / zero-day và thresholds đã huấn luyện.
MODEL_DIR = Path(os.environ.get("FRAUD_MODEL_DIR", ROOT / "models")).resolve()
# LOG_DIR lưu evidence log và nhật ký chạy cho audit và monitoring.
LOG_DIR = Path(os.environ.get("FRAUD_LOG_DIR", ROOT / "logs")).resolve()
# STATIC_DIR lưu tài nguyên tĩnh cho dashboard / UI nội bộ.
STATIC_DIR = ROOT / "static"
for _d in (DATA_DIR, MODEL_DIR, LOG_DIR, STATIC_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---- SLA budgets (from the brief) ----
# SLA_TOTAL_MS là tổng ngân sách latency cho một quyết định từ nhận event đến action.
SLA_TOTAL_MS = 200.0  # whole decision path
# SLA_TIER1_MS là ngân sách pre-call tier 1, nơi phản hồi phải rất nhanh.
# Event time is Unix seconds (UTC). Hour-of-day features and the simulator's daily profile both use
# local Vietnam time (UTC+7) through local_hour(), so training data and live traffic agree (D6).
LOCAL_UTC_OFFSET_S = 7 * 3600


def local_hour(ts: float) -> float:
    return ((ts + LOCAL_UTC_OFFSET_S) / 3600.0) % 24


SLA_TIER1_MS = 30.0  # pre-call signalling decision
# SLA_TIER2_MS là ngân sách cho audio / content tier 2, chấm điểm kỹ hơn.
SLA_TIER2_MS = 170.0  # in-call audio / SMS content decision
# SLA_FEATURE_STORE_MS là thời gian tối đa để truy vấn và cập nhật feature store.
SLA_FEATURE_STORE_MS = 10.0  # feature lookup
# TARGET_TPS là mục tiêu throughput hàng chục nghìn event mỗi giây.
TARGET_TPS = 50_000

# ---- Safety targets ----
# TARGET_BLOCK_FPR là mục tiêu FPR cho chặn cứng, phải cực thấp để tránh phạt sai người dùng.
TARGET_BLOCK_FPR = 0.0001  # < 0.01 % of legitimate events may be hard-blocked
# TARGET_FLAG_FPR là mục tiêu FPR cho flag / soft action để cảnh báo nhưng không chặn ngay.
TARGET_FLAG_FPR = 0.005  # soft actions (warning / analyst queue) may touch <= 0.5 %

# ---- Circuit breaker ----
# CB_FAILURE_THRESHOLD là số lỗi hoặc timeout liên tiếp trước khi breaker mở.
CB_FAILURE_THRESHOLD = 5  # consecutive failures/timeouts that open the breaker
# CB_COOLDOWN_S là thời gian breaker giữ OPEN trước khi thử half-open một lần.
CB_COOLDOWN_S = 1.0  # how long the breaker stays open before a half-open trial

# MODEL_VERSION là phiên bản mô hình gắn vào mọi kết quả và evidence để trace.
MODEL_VERSION = os.environ.get("FRAUD_MODEL_VERSION", "tfd-v2.1")
