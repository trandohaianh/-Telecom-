# Bộ eval Telecom Fraud Shield

```bash
python evals/run_evals.py                  # chất lượng mô hình + SLA phục vụ thật qua HTTP (≈1 phút)
python evals/run_evals.py --skip-serving   # chỉ chất lượng (≈15 giây)
python evals/run_evals.py --update-baseline
```

**1. Chất lượng.** Cửa sổ test độc lập gồm 50.000 sự kiện, warm-up 24 giờ, chấm bằng đúng bundle đang
phục vụ (`runtime/artifacts_v22`).
- Recall, precision và FPR của BLOCK và của BLOCK ∪ FLAG, kèm khoảng tin cậy Wilson 95%.
- Tỉ lệ phát hiện theo từng họ lừa đảo.
- Zero-day: Wangiri là kiểu chưa có khi huấn luyện.
- Recall theo phiên cuộc gọi trước khi đổ chuông.

**2. Phục vụ.** Chạy tiến trình uvicorn thật, gửi yêu cầu có ký HMAC giống tổng đài, ở 50 và 100 EPS.
- Phần trăm quyết định chấm xong trong 30 ms; p50, p95, p99.
- Số lần fail-open; khuyến nghị enforce xuất hiện ngoài ý muốn.
- Chuỗi bằng chứng còn toàn vẹn sau khi khởi động lại.

**Cổng chặn** (`thresholds.json`):
- FPR chặn cứng: cận trên của khoảng tin cậy 95% phải ≤ 0,01%.
- Recall chặn ≥ 45%, recall chặn hoặc gắn cờ ≥ 80%.
- Mọi họ lừa đảo được phát hiện ≥ 75%.
- p99 ≤ 30 ms, không fail-open, không enforce ngoài ý muốn.

Ngoài ra không chỉ số nào được tụt quá 3 điểm phần trăm so với `baselines/*.json`. Kết quả ghi vào
`reports/evals/` (JSON, Markdown, `history.jsonl`).

## Kết quả hiện tại (16/11/2026, Linux 2 CPU)

- BLOCK: recall 54,6%, precision 100%, FPR 0% (cận trên CI 95%: 0,0077%).
- BLOCK ∪ FLAG: recall 86,4%, FPR 0,11%.
- Họ phát hiện kém nhất 80% (deepfake). Zero-day Wangiri 91,7%.
- Phục vụ ở 50–100 EPS: 99,5% quyết định xong trong 30 ms, p99 12–19 ms, không fail-open.

**Chưa đạt mục tiêu ban đầu:**
- Một tiến trình chỉ phục vụ khoảng 300 EPS. Ở 300 EPS, p99 là 66 ms và có fail-open; ở 600 EPS trở
  lên thì bắt đầu loại bỏ yêu cầu. Mục tiêu 50.000 TPS cần chia phân vùng ra nhiều máy và một feature
  store dùng chung.
- Mọi số liệu trên dựa trên dữ liệu mô phỏng; chưa có lưu lượng thật có nhãn.
