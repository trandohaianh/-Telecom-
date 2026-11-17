# Báo cáo theo hệ thống con

Ngày: 17/11/2026.

Thư mục `v2` gồm bảy hệ thống. Chúng có entry point, môi trường phụ thuộc và vòng đời riêng.
- Các bản sửa (F1–F18 ở vòng 1, G1–G27 ở vòng 2, H1–H12 ở vòng 3) và các quyết định cho chủ dự án (D1–D14) dùng cùng mã số với [../01_Technical_Code_Audit_Report.md](../01_Technical_Code_Audit_Report.md).
- Vòng 4 (đánh giá đa vai trò, R4-*): [../04_Danh_gia_da_vai_tro_va_thuat_toan.md](../04_Danh_gia_da_vai_tro_va_thuat_toan.md).
- Công thức chi tiết nằm trong [../02_Deep_Technical_Explanation_Report.md](../02_Deep_Technical_Explanation_Report.md).

| # | Báo cáo | Mã nguồn | Bản sửa | Test (trước → sau) |
|---|---|---|---|---|
| 1 | [Runtime phát hiện gian lận](01_runtime_phat_hien.md) | `src/server.py`, `backend_*`, `feature_store`, `ingest_guard`, `detector`, `resilience`, `drift` | F7–F9, F18; G1–G7, G16–G19; H1, H8, H11, H12 | `tests/`: 426 → 442 → 464 → 476 (bộ chung) |
| 2 | [Tài khoản, xác thực, giao diện](02_tai_khoan_xac_thuc.md) | `src/security.py`, `accounts.py`, `portal.py`, `appdb.py`, `static/` | F1–F6, F11; G8–G14; H2 | (trong bộ `tests/`) |
| 3 | [Pipeline giọng nói](03_giong_noi.md) | `src/voice_api.py`, `voice_pipeline.py`, `voice_model.py`, `train_voice.py` | F10; G15 | (trong bộ `tests/`) |
| 4 | [RAM online (Bloom + Redis)](04_ram_online.md) | `ram_online/` | F12; G20, G21 | 17/18 → 19/20 → 22/23 trên fakeredis (môi trường) |
| 5 | [gRPC edge](05_telecom_grpc.md) | `telecom_grpc/` | F13; G23 | 2 → 2 |
| 6 | [Spark/Kafka features](06_spark.md) | `spark/` | G22; H6, H9, H10 | 23 → 23 → 24 |
| 7 | [EduDSS](07_edu_dss.md) | `edu_dss/` | F16; G24, G25; H7 | 9 → 11 → 15 → 16 |
| 8 | [Triển khai, giám sát, Android](08_trien_khai_giam_sat.md) | `Dockerfile`, `docker/`, `deploy/`, `.github/`, `monitoring/`, `android/` | F13–F15, F17; G26, G27; H3–H5 | kiểm tĩnh |

Quan hệ giữa các hệ thống:
- **Gắn trực tiếp vào API chính:** 1, 2 và 3. Pipeline giọng nói được mount trong cùng ứng dụng FastAPI.
- **Chưa nối với API chính:** 4, 5 và 6 là các nguyên mẫu online/phân tán.
- **Ứng dụng độc lập:** 7 (EduDSS).
- **Client:** Android chỉ bọc PWA `/app/` trong một WebView.
