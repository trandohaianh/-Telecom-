# 2. Tài khoản, xác thực và giao diện

## Vai trò
- **App thuê bao (`/app/`):** đăng ký bằng email/mật khẩu hoặc Google, gắn số điện thoại, xem inbox cảnh báo, tra số, báo cáo số, bảo vệ người thân.
- **Bảng vận hành (`/admin/`, `/admin/manage`):** dashboard, duyệt đề xuất của agent, duyệt báo cáo số, quản lý nhân viên, theo dõi chi phí và pipeline.

## Thành phần
| Module | Nội dung |
|---|---|
| `security.py` | Vai trò `viewer < analyst < admin`. Phiên vận hành hết hạn sau 8 giờ, hoặc sau 30 phút không thao tác. `require()`. Che số. Audit log dùng chuỗi SHA-256 cộng HMAC theo từng người. HMAC cho link nhà mạng. |
| `accounts.py` | scrypt (2^14, 8, 1). Rate limiter. Xác minh Google ID token (RS256, JWKS, aud/iss/exp, email_verified). OTP 6 số, 300 s, 5 lần. Phiên thuê bao: token 32 byte, chỉ lưu SHA-256. |
| `portal.py` | Các route `/api/app/auth/*`, `/api/app/phone/*`, `/api/admin/*`. Báo cáo số, gia đình, chi phí. |
| `appdb.py` | SQLite. `tx()` = `BEGIN IMMEDIATE` … COMMIT, ROLLBACK khi có exception. |
| `static/app`, `static/admin` | PWA và console. Có hàm `esc()` để escape HTML. |

## Bản sửa
| # | Lỗi | Sửa |
|---|---|---|
| F1 | OTP bị dò không giới hạn: lần đếm sai bị ROLLBACK cùng với exception. | Commit trước, raise sau. Lần sai thứ 5 xoá mã. |
| F2 | Chiếm chỗ email trước: kẻ gian đăng ký trước và vẫn giữ mật khẩu sau khi nạn nhân đăng nhập bằng Google. | Khi liên kết Google thì xoá `password_hash` và toàn bộ phiên của tài khoản đó. |
| F3 | Admin bị hạ quyền vẫn giữ quyền admin tới 8 giờ. | `require()` đọc vai trò hiện tại của tài khoản. |
| F4 | Thêm khoảng trắng vào tên là được một bucket rate limit mới. | Khoá rate limit dùng `strip().lower()`. |
| F5 | Kiểm tra "tự hạ quyền" so sánh tên chưa chuẩn hoá. | Chuẩn hoá tên trước khi so. |
| F6 | `family/respond` chấp nhận dù không có lời mời. | Trả 404 khi không có lời mời đang chờ. |
| F11 | XSS trong thẻ cảnh báo của console, từ các chuỗi do nhà mạng gửi tới. | `esc()` cho mọi trường. |

## Bản sửa vòng 2
| # | Sửa |
|---|---|
| G8 | Khoá tài khoản hoặc đổi mật khẩu sẽ xoá mọi phiên, kể cả trong kho bền (`delete_sessions_for`). |
| G9 | Tài khoản không tồn tại vẫn chạy scrypt giả, nên không đo được thời gian để dò tài khoản. |
| G10 | `verify_audit` báo `valid: false` khi gặp dòng hỏng, không còn trả 500. |
| G11 | Ghi audit khi thêm hoặc xoá chi phí hạ tầng. |
| G12 | Report/invite kiểm định dạng số (`[0-9A-Za-z]{3,15}`). Giới hạn 30 report và 10 invite mỗi giờ. Lý do tối đa 500 ký tự. |
| G13 (D3) | Ba số demo công khai không bao giờ được cấp cho tài khoản thật. Phiên demo kiểu cũ từ chối số đã có chủ. |
| G14 (D4) | Khi số đổi chủ qua OTP, inbox, người giám hộ và lời mời của chủ cũ bị xoá. |

Test: `tests/test_audit_regressions_round2.py`.

## Bản sửa vòng 3
**H2.** `verify_audit` trả `head` (`records`, `hash`). Hãy chép giá trị này định kỳ ra hệ thống ngoài. Khi gửi lại qua `/api/audit?anchor_records=&anchor_hash=`, hệ thống phát hiện log bị cắt đuôi hoặc bị viết lại.

Test: `test_external_anchor_detects_truncated_audit_tail`.

## Còn mở
- `register` trả 400 khi email đã dùng. Test hiện có khẳng định hành vi này; đây là lựa chọn trải nghiệm người dùng, và rate limit đã giới hạn việc dò.
- Admin tự đổi mật khẩu thì bị đăng xuất. Đây là hệ quả có chủ đích của G8.

## Đã kiểm và thấy đúng
- HMAC so sánh constant-time, cửa sổ thời gian ±30 s, cache chữ ký giữ gấp 2 cửa sổ.
- Mọi câu SQL đều có tham số.
- Cách ly theo số lấy từ phiên.
- Mọi đường demo đều có chặn `demo_only`.
- Tài khoản bị tạm khoá bị chặn ngay ở `require`.
- App thuê bao escape toàn bộ dữ liệu.
