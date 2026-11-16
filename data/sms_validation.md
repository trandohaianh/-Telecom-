# Xác thực lựa chọn mô hình SMS / SMS model validation

6 lần huấn luyện (seed train/val khác nhau) · test: 157,494 SMS thật + 60 SMS lừa (11 cửa sổ) · stress: 14,400 tin lừa biến đổi mỗi lần · dữ liệu giả lập.

## Bước 2 – Chặn nhầm tin nhắn thật qua các seed

| Mô hình | seed 11/22 | seed 301/401 | seed 302/402 | seed 303/403 | seed 304/404 | seed 305/405 | TB chặn nhầm | TB cờ nhầm |
|---|---|---|---|---|---|---|---|---|
| LogReg (đang dùng) | 10 | 0 | 0 | 0 | 0 | 0 | 1.67 | 90.7 |
| Linear SVM | 0 | 0 | 0 | 0 | 0 | 0 | 0.0 | 184.8 |
| Naive Bayes | 0 | 0 | 0 | 0 | 0 | 0 | 0.0 | 242.8 |

## Bước 1 – McNemar (so với LogReg, cùng tin nhắn)

| Seed | Ứng viên | Chặn nhầm chỉ LogReg / chỉ ứng viên | p | Tin lừa (stress) chỉ LogReg bắt / chỉ ứng viên bắt | p |
|---|---|---|---|---|---|
| 11/22 | Linear SVM | 10 / 0 | 0.00195 | 0 / 0 | 1.0 |
| 11/22 | Naive Bayes | 10 / 0 | 0.00195 | 13 / 0 | 0.00024 |
| 301/401 | Linear SVM | 0 / 0 | 1.0 | 0 / 0 | 1.0 |
| 301/401 | Naive Bayes | 0 / 0 | 1.0 | 11 / 0 | 0.00098 |
| 302/402 | Linear SVM | 0 / 0 | 1.0 | 0 / 0 | 1.0 |
| 302/402 | Naive Bayes | 0 / 0 | 1.0 | 13 / 0 | 0.00024 |
| 303/403 | Linear SVM | 0 / 0 | 1.0 | 0 / 0 | 1.0 |
| 303/403 | Naive Bayes | 0 / 0 | 1.0 | 13 / 0 | 0.00024 |
| 304/404 | Linear SVM | 0 / 0 | 1.0 | 0 / 0 | 1.0 |
| 304/404 | Naive Bayes | 0 / 0 | 1.0 | 13 / 0 | 0.00024 |
| 305/405 | Linear SVM | 0 / 0 | 1.0 | 0 / 0 | 1.0 |
| 305/405 | Naive Bayes | 0 / 0 | 1.0 | 14 / 0 | 0.00012 |

p < 0,05: khác biệt có ý nghĩa thống kê trong lần chạy đó.

## Bước 3 – Stress test: % tin lừa bị CHẶN hoặc GẮN CỜ (trung bình các seed, [thấp nhất])

### A. Kênh gốc của kẻ lừa

| Biến đổi | LogReg (đang dùng) | Linear SVM | Naive Bayes |
|---|---|---|---|
| Gốc (không đổi) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Tách chữ (t.a.i k-h-o-a-n, ký tự ẩn) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Leetspeak mới (4, 7, 9, 8) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Teencode / viết tắt | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Diễn đạt lại (từ đồng nghĩa) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Chèn emoji + khoảng trắng | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Kết hợp (tách + leet + đồng nghĩa) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Mẫu lừa hoàn toàn mới (12 kịch bản) | 100.0% [100] | 100.0% [100] | 100.0% [100] |

### B. Gửi P2P từ SIM thường

| Biến đổi | LogReg (đang dùng) | Linear SVM | Naive Bayes |
|---|---|---|---|
| Gốc (không đổi) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Tách chữ (t.a.i k-h-o-a-n, ký tự ẩn) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Leetspeak mới (4, 7, 9, 8) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Teencode / viết tắt | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Diễn đạt lại (từ đồng nghĩa) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Chèn emoji + khoảng trắng | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Kết hợp (tách + leet + đồng nghĩa) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Mẫu lừa hoàn toàn mới (12 kịch bản) | 100.0% [100] | 100.0% [100] | 100.0% [100] |

### C. P2P + link trên domain hợp lệ (khó nhất)

| Biến đổi | LogReg (đang dùng) | Linear SVM | Naive Bayes |
|---|---|---|---|
| Gốc (không đổi) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Tách chữ (t.a.i k-h-o-a-n, ký tự ẩn) | 100.0% [100] | 100.0% [100] | 99.8% [100] |
| Leetspeak mới (4, 7, 9, 8) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Teencode / viết tắt | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Diễn đạt lại (từ đồng nghĩa) | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Chèn emoji + khoảng trắng | 100.0% [100] | 100.0% [100] | 100.0% [100] |
| Kết hợp (tách + leet + đồng nghĩa) | 100.0% [100] | 100.0% [100] | 98.0% [98] |
| Mẫu lừa hoàn toàn mới (12 kịch bản) | 100.0% [100] | 100.0% [100] | 100.0% [100] |

| Tổng hợp stress | LogReg (đang dùng) | Linear SVM | Naive Bayes |
|---|---|---|---|
| TB % bắt (chặn+cờ) | 100.0% | 100.0% | 99.91% |
| TB % CHẶN | 99.75% | 100.0% | 99.26% |

### Tin nhắn THẬT bị biến đổi – số tin bị chặn nhầm (trung bình mỗi seed)

| Biến đổi | LogReg (đang dùng) | Linear SVM | Naive Bayes |
|---|---|---|---|
| Teencode / viết tắt | 0.17 | 2092.0 | 0.0 |
| Leetspeak mới (4, 7, 9, 8) | 9.67 | 8272.83 | 94.0 |
| Chèn emoji + khoảng trắng | 0.17 | 0.0 | 0.0 |

## Kết luận theo quy tắc

Đổi chỉ khi ứng viên thắng/hòa ở bước 2 **và** bước 3, **và** không đẩy thêm chi phí sang người dùng thật (cờ nhầm ≤ 1,5× LogReg; chặn nhầm tin thật viết teencode/leetspeak/emoji ≤ 2× LogReg + 5).

| Ứng viên | Bước 2 (chặn nhầm) | Bước 3 (chênh % bắt so với LogReg) | Chi phí cho người dùng thật | Nên đổi? |
|---|---|---|---|---|
| Linear SVM | thắng (tốt hơn 1/6, bằng 5) | hòa (+0.00 điểm; CHẶN +0.25) | thua (cờ nhầm ×2.04; chặn nhầm tin biến đổi 10364.8 vs 10.0) | **KHÔNG** |
| Naive Bayes | thắng (tốt hơn 1/6, bằng 5) | hòa (-0.09 điểm; CHẶN -0.49) | thua (cờ nhầm ×2.68; chặn nhầm tin biến đổi 94.0 vs 10.0) | **KHÔNG** |

Ngưỡng: lượng tử thuần trên validation (không áp FLOORS của train.py), giống compare_models.py.