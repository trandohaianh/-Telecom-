# Báo cáo sẵn sàng production: Telecom Fraud Shield v2

**Ngày:** 17/11/2026
**Nhánh:** `audit/telecom-v2-fixes` (chưa push)
**Vai trò rà soát:** Tech Lead full-stack, DevOps, QA.

**Phạm vi:**
- API chính (`src/`);
- PWA thuê bao, dashboard và trang quản trị (`static/`);
- vỏ Android (`android/`);
- triển khai (`Dockerfile`, `deploy/cloud/`, `.github/workflows/`);
- CSDL SQLite (`appdb.py`, `backend_state.py`).

EduDSS, Spark, `ram_online` và `telecom_grpc` chỉ được chạy lại test và kiểm tĩnh.

**Ràng buộc đã giữ:**
- Không đọc `.env` thật, `secrets/` hay `*.docx`; chỉ đọc các file `*.env.example` và code.
- Không in giá trị bí mật nào.

---

## 1. Kết luận

### Trạng thái chung: **Needs Attention**: sẵn sàng chạy thử (dry-run) ở chế độ shadow/demo, chưa sẵn sàng cho chế độ chặn thật

| Hạng mục | Trạng thái | Ghi chú |
|---|---|---|
| Mã nguồn và test | **Ready** | Bộ chính 604/604; Spark + gRPC 26/26; EduDSS 16/16; QA end-to-end 32/32 trên server thật có ký HMAC |
| Bảo mật ứng dụng | **Ready** (đã sửa trong đợt này) | Không có SQL injection; RBAC đủ; không có IDOR; header bảo mật, giới hạn body và 500 an toàn đã được thêm |
| Cấu hình triển khai | **Ready** sau sửa, nhưng **chưa build bằng Docker thật** | Máy này có Docker CLI và Compose v5.5.1 nhưng daemon không chạy. `docker compose config` đã qua; lệnh build và chạy ở §5 |
| SLA ≤ 30 ms | **Needs Attention** | Trên máy WSL: p99 33,4 ms ở 100 EPS, vì fsync của đĩa ảo chậm. Chủ dự án đã quyết giữ `FULL`, nên phải đo lại trên phần cứng đích (§4) |
| Chế độ chặn thật (enforce) | **Blocked**, đúng thiết kế | Cần kiểm định trên lưu lượng thật có nhãn và đặt `FRAUD_REAL_TRAFFIC_VALIDATED=1`. Toàn bộ số đo chất lượng mới chỉ trên dữ liệu mô phỏng |

---

## 2. Cách rà soát

1. **Ba nhánh rà soát song song, chỉ đọc:** backend; ops/deploy; frontend và Android.
2. **Kiểm lại từng phát hiện trên code trước khi sửa.** Lỗi Web Push trong container được hai nhánh tìm ra độc lập.
3. **Mỗi bản sửa code có test hồi quy** trong `tests/test_round7_readiness.py`: 27 test, cả 27 đều hỏng trên commit trước (`b5cc1fb`) và đều đạt sau khi sửa.
4. **Chạy lại toàn bộ bộ test và QA** trên WSL, bản sao sạch của cây làm việc, bundle `tfd-v2.6`. QA chạy trên server thật: đăng ký, OTP demo, mô phỏng tấn công, tra số, báo cáo số, người thân, đăng nhập vận hành, audit + neo, phán quyết ký HMAC, phát lại, CDR, WAV.
5. **Kiểm tĩnh:**
   - `docker compose config` (có cả profile standalone);
   - YAML của mọi workflow;
   - `bash -n` mọi script deploy;
   - `node --check` mọi khối script của 3 trang HTML và `sw.js`;
   - `pip check`.
6. **Kiểm sống trên server:**
   - `/api/app/push/key` → 200;
   - header CSP / Referrer-Policy / X-Frame-Options có mặt;
   - body 1,1 MB tới `/api/login` → 413.

---

## 3. Lỗi đã tìm và đã sửa

### 3.1 Backend

| Mức | Lỗi | Kịch bản hỏng | Sửa | File |
|---|---|---|---|---|
| **Cao** | Web Push hỏng trong container | Khoá VAPID mặc định nằm ở `/srv/app/secrets`, thuộc root, nhưng app chạy user `app`, nên `mkdir` ném PermissionError: `/api/app/push/key` luôn 500. Nếu có ghi được thì khoá cũng mất sau mỗi lần build, mọi thiết bị phải đăng ký lại | Dockerfile đặt `FRAUD_VAPID_PRIVATE_KEY_FILE=/srv/state/secrets/vapid_private.pem` (trong volume). `push._vapid()` gặp lỗi I/O thì tắt push (503, có log), không còn 500 | `Dockerfile`, `src/push.py` |
| **Cao** | Cấu hình OTP sai làm hỏng trang đăng nhập | `FRAUD_OTP_PROVIDER=sms-http` mà thiếu URL: `KeyError` trong `/api/auth/config`, mọi lần tải trang 500 | `OtpConfigError` có thông báo rõ ràng. Kiểm **một lần lúc khởi động** (sai thì không khởi động). `/api/auth/config` không bao giờ 500. Mẫu JSON hỏng cũng bị bắt lúc khởi động | `src/accounts.py`, `src/portal.py` |
| TB | Khoá tài khoản bằng cách gõ sai mật khẩu | 8 lần sai / 15 phút từ bất kỳ đâu là khoá được `viettel_admin` vô thời hạn; thuê bao cũng vậy | Chỉ đếm lần **sai**. Ngăn chặt theo (tài khoản, IP): 8 cho operator, 10 cho thuê bao. Ngăn rộng theo tài khoản: 50 / 15 phút, chống dò phân tán. Đăng nhập đúng không bị đếm | `src/accounts.py` (`check`/`record`), `src/portal.py` |
| TB | Không giới hạn kích thước body | `/api/login` không cần xác thực mà đọc toàn bộ body. Profile Caddy standalone không có `max_size` | `BodyLimitMiddleware` (ASGI) chạy trước mọi route: mặc định 1 MiB (`FRAUD_MAX_BODY_BYTES`); `/api/v1/audio` giữ giới hạn riêng. Content-Length vượt thì 413 ngay; body chunked vượt thì 413 và báo app là client đã ngắt | `src/http_hardening.py`, `src/server.py` |
| TB | Header bảo mật thiếu, và không có trên trang lỗi 500 | Không có Referrer-Policy; CSP chỉ có `frame-ancestors`. Lỗi 500 đi ra không có header, vì middleware lỗi của Starlette nằm ngoài `@app.middleware` | Thêm `Referrer-Policy: same-origin`, `Permissions-Policy`, CSP `object-src 'none'; base-uri 'self'`, HSTS khi chạy https. Handler bắt mọi exception trả JSON `{"detail": "Internal server error"}` kèm đủ header, không lộ nội dung exception | `src/http_hardening.py`, `src/server.py` |
| TB | Bộ nhớ phình không giới hạn qua `/api/app/family/respond` | `engine.inbox[guardian]` chạy cả khi từ chối, với chuỗi bất kỳ: mỗi chuỗi tạo một hàng đợi mới | Kiểm định dạng; rate limit 30 / giờ; từ chối thứ không tồn tại thì 404 và **không tạo trạng thái**; chỉ tạo inbox khi chấp nhận | `src/server.py` |
| TB | Khoá nhân viên ở chế độ demo mất khi khởi động lại | Bản công khai chạy `FRAUD_DEMO=1`, không có kho phiên bền | **Quyết định chủ dự án:** lưu cờ `disabled` vào `users.json`; admin mở khoá ở `/admin/manage` | `src/security.py` |
| TB | OTP `console` ghi mã ra log ở chế độ thật | Ai đọc được log là chiếm được số điện thoại | **Quyết định chủ dự án:** `console` chỉ chạy khi `FRAUD_DEMO=1`. Chế độ thật thì từ chối khởi động; trang đăng nhập báo "unavailable" | `src/portal.py`, `serve.py`, `.env.example` |
| Thấp | Header có ký tự ngoài ASCII gây 500 | `hmac.compare_digest(str, str)` ném TypeError (ở `/api/login` Bearer, `/metrics`, chữ ký nhà mạng) | So sánh trên bytes, không bao giờ ném lỗi | `src/security.py`, `src/observability.py` |
| Thấp | `/api/audit?n=0` trả toàn bộ log | `[-0:]` | `Query(ge=1, le=500)` | `src/server.py` |
| Thấp | `/api/evidence/latest` đọc cả file, và 500 khi dòng cuối đang ghi dở | | Đọc ngược từ cuối file theo khối, bỏ qua dòng chưa ghi xong | `src/server.py` |
| Thấp | `FRAUD_PIPELINE_DAILY_AT` sai làm thread backup chết âm thầm | | Kiểm `HH:MM` và `FRAUD_KPI_EVERY_S` lúc khởi động | `src/ops_pipeline.py` |
| Thấp | CSDL: thiếu chỉ mục; không có migration | Push, xoá dữ liệu số và usage quét toàn bảng. Lần sau thêm cột sẽ không tới được CSDL cũ | Khung migration `PRAGMA user_version`. Migration 1 thêm 4 chỉ mục: `push_subscriptions(subscriber_id)`, `guardians(guardian)`, `invites(guardian)`, `usage_events(subscriber_id)` | `src/appdb.py` |
| Thấp | Compose truyền **chuỗi rỗng** cho `${VAR:-}` | Mẫu SMS rỗng dẫn tới lỗi JSON; subject VAPID rỗng | Chuỗi rỗng được hiểu là "dùng mặc định" | `src/accounts.py`, `src/push.py` |

**Đã kiểm, không có lỗi:**
- mọi câu SQL đều có tham số;
- không có CORS (chỉ cùng nguồn) và không dùng cookie (Bearer), nên CSRF không áp dụng;
- mọi route admin có `require(...)`; dữ liệu thuê bao lọc theo số của phiên, nên không có IDOR;
- đường nhà mạng ký HMAC, chống phát lại, có giới hạn kích thước.

### 3.2 Triển khai và vận hành

| Mức | Lỗi | Sửa | File |
|---|---|---|---|
| TB | Compose không truyền nhiều biến đã có trong tài liệu (`FRAUD_REAL_TRAFFIC_VALIDATED`, `FRAUD_SMS_HTTP_*`, `FRAUD_PUSH`, `FRAUD_VAPID_SUBJECT`, …) | Truyền đủ, có mặc định an toàn (`FRAUD_SQLITE_SYNCHRONOUS` mặc định FULL, `FRAUD_REAL_TRAFFIC_VALIDATED` mặc định 0) | `deploy/cloud/docker-compose.yml` |
| TB | Caddy standalone (`caddy reverse-proxy`) thiếu HSTS, nosniff, Referrer-Policy, `max_size` | Thêm `standalone.Caddyfile` với cùng header và giới hạn như `telecom.caddy`; thêm volume `caddy_config` | `deploy/cloud/standalone.Caddyfile`, compose |
| TB | Log container lớn vô hạn trên đĩa Always Free | `json-file` 10 MB × 5 cho cả hai service | compose |
| TB | Chạy deploy tay có thể triển khai một commit CI chưa kiểm | `workflow_dispatch` bắt buộc nhập SHA 40 ký tự; job chạy trong `environment: production`; `permissions: contents: read` cho cả CI lẫn deploy | `.github/workflows/deploy.yml`, `ci.yml` |
| Thấp | `.env` được tạo trước khi `chmod 600` (có khoảng hở) | `umask 077` trước khi `cp` | `deploy/cloud/setup.sh` |
| Thấp | Image chứa log pytest của bundle; build context có `models/` và `evals/` | Thêm vào `.dockerignore` | `.dockerignore` |
| Thấp | HEALTHCHECK cố định cổng 8080; image không bắt buộc manifest mô hình | Đọc `PORT`; `FRAUD_REQUIRE_MANIFEST=1` | `Dockerfile` |
| Thấp | `py-vapid` và `cryptography` được import trực tiếp nhưng chỉ có nhờ phụ thuộc bắc cầu; `requirements.txt` (dev) thiếu `pywebpush` | Ghim `py-vapid==1.9.6`, `cryptography==50.0.2` (đúng bản đã kiểm, `pip check` sạch); thêm `pywebpush` cho dev | `requirements-serve.txt`, `requirements.txt` |
| Thấp | Biến môi trường chưa có trong tài liệu | Bổ sung vào `.env.example`: `FRAUD_SMS_HTTP_*`, `FRAUD_PUSH`, `FRAUD_VAPID_SUBJECT`, `FRAUD_REAL_TRAFFIC_VALIDATED`, `FRAUD_SQLITE_SYNCHRONOUS`, `FRAUD_MAX_BODY_BYTES`, `LOG_LEVEL`, `FORWARDED_ALLOW_IPS`, `TELECOM_PROXY_CONTAINER` | `deploy/cloud/.env.example` |

### 3.3 Frontend và Android

| Mức | Lỗi | Sửa | File |
|---|---|---|---|
| TB | PWA mở lúc offline (hoặc server 5xx) thì màn hình trắng | Luôn hiện một tab, báo lỗi mạng, tự thử lại sau 5 giây khi còn phiên | `static/app/index.html` |
| TB | Dashboard coi mọi phản hồi khác 401/403 là thành công | `api()` ném lỗi với mọi phản hồi không phải 2xx. Nhờ vậy nút tiêm tấn công và audit không còn treo ở "…", và metrics lỗi không còn làm hỏng việc đổi ngôn ngữ / giao diện | `static/admin/index.html` |
| TB | Dashboard poll 700 ms / 3 s cả khi tab ẩn, và không giãn nhịp khi lỗi | Dừng khi tab ẩn; khi lỗi giãn dần 0,7 → 10 s; poll lại ngay khi tab hiện | `static/admin/index.html` |
| TB | Đăng nhập vận hành không bắt lỗi mạng | try/catch, có thông báo; nút Google co theo bề rộng form | `static/admin/index.html` |
| TB | Trang quản trị trống trơn khi `whoami` lỗi; bảng tràn ngang ở 360 px | Hiện lỗi; `overflow-x:auto` | `static/admin/manage.html` |
| TB | **Quyết định chủ dự án:** Android bản release cho trỏ sang bất kỳ máy chủ https nào (nguy cơ phishing) | Chỉ bản debug được đổi máy chủ. Release luôn dùng `DEFAULT_SERVER` và bỏ qua giá trị đã lưu từ bản cũ | `android/.../MainActivity.kt` |
| Thấp | Số điện thoại ở bảng người thân, vài trường của dashboard và trang quản trị chèn vào HTML mà không escape; giá trị trong đường dẫn URL không mã hoá | `esc()` và `encodeURIComponent` | 3 trang HTML |
| Thấp | Thẻ cảnh báo sập với loại sự kiện lạ hoặc `anomaly_score` rỗng (làm hỏng cả feed) | Có giá trị dự phòng | `static/admin/index.html` |
| Thấp | Service worker: mở deep link khi offline không có trang dự phòng; bấm thông báo không điều hướng tới đích | Dự phòng bằng `index.html`; `client.navigate(target)`; cache `la-chan-v5` | `static/app/sw.js` |

---

## 4. Còn lại và cách khắc phục

| Mức | Việc | Vì sao chưa xong | Cách làm |
|---|---|---|---|
| **Cao trước khi go-live** | **Đo lại SLA p99 ≤ 30 ms trên phần cứng đích** | Đĩa ảo WSL fsync mất 4–25 ms. Chủ dự án quyết giữ `synchronous=FULL` (không mất phán quyết khi mất điện) | Trên máy đích, cần SSD có tụ chống mất điện, fsync < 0,5 ms. Chạy ba lần: `python -B src/verify_live_http.py --rates 50,100 --seconds 30`. Đạt khi p99 ≤ 30 ms, fail-open = 0, ≥ 95 % trong 30 ms. Chi tiết A/B ở báo cáo 05 §5 |
| **Cao trước khi go-live** | **Build và chạy image thật** | Docker daemon không chạy trên máy này | Các lệnh ở §5 (build, `id` = 10001, healthy, smoke, push key = 200) |
| TB | IP proxy tin cậy được chụp lúc deploy | Khi FieldOps tạo lại Caddy, IP của nó đổi, mọi người dùng thành một IP, và rate limit thành giới hạn chung | Gán subnet cố định cho mạng `edge` và `ipv4_address` cố định cho Caddy, rồi đặt `FORWARDED_ALLOW_IPS` theo IP đó. Cần sửa ở repo FieldOps (ngoài phạm vi) |
| TB | Backup nằm cùng volume với dữ liệu | Mất VM là mất cả hai | Thêm cron rclone (hoặc tương đương) chép `/srv/state/logs/backups` lên object storage; ghi quy trình restore |
| TB | CSP chưa có `script-src` | Ba trang dùng script nội tuyến; phiên thuê bao lưu trong `localStorage` | Tách script ra file `.js`, rồi `script-src 'self' https://accounts.google.com`. Chạy `Content-Security-Policy-Report-Only` trước một thời gian |
| TB | Lock phụ thuộc đầy đủ có hash | Cần mạng và `pip-compile` trong image CI | `pip-compile --generate-hashes requirements-serve.txt -o requirements-serve.lock`, rồi `pip install --require-hashes` trong Dockerfile |
| TB | Duyệt deploy production | `environment: production` đã khai báo nhưng người duyệt phải đặt trên GitHub | Settings → Environments → production → Required reviewers |
| TB | Android: Google Sign-In và Web Push không chạy trong WebView | Giới hạn của WebView (Google chặn OAuth nhúng; WebView không có Push API) | Ẩn nút Google khi user agent có `LaChanAndroid`; dùng FCM gốc cho thông báo. Cần SDK để build |
| Thấp | `events` trong `backend_state` không bao giờ bị dọn; `sessions` chỉ xoá khi bị truy cập | | Job retention theo `FRAUD_USAGE_RETENTION_DAYS`, chỉ xoá bản ghi đã xuất bằng chứng và đã ack |
| Thấp | EduDSS: `--forwarded-allow-ips "*"`; nginx dùng cú pháp `listen ... http2` cũ; bind 80/443 trùng với Caddy nếu chạy cùng VM | Thuộc dự án EduDSS (nay đã tách nhánh `edudss-standalone`) | Subnet cố định cho nginx; `http2 on;`; đổi cổng hoặc đứng sau Caddy chung |
| Thấp | Ảnh base ghim theo tag, chưa theo digest | | `FROM python:3.12.12-slim-bookworm@sha256:…` |
| Thấp | Android `isMinifyEnabled=false`; manifest PWA chỉ có icon SVG | Không có Android SDK | Bật R8 và kiểm `./gradlew assembleRelease`; thêm icon PNG 192/512 |

---

## 5. Checklist trước `docker compose up --build`

**A. Trên máy có Docker, kiểm image:**
```bash
cd deploy/cloud && cp .env.example .env && chmod 600 .env   # hoặc: bash setup.sh (tự sinh mật khẩu, token)
# điền TELECOM_DOMAIN; FRAUD_ADMIN_PASSWORD và METRICS_TOKEN (setup.sh tự sinh nếu để trống)
docker network create edge || true
docker compose --env-file .env config -q && docker compose --env-file .env --profile standalone config -q
docker build -t telecom-fraud:local ../..
docker run --rm telecom-fraud:local id                                   # uid=10001(app)
docker run --rm --entrypoint sh telecom-fraud:local -c 'ls -a /srv/app'  # không có secrets/, .env, logs/
docker run -d --name t -p 8080:8080 -e FRAUD_ADMIN_PASSWORD=doi-ngay-123456 -e METRICS_TOKEN=ci telecom-fraud:local
docker inspect -f '{{.State.Health.Status}}' t                           # healthy (sau ~90 s)
curl -s localhost:8080/health/ready | grep -o '"journal_durability":"FULL"'
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/api/app/push/key # 200
docker rm -f t
```

**B. Kiểm cấu hình `.env`:**
- [ ] `FRAUD_DEMO=1` cho bản công khai. `0` chỉ khi đã có lưu lượng nhà mạng và OTP thật (`FRAUD_OTP_PROVIDER=twilio` hoặc `sms-http`; `console` sẽ bị từ chối).
- [ ] `FRAUD_REAL_TRAFFIC_VALIDATED=0`, cho tới khi có kiểm định trên lưu lượng thật có nhãn.
- [ ] `FRAUD_SQLITE_SYNCHRONOUS=FULL` (quyết định 17/11/2026).
- [ ] OTP qua `sms-http`: đặt `FRAUD_SMS_HTTP_URL` (https) và chép token vào volume:
  ```bash
  docker compose cp sms_token telecom:/srv/state/secrets/sms_token
  ```
  Sau đó đặt `FRAUD_SMS_HTTP_TOKEN_FILE=/srv/state/secrets/sms_token`.
- [ ] `FRAUD_VAPID_SUBJECT=mailto:<email vận hành>`.
- [ ] Đổi mật khẩu admin ngay sau lần đăng nhập đầu.

**C. Chạy và kiểm:**
```bash
bash deploy/cloud/setup.sh            # hoặc deploy.sh cho lần cập nhật
docker compose ps                     # telecom: healthy
docker compose exec telecom env | grep -E 'FORWARDED_ALLOW_IPS|REAL_TRAFFIC|SYNCHRONOUS'
bash deploy/cloud/smoke.sh https://$TELECOM_DOMAIN
curl -sI https://$TELECOM_DOMAIN/app/ | grep -iE 'strict-transport|x-frame|content-security|referrer'
promtool check rules monitoring/alerts/telecom-rules.yml
```

**D. Sau khi chạy:**
- [ ] Grafana scrape `/metrics` bằng `METRICS_TOKEN`; các cảnh báo `TfsDown` và `TfsFailOpen` hoạt động.
- [ ] Đo SLA ba lần trên chính VM đích (§4, dòng 1). Ghi kết quả vào báo cáo 05 §5.
- [ ] Bật người duyệt cho environment `production` trên GitHub. Đặt secret `DEPLOY_KNOWN_HOSTS` (deploy dừng nếu thiếu).
- [ ] Lên lịch chép backup ra ngoài VM.

---

## 6. Danh sách file thay đổi

| File | Lý do |
|---|---|
| `src/http_hardening.py` (mới) | Giới hạn body; header bảo mật dùng chung |
| `src/server.py` | Gắn middleware; handler 500 an toàn; `family/respond`; `api_audit` giới hạn n; đọc ngược file bằng chứng |
| `src/portal.py` | Rate limit chỉ đếm lần sai; kiểm OTP lúc khởi động; chặn OTP console ở chế độ thật |
| `src/accounts.py` | `check`/`record` cho limiter; `OtpConfigError`; mặc định cho chuỗi rỗng |
| `src/security.py` | So sánh bí mật không ném lỗi; lưu khoá nhân viên ở demo |
| `src/observability.py` | So sánh token `/metrics` trên bytes |
| `src/push.py` | Không 500 khi không ghi được khoá; mặc định cho chuỗi rỗng |
| `src/appdb.py` | Migration `user_version` + 4 chỉ mục |
| `src/ops_pipeline.py` | Kiểm lịch và chu kỳ lúc khởi động |
| `serve.py` | Tài liệu OTP |
| `Dockerfile`, `.dockerignore` | Đường dẫn khoá VAPID; bắt buộc manifest; HEALTHCHECK theo `PORT`; bỏ log khỏi image |
| `deploy/cloud/docker-compose.yml`, `standalone.Caddyfile` (mới), `.env.example`, `setup.sh` | Biến môi trường; xoay vòng log; Caddy standalone có header; `umask` |
| `.github/workflows/ci.yml`, `deploy.yml` | Quyền tối thiểu; deploy tay cần SHA; environment production |
| `requirements-serve.txt`, `requirements.txt` | Ghim phụ thuộc import trực tiếp; `pywebpush` cho dev |
| `static/app/index.html`, `static/app/sw.js`, `static/admin/index.html`, `static/admin/manage.html` | Xử lý lỗi, polling, escape, offline, thông báo |
| `android/.../MainActivity.kt` | Chỉ bản debug được đổi máy chủ |
| `tests/test_round7_readiness.py` (mới) | 27 test hồi quy |
