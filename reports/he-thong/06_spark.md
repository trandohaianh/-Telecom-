# 6. Spark/Kafka: đặc trưng CALL phân tán (`spark/`)

## Vai trò
Thư mục này tính lại các đặc trưng CALL của FeatureStore theo hai cách: batch và stream có trạng thái. Có hai engine:
- **local:** `stateful_stream.py`;
- **phân tán:** `distributed_stream.py` và `distributed_features.py`.

Nguồn là file hoặc Kafka (`produce_to_kafka.py`, `provision_topic.py`). Hợp đồng sự kiện nằm ở `event_contract.py`. Để kiểm tương đương, `compare_features.py` và `verify_*.py` so kết quả với `src/feature_store.py`.

## Thuật toán
- **Watermark** = `max(prev, max_ts − L)`, và bằng `max_ts` khi flush.
- **Phát ra** các sự kiện có ts ≤ watermark, theo thứ tự `(ts, event_id)`.
- **Late:** ts < watermark trước đó, hoặc khoá `(ts, id)` ≤ khoá cuối đã xử lý.
- **Dedup** giữ các khoá có ts ≥ `max_ts − H`, với H ≥ max(1, L).
- **Bản batch phân tán:**
  - sắp xếp theo `(entity, ts, event_id, role)`;
  - fan-out cuộc gọi đến để dựng `incoming_24h` và `first_seen`;
  - `prune_state` chỉ chạy tới watermark.
- **Phát lại idempotent:**
  - engine local dùng receipt kèm fingerprint đầu vào;
  - engine phân tán so sánh bằng `exceptAll`.

## Bản sửa
Không có. Không phát hiện lỗi nào vừa có kịch bản hỏng vừa đủ an toàn để sửa trong phạm vi này.

## Bản sửa vòng 2
**G22:**
- Hai engine fsync thư mục cha sau `rename`.
- `source.json` của engine phân tán được ghi nguyên tử.
- Compose:
  - Kafka 29092 và cổng 4040 chỉ bind vào 127.0.0.1;
  - `secrets/`, `telecom_grpc/secrets` và `telecom_grpc/tls` bị che bằng tmpfs trong container `spark`.

**Đã thử rồi hoàn tác.** Hai thay đổi bị bỏ vì test hiện có khẳng định hành vi cũ:
- giới hạn `ts` ≤ 1e11: `test_stream_contracts.py:34` chấp nhận `ts=1e308`;
- xử lý bản trùng trong batch giống nhau giữa hai engine: `test_stateful_stream.py:122-132`.

## Bản sửa vòng 3
| # | Sửa |
|---|---|
| H6 | `imei_entity()` cho mỗi dòng không có IMEI một khoá riêng, không còn partition nóng. |
| H9 (D12) | `validate_call` từ chối `ts` ngoài [0, 1e11] giây. `test_stream_contracts.py:34` được đổi theo quyết định của chủ dự án. |
| H10 (D13) | Engine local cách ly mọi bản trùng xung đột trong cùng batch, giống engine phân tán. `test_stateful_stream.py` được đổi (6 bản bị cách ly, 0 bản trùng). |
| H11 (D6) | `hour_of_day` của Spark dùng UTC+7, giống `config.local_hour`. |

## Còn mở
- Spark chỉ xử lý CALL, không có SMS và warm-up. Đây là hợp đồng hiện tại; mở rộng là tính năng mới.
- `flush` của hai engine khác nhau khi bộ đệm rỗng.
- Engine local deep-copy store ở mỗi batch. Việc này chỉ ảnh hưởng hiệu năng.

## Kiểm thử
23 passed và 65 subtests, cả trước và sau. Không chạy được Spark cluster hay Kafka: máy không có Java và không có Docker daemon.
