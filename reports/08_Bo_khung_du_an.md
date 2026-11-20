# 08 — Bộ khung dự án Telecom Fraud Shield v2

Ngày 20/11/2026. Nhánh làm việc cục bộ, chưa đẩy lên remote. Repo GitHub trống sẽ được gửi sau.

Báo cáo đi theo từng mục của checklist "bộ khung dự án". Mỗi mục ghi bốn ý: hiện trạng trước khi sửa, thay đổi, đường dẫn và cách chạy. Phần cuối là kết quả lint, kết quả test, danh sách commit và những việc còn mở.

## Tóm tắt

| Mục checklist | Trạng thái | Bằng chứng |
|---|---|---|
| Cấu trúc thư mục rõ ràng | Đạt | §1, `docs/CONVENTIONS.md` §1 |
| Tách frontend / backend / database / module | Đạt | §2. `server.py` được tách ra, thêm `live_engine.py`. |
| README | Đạt (đã cập nhật) | `README.md` (mục Cấu trúc, mục Phát triển) |
| .gitignore | Đạt (đã bổ sung) | `.gitignore` |
| .env.example không chứa khoá bí mật | Đạt (đã kiểm) | §5 |
| Quy tắc đặt tên | Đạt (mới) | `docs/CONVENTIONS.md` §2 |
| Lint và format | Đạt (mới) | §7: ruff, eslint, prettier, pre-commit, job CI `lint` |
| Không viết cả phần mềm trong một file lớn | Đạt một phần | §8. Còn 3 file trên 800 dòng, đã ghi lý do. |
| Commit tại các mốc ổn định | Đạt | §9: 6 commit, mỗi commit sau khi test đạt |
| Đầu ra chạy được, có quản lý phiên bản | Đạt cục bộ | §10. Chưa push vì chưa có remote. |

Ngoài checklist, đợt này còn sửa các lỗi trong reports/06 §4 không cần phần cứng hay giá trị của chủ dự án, và tách HTTPS riêng cho từng dự án (§11).

## 1. Cấu trúc thư mục

**Trước:** đã có cấu trúc theo chức năng (`src/`, `static/`, `tests/`, `deploy/`, `docs/`…), nhưng chưa có tài liệu nào mô tả nó. README chỉ liệt kê một phần file.

**Thay đổi:**
- `docs/CONVENTIONS.md` §1 có bảng thư mục: vai trò của từng thư mục, thư mục nào không được commit, và file backend nào giữ chức năng nào.
- Thêm thư mục `tools/` cho các script kiểm tra dùng chung bởi CI và pre-commit.

## 2. Tách frontend, backend, database và module

| Lớp | Vị trí |
|---|---|
| Frontend | `static/app/` (PWA thuê bao), `static/admin/` + `static/admin/assets/` (bảng vận hành) |
| Backend | `src/server.py` (route vận hành), `src/portal.py` (app thuê bao, quản trị), `src/live_engine.py` (engine), `src/backend_runtime.py` (luồng suy luận) |
| Database | `src/backend_state.py` (journal SQLite WAL), `src/appdb.py` (thuê bao, OTP, báo cáo số, người giám hộ, chi phí, KPI) |
| Module phụ | `spark/`, `telecom_grpc/`, `ram_online/`, `evals/` |

**Thay đổi trong đợt này:**
- Script inline trong HTML được chuyển ra file `.js`: `static/app/theme-init.js`, `app.js`, `theme-toggle.js` và `static/admin/assets/theme-init.js`, `dashboard.js`, `manage.js`. Việc này tách rõ nội dung trang khỏi logic, và cho phép CSP `script-src 'self'`.
- `/admin/assets` được mount làm thư mục tĩnh.
- `onclick` inline được thay bằng `data-href` cộng listener.
- `LiveEngine` được tách khỏi `server.py` sang `src/live_engine.py` (xem §8).

## 3. README

**Thay đổi trong `README.md`:**
- Mục "Cấu trúc / Layout" có thêm `live_engine.py`, `static/admin|app`, `backend_state.py` và `appdb.py`.
- Mục mới "Phát triển / Development" ghi đường dẫn tới quy ước, lệnh cài công cụ, lệnh kiểm tra và ghi chú về file khoá phụ thuộc.
- `deploy/cloud/README.md` có bảng hai chế độ HTTPS (own / edge).

## 4. .gitignore

**Trước:** đã chặn `secrets/`, `logs/` và các file sinh ra, nhưng chưa chặn `.env` ở thư mục con, khoá riêng hay `*.docx`.

**Đã thêm:** `.env`, `.env.*`, `**/.env`, `**/.env.*`, ngoại lệ `!**/.env.example`, `*.key`, `*.pem`, `*.docx`.

**Lớp kiểm tra thứ hai:** `tools/check_tracked_secrets.py`.
- Script báo lỗi khi `secrets/`, `.env*` (trừ `.env.example`), `*.key`, `*.pem`, `*.p12`, `*.pfx` hay `*.docx` bị track hoặc stage.
- Nó chạy trong pre-commit và trong job CI `lint`.

Ba file `.docx` từng được track đã được **bỏ track** (`git rm --cached`) theo lựa chọn của chủ dự án:
- `docs/PORTFOLIO_THREE_SYSTEMS.docx`
- `docs/Telecom_Fraud_Report_v2_EN.docx`
- `docs/Telecom_Fraud_Report_v2_VI.docx`

File vẫn nằm trên đĩa, nhưng các commit cũ vẫn chứa chúng (lịch sử không bị viết lại). `ALLOWED_LEGACY` trong script giờ để trống.

## 5. .env.example không chứa khoá bí mật

Repo track hai file: `deploy/cloud/.env.example` và `edu_dss/.env.example`. Kết quả kiểm:
- Mọi biến có tên dạng `*PASSWORD*`, `*TOKEN*`, `*KEY*` hay `*SECRET*` đều để trống: `FRAUD_ADMIN_PASSWORD`, `METRICS_TOKEN`, `TWILIO_AUTH_TOKEN`, `DUCKDNS_TOKEN`, `FRAUD_SMS_HTTP_TOKEN_FILE`.
- Không có chuỗi dài giống khoá.
- Không có URL nhúng `user:pass@`.

Các biến mới của đợt này là `TELECOM_PROXY_MODE=own`, `TELECOM_PROXY_SUBNET` và `TELECOM_CADDY_IP`. Chúng là cấu hình, không phải bí mật. Khoá thật được sinh trên máy chủ bằng `deploy/cloud/setup.sh`, hoặc đặt trong GitHub Secrets.

## 6. Quy tắc đặt tên

Viết mới trong `docs/CONVENTIONS.md` §2, gồm:
- Python: `snake_case`, `PascalCase`, `UPPER_SNAKE_CASE`, tiền tố `_` cho tên nội bộ.
- Biến môi trường: `FRAUD_` cho engine, `TELECOM_` cho triển khai. Giá trị sai phải báo `ValueError`.
- JS: `camelCase`; HTML: `kebab-case`; điều hướng bằng `data-href`.
- Route: `/api`, `/api/app`, `/api/v1`, `/health`.
- Test: `tests/test_<chủ đề>.py`, tên hàm test là một câu mô tả hành vi.
- Commit message.

Đợt lint cũng đổi các tên vi phạm có thật:
- Biến vòng lặp không dùng đổi thành `_` hoặc `_ten`.
- Biến `l` dễ nhầm với `1` đổi thành `lat` trong `evals/run_evals.py`.

## 7. Lint và format

| Công cụ | Phiên bản | Cấu hình | Phạm vi |
|---|---|---|---|
| ruff (lint + isort) | 0.17.6 | `pyproject.toml` | Python. Bộ luật F, E4, E7, E9, B, I, PLE; dòng 120 ký tự; py312. |
| ruff format | 0.17.6 | `pyproject.toml` (giữ kiểu dấu nháy) | Python |
| eslint | 10.15.0 | `eslint.config.js` (flat, CommonJS; globals browser, serviceworker, `google`) | `static/**/*.js` |
| prettier | 3.10.1 | `.prettierrc.json`, `.prettierignore` | `static/**/*.js` |
| EditorConfig | — | `.editorconfig` | mọi file |
| pre-commit | 4.7.0 | `.pre-commit-config.yaml` (hook local/system) | chặn bí mật, ruff check, ruff format, eslint, prettier |

Công cụ được ghim trong `requirements-dev.txt`, `package.json` và `package-lock.json`, và không đưa vào image.

**Job CI `lint`** (`.github/workflows/ci.yml`):
1. Chặn file bí mật.
2. Kiểm lock phụ thuộc khớp với file `.txt` (`tools/check_lock_sync.py`).
3. `ruff check`, `ruff format --check`.
4. `npm run lint`, `npm run format:check`.

`setup-node` được ghim theo SHA của v4.4.0; SHA đã đối chiếu với tag trên GitHub.

**Sửa lỗi để lint sạch** (commit e695b8f):
- ruff `--fix` sửa tự động 133 lỗi: sắp xếp import, bỏ import thừa, bỏ f-string không có biến, ký tự zero-width viết thành `\u200B`.
- Sửa tay các lỗi còn lại:
  - B904: `raise ... from exc` / `from None` trong `server.py`, `backend_runtime.py`.
  - B007: biến vòng lặp không dùng.
  - E741: biến `l`.
- Thêm 11 chỉ thị `noqa` có chủ đích:
  - B023 báo nhầm cho lambda gọi ngay trong vòng lặp.
  - E402 cho các import đặt sau bước cấu hình môi trường, cố ý, trong `data_generator`, `detector`, `server`, `train`.

**Format** (commit 1ec962c):
- `ruff format` áp cho toàn bộ Python.
- Với JS, prettier đã cấu hình nhưng 7 file JS hiện có nằm trong `.prettierignore`. Lý do: format lại chúng làm hỏng 6 test so khớp đúng từng đoạn mã nguồn (ví dụ `catch(e){ fail(e); return; }`). Nên giữ các test đó hơn là định dạng đẹp. File JS mới vẫn phải qua prettier.

**Cách chạy:**

```bash
pip install -r requirements-dev.txt
npm ci --ignore-scripts
pre-commit install          # từ giờ mỗi git commit tự kiểm
pre-commit run --all-files  # chạy tay toàn bộ
ruff check . && ruff format --check . && npm run lint && npm run format:check
```

**Kết quả lần chạy cuối** (20/11/2026):

| Lệnh | Kết quả |
|---|---|
| `ruff check .` | All checks passed (0 lỗi) |
| `ruff format --check .` | 182 file đã đúng format |
| `eslint static --max-warnings=0` | 0 lỗi, 0 cảnh báo |
| `prettier --check "static/**/*.js"` | Đạt |
| `pre-commit run --all-files` | forbid-secret-files, ruff-check, ruff-format: Passed. eslint/prettier bỏ qua trên máy này vì Node nằm ở `E:\dev-tools`; CI vẫn chạy hai bước đó. |
| `tools/check_tracked_secrets.py` | 0 file bị chặn; thử với `.env`, `a/.env.local`, `secrets/x`, `x.pem` đều bị chặn đúng |
| `tools/check_lock_sync.py` | Lock khớp với `requirements-serve.txt` |

## 8. Không viết cả phần mềm trong một file lớn

**Trước:** `src/server.py` dài 1435 dòng, chứa cả engine lẫn toàn bộ route.

**Thay đổi** (commit a02a101):
- Lớp `LiveEngine`, các thiết lập `DEMO_RATE` / `EVIDENCE_PATH` và `_demo_evidence_cap` được chuyển nguyên vẹn sang `src/live_engine.py` (633 dòng).
- `server.py` còn 836 dòng, chỉ gồm route và phân quyền. File này nhập lại các tên đã chuyển nên `server.LiveEngine` vẫn dùng được.
- Engine nhận audit log qua tham số `audit_log`. Lifespan truyền vào `server.audit`, nên chuỗi audit ký số vẫn là một instance duy nhất.
- Bước kiểm tra của QA "audit verifies + head" đạt sau khi tách.
- Các test đọc mã nguồn được trỏ sang file mới.
- Danh sách băm mã nguồn trong `docker/backend/verify_entrypoint.py` có thêm `live_engine.py`.

**Còn trên 800 dòng, cố ý chưa tách:**

| File | Dòng | Lý do |
|---|---|---|
| `src/backend_runtime.py` | 1026 | Lõi một luồng sở hữu trạng thái (hàng đợi, checkpoint, dọn journal). Tách lớp này cần đổi ranh giới khoá và thứ tự commit; nên làm như một thay đổi riêng, có đo SLA lại. |
| `src/feature_store.py` | 836 | Nằm trên đường nóng ~65 µs/sự kiện. Tách module có thể thêm lượt tra thuộc tính. Chỉ vượt ngưỡng 36 dòng. |
| `src/server.py` | 836 | Đã tách một lần. Phần còn lại là route; có thể tách tiếp nhóm `/api/app/*` sang router riêng nếu cần. |

## 9. Commit tại các mốc ổn định

Mỗi commit dưới đây chỉ được tạo sau khi bộ test đầy đủ đạt. Thay đổi rủi ro (format hàng loạt, tách file) nằm trong commit riêng, ngay sau một mốc đã xanh.

| Commit | Nội dung |
|---|---|
| fd0b9fd | Deploy: mỗi dự án tự có HTTPS (Caddy riêng, IP tĩnh); dùng chung edge FieldOps là tuỳ chọn |
| a201868 | Các mục reports/06 §4: CSP `script-src`, ghim digest image, dọn journal, ẩn nút Google trong WebView Android |
| e695b8f | Thiết lập lint, sửa lỗi lint, khoá phụ thuộc bằng hash, bổ sung `.gitignore` |
| 1ec962c | (rủi ro) `ruff format` toàn bộ Python |
| 71c4eac | pre-commit, job CI `lint`, `docs/CONVENTIONS.md`, README |
| a02a101 | (rủi ro) tách `LiveEngine` ra `live_engine.py` |

## 10. Đầu ra: chạy được và có quản lý phiên bản

| Kiểm thử (WSL, Python 3.12) | Kết quả |
|---|---|
| `pytest tests` | 639 passed |
| `pytest spark telecom_grpc` | 26 passed, 65 subtests passed |
| QA luồng thật trên server (`serve.py`, cổng 18080) | 31/32. Lỗi duy nhất là D1 PENDING, lỗi chập chờn đã biết của chế độ demo, có từ trước đợt này. |
| Trình duyệt | App thuê bao, bảng vận hành, điều hướng `data-href` chạy được; không có vi phạm CSP. Chưa xác minh được service worker trong browser pane nhúng (giới hạn của pane khi tải script). |

`requirements-serve.lock` có 49 gói và 1574 hash. Lock được kiểm bằng `pip install --dry-run --require-hashes` trên Linux.

Docker không được build lại trên máy này vì Docker daemon không chạy. Job CI `docker-image` build cho amd64 và arm64 khi có remote.

Repo là git cục bộ, chưa có remote. Trước khi push sẽ:
- kiểm lại không có file bí mật bị track;
- không force-push.

## 11. Ngoài checklist: sửa lỗi và tách HTTPS

**Mỗi dự án tự có HTTPS:**
- `deploy/cloud/docker-compose.yml` mặc định chạy Caddy riêng, IP tĩnh `172.29.10.2` trên subnet cố định.
- `FORWARDED_ALLOW_IPS` là IP tĩnh đó, không còn dò lúc chạy.
- Dùng chung edge FieldOps là tuỳ chọn: `TELECOM_PROXY_MODE=edge` cộng `docker-compose.edge.yml`.
- `setup.sh`, `deploy.sh`, `smoke.sh` (kiểm thêm HSTS) và README đã cập nhật.
- Test: `tests/test_round8_http_split.py`.

**CSP:** thêm `script-src 'self' https://accounts.google.com`, `object-src 'none'`, `base-uri 'self'`. Riêng `/docs` và `/redoc` giữ CSP cũ để Swagger UI chạy.

**Ghim image theo digest đa kiến trúc:** `python:3.12.12-slim-bookworm`, `python:3.11-slim`, `caddy:2.10-alpine`, `apache/kafka:3.8.0`, `apache/kafka:3.9.0`.

**Dọn journal sự kiện:**
- Thiết lập `FRAUD_EVENT_RETENTION_DAYS`, mặc định 30, khoảng 0–3650.
- Chỉ xoá sự kiện thoả đủ bốn điều kiện: đã có kết quả, đã xuất bằng chứng, nằm trước checkpoint, và cũ hơn hạn giữ.
- Chạy mỗi giờ khi rảnh, theo lô 5000.

**WebView Android:** không tải Google Sign-In, vì Google chặn trong WebView.

**Test:** `tests/test_round8_fixes.py`, 8 test.

## 12. Việc còn mở (cần chủ dự án)

1. **Ba file `.docx`:** đã bỏ track. Chúng vẫn còn trong các commit cũ; muốn xoá hẳn thì phải viết lại lịch sử trước lần push đầu tiên.
2. **Tên repo GitHub trống** để push. Sẽ push khi nhận được, không force-push.
3. **Tách tiếp `backend_runtime.py`:** nên làm như một thay đổi riêng, có đo lại SLA (xem §8).
