"""Chạy Telecom Fraud Shield khi triển khai công khai (container / VPS).

Khác run_demo.py: không in token/mật khẩu ra log, bắt buộc /metrics có token, tắt đăng nhập demo
không cần tài khoản, lắng nghe sau reverse proxy (Caddy) với proxy headers.

Biến môi trường chính:
  FRAUD_DEMO=1            phát lại luồng lưu lượng MÔ PHỎNG (mặc định; chưa có lưu lượng nhà mạng thật)
  FRAUD_ADMIN_PASSWORD    mật khẩu ban đầu cho tài khoản admin (đặt một lần)
  GOOGLE_CLIENT_ID        bật đăng nhập Google cho thuê bao và nhân viên (email đã gắn)
  METRICS_TOKEN           Bearer token cho Grafana Cloud đọc /metrics
  FRAUD_OTP_PROVIDER      twilio | console  (chế độ thật: xác minh số bằng OTP SMS)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    artifact = Path(os.environ.get("FRAUD_ARTIFACT_DIR", root / "runtime" / "artifacts_v22"))
    if (artifact / "models" / "manifest.json").is_file():
        os.environ.setdefault("FRAUD_DATA_DIR", str(artifact / "data"))
        os.environ.setdefault("FRAUD_MODEL_DIR", str(artifact / "models"))
    os.environ.setdefault("FRAUD_DEMO", "1")
    os.environ.setdefault("FRAUD_PUBLIC", "1")
    os.environ.setdefault("FRAUD_LEGACY_DEMO_LOGIN", "0")
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ.setdefault(name, "1")
    if os.environ.get("FRAUD_PUBLIC") == "1" and not os.environ.get("METRICS_TOKEN"):
        print("Cảnh báo: chưa đặt METRICS_TOKEN — /metrics sẽ trả 401 cho mọi người.", file=sys.stderr)
    sys.path.insert(0, str(root / "src"))
    from security import network_key

    network_key()  # tạo khoá HMAC liên kết nhà mạng nếu chưa có (không in ra)
    from server import app  # noqa: F401  (nạp thư viện số trước khi chạy loop)
    import uvicorn

    uvicorn.run("server:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")),
                log_level=os.environ.get("LOG_LEVEL", "warning"), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
                loop="precise_event_loop:uvicorn_loop_factory")


if __name__ == "__main__":
    main()
