# 5. gRPC edge (`telecom_grpc/`)

## Vai trò
Cung cấp cổng gRPC bất đồng bộ cho adapter tổng đài, chạy trên `ram_online.OnlineGate` và Redis policy.

- `proto/telecom.proto` được biên dịch trong Docker.
- Kênh truyền dùng mTLS bắt buộc (`tls_config.py`).
- Chế độ insecure chỉ cho phép trên loopback.

## Thành phần
| Tệp | Nội dung |
|---|---|
| `server.py` | Các handler RPC; ánh xạ `CallDecision.status` sang ALLOW, BLOCK hoặc UNKNOWN; tuỳ chọn fast-allow khi snapshot còn mới; `so_reuseport` |
| `tls_config.py` | Tạo credential: thiếu `GRPC_CLIENT_CA` thì fail-closed; `require_client_auth=True` |
| `provision_policy.py`, `client_example.py` | Nạp policy và client mẫu |
| `Dockerfile`, `Dockerfile.dockerignore`, `docker-compose.yml`, `bootstrap-local.ps1` | Image, Redis cục bộ, khoá thử nghiệm |

## Bản sửa
**F13 (Cao).** `bootstrap-local.ps1` sinh ra `telecom_grpc/secrets/` (mật khẩu và ACL Redis) và `telecom_grpc/tls/` (`server.key`, `client.key`). Hai quy tắc sau cùng đưa các khoá này vào layer image:
- whitelist `!telecom_grpc/**` trong `.dockerignore`;
- lệnh `COPY telecom_grpc` trong `Dockerfile`.

Ai lấy được image là có khoá client và vượt qua mTLS.

Cách sửa: `.dockerignore` loại trừ `telecom_grpc/secrets`, `telecom_grpc/tls`, `**/*.key`, `**/*.pem`, `**/.env*`, `__pycache__`. Có test tĩnh `test_docker_contexts_exclude_keys_and_env_files`. Docker build chưa chạy được vì không có daemon.

## Bản sửa vòng 2
- **G21** (trong `ram_online/production.py`): lỗi bất ngờ không còn tới client dưới dạng mã gRPC UNKNOWN. Thay vào đó là một quyết định fail-open có lý do `policy_error`.
- **G23:**
  - healthcheck Redis dùng `REDISCLI_AUTH`, nên mật khẩu không còn hiện trong `ps`;
  - service `call-policy-grpc` có healthcheck TCP.
- `docker compose config` đã qua.

## Còn mở
- Fast-allow là tuỳ chọn có tài liệu.
- Redis policy chạy qua `redis://` trên mạng internal.

## Đã kiểm và thấy đúng
- Ánh xạ UNKNOWN đúng ở cả hai RPC.
- Số rỗng trả INVALID_ARGUMENT.
- `max_length=16`.
- API key so sánh constant-time.
- mTLS bắt buộc (có 2 test).

## Kiểm thử
2 passed trước và sau sửa. Phải chạy `python -m pytest telecom_grpc/tests` từ thư mục gốc repo; chạy bên trong `telecom_grpc/` sẽ gặp `ModuleNotFoundError`.
