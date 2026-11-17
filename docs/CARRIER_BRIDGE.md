# Cầu nối dữ liệu nhà mạng (`src/carrier_bridge.py`)

## 1. Mục đích

Quyết định 17/11/2026: **API chính là nguồn quyết định duy nhất.** Mọi quyết định chống lừa đảo đi qua
FastAPI chính (`src/server.py`):

| Sự kiện | Endpoint | Trả lời thành công |
|---|---|---|
| Thiết lập cuộc gọi, SMS, âm thanh | `POST /api/v1/verdict` | 200 |
| Cuộc gọi đã kết thúc (CDR) | `POST /api/v1/cdr` | 200 (đã ghi bền) hoặc 202 (đã nhận, đang xử lý) |

`ram_online/`, `telecom_grpc/` và `spark/` vẫn nằm trong repo cùng bộ test nhưng là **thử nghiệm, không triển khai**.

`carrier_bridge.py` là bộ chuyển đổi đọc luồng sự kiện của nhà mạng (file NDJSON, stdin hoặc Kafka), đổi tên
trường theo file ánh xạ, ký HMAC từng yêu cầu rồi gửi vào API chính. Bộ cầu nối không tự quyết định gì: nó chỉ
chuyển dữ liệu và ghi lại những bản ghi không gửi được.

## 2. Hợp đồng dữ liệu

Mỗi dòng (hoặc mỗi message Kafka) là một đối tượng JSON, theo một trong hai dạng:

```json
{"kind": "VERDICT", "event": {"event_type": "CALL", "event_id": "...", "ts": 1794873600.0, "...": "..."}}
{"event_type": "CALL", "event_id": "...", "ts": 1794873600.0, "duration_s": 42}
```

- Dạng thứ hai (sự kiện trần): `kind` được suy ra **sau khi ánh xạ**: có `duration_s` → `CDR`, không có → `VERDICT`.
- Trường bắt buộc do API kiểm tra (`src/backend_runtime.py`, `REQUIRED`):
  - `CALL`: `event_id, ts, caller, callee, cell_id, imei, origin_trunk, network`
  - `SMS`: `event_id, ts, sender, recipient, sms_route, cell_id, text`
  - `AUDIO`: `event_id, ts, caller, callee, codec, vocoder_artifact, pitch_jitter, spectral_flatness, speaker_drift`
  - `CDR`: `event_type="CALL", event_id, ts, caller, duration_s` (cùng `event_id` với lần thiết lập cuộc gọi)
- `ts`, `end_ts` tính bằng **giây Unix**; trường lạ bị API từ chối (400); thân yêu cầu tối đa 64 KiB.
- Ký: `x-timestamp` = thời điểm hiện tại (giây, 9 chữ số thập phân), `x-signature` = HMAC-SHA256(khóa,
  `ts + "." + body`), đúng như `security.sign_network`. Mỗi lần thử lại ký lại với dấu thời gian mới
  (máy chủ chặn phát lại chữ ký và lệch đồng hồ quá 30 s).

## 3. File ánh xạ (`--mapping`)

```json
{
  "calling_number": "caller",
  "called_number": "callee",
  "setup_time": "ts",
  "ts_unit": "ms",
  "duration_unit": "s",
  "defaults": {"network": "4G"},
  "drop": ["vendor_seq", "record_type"],
  "values": {"event_type": {"MOC": "CALL", "MO_SMS": "SMS"}}
}
```

- Mọi cặp `"tên_nhà_mạng": "tên_API"` ở cấp ngoài cùng (hoặc trong `"fields": {...}`) là phép đổi tên.
- `ts_unit`: `s` (mặc định), `ms` (chia 1000) hoặc `iso` (chuỗi ISO-8601 **bắt buộc có múi giờ**, ví dụ
  `2026-11-17T07:00:00+07:00`; thiếu múi giờ → dead-letter). Áp dụng cho `ts` và `end_ts`.
- `duration_unit`: `s` hoặc `ms` cho `duration_s`. Số dạng chuỗi (`"5000"`) được chuyển thành số.
- `defaults`: giá trị điền khi bản ghi không có; `drop`: trường bỏ đi; `values`: đổi giá trị mã của hãng.

## 4. Chạy

```bash
# File (đọc hết rồi dừng) hoặc theo dõi file đang lớn dần (xử lý cả xoay vòng/cắt file)
python src/carrier_bridge.py --api https://fraud.internal --key-file secrets/network_link.key \
    --input events.jsonl --follow --mapping map.json --dead-letter dlq.jsonl --concurrency 32

# stdin
cat events.jsonl | python src/carrier_bridge.py --api https://fraud.internal --key-file secrets/network_link.key --input -

# Kafka (cần confluent-kafka)
python src/carrier_bridge.py --api https://fraud.internal --key-file secrets/network_link.key \
    --kafka-bootstrap broker1:9092 --kafka-topic carrier.events --kafka-group fraud-bridge \
    --kafka-config security.protocol=SASL_SSL --mapping map.json --dead-letter dlq.jsonl
```

Tùy chọn khác: `--timeout 5` (giây/yêu cầu), `--max-retries 0` (0 = thử lại đến khi API nhận),
`--backoff-base 0.2 --backoff-max 10`, `--metrics-every 10`, `--drain-timeout 30`, `--verify true|false|ca.pem`.

- Khóa đọc từ `--key-file` (64 ký tự hex như `secrets/network_link.key`, hoặc 32 byte thô); không bao giờ in ra.
- Mỗi `--metrics-every` giây in một dòng JSON ra stdout: `received, sent, ok, duplicates, retried,
  dead_lettered, inflight, latency_p50_ms, latency_p99_ms, status{...}` — không có số thuê bao. Log ra stderr.
- SIGINT/SIGTERM: ngừng đọc, chờ các yêu cầu đang bay xong (tối đa `--drain-timeout`), commit offset, in dòng
  `"final": true`. Mã thoát 2 nếu ghi dead-letter thất bại (ví dụ đầy đĩa) — khi đó bản ghi không được xác nhận.

## 5. Ngữ nghĩa giao nhận: at-least-once + API khử trùng

| Trả lời của API | Xử lý |
|---|---|
| 200, 202 | Xong → xác nhận nguồn (commit offset Kafka) |
| 200 có `retryable=true` và `durable=false` (API quá tải, trả fail-open) | Thử lại |
| 5xx, 429, 408, lỗi kết nối/timeout | Thử lại, backoff mũ có jitter, tôn trọng `Retry-After` |
| 401 | Thử lại và log lỗi (khóa sai/lệch đồng hồ không được làm rỗng luồng vào dead-letter) |
| 4xx khác, 409 (trùng `event_id` nhưng nội dung khác) | Ghi `dead-letter` kèm lý do rồi đi tiếp |

- Kafka: `enable.auto.commit=false` (không ghi đè được). Offset N chỉ được commit khi **mọi** offset trước nó
  trong cùng partition đã được API nhận hoặc đã ghi dead-letter (fsync). Khi rebalance, phần đã xong được commit
  trước khi mất partition; phần chưa xong sẽ được consumer mới gửi lại.
- Gửi lại là an toàn: API khử trùng theo `event_id` (trả `duplicate=true`, không cập nhật trạng thái lần hai).
- Back-pressure: tối đa `--concurrency` yêu cầu đang bay; khi API chậm/sập, bộ cầu nối ngừng đọc thêm thay vì
  dồn bộ nhớ. Kafka giữ phần tồn.
- Nguồn file không lưu vị trí đã đọc: chạy lại sẽ gửi lại từ đầu file (an toàn nhờ khử trùng, nhưng tốn tải).
  Với luồng thật nên dùng Kafka.
- `dlq.jsonl`: mỗi dòng `{"at", "reason", "status", "kind", "record"}` — **chứa số thuê bao**, phải lưu như dữ
  liệu cá nhân (quyền truy cập hạn chế, thời hạn lưu). Sửa xong có thể phát lại: trích `record` thành NDJSON và
  chạy lại với `--input`.

## 6. Những gì còn cần nhà mạng thật

Bộ cầu nối chỉ nhận luồng **đã có**. Phần tạo luồng nằm ở phía nhà mạng:

1. **Nguồn tín hiệu (tap):** bản sao báo hiệu thời gian thực từ SIP/IMS (S-CSCF/SBC, sự kiện INVITE), Diameter
   (Rf/Ro cho tính cước, CDR sau cuộc gọi), CAMEL/INAP (IDP từ MSC cho 2G/3G), SMSC (MO/MT SMS). Bước tiếp:
   nhà mạng chỉ định điểm lấy (port mirror/probe, ví dụ hệ thống giám sát tín hiệu hiện có) và xuất ra Kafka.
2. **Bộ chuẩn hóa của nhà mạng:** chuyển bản ghi thô (ASN.1/TAP, CSV CDR) thành JSON một dòng/sự kiện với
   `event_id` ổn định (ví dụ Call-ID SIP hoặc mã CDR) để thiết lập và CDR khớp nhau và khử trùng đúng.
3. **File ánh xạ cho từng hãng thiết bị** (Ericsson, Nokia, Huawei, ZTE…): tên trường, đơn vị thời gian, mã
   `event_type`, mã trunk (`origin_trunk`: LOCAL/INTL…), mạng (`network`). Bước tiếp: lấy 1 000 bản ghi mẫu
   đã ẩn danh của mỗi hãng, viết `map_<hãng>.json`, chạy thử vào môi trường shadow, đọc `dlq.jsonl` để sửa
   đến khi tỷ lệ dead-letter ≈ 0.
4. **Triển khai:** cấp khóa liên kết mạng qua kho bí mật (không chép tay), TLS nội bộ (`--verify ca.pem`),
   chạy nhiều bản cầu nối cùng `--kafka-group` (mỗi partition một bản), đặt API ở chế độ `shadow` cho đến khi
   bộ phận chống lừa đảo duyệt chuyển sang `enforce`.
5. **Giám sát:** thu dòng metric JSON vào hệ thống giám sát; cảnh báo khi `dead_lettered` tăng, `retried` tăng
   liên tục hoặc độ trễ p99 vượt ngân sách; theo dõi consumer lag của Kafka.

## 7. Kiểm thử

```bash
python -m pytest -q tests/test_carrier_bridge.py
```

Bao gồm: ánh xạ và đổi ms→s/ISO, suy ra kind, chữ ký được `security.verify_network` chấp nhận, thử lại khi 503,
dead-letter khi 400/409, giới hạn in-flight, thứ tự commit Kafka với consumer giả, và chạy đầu-cuối qua app
FastAPI thật (`httpx.ASGITransport`, `LiveEngine` với detector giả): trùng lặp được khử, CDR được nhận, 409/400
vào dead-letter.
