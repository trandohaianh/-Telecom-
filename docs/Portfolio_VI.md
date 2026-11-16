# Dự án 1 · Telecom Fraud Shield
**Hệ thống AI phát hiện & ngăn chặn lừa đảo viễn thông thời gian thực** · Trần Đỗ Hải Anh · 10/2026

> Cập nhật 06/11/2026: bảng v2.1 phía dưới là dữ liệu mô phỏng lịch sử, không phải kết quả mới nhất. HTTP p99 của v2.2 chưa đạt SLA 30 ms. Xem [technical defense](TECHNICAL_DEFENSE_VI.md), [báo cáo SLA](SLA_TARGET_20261025.md) và [validation v2.2](VALIDATION_V22.md).

- Video demo (quét QR trong slide/portfolio): https://claude.ai/artifact/1YaojRoGZTyfxCVck7BCy4
- Portfolio web: https://claude.ai/artifact/WpNjtemXE1yq1WxoCxYhG7

## Vấn đề
Mục tiêu sản phẩm là đưa ra khuyến nghị trước khi đổ chuông trong < 30 ms; benchmark HTTP đầu-cuối hiện chưa đạt. Gian lận giả lập chỉ ~0,1% lưu lượng nên phải kiểm soát chặn nhầm nghiêm ngặt; kẻ gian liên tục đổi chiêu (zero-day).

## Giải pháp
- Kho đặc trưng streaming tính nhân quả (cửa sổ 1 phút–24 giờ, đồ thị IMEI–SIM, di chuyển)
- CALL: LightGBM + Isolation Forest theo nhóm; SMS: TF-IDF + Logistic Regression; audio thật chưa được chứng nhận trên corpus tiếng Việt có nhãn và mặc định OBSERVE/NONE
- Ngưỡng BLOCK/FLAG chọn theo ngân sách FPR trên legitimate calibration samples; reason codes từ model contributions, có evidence gate trước hard block
- Fail-open + circuit breaker; nhật ký bằng chứng chuỗi SHA-256; dashboard song ngữ

## Kết quả v2.1 lịch sử (dữ liệu mô phỏng, không dùng làm metric hiện tại)
| Chỉ tiêu | Kết quả |
|---|---|
| Chặn nhầm trên 500.000 sự kiện | 0.0012 % (mục tiêu < 0.01 %) |
| Gian lận bị chặn hoặc gắn cờ | 85.2 % |
| Zero-day chưa từng huấn luyện bị gắn cờ | 88.8 % |
| Độ trễ p99 Tầng 1 | 0.45 ms |
| Thông lượng trong SLA | ~25,000 sự kiện/giây trên 2 vCPU (chưa đạt 50.000 TPS) |

**Giới hạn:** số liệu v2.1 bên dưới là mô phỏng; audio thật chưa có corpus WAV tiếng Việt đủ nhãn và đường real-audio mặc định OBSERVE/NONE. SIM Box khó phát hiện và throughput cũ không phải cam kết hiện tại; xem báo cáo v2.2 mới nhất.

**Stack:** Python · FastAPI · LightGBM · scikit-learn · Isolation Forest · Numba · pandas · pytest · Playwright
