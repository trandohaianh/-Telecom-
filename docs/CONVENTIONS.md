# Quy ước dự án Telecom Fraud Shield v2

Tài liệu này là luật chung cho người viết code trong repo. Lint và format được kiểm tự động (pre-commit và job `lint` của CI). Các mục còn lại do người review kiểm.

## 1. Cấu trúc thư mục

| Thư mục | Vai trò | Ghi chú |
|---|---|---|
| `static/` | **Frontend**: HTML/CSS/JS chạy trên trình duyệt, không có bước build | `static/app/` là app thuê bao (PWA). `static/admin/` là bảng vận hành; script nằm ở `static/admin/assets/`. Không viết `<script>` inline vì CSP chặn. |
| `src/` | **Backend** Python (FastAPI) | Mỗi file một chức năng, xem bảng dưới |
| `src/backend_state.py`, `src/appdb.py` | **Database** (SQLite WAL) | `backend_state` là journal sự kiện, checkpoint và dọn dữ liệu cũ. `appdb` chứa tài khoản thuê bao, phiên, OTP, báo cáo số, người giám hộ, chi phí và KPI. |
| `tests/` | Test pytest cho `src/` và `static/` | `tests/_pages.py` đọc HTML cùng các file `.js` mà nó tham chiếu |
| `spark/`, `telecom_grpc/`, `ram_online/` | Module phụ, có test riêng | Chạy `pytest spark telecom_grpc` |
| `evals/` | Đo chất lượng mô hình và SLA HTTP (cổng chặn hồi quy) | |
| `deploy/`, `docker/`, `Dockerfile`, `monitoring/` | Triển khai và giám sát | `deploy/cloud/` mặc định tự có Caddy (HTTPS riêng). Dùng chung edge là tuỳ chọn. |
| `android/` | App Android (WebView) | |
| `docs/`, `reports/` | Tài liệu và báo cáo | |
| `tools/` | Script kiểm tra cho CI và pre-commit | |
| `runtime/`, `data/`, `models/` | Mô hình và dữ liệu mô phỏng đã sinh | |
| `secrets/`, `logs/`, `backup_tfd-v2.0/` | Dữ liệu cục bộ | **Không commit** |

Các khối chính trong backend:

| Chức năng | File |
|---|---|
| Route HTTP, phân quyền theo route | `server.py` (route vận hành), `portal.py` (app thuê bao, quản trị) |
| Engine trực tiếp (replay hoặc live, journal, bằng chứng, agent) | `live_engine.py` |
| Luồng suy luận một chủ sở hữu, hàng đợi, checkpoint, dọn journal | `backend_runtime.py` |
| Đặc trưng và mô hình | `feature_store.py`, `detector.py`, `fast_iforest.py`, `train.py` |
| An ninh | `security.py` (vai trò, phiên, audit ký số), `http_hardening.py` (CSP, HSTS), `accounts.py` |
| Chịu lỗi và bằng chứng | `resilience.py`, `evidence.py`, `backend_evidence.py` |

**Giới hạn kích thước:** một file mã nguồn mới không nên vượt ~800 dòng. File đang lớn hơn chỉ được tách khi có test bao phủ, và tách theo chức năng (ví dụ `live_engine.py` được tách ra khỏi `server.py`). Không viết cả phần mềm trong một file.

## 2. Quy tắc đặt tên

- **Python:**
  - module và hàm dùng `snake_case`; lớp dùng `PascalCase`; hằng dùng `UPPER_SNAKE_CASE`.
  - hàm hoặc biến nội bộ bắt đầu bằng `_`.
  - biến vòng lặp không dùng đặt là `_` hoặc `_ten`.
- **Biến môi trường:**
  - tiền tố `FRAUD_` cho thiết lập của engine, ví dụ `FRAUD_EVENT_RETENTION_DAYS`, `FRAUD_SQLITE_SYNCHRONOUS`.
  - tiền tố `TELECOM_` cho thiết lập triển khai, ví dụ `TELECOM_PROXY_MODE`, `TELECOM_CADDY_IP`.
  - Mỗi biến mới phải có một dòng trong `.env.example` tương ứng, kèm giá trị mẫu **không phải khoá thật**.
  - Giá trị sai phải báo lỗi (`ValueError`) chứ không được lặng lẽ dùng mặc định.
- **JavaScript (`static/`):**
  - hàm và biến dùng `camelCase`; hằng cấu hình dùng `UPPER_SNAKE_CASE`.
  - id và class trong HTML dùng `kebab-case`.
  - điều hướng bằng thuộc tính `data-href` cùng listener, không dùng `onclick` inline.
- **Route HTTP:**
  - `/api/...` cho bảng vận hành; `/api/app/...` cho app thuê bao; `/api/v1/...` cho tích hợp nhà mạng (ký HMAC).
  - `/health/live` và `/health/ready` cho kiểm tra sống.
- **Test:**
  - file `tests/test_<chủ đề>.py`.
  - tên hàm test là một câu mô tả hành vi, ví dụ `test_writer_failure_stops_the_consumer_instead_of_committing_offsets`.
- **Git:**
  - commit message là một câu mệnh lệnh tiếng Anh ở dòng đầu, ví dụ "Split LiveEngine out of server.py".
  - thân commit giải thích *vì sao*.

## 3. Lint và format

| Công cụ | Phạm vi | Cấu hình | Lệnh |
|---|---|---|---|
| ruff (lint + sắp import) | Python | `pyproject.toml` `[tool.ruff]` | `ruff check .` |
| ruff format | Python | `pyproject.toml` `[tool.ruff.format]` (giữ nguyên kiểu dấu nháy) | `ruff format .` |
| eslint | `static/**/*.js` | `eslint.config.js` | `npm run lint` |
| prettier | `static/**/*.js` | `.prettierrc.json`, `.prettierignore` | `npm run format:check` |
| EditorConfig | mọi file | `.editorconfig` | trình soạn thảo tự áp dụng |

Các file JS cũ được liệt kê trong `.prettierignore`, vì nhiều test so khớp đúng từng đoạn mã của chúng. File JS mới vẫn phải qua prettier.

Cài một lần:

```bash
pip install -r requirements-dev.txt
npm ci --ignore-scripts
pre-commit install
```

Sau đó mỗi `git commit` tự chạy ruff, eslint và prettier, đồng thời chặn file bí mật. Máy không có Node thì đặt `SKIP=eslint,prettier`; job `lint` trên CI vẫn kiểm đủ.

## 4. Bí mật và file cá nhân

- Không bao giờ commit `secrets/`, `.env` hay `.env.*` (trừ `.env.example`), `*.key`, `*.pem`, `*.docx`.
  - `.gitignore` chặn các file này.
  - `tools/check_tracked_secrets.py` kiểm lại trong pre-commit và CI.
- `.env.example` chỉ chứa tên biến và giá trị mẫu hoặc rỗng. Khoá thật được sinh trên máy chủ (`deploy/cloud/setup.sh`) hoặc đặt trong GitHub Secrets.
- Log và thông báo lỗi không được in ra khoá, mật khẩu hay token.

## 5. Phụ thuộc

- `requirements-serve.txt` ghim phiên bản trực tiếp.
- `requirements-serve.lock` ghim toàn bộ phụ thuộc kèm hash (pip-compile). Docker và CI cài bằng `--require-hashes`.
  - Đổi một phiên bản thì sinh lại lock bằng lệnh ghi trong `tools/check_lock_sync.py`.
  - CI báo lỗi khi lock lệch với file `.txt`.
- Image nền (`python`, `caddy`, `apache/kafka`) ghim bằng digest `@sha256:...`.
- Công cụ dev nằm ở `requirements-dev.txt` và `package.json`/`package-lock.json`, không đưa vào image.

## 6. Commit và kiểm thử

- Commit tại các mốc ổn định:
  - **trước** một thay đổi rủi ro (format hàng loạt, tách file, đổi lược đồ DB);
  - **sau** khi test đạt.
- Trước khi commit, chạy đủ:
  - `python -B -m pytest -q -p no:cacheprovider tests`
  - `python -B -m pytest -q spark telecom_grpc`
  - `ruff check .` và `ruff format --check .`
- Không force-push. Không commit file chưa xem nội dung.
