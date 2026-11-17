# 4. RAM online: tra danh sách chặn bằng Bloom và Redis

## Vai trò
Đây là nguyên mẫu độc lập, chưa nối vào API chính. Mỗi cuộc gọi đi qua ba bước:
1. Một Bloom filter bất biến nằm trong RAM.
2. Đúng **một** lần gọi Lua tới Redis: kiểm phiên bản và số phần tử, rồi kiểm thành viên chính xác nếu cần.
3. Trả về ALLOW, BLOCK hoặc UNKNOWN.

Song song, cửa sổ 60 s trên một Redis phân tích riêng đếm tần suất gọi (nguồn: Kafka).

## Thành phần
| Module | Nội dung |
|---|---|
| `core.py` | `BloomFilter` (m, k, băm kép BLAKE2b), `PolicyStore` (SADD/SREM + manifest `uuid4:SCARD`), `OnlineGate` (`max_inflight`, ngân sách, `CHECK_LUA`), `normalize_phone` (E.164) |
| `production.py` | `CircuitBreaker` (2 lỗi, 1 s, có epoch), `FailOpenGate`, `CallDecision.status` (UNKNOWN khi policy chưa biết) |
| `streaming.py` | `WINDOW_LUA`, `SlidingWindowDetector`, consumer Kafka (commit sau khi xử lý) |
| `api.py`, `demo.py`, `resources.py` | HTTP API, demo đo đạc, vòng đời client |

## Bản sửa
**F12:** khi bị quá tải cục bộ (vượt `max_inflight`, hoặc event loop lag trước khi kịp gọi Redis), `OnlineUnavailable` bị đếm như một lỗi Redis. Chỉ cần 2 lần như vậy là breaker mở, và trong suốt cooldown **mọi số bị chặn đều được cho qua**. Kẻ gian chỉ cần làm ngập request là vô hiệu được danh sách chặn.

Cách sửa:
- Thêm lớp `OnlineOverloaded` cho lỗi quá tải cục bộ.
- Adapter trả UNKNOWN với lý do `online_overloaded` và không đếm lỗi cho breaker.
- `CircuitBreaker.released()` để request kế tiếp được probe ngay.

Test: `ram_online/test_overload_breaker.py` (2 test, không cần Redis). Cả hai thất bại trên HEAD với `'open' == 'closed'`.

Dọn dẹp: bỏ import `make_client` không dùng trong `streaming.py`.

## Bản sửa vòng 2
| # | Sửa |
|---|---|
| G20 (D8) | Thêm tuỳ chọn `--dlq-topic` hoặc `KAFKA_DLQ_TOPIC`.<br>• Bản ghi lỗi dữ liệu (JSON hỏng, sai key, xung đột `call_id`) được ghi sang DLQ với acks=all, chờ ack, rồi mới commit offset.<br>• Lỗi hạ tầng (Redis down) không vào DLQ mà vẫn dừng như trước.<br>• Không cấu hình DLQ thì giữ nguyên hợp đồng fail-stop. |
| G21 | Exception không bọc (ví dụ `OSError`) trả về `CallDecision(UNKNOWN, policy_error)` và được tính vào breaker. |

Test: `ram_online/test_round2.py` (3 test, không cần Redis).

## Còn mở
- Cửa sổ có cận dưới mở và neo theo frontier. Test `test_ram_online.py:99` khẳng định hành vi này.
- Policy Redis dùng AOF `everysec`.

## Đã kiểm và thấy đúng
- Bloom không có âm tính giả.
- Rào phiên bản không bị ABA.
- `SCARD` phát hiện khi mất cả tập.
- Snapshot chỉ được cài khi manifest không đổi trước và sau SSCAN.
- Hash tag tương thích Cluster.
- Tắt retry.
- E.164 được kiểm trước mọi RPC.
- Kafka commit sau khi thành công (có test).
- Phát lại idempotent nhờ `ZADD NX` cộng kiểm xung đột `call_id`/ts.

## Kiểm thử
**Môi trường:** không có Redis thật hay Docker daemon, nên dùng `fakeredis.TcpFakeServer` cộng `lupa`.

| | Mốc | Sau sửa |
|---|---|---|
| Kết quả | 17/18 | 19/20 |

Ca hỏng duy nhất là `test_window_rejects_call_id_with_changed_setup_timestamp`: fakeredis đóng kết nối khi Lua trả `error_reply`. Ca này hỏng giống hệt trên HEAD. Khi không có URL, 8 ca cần Redis đều báo `KeyError: RAM_TEST_POLICY_URL`.
