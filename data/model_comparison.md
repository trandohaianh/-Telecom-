# So sánh thuật toán / Model comparison

Huấn luyện: cửa sổ train · Ngưỡng: validation · Đánh giá: 11 cửa sổ test × 50.000 sự kiện (dữ liệu giả lập). Độ trễ = chấm **1 sự kiện**, đã gồm tiền xử lý, CPU 8 luồng.

## Tầng 1 – Cuộc gọi (SIM box, giả mạo số)

Ngân sách độ trễ 30 ms.

| Mô hình | PR-AUC | Bắt khi CHẶN | Bắt khi CHẶN+CỜ | Chặn nhầm (người thật) | Cờ nhầm | Độ trễ p99 | Train |
|---|---|---|---|---|---|---|---|
| LightGBM (đang dùng) | 0.9737 | 84.0% (284/338) | 99.1% | 11 (0.0030%) | 77 | 0.578 ms | 4.0 s |
| XGBoost | 0.9358 | 64.5% (218/338) | 95.9% | 9 (0.0020%) | 107 | 1.289 ms | 9.5 s |
| Random Forest | 0.9904 | 95.6% (323/338) | 100.0% | 23 (0.0060%) | 175 | 65.232 ms | 30.6 s |
| Logistic Regression | 0.9784 | 53.5% (181/338) | 96.7% | 0 (0.0000%) | 81 | 1.099 ms | 1.2 s |
| MLP (neural net) | 0.8682 | 53.3% (180/338) | 71.6% | 0 (0.0000%) | 49 | 1.048 ms | 27.7 s |

## Zero-day – Wangiri (không có trong dữ liệu huấn luyện)

| Mô hình | Bắt được | Người thật bị gắn cờ | ROC-AUC | Độ trễ p99 |
|---|---|---|---|---|
| Isolation Forest (đang dùng) | 90.8% (108/119) | 0.040% | 0.9962 | 23.427 ms |
| Local Outlier Factor | 42.9% (51/119) | 0.030% | 0.9952 | 3.606 ms |
| One-Class SVM (Nyström) | 79.0% (94/119) | 0.011% | 0.8032 | 2.64 ms |
| Autoencoder | 16.8% (20/119) | 0.047% | 0.9968 | 1.05 ms |
| Chỉ LightGBM (không có mô hình bất thường) | 0.0% (0/119) | 0.020% | – | – ms |

Độ trễ Isolation Forest ở đây là bản sklearn; lúc chạy thật project dùng bản Numba (`fast_iforest.py`), nằm trong p99 0,45 ms của cả tầng 1 (`benchmark_results.json`).

## Tầng 2 – SMS lừa đảo

Ngân sách độ trễ 170 ms.

| Mô hình | PR-AUC | Bắt khi CHẶN | Bắt khi CHẶN+CỜ | Chặn nhầm (người thật) | Cờ nhầm | Độ trễ p99 | Train |
|---|---|---|---|---|---|---|---|
| TF-IDF + LogReg (đang dùng) | 1.0000 | 100.0% (60/60) | 100.0% | 10 (0.0060%) | 230 | 4.182 ms | 13.4 s |
| TF-IDF + Linear SVM | 1.0000 | 100.0% (60/60) | 100.0% | 0 (0.0000%) | 214 | 4.277 ms | 47.5 s |
| TF-IDF + Naive Bayes | 1.0000 | 100.0% (60/60) | 100.0% | 0 (0.0000%) | 181 | 4.741 ms | 13.1 s |
| TF-IDF + LightGBM | 1.0000 | 100.0% (60/60) | 100.0% | 20 (0.0130%) | 375 | 163.613 ms | 21.5 s |

## Tầng 2 – Giọng nói deepfake

Ngân sách độ trễ 170 ms. Ít mẫu, khoảng tin cậy rộng.

| Mô hình | PR-AUC | Bắt khi CHẶN | Bắt khi CHẶN+CỜ | Chặn nhầm (người thật) | Cờ nhầm | Độ trễ p99 | Train |
|---|---|---|---|---|---|---|---|
| LightGBM (đang dùng) | 0.9043 | 75.8% (50/66) | 93.9% | 4 (0.0410%) | 48 | 0.965 ms | 0.3 s |
| XGBoost | 0.8988 | 72.7% (48/66) | 93.9% | 4 (0.0410%) | 42 | 1.25 ms | 0.5 s |
| Random Forest | 0.8897 | 69.7% (46/66) | 92.4% | 10 (0.1020%) | 27 | 85.093 ms | 2.4 s |
| Logistic Regression | 0.8986 | 77.3% (51/66) | 86.4% | 7 (0.0710%) | 31 | 1.442 ms | 0.0 s |
| MLP (neural net) | 0.8694 | 75.8% (50/66) | 83.3% | 12 (0.1220%) | 34 | 1.389 ms | 1.3 s |

Khoảng tin cậy 95 % (Wilson) và mọi số liệu chi tiết: `data/model_comparison.json`.