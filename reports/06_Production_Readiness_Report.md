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
| Mã nguồn và test | **Ready** | Bộ chính 606/606; Spark + gRPC 26/26; EduDSS 16/16; QA end-to-end 32/32 trên server thật có ký HMAC |
| Bảo mật ứng dụng | **Ready** (đã sửa trong đợt này) | Không có SQL injection; RBAC đủ; không có IDOR; header bảo mật, giới hạn body và 500 an toàn đã được thêm |
| Cấu hình triển khai | **Ready**: **đã build và chạy thật bằng Docker** (§7) | `docker compose build` đạt (390 s, image 1,38 GB, `pip check` sạch). Container healthy sau 88 s; smoke 7/7; QA 32/32; dữ liệu còn nguyên sau khi build lại và tạo lại container. Đợt chạy thật tìm thêm một lỗi (bằng chứng demo không xoay vòng) và đã sửa |
| SLA ≤ 30 ms | **Needs Attention** | Trên WSL: p99 33,4 ms ở 100 EPS. **Trong container Docker Desktop:** fsync p50 21 ms (p99 40 ms), nên một commit `FULL` mất khoảng 37 ms và SLA là **không thể** trên môi trường này. Chủ dự án quyết giữ `FULL`, nên phải chốt trên phần cứng đích có fsync dưới 1 ms (§4, §7) |
| Chế độ chặn thật (enforce) | **Blocked**, đúng thiết kế | Cần kiểm định trên lưu lượng thật có nhãn và đặt `FRAUD_REAL_TRAFFIC_VALIDATED=1`. Toàn bộ số đo chất lượng mới chỉ trên dữ liệu mô phỏng |

---

## 2. Cách rà soát

1. **Ba nhánh rà soát song song, chỉ đọc:** backend; ops/deploy; frontend và Android.
2. **Kiểm lại từng phát hiện trên code trước khi sửa.** Lỗi Web Push trong container được hai nhánh tìm ra độc lập.
3. **Mỗi bản sửa code có test hồi quy** trong `tests/test_round7_readiness.py`: 29 test, cả 29 đều hỏng trên commit trước (`b5cc1fb`) và đều đạt sau khi sửa.
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
| **Cao trước khi go-live** | **Đo lại SLA p99 ≤ 30 ms trên phần cứng đích** | Đĩa ảo WSL fsync mất 4–25 ms; Docker Desktop 21–40 ms. Chủ dự án quyết giữ `synchronous=FULL` (không mất phán quyết khi mất điện) | Trên máy đích, cần SSD có tụ chống mất điện, fsync < 0,5 ms. Chạy ba lần: `python -B src/verify_live_http.py --rates 50,100 --seconds 30`. Đạt khi p99 ≤ 30 ms, fail-open = 0, ≥ 95 % trong 30 ms. Chi tiết A/B ở báo cáo 05 §5 |
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
| `src/backend_evidence.py`, `src/server.py` (đợt Docker) | Xoay vòng file bằng chứng ở chế độ demo (`FRAUD_DEMO_EVIDENCE_MAX_MB`, mặc định 256); kiểm tăng dần bắt đầu lại sau khi xoay |
| `tests/test_round7_readiness.py` (mới) | 29 test hồi quy |

---

## 7. Dry run Docker thật (17/11/2026, Docker Desktop 29.8.0, Compose v5.5.1, VM 8 GB / 8 CPU)

**Cách chạy:**
- Project riêng `telecom-dryrun`, không đụng tới dry run EduDSS chạy song song (`edudss-saas`).
- File env **tạm**, secret sinh ngẫu nhiên, không dùng `.env` thật.
- File override: mạng riêng `telecom-dryrun-edge` thay cho mạng `edge` ngoài; cổng chỉ mở trên `127.0.0.1:18090`.
- Dọn bằng `down -v` khi xong: 0 container, 0 volume, 0 mạng còn lại; file env tạm đã xoá. Image `telecom-fraud:local` được giữ làm cache; xoá bằng `docker image rm telecom-fraud:local`.

### 7.1 Kết quả

| Bước | Kết quả |
|---|---|
| `docker compose build` | **Đạt**: 390 s lần đầu, 42 s khi build lại; image 1,38 GB; `pip check` sạch. Cảnh báo numpy 2.4.0 bị "yank" đã biết: đây là bản đã kiểm với mô hình |
| User trong image | `uid=10001(app)`; `/srv/app` chỉ đọc (`touch` bị từ chối) |
| Nội dung image | Chỉ có `requirements-serve.txt`, `runtime/`, `serve.py`, `src/`, `static/`. Không có `secrets/`, `.env`, `*.docx`, `network_link.key`, `users.json`; log pytest của bundle đã bị loại |
| Biến trong image | `FRAUD_VAPID_PRIVATE_KEY_FILE=/srv/state/secrets/…`, `FRAUD_REQUIRE_MANIFEST=1`, CSDL và log đều nằm dưới `/srv/state` |
| Healthcheck | **healthy** sau 88 s lần đầu, 44 s khi khởi động lại; RAM 664 MiB, ổn định ở **774–778 MiB suốt 18 phút** (không rò rỉ) trên giới hạn 2 GiB |
| `/health/ready` | `tfd-v2.6`, manifest `verified`, `journal_durability: FULL`, demo bật |
| `smoke.sh` | **7/7** (app, admin, ready, auth config, metrics 401 không token / 200 có token, demo ẩn danh tắt 403) |
| Kiểm cứng hoá | `/api/app/push/key` → **200** (trước bản sửa là 500); đủ 5 header bảo mật; body 1,1 MB → **413**; Bearer có ký tự ngoài ASCII → **401** |
| QA end-to-end (32 luồng) | **32/32** ở các lượt sau. Lượt **đầu tiên** sau mỗi lần khởi động hỏng 1 luồng (`1 ms reorder … scored`: trả `PENDING`). Xem §7.2 |
| Dữ liệu sau khi build lại và tạo lại container | Khoá VAPID và `users.json` **giữ nguyên** (cùng SHA-256), nên các thiết bị đã đăng ký push không phải đăng ký lại |
| Xoay vòng bằng chứng demo (ngưỡng thử 8 MB) | File cũ 21,7 MB thành `evidence_demo.1.jsonl`; file mới bắt đầu chuỗi từ GENESIS. **Cả hai chuỗi hợp lệ** (`verify`: 25 651 và 1 167 bản ghi) |

### 7.2 Phát hiện mới từ đợt chạy thật

1. **Bằng chứng demo tăng khoảng 1,25 GB/ngày và không xoay vòng (đã sửa).**
   - Đo được 0,9 MB/phút.
   - Lúc khởi động, `DurableEvidenceLog` kiểm và đọc **cả file hai lần**, nên vài tuần sau một lần khởi động lại sẽ OOM; với `restart: unless-stopped`, container rơi vào vòng khởi động lại liên tục.
   - **Sửa:** chỉ ở chế độ demo (dữ liệu mô phỏng), xoay khi đạt `FRAUD_DEMO_EVIDENCE_MAX_MB` (mặc định 256 MB), giữ 1 bản cũ. Chế độ thật **không xoay**, vì chính sách lưu giữ bằng chứng thật cần chủ dự án quyết.
   - Test: `test_demo_evidence_rotates_and_both_chains_stay_valid`, `test_only_demo_mode_rotates_evidence`.
2. **Trên Docker Desktop, SLA `FULL` không thể đạt.** Đo trong container sạch, từ cùng image, không có server demo:

   | Phép đo | Kết quả |
   |---|---|
   | fsync thô trong container | p50 **21,05 ms**, p99 40,2 ms (WSL trực tiếp: 3,4 ms) |
   | `verify_live_http` 50 / 100 EPS, `FULL` | p50 37,6 / 37,4 ms; 0 % trong 30 ms |
   | Cùng phép đo với `NORMAL` (chỉ để chẩn đoán, **không** đổi mặc định) | p50 10,7 / 10,5 ms; 94,8 / 94,7 % trong 30 ms |

   **Kết luận:** chi phí là một lần fsync cố định cho mỗi commit trên overlay filesystem của Docker Desktop. Docker Desktop trên Windows **không** phải môi trường hợp lệ để chốt SLA. Trên VM đích, đo fsync trên chính volume trạng thái trước (cần p99 dưới 1 ms; nếu cao hơn, chọn block volume hiệu năng cao hơn hoặc đĩa local NVMe):
   ```bash
cat > fsync_probe.py <<'EOF'
import os, tempfile, time
f = open(tempfile.mkdtemp(dir=".") + "/x", "wb"); t = []   # in the current dir = the state volume
for _ in range(100):
    f.write(b"x" * 512); f.flush(); a = time.perf_counter(); os.fsync(f.fileno()); t.append((time.perf_counter() - a) * 1000)
t.sort(); print("fsync p50 %.2f ms p99 %.2f ms" % (t[50], t[98]))
EOF
docker run --rm -v "$PWD/fsync_probe.py:/tmp/p.py:ro" -v telecom_telecom_state:/srv/state --entrypoint sh telecom-fraud:local \
  -c 'cd /srv/state && python /tmp/p.py'      # đo trên chính volume trạng thái
   ```
3. **Chế độ demo: khoảng 2 % phán quyết API vượt hạn 30 ms** (6 / 290 là `PENDING` trong 150 s, rải đều).
   - Nguyên nhân là luồng phát lại dashboard 300 sự kiện/giây giữ `det_lock` và tranh GIL với API trên cùng tiến trình; commit tối đa chỉ 0,66 ms vì demo dùng kho trong RAM.
   - Giả thuyết "chấm lại sau fail-open" đã bị loại: `rescored = 0`.
   - Ở shadow mode, `PENDING` tức là cho qua và không hành động, nên **vô hại**. Chế độ thật (`FRAUD_DEMO=0`) không có luồng phát lại này.
   - Đây là lý do của luồng QA thỉnh thoảng hỏng ở lượt đầu. **Không phải lỗi production.**
4. **Công cụ đo SLA cần thư mục ghi được.** `verify_live_http.py` ghi vào `ROOT/data`, mà thư mục này chỉ đọc trong image. Muốn chạy trong container thì chép sang `/tmp` trước:
   ```bash
   docker run --rm --entrypoint sh telecom-fraud:local -c 'cp -r /srv/app /tmp/app && cd /tmp/app && python -B src/verify_live_http.py --rates 50,100 --seconds 30 --model-dir /tmp/app/runtime/artifacts_v22/models'
   ```
5. **`smoke.sh` dưới Git Bash trên Windows.** Đặt `MSYS_NO_PATHCONV=1` thì `curl -H @/tmp/…` không đọc được file header. Chỉ là lỗi môi trường: không đặt biến đó thì đạt 7/7, và trên VM Linux không xảy ra.

### 7.3 Trạng thái sau dry run

**Needs Attention**, không đổi:
- **Đã sẵn sàng:** build, chạy, bảo mật, dữ liệu sau khi khởi động lại, cấu hình demo. Đã kiểm bằng Docker thật.
- **Còn chặn go-live:** đo SLA trên phần cứng đích có fsync dưới 1 ms (giữ `FULL`).
- **Chế độ enforce:** vẫn khoá đúng thiết kế, cho tới khi có kiểm định trên lưu lượng thật.

---

## 8. Demo Kafka: đo thật trên laptop này, Docker Desktop so với WSL2 trực tiếp (18/11/2026)

**Kết luận ngắn:**
- **SLA p99 ≤ 30 ms: KHÔNG đạt ở bất kỳ tốc độ nào**, trên cả hai môi trường.
- **50 000 sự kiện/s: KHÔNG đạt**. Mức cao nhất đo được là **4 240 sự kiện/s**, trên WSL, pipeline A1–A3, 4 partition.
- Trên WSL, **p50** xuống dưới 30 ms ở 100–500 sự kiện/s, nhưng p99 vẫn từ 260 ms trở lên.
- **Docker Desktop** chậm hơn WSL khoảng **5–7 lần về độ trễ** ở cùng tốc độ, chủ yếu vì fsync của nó mất 22 ms, so với 4–6 ms trên WSL.

> **Đính chính số đo lần đầu.** Bảng Docker công bố ở commit `d2a207f` **sai về độ trễ**. Code đo khi đó gọi `consume(num_messages=1000, timeout=0.5)`, mà lệnh này của confluent-kafka **chặn cho tới khi đủ 1 000 bản tin hoặc hết 0,5 s**. Sink cũng gọi `consume(5000, 0.2)`. Hậu quả: mỗi lô phải chờ thêm tới 0,5 s, và sink cộng thêm tới 0,2 s vào số đo. Lỗi này gây ra con số "p50 khoảng 0,85–1 s ở 50 % tải". Đã sửa: lấy bản tin đầu với `num_messages=1`, vét phần sẵn có với `timeout=0`, rồi chỉ chờ thêm đúng `max_wait`. Có test với consumer mô phỏng đúng ngữ nghĩa chặn này. Số cũ được giữ ở `deploy/kafka-demo/results/docker_v1_blocking_consume/` để đối chiếu. **Mọi bảng dưới đây đo lại bằng code đã sửa, cùng một bản code cho cả hai môi trường.**

### 8.1 Cách dựng

**Không có engine thứ hai** (`src/kafka_pipeline.py`, `deploy/kafka-demo/`):
- **Chấm điểm:** `scaleout.ShardedEngine` sẵn có, gồm router, N tiến trình chấm và side update chéo khoá. Số shard bằng số partition.
- **Ghi bền:** `BackendState` (SQLite WAL, `synchronous=FULL`). **Một giao dịch, tức một fsync, cho mỗi micro-batch.**
- **Thứ tự giao nhận:** at-least-once. Offset chỉ được commit sau khi lô đã ghi bền; bản tin gửi lại không bị chấm lần hai.
- **Đo độ trễ đầu-cuối:** từ lúc producer **gửi** tới lúc sink **nhận** phán quyết trên `fraud.verdicts`, gồm cả thời gian chờ gom lô, linger của Kafka và chặng broker.
- **Producer:** gửi theo lịch tuyệt đối ở tốc độ đặt trước, key = số chủ gọi, `acks=all`, idempotent.

**Hai pipeline được đo** (chọn bằng `--pipeline`):
- **`sequential`:** bản demo đầu. Lần lượt nhận, chấm, ghi bền (ghi 2 lần mỗi sự kiện: `reserve` rồi `finish`), gửi phán quyết, commit offset. Lô tối đa 1 000, chờ 5 ms.
- **`pipelined` (A1–A3):**
  - **A1:** chấm lô N+1 trong lúc lô N đang fsync. Một thread ghi duy nhất, xử lý FIFO, giữ tối đa 1 lô đang bay. Thread đó ghi bền, rồi gửi phán quyết, rồi commit **đúng offset của lô đó**. Kiểm trùng xem map "đang bay" trước, rồi CSDL sau qua kết nối chỉ đọc riêng, nên không phải chờ fsync và không bao giờ chấm trùng.
  - **A2:** nhật ký ghi **một lần** mỗi sự kiện (`record_many`), bỏ nhãn chỉ có trong mô phỏng.
  - **A3:** giới hạn lô theo ngân sách độ trễ (mặc định 10 ms), chỉ tăng lô khi tồn đọng kéo dài. Chờ 1 ms.

**Môi trường đo:**
- **Docker Desktop 29.8.0** (VM 8 GB / 8 CPU): broker `apache/kafka:3.8.0` trong container; producer, consumer và nhật ký trong container demo, ghi lên volume Docker.
- **WSL2 trực tiếp** (Ubuntu, Python 3.12, 8 lõi): **cùng** bản Kafka 3.8.0 dạng binary, chạy trên JRE Temurin 17.0.21. Cả hai file đã kiểm SHA-512 / SHA-256 và đã xoá sau khi đo. Broker chỉ lắng nghe trên `127.0.0.1`. Mọi dữ liệu nằm trên ext4 của WSL, không dùng `/mnt/d`.
- Chạy lần lượt, **không** song song. Mỗi lượt 50 000 sự kiện của cửa sổ test (lượt quét dùng 20 000). Stack EduDSS (Postgres) vẫn chạy nền.

### 8.2 ĐÃ ĐO: fsync của đĩa ghi nhật ký

| Môi trường | fsync p50 | fsync p99 |
|---|---|---|
| Docker Desktop, volume `/srv/state` | **22,4 ms** | 45,0 ms |
| WSL2 ext4, trước lượt đo | **3,8 ms** | 9,3 ms |
| WSL2 ext4, sau lượt đo (broker cũng ghi lên cùng đĩa) | 6,1 ms | 16,5 ms |

### 8.3 ĐÃ ĐO: thông lượng tối đa (producer gửi khoảng 20 000 sự kiện/s, vượt khả năng xử lý)

| Partition = shard | Docker, sequential | Docker, pipelined | **WSL, sequential** | **WSL, pipelined** |
|---|---|---|---|---|
| 1 | 1 975 | 2 611 | 2 577 | 3 127 |
| 2 | 2 498 | 2 690 | 3 278 | 3 142 |
| 4 | 2 681 | 3 507 | 3 911 | **4 240** |

(đơn vị: sự kiện/s)

**Đọc bảng:**
- Ở trạng thái bão hoà, lô luôn đầy khoảng 900–1 000 sự kiện. Ghi bền mỗi lô: Docker 137–180 ms, WSL 76–134 ms. Chấm điểm mỗi lô: 96–264 ms.
- **A1–A3 tăng thông lượng** ở 4 partition: +31 % trên Docker, +8 % trên WSL. Với 1 partition tăng +32 % và +21 %. Riêng WSL ở 2 partition thì tương đương (3 142 so với 3 278, nằm trong độ nhiễu).
- Khi bão hoà, hàng đợi dồn ứ: p99 lên 8–21 s, lag khoảng 40–46 nghìn. Đây là kỳ vọng khi gửi vượt khả năng xử lý, không phải độ trễ vận hành.

### 8.4 ĐÃ ĐO: độ trễ đầu-cuối ở khoảng 50 % thông lượng của từng cấu hình

| Môi trường / pipeline | Partition | Tốc độ gửi | p50 | p95 | p99 | Lô TB | Lag tối đa |
|---|---|---|---|---|---|---|---|
| Docker / sequential | 1 | 987 | 173 ms | 305 ms | 392 ms | 117 | 206 |
| Docker / sequential | 4 | 1 340 | 184 ms | 349 ms | 754 ms | 172 | 466 |
| Docker / pipelined | 1 | 1 305 | 368 ms | 516 ms | 619 ms | 87 | 577 |
| Docker / pipelined | 4 | 1 753 | 672 ms | 1 167 ms | 1 303 ms | 171 | 1 608 |
| **WSL / sequential** | 1 | 1 288 | **42 ms** | 90 ms | 182 ms | 39 | 74 |
| WSL / sequential | 4 | 1 955 | 48 ms | 264 ms | 485 ms | 70 | 628 |
| **WSL / pipelined** | 1 | 1 563 | **33 ms** | 185 ms | 735 ms | 18 | 794 |
| **WSL / pipelined** | 4 | 2 120 | 95 ms | 156 ms | **191 ms** | 41 | 191 |

(Đủ cả 2 partition trong `deploy/kafka-demo/results/`.)

Mức "50 %" của pipeline A1–A3 cao hơn của bản tuần tự, vì nó có thông lượng tối đa cao hơn. Vì vậy so sánh công bằng là ở **cùng tốc độ gửi**, trong §8.5.

### 8.5 ĐÃ ĐO: quét tốc độ thấp và lô nhỏ (pipelined, 4 partition, 20 000 sự kiện): có chỗ nào p99 ≤ 30 ms không?

| Môi trường | Tốc độ | Ngân sách lô / chờ | p50 | p95 | p99 | Lô TB | Ghi bền / lô |
|---|---|---|---|---|---|---|---|
| WSL | 100 | 2 ms / 0,5 ms | **22 ms** | 55 ms | 1 881 ms | 1,0 | 6,0 ms |
| WSL | 200 | 2 ms / 0,5 ms | **26 ms** | 45 ms | 260 ms | 1,4 | 5,7 ms |
| WSL | 200 | 5 ms / 1 ms | **24 ms** | 60 ms | 317 ms | 1,3 | 4,7 ms |
| WSL | 500 | 5 ms / 1 ms | **30 ms** | 47 ms | 276 ms | 3,9 | 6,8 ms |
| WSL | 1 000 | 5 ms / 1 ms | 40 ms | 131 ms | 388 ms | 10,9 | 9,4 ms |
| WSL | 1 000 | 10 ms / 1 ms | 37 ms | 68 ms | 343 ms | 10,0 | 8,5 ms |
| Docker | 200 | 5 ms / 1 ms | 148 ms | 222 ms | 348 ms | 8,0 | 38,3 ms |
| Docker | 1 000 | 10 ms / 1 ms | 368 ms | 538 ms | 615 ms | 65,6 | 62,2 ms |

**Trả lời câu hỏi "có đạt SLA ở tốc độ nào không":**
- **p99 ≤ 30 ms: không**, ở mọi tốc độ đã thử, trên cả hai môi trường. Thấp nhất là p99 260 ms (WSL, 200 sự kiện/s).
- **p50 ≤ 30 ms:** đạt trên WSL ở **100, 200 và 500 sự kiện/s**. Không đạt trên Docker ở bất kỳ tốc độ nào.
- **Đuôi p99** (vài trăm ms tới 1,9 s) xuất hiện cả khi lô chỉ có 1 sự kiện và ghi bền mất 6 ms. Vậy nó **không** đến từ nhật ký. Nguồn nhiều khả năng là các cú dừng lác đác trong VM dùng chung: lịch CPU của WSL/Windows, cùng tiến trình JVM của broker và Python trên cùng 8 lõi, các lần fsync của broker. Nguyên nhân chưa được tách riêng từng phần. Đây là việc cần làm khi đo trên máy đích.

### 8.6 Chất lượng, fail-open, lag (ĐÃ ĐO)

- **Fail-open = 0** ở mọi lượt. Đường lô không có hạn chót theo từng sự kiện, nên cái giá phải trả hiện ra ở độ trễ.
- **Phán quyết so với replay trong tiến trình cùng số shard:** 100 % ở 31/32 lượt; **99,998 %** (1 / 50 000) ở lượt WSL pipelined 2 partition. Lý do: `ShardedEngine` áp side update của cả lô trước khi chấm, và điều này được ghi rõ trong tài liệu là "nhìn trước tối đa một lô". Nên kết quả phụ thuộc vào ranh giới lô, mà ranh giới lô do A3 tự chọn.
- **Chỉ số chất lượng** của mọi lượt 50 000 sự kiện giống hệt replay trong tiến trình. Giữa 1 shard và từ 2 shard trở lên vẫn có 1 trên 71 sự kiện gian lận đổi BLOCK → FLAG; sai khác này đến từ scale-out trong tiến trình, không do Kafka.
- **Lag tối đa ở khoảng 50 % tải:** WSL 74–794, Docker 206–1 608 bản tin.

### 8.7 Docker Desktop chiếm bao nhiêu phần của nút thắt

| So ở cùng tốc độ (pipelined, 4 partition) | Docker | WSL | Hệ số |
|---|---|---|---|
| 200 sự kiện/s: p50 / p99 | 148 / 348 ms | 24 / 317 ms | p50 chậm **6,2 lần**; p99 gần bằng nhau |
| 1 000 sự kiện/s: p50 / p99 | 368 / 615 ms | 37 / 343 ms | p50 chậm **10 lần**; p99 chậm 1,8 lần |
| Ghi bền một lô nhỏ | 38–62 ms | 5–9 ms | **khoảng 7 lần** |
| fsync p50 | 22,4 ms | 3,8–6,1 ms | **khoảng 4–6 lần** |
| Thông lượng tối đa (4 partition) | 3 507 sự kiện/s | 4 240 sự kiện/s | 1,2 lần |

**Kết luận:**
- **p50 / trung vị:** fsync của Docker Desktop là **nút thắt chính**. Ra khỏi Docker Desktop, p50 giảm 6–10 lần.
- **Thông lượng tối đa:** khác biệt chỉ khoảng 20 %, vì ở đó chấm điểm và tranh CPU trong VM chiếm phần lớn.
- **Đuôi p99:** vẫn còn cả trên WSL, nên đó là vấn đề của máy dùng chung, không riêng Docker.

### 8.8 NGOẠI SUY cho 50 000 sự kiện/s (chưa đo, không phải cam kết)

- **Chấm điểm:** khoảng 0,089 ms mỗi sự kiện sở hữu, cộng 0,0016 ms cho mỗi side update (`docs/SCALE_OUT.md`). Như vậy cần khoảng **5–6 lõi chỉ để chấm**, cộng router, consumer và producer: ước **8–12 lõi vật lý riêng**.
- **Ghi bền với `FULL`:** phần đo được tốt nhất trên WSL khoảng 76–134 ms cho mỗi lô 1 000, tức khoảng 0,1 ms mỗi sự kiện. Mức này giới hạn **một** nhật ký ở khoảng 7,5–13 nghìn sự kiện/s nếu chấm không tốn thời gian. 50 000 sự kiện/s cần:
  - nhiều nhật ký song song (mỗi nhóm partition một nhật ký, tức nhiều instance consumer);
  - hoặc NVMe có fsync dưới 1 ms cùng bản ghi gọn hơn nữa.
- **Mở rộng ngang:** cần topic side update giữa các instance (thiết kế ở `docs/SCALE_OUT.md`). **Demo chưa dựng phần này**; nó chạy một consumer với N tiến trình chấm trên một máy.
- **SLA 30 ms cho đường Kafka:** chỉ khả thi khi **p99** của cả chuỗi (fsync + chấm + broker) nằm dưới khoảng 20 ms. Muốn biết thì phải đo trên máy riêng, không đo trên laptop dùng chung. Đường chặn trước khi đổ chuông nên tiếp tục đi qua HTTP từng sự kiện (§7).

### 8.9 Còn lại

1. Đo `deploy/kafka-demo` trên VM Linux đích: fsync của volume (lệnh ở §7.2), broker và consumer trên lõi riêng. Mỗi cấu hình chạy ≥ 3 lần × ≥ 5 phút, báo trung vị kèm khoảng dao động.
2. Tách nguồn đuôi p99: đo GC của Python, độ trễ ghi của broker, thời gian chờ lịch CPU.
3. Nhiều instance consumer, mỗi instance một nhật ký, cùng topic side update.

**Dọn dẹp:**
- Docker: `down -v` riêng project `telecom-kafka-demo`, không còn container hay volume nào.
- WSL: đã dừng broker (không còn tiến trình Java, cổng 9092/9093 đã trống) và xoá `~/kafka-wsl` (Kafka, JRE, dữ liệu).
- Docker Desktop và EduDSS vẫn chạy.

**Số liệu gốc:** `deploy/kafka-demo/results/{docker,wsl,docker_v1_blocking_consume}/`.
