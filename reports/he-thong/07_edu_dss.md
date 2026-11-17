# 7. EduDSS (`edu_dss/`)

## Vai trò
Ứng dụng hỗ trợ quyết định giáo dục, độc lập với hệ thống viễn thông. Gồm:
- giao diện Streamlit;
- backend FastAPI xác thực bằng JWT do IdP phát;
- Celery chạy trên Redis cho các job: AutoML, tiền xử lý giải thích bài giảng, tư vấn cá nhân, dự đoán điểm.

Các adapter miền là dịch vụ bên ngoài. Khi chưa cấu hình thì hệ thống fail-closed. Riêng chế độ demo dùng dữ liệu cố định (fixture).

## Thành phần
| Tệp | Nội dung |
|---|---|
| `backend/app/main.py` | Route theo vai trò; model Pydantic `extra=forbid`, `strict`; giao task có kiểm chủ sở hữu |
| `security.py` | HS256 ghim; bắt buộc `iss`, `exp`, `sub`, `role`; từ chối `iat` ở tương lai |
| `course_access.py` | Admin truy cập toàn bộ; sinh viên và giảng viên theo claim khoá học |
| `services.py`, `tasks/jobs.py` | Gọi adapter, dữ liệu demo, giới hạn tần suất gọi LLM bằng Lua |
| `cache_keys.py` (**mới**) | Dựng khoá Redis an toàn |
| `request_limits.py` | Giới hạn body (chunked, Content-Length âm hoặc sai) |
| `grade_baseline.py` | OLS minh hoạ, chạy trên 7 cặp mô phỏng |
| `nginx/nginx.conf`, `docker-compose.yml` | Proxy và `limit_req`; mạng internal; container read-only, chạy non-root |

## Bản sửa
**F16.** Trước đây khoá `edu:explanation:{course}:{lecture}` được ghép chuỗi trực tiếp. Hệ quả:
- Sinh viên lớp `CS101` gọi lecture `sec2:lec1` sẽ đọc được bài của lớp `CS101:sec2`.
- Theo cách tương tự, giảng viên có thể ghi đè dữ liệu của lớp khác.

Leaderboard `edu:leaderboard:{sub}:{model_ref}` gặp đúng lỗi này.

Cách sửa: `cache_keys.py` percent-encode từng phần của khoá. ID chỉ gồm `[A-Za-z0-9_.-]` giữ nguyên khoá cũ. Test nằm ở `backend/tests/test_cache_keys.py` (2 test; trên HEAD báo `ModuleNotFoundError`).

## Bản sửa vòng 2
| # | Sửa |
|---|---|
| G24 (D9) | `TrainingRequest.course_id` là bắt buộc và được kiểm bằng `require_course_access`. UI đã có thêm ô nhập; adapter nhận `course_id`. |
| G25 (D10) | Token demo mang `demo=true` và bị từ chối khi `EDU_DEMO_MODE≠1`. Thêm tuỳ chọn `JWT_AUDIENCE` (bắt buộc và kiểm `aud`). Worker Celery không còn nhận `JWT_SECRET` (nạp lười, chỉ API và CLI dùng). Body tối đa 1,1 MB ở cả backend và nginx. nginx ghi đè `X-Forwarded-For`, thêm `X-Frame-Options: DENY` và CSP `frame-ancestors 'none'`. |

Test: `backend/tests/test_round2_auth.py` (4 test).

**Lưu ý:** client gọi AutoML phải gửi thêm `course_id`.

## Bản sửa vòng 3
**H7.** `JWT_SECRET` phải dài ít nhất 32 ký tự; `bootstrap-local.ps1` tạo secret 64 ký tự. Muốn bỏ qua kiểm tra này phải đặt rõ `EDU_ALLOW_WEAK_JWT_SECRET=1`.

Test: `test_short_jwt_secret_is_refused`.

## Còn mở
- Backend vẫn đặt `--forwarded-allow-ips "*"`. Điều này chấp nhận được vì chỉ nginx gọi được tới backend (mạng internal), và nginx đã ghi đè XFF.

## Đã kiểm và thấy đúng
- Kiểm chủ sở hữu task trước khi trả kết quả. Khoá owner sống 24 giờ, dài hơn `result_expires` (1 giờ). Redis đặt `noeviction`.
- Task lỗi không làm lộ nội dung exception.
- Celery chỉ nhận JSON; broker nằm trên mạng internal và có mật khẩu.
- Body limit phát lại body đúng một lần (có test).
- Kết quả dự đoán điểm được chặn trong khoảng 0..4.

## Kiểm thử
9 → 11 passed (kèm 4 subtests).
