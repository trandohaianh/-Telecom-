# Triển khai Telecom Fraud Shield (miễn phí, chung VM với FieldOps)

Image chạy `serve.py`. Bản công khai **phát lại lưu lượng mạng mô phỏng** (`FRAUD_DEMO=1`) vì chưa có
lưu lượng nhà mạng thật. Mọi người dùng đều đi qua tài khoản thật; chế độ đăng nhập demo ẩn danh đã tắt.

| Địa chỉ | Dành cho |
|---|---|
| `https://<TELECOM_DOMAIN>/app/` | Thuê bao: đăng ký email/Google → nhận một số giả lập riêng → xem cảnh báo, thử kịch bản lừa đảo, báo cáo số, bảo vệ người thân |
| `https://<TELECOM_DOMAIN>/admin/` | Nhân viên vận hành: giám sát luồng, bằng chứng, AI agent (đăng nhập tên + mật khẩu, hoặc Google nếu email đã được gắn) |
| `https://<TELECOM_DOMAIN>/admin/manage` | Quản trị: duyệt số bị báo cáo, tài khoản nhân viên, chi phí, pipeline/KPI |

## Cài lên VM (≈15 phút)

Yêu cầu: VM đã cài FieldOps theo `deploy/cloud/README.md` của repo FieldOps. Telecom dùng chung
Docker, mạng `edge` và Caddy HTTPS. Nếu chưa có FieldOps, script tự chạy một Caddy riêng.

1. **DuckDNS**: tạo thêm 1 subdomain (VD `laichan-demo`) trỏ về IP của VM.
2. **Google Sign-In** (tuỳ chọn): trong OAuth client đang dùng cho FieldOps, thêm
   `https://laichan-demo.duckdns.org` vào *Authorized JavaScript origins*.
3. Trên VM:
   ```bash
   git clone <repo-telecom> ~/telecom
   bash ~/telecom/deploy/cloud/setup.sh      # lần 1: tạo .env (tự sinh METRICS_TOKEN, mật khẩu admin)
   nano ~/telecom/deploy/cloud/.env          # điền TELECOM_DOMAIN, GOOGLE_CLIENT_ID, FRAUD_ADMIN_EMAIL
   bash ~/telecom/deploy/cloud/setup.sh      # lần 2: build, chạy, nối vào Caddy, smoke test
   ```
4. Đăng nhập `/admin/` bằng `viettel_admin` và `FRAUD_ADMIN_PASSWORD` trong `.env`. Sau đó vào
   **Quản trị → Tài khoản nhân viên** để đổi mật khẩu và tạo tài khoản cho người khác.

Trạng thái (CSDL tài khoản, khoá ký nhật ký, nhật ký audit, bằng chứng, backup) nằm trong volume
`telecom_state`. Pipeline tự chạy: KPI mỗi giờ; backup và dọn dẹp lúc `FRAUD_PIPELINE_DAILY_AT` (UTC).

## Monitoring (Grafana Cloud — dùng chung stack với FieldOps)

- **Connections → Metrics Endpoint**: URL `https://<TELECOM_DOMAIN>/metrics`, xác thực Bearer bằng
  `METRICS_TOKEN`.
- **Import dashboard**: `monitoring/grafana/telecom-production.json`.
- **Cảnh báo**: `monitoring/alerts/telecom-rules.yml` có 12 rule, gồm fail-open, p99 > 30 ms, breaker
  mở, chế độ enforce bị bật, drift, backup trễ, báo cáo tồn đọng, dò mật khẩu, chi phí SMS tăng đột biến.

## Chế độ thật (khi có lưu lượng nhà mạng)

Đặt `FRAUD_DEMO=0`. Thuê bao khi đó phải xác minh số bằng OTP SMS: `FRAUD_OTP_PROVIDER=twilio` cùng
`TWILIO_*`. Mỗi SMS được ghi vào bảng chi phí với giá `SMS_COST_USD`. Chuyển `enforce` vẫn bị khoá
cho tới khi đặt `FRAUD_REAL_TRAFFIC_VALIDATED=1`, tức là sau khi đã kiểm chứng trên lưu lượng thật có nhãn.

## Cập nhật

GitHub Actions `deploy.yml` (sau khi CI xanh trên main) SSH vào VM và chạy `deploy/cloud/deploy.sh`:
backup → kéo code → build lại → smoke test. Có thể chạy tay script này.
