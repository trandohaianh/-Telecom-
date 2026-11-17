# 1. Runtime phát hiện gian lận

## Vai trò
Hệ thống nhận bản sao sự kiện CALL, SMS, AUDIO và CDR từ nhà mạng. Mỗi sự kiện được chấm điểm trong ngân sách 30 ms hoặc 170 ms. Kết quả được ghi bền vào SQLite cùng bằng chứng có chuỗi hash. Hệ thống trả về một khuyến nghị; nhà mạng tự quyết định có thực thi hay không.

## Thành phần
| Module | Trách nhiệm |
|---|---|
| `server.py` | Ứng dụng FastAPI, `LiveEngine` (dashboard, inbox, cảnh báo), các route `/api/v1/verdict` và `/api/v1/cdr` |
| `backend_runtime.py` | `validate_event`; admission (129 request đang xử lý, hàng đợi 128); owner thread; batch ≤ 16; checkpoint; phục hồi |
| `backend_state.py` | SQLite WAL, `synchronous=FULL`, giao dịch lồng, journal idempotent theo `(kind, event_id)` + SHA-256 |
| `backend_evidence.py`, `evidence.py` | Outbox bằng chứng → JSONL chuỗi SHA-256; fsync trước khi ack; ALLOW chỉ lấy mẫu 1/1000 |
| `ingest_guard.py` | Chống trùng (TTL 900 s theo thời gian sự kiện), watermark `max_ts` |
| `feature_store.py` | Đặc trưng nhân quả: cửa sổ 1 phút / 10 phút / 1 giờ / 24 giờ, đồ thị SIM–IMEI, vận tốc |
| `detector.py` | LightGBM, IsolationForest theo segment, TF-IDF/LR cho SMS, chính sách chặn, trí nhớ thu giọng |
| `resilience.py` | Breaker (5 lỗi / 1 s), luật dự phòng, hàng chấm lại 50k, cảnh báo hồi tố |
| `drift.py`, `shadow_run.py`, `ops_pipeline.py` | PSI, dải quyết định, KPI, backup, retention |
| `agent.py` | Agent chỉ đề xuất; admin duyệt mới có hành động |

## Luồng
`network_request` lần lượt: cắt body ở 64 KB → kiểm HMAC → `validate_event` → kiểm `ts`. Sau đó là `submit`, owner thread, rồi một giao dịch gồm `reserve` → `admit` → `score_batch` → `_finish`. Sau COMMIT mới giải quyết future, ghi bằng chứng và trả lời HTTP. Chi tiết ở báo cáo 02, mục 2.1.

## Bản sửa trong hệ thống này
| # | Lỗi | Sửa |
|---|---|---|
| F7 | Một `ts` sai đơn vị (ms) hoặc 1e308 đẩy watermark lên. Từ đó mọi sự kiện thật thành LATE và không được chấm, kéo dài qua cả lần khởi động lại. JSON lồng sâu trả lỗi 500. | Giới hạn `ts` ≤ 1e11 giây. Link nhà mạng từ chối mốc Unix vượt đồng hồ thật hơn 300 s. `RecursionError` trả 400. |
| F8 | Trí nhớ thu giọng chặn cả cuộc gọi thật từ trung kế ngân hàng sau khi CLI bị giả mạo. | Bỏ qua các số trong `PROTECTED_CLI`. |
| F9 | Serving không bao giờ gọi `prune_state`, nên state và checkpoint tăng không giới hạn. | Gọi `_compact_state()` trước checkpoint, với horizon = `min(max_ts, ts nhỏ nhất của hàng chấm lại)`. |
| F18 | PSI tạo khoảng 500 bin cho đặc trưng mà phần lớn giá trị bằng 0, gây báo lệch giả. | Tách riêng giá trị mode và chia phần còn lại theo quantile. |

Các bản sửa này có test trong `tests/test_audit_regressions_20261117.py`. Mỗi test thất bại trên HEAD.

## Bản sửa vòng 2
| # | Sửa |
|---|---|
| G1 | `drain_rescore` chỉ so thời gian `score_batch` với ngân sách. Trước đây thời gian sao chép và khôi phục state cũng bị tính, nên có thể làm mở breaker. |
| G2 | Owner thread chạy bảo trì (chấm lại, phát cảnh báo hồi tố) cả khi đang bận, tối đa mỗi giây một lần, mỗi lần ≤ 8 sự kiện. Trước đây việc này chỉ chạy khi hàng đợi rỗng đủ 100 ms. |
| G3 | Batch không có CALL dùng ngân sách 170 ms (`content_budget_ms`). |
| G4 | Loại phần tử khỏi deny list bằng `heapq.nsmallest`, không còn sắp xếp toàn bộ. |
| G5 | Agent không đề xuất lại các cảnh báo hồi tố đã khôi phục sau khởi động lại. |
| G6 | Thao tác tiêm sự kiện demo được tuần tự hoá; ID ngẫu nhiên. |
| G7 (D1) | Sự kiện LATE/DUPLICATE được đếm riêng: `ingest_unscored`, metric `tfs_ingest_unscored`, cảnh báo `TfsLateEventsUnscored`. |
| G16, G17 | Drift: cảnh báo khi tham chiếu BLOCK = 0 mà hiện tại > 0,1 %; segment rỗng trả `NO_ACTION`. |
| G18, G19 | Eval: FPR hồi quy theo dung sai tương đối; `smoke_passed` trở thành cổng chặn. `train.py` báo lỗi rõ ràng khi một segment không có dữ liệu. `shadow_run` dùng `RuntimeError` thay cho `assert`. |

Test: `tests/test_audit_regressions_round2.py`.

## Bản sửa vòng 3
| # | Sửa |
|---|---|
| H1 | `/metrics` không có token thì chỉ mở ở chế độ demo. Trước đây `run_live.py` mở endpoint này cho mọi người. |
| H8 (D1) | Frontier theo từng bên gọi, cộng sàn toàn cục 120 s. Hai trung kế lệch nhau vài mili-giây không còn làm sự kiện bị bỏ qua. FeatureStore chịu được lệch thứ tự có giới hạn (`in_calls` chèn đúng thứ tự và đếm nhân quả; `max` cho `last_seen`/IMEI/SIM mới). Checkpoint guard v2 vẫn đọc được v1. |
| H11 (D6) | `hour_of_day = local_hour(ts)` (UTC+7) ở FeatureStore, bộ mô phỏng và Spark. Bundle được dựng lại thành **tfd-v2.3**. |
| H12 (D7) | Tham chiếu drift là cửa sổ `ref` với tỷ lệ gian lận thực tế. PSI vận hành chỉ tính trên các quyết định CALL kể từ snapshot trước. |

Test: `tests/test_audit_regressions_round3.py`.

## Còn mở
- **D14 (bắt buộc sửa sau):** checkpoint vẫn là snapshot đầy đủ. Kế hoạch: delta theo khoá đã chạm, snapshot ở luồng nền, phục hồi = snapshot + delta + journal (báo cáo 01 §3c).
- **D2:** bộ ghi bằng chứng lỗi thì dừng suy luận. Giữ fail-closed có chủ đích.

## Đã kiểm và thấy đúng
- Giao dịch lồng nhau và cơ chế rollback-only.
- Idempotency và xung đột CDR so với setup.
- Outbox không mất và không trùng bản ghi khi crash.
- Phục hồi theo journal có ghim model identity.
- Cửa sổ đóng ở cả hai đầu; không có phép chia cho 0.
- Thứ tự đặc trưng khớp giữa train và serve.
- Ngưỡng FPR đúng trong trường hợp có điểm bằng nhau.
- `fast_iforest` khớp với sklearn.
- Luật dự phòng không bao giờ chặn số được bảo vệ.

## Kiểm thử
- `tests/`: 442 passed (mốc 426).
- Eval: chất lượng giống hệt HEAD.
- SLA HTTP 30 ms không đạt trên cả hai bản. Nguyên nhân là môi trường và hiệu năng đã biết.
