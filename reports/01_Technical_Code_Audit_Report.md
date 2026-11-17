# Báo cáo kiểm toán mã nguồn

**Ngày:** 17/11/2026
**Thư mục:** `D:\Du_An_1_Telecom_AI_Full\v2`, nhánh hiện tại, HEAD `b6f0aee`.
**Trạng thái mã:** các bản sửa của ba vòng (F1–F18, G1–G27, H1–H12) được commit trên nhánh `audit/telecom-v2-fixes` (chưa push).
**Báo cáo thay thế:** bản ngày 13/11/2026. Bản đó viết khi thư mục chưa có Git. Phần sửa mTLS của gRPC trong bản cũ vẫn còn trong mã và đã được kiểm lại.

Báo cáo đi kèm:
- [02_Deep_Technical_Explanation_Report.md](02_Deep_Technical_Explanation_Report.md): giải thích luồng xử lý và thuật toán.
- [he-thong/](he-thong/README.md): một báo cáo cho mỗi hệ thống con.
- [04_Danh_gia_da_vai_tro_va_thuat_toan.md](04_Danh_gia_da_vai_tro_va_thuat_toan.md): vòng 4, đánh giá đa vai trò (CEO, ML, staff, bảo mật, UX, QA), bảng thuật toán, bản sửa R4-*, bundle tfd-v2.4 và kết quả đo gộp 11 cửa sổ. §10 của báo cáo đó là vòng 5 (R5-*): D14 checkpoint tăng dần, tách cổng an toàn/chất lượng, Web Push + OTP SMS HTTP, cầu nối nhà mạng, UX, bundle tfd-v2.5. §11 là vòng 6 (R6-*): sửa SIM box / min-window bằng đặc trưng farm đồng vị trí, gỡ ba nguồn dừng ở đường phục vụ, bundle tfd-v2.6.
- [05_Thuat_toan_ly_do_va_cong_thuc.md](05_Thuat_toan_ly_do_va_cong_thuc.md): mọi thuật toán của 3 dự án, vì sao chọn, công thức, đã phù hợp chưa.

---

## 1. Phạm vi và độ tin cậy

### Đã xem

| Nhóm | Nội dung |
|---|---|
| Ứng dụng chính | `src/` (48 module), `serve.py`, `run_live.py`, `run_demo.py`, `static/` |
| Hệ thống độc lập | `ram_online/`, `telecom_grpc/`, `spark/`, `edu_dss/`, `android/` |
| Kiểm thử, triển khai | `evals/`, `docker/`, `deploy/`, `monitoring/`, `.github/workflows/`, `Dockerfile`, `.dockerignore`, `requirements*.txt` |

### Cách làm

1. Lập bản đồ entry point và chạy toàn bộ bộ test để lấy mốc.
2. Năm người review chạy song song, chia theo vùng, chỉ đọc mã:
   - xác thực và phân quyền;
   - runtime suy luận và trạng thái;
   - ML và giọng nói;
   - Spark, RAM online và gRPC;
   - EduDSS và cấu hình triển khai.
3. Tôi tự đọc lại mã cho từng phát hiện trước khi sửa. Chỉ sửa lỗi có kịch bản hỏng cụ thể.
4. Mỗi lỗi quan trọng có một test hồi quy. Test đó được chạy trên bản `git archive HEAD` (mã gốc) và phải **thất bại** ở đó, rồi mới chạy trên mã đã sửa và phải **qua**.

### Không xem

| Mục | Lý do |
|---|---|
| `secrets/`, mọi `.env*` (trừ `.env.example`), `*.docx` | Tệp bí mật và cá nhân, không mở |
| `logs/`, `backup_tfd-v2.0/` | Sản phẩm cục bộ, không sửa |
| `docs/build/node_modules/` | Mã bên thứ ba |

### Độ tin cậy

| Mức | Phạm vi |
|---|---|
| **Cao** | 18 lỗi vòng 1 (F), 27 mục vòng 2 (G) và 12 mục vòng 3 (H): mỗi mục có test thất bại trên mã trước vòng đó. Riêng G4 là thay đổi hiệu năng giữ nguyên hành vi. Một số test của G22/G23/G26/G27 là test tĩnh trên file cấu hình. |
| **Trung bình** | Mục 4 (còn mở sau vòng 3): phần lớn tôi đã đọc lại mã. Mục nào chỉ dựa trên phân tích tĩnh của người review thì ghi rõ "chưa tái hiện". |
| **Không kiểm chứng được** | Docker build, Redis thật, Kafka, Spark cluster, Android build, deploy thật. Lý do: máy không có Docker daemon, Redis, Java hay Android SDK đang chạy. |

---

## 2. Kiến trúc

Thư mục gồm **bảy hệ thống** với vòng đời riêng. Chi tiết từng hệ thống: [he-thong/README.md](he-thong/README.md).

| Hệ thống | Entry point | Lưu trữ | Gắn với app chính? |
|---|---|---|---|
| Telecom Fraud Shield (API chính) | `serve.py` → `server:app`; `run_demo.py`, `run_live.py` | SQLite WAL (`backend_state.py`), SQLite app (`appdb.py`), JSONL bằng chứng và audit | — |
| Pipeline giọng nói | `src/voice_api.py` (mount vào app chính) | Dùng chung state chính | Có |
| RAM online (Bloom + Redis) | `ram_online/api.py`, `streaming.py` | Redis policy và Redis phân tích | Không |
| gRPC edge | `telecom_grpc/server.py` | Redis policy qua `ram_online` | Không |
| Spark/Kafka features | `spark/spark_features.py` | Checkpoint file | Không |
| EduDSS | `edu_dss/backend/app/main.py`, `ui/app.py` | Redis, Celery | Không |
| Android | `android/` (WebView bọc `/app/`) | — | Chỉ là client |

Luồng chính:

```
Nhà mạng (bản sao báo hiệu) ──HMAC──► POST /api/v1/verdict ─► validate_event ─► InferenceRuntime (owner thread)
     IngestGuard (trùng / trễ) ─► GuardedDetector (deadline 30 ms, breaker, luật dự phòng)
     ─► Detector (LightGBM + IsolationForest + TF-IDF/LR) ─► SQLite journal (BEGIN IMMEDIATE) ─► bằng chứng JSONL
     ─► khuyến nghị (chỉ ở chế độ enforce; mặc định là shadow)
Thuê bao (PWA /app/) ─► /api/app/*  (phiên thuê bao, số lấy từ phiên)
Nhân viên (/admin/)  ─► /api/* (phiên vận hành, vai trò viewer < analyst < admin)
```

---

## 3. Lỗi đã sửa

Ký hiệu cột Test:
- **T** = `tests/test_audit_regressions_20261117.py`
- **R** = `ram_online/test_overload_breaker.py`
- **E** = `edu_dss/backend/tests/test_cache_keys.py`

Mọi test trong bảng đều **thất bại trên HEAD** và **qua trên mã đã sửa**.

| # | Mức | Vị trí | Kịch bản hỏng | Cách sửa | Test |
|---|---|---|---|---|---|
| F1 | **Cao** | `src/accounts.py` `otp_verify` | Lệnh `UPDATE attempts+1` chạy rồi `raise HTTPException` ngay trong `db.tx()`. `tx()` bắt exception và ROLLBACK, nên lần đếm sai bị xoá. Kẻ tấn công đoán mã 6 số không giới hạn (route verify cũng không có rate limit) và chiếm số của nạn nhân, kể cả inbox SMS và cảnh báo gia đình. | Ghi lỗi vào biến, COMMIT, rồi mới raise. Lần sai thứ 5 xoá mã. | T `test_otp_wrong_attempts_are_persisted…` |
| F2 | **Cao** | `src/accounts.py` `google` | Kẻ tấn công đăng ký `victim@gmail.com` bằng mật khẩu của mình (email không được xác minh). Khi nạn nhân đăng nhập Google, hệ thống gắn Google vào chính hàng đó, nên kẻ tấn công vẫn đăng nhập bằng mật khẩu và đọc số, inbox của nạn nhân. | Vẫn liên kết như cũ (hành vi test hiện có giữ nguyên), nhưng xoá `password_hash` và mọi phiên của hàng đó vì Google đã chứng minh quyền sở hữu email. | T `test_google_link_removes_preregistered…` |
| F3 | Trung bình | `src/security.py` `require` | Vai trò được chép vào phiên lúc đăng nhập. Admin bị hạ xuống viewer vẫn giữ quyền admin tối đa 8 giờ (đổi chế độ mạng, quản lý nhân viên, xem audit). | `require` đọc vai trò hiện tại của tài khoản ở mỗi request. | T `test_demoted_operator_loses_admin…` |
| F4 | Thấp | `src/accounts.py` `RateLimiter` | Khoá rate limit chỉ lowercase, còn phần tra tài khoản lại strip. `" admin"`, `"admin "`… mỗi biến thể có 8 lần thử riêng. | Chuẩn hoá bằng `strip().lower()` ở cả `hit` và `reset`. | T `test_rate_limit_key_ignores…` |
| F5 | Thấp | `src/portal.py` `save_operator` | Chặn tự khoá hoặc tự hạ quyền chỉ so tên gốc. Gửi `"BOSS"` là qua được, trong khi `upsert_operator` lại lowercase tên. | So tên sau khi chuẩn hoá. | T `test_self_demotion_guard…` |
| F6 | Thấp | `src/server.py` `app_family_respond` | `accept=true` chấp nhận bất kỳ chuỗi `guardian` nào dù không có lời mời. Mỗi chuỗi tạo một inbox mới (bộ nhớ tăng) và một liên kết giám hộ không ai yêu cầu. | Trả 404 khi không có lời mời đang chờ. | T `test_family_accept_requires…` |
| F7 | **Cao** | `src/backend_runtime.py` `validate_event`; `src/server.py` `network_request` | Chỉ một sự kiện có `ts` tính bằng ms (1,7e12) hoặc 1e308 là đẩy watermark `IngestGuard.max_ts`. Từ đó mọi sự kiện thật đều thành LATE: ALLOW, không chấm điểm, không cập nhật đặc trưng. Watermark nằm trong checkpoint nên khởi động lại không hết. Ngoài ra JSON lồng sâu gây `RecursionError` và trả 500. | (1) `ts`/`end_ts` > 1e11 (giây Unix) bị từ chối. (2) Ở link nhà mạng, mốc kiểu Unix (≥ 1e9) vượt đồng hồ thật hơn 300 s bị từ chối 400. Mốc < 1e9 là đồng hồ giả lập của demo và test nên không so. (3) `RecursionError` trả 400. | T `test_millisecond_or_huge…`, `test_network_link_rejects…` |
| F8 | Trung bình | `src/detector.py` (trí nhớ thu giọng) | Kẻ gian giả CLI tổng đài ngân hàng, gọi hai đoạn AUDIO im lặng. Trong 1 giờ sau đó **mọi cuộc gọi thật** từ trung kế ngân hàng bị BLOCK, trái với nguyên tắc "số được bảo vệ là nạn nhân, không bao giờ bị chặn" mà luật dự phòng tuân thủ. | Bỏ qua số trong `PROTECTED`. Cuộc gọi giả không qua trung kế đã có `protected_cli_mismatch` và luật dự phòng xử lý. | T `test_harvest_memory_never_blocks…` |
| F9 | Cao (bộ nhớ, độ trễ) | `src/backend_runtime.py` | `FeatureStore.prune_state` chỉ được Spark gọi, phần serving không bao giờ gọi. Các deque và map tăng mãi suốt đời tiến trình: `in_calls`, liên kết IMEI/cell, heap CDR của người im lặng. Toàn bộ lại bị JSON hoá vào mỗi checkpoint trong lúc giữ khoá ghi. | `_compact_state()` trước mỗi checkpoint định kỳ, với mốc an toàn = `min(guard.max_ts, ts nhỏ nhất trong hàng chấm lại)`. Không áp TTL lên metadata. | T `test_periodic_compaction…` |
| F10 | Trung bình | `src/voice_pipeline.py`, `src/voice_model.py` | File stereo không có `caller_channel` vẫn được chấm trên kênh 0, có thể là giọng người nghe. Mô hình huấn luyện theo đúng kênh người gọi, nên đây là lệch train/serve và có thể ra BLOCK/FLAG cho sai người. | Thêm cảnh báo `caller_channel_required`. Mô hình trả OBSERVE (`party_attribution_abstention`). | T `test_stereo_audio_without…` |
| F11 | Trung bình | `static/admin/index.html` `card()` | `party`, `counterparty`, `event_id`, `time`, `action`, mã và lý do được chèn vào `innerHTML` không escape. `sender` hoặc `caller` (chuỗi bất kỳ ≤ 200 ký tự từ link nhà mạng) có thể chạy script trên trình duyệt nhân viên và đọc phiên trong `sessionStorage`. | Dùng `esc()` (đã có sẵn trong file) cho mọi trường. | T `test_operator_alert_card_escapes…` |
| F12 | Trung bình | `ram_online/core.py`, `production.py` | Vượt `max_inflight` hoặc lag vòng lặp sinh ra `OnlineUnavailable`, và nó bị đếm như lỗi Redis. Hai request bị shed là mở breaker, khiến **mọi số bị chặn được cho qua** trong thời gian cooldown. Kẻ gian chỉ cần làm ngập là vô hiệu hoá danh sách chặn. | Thêm lớp con `OnlineOverloaded`. Adapter trả UNKNOWN (`online_overloaded`) mà không mở breaker; probe bị shed cho request kế tiếp probe ngay. | R (2 test) |
| F13 | **Cao** | `telecom_grpc/Dockerfile.dockerignore`, `.dockerignore` | `bootstrap-local.ps1` tạo `telecom_grpc/secrets/` (mật khẩu Redis) và `telecom_grpc/tls/` (`server.key`, `client.key`). Whitelist `!telecom_grpc/**` cộng `COPY telecom_grpc` đưa các file này vào layer image. Ngoài ra `.env` của deploy bị gửi vào build context. | Loại trừ `telecom_grpc/secrets`, `telecom_grpc/tls`, `**/*.key`, `**/*.pem`, `**/.env*`. Root loại thêm `**/.env*` và `**/secrets`. | T `test_docker_contexts_exclude…` |
| F14 | Trung bình (chức năng) | `docker/backend/requirements-linux.txt` | Image verifier thiếu `prometheus-client`. Chuỗi import `server` → `portal` → `observability` lỗi ngay, nên quy trình verifier trong README không chạy được. | Thêm `prometheus-client`, `PyJWT[crypto]`, `pyarrow` đúng phiên bản của `requirements-serve.txt`. | T `test_verifier_image_installs…` |
| F15 | Thấp–TB | `monitoring/alerts/telecom-rules.yml` | `time() - max(<rỗng>)` không trả gì, nên `TfsBackupStale`/`TfsKpiStale` im lặng đúng lúc backup chưa từng thành công. | Thêm `or absent(...)`. Pipeline chạy bù lúc khởi động nên lần đầu không báo nhầm. | T `test_stale_pipeline_alerts…` |
| F16 | Trung bình | `edu_dss/backend/app/main.py`, `services.py` | Khoá `edu:explanation:{course}:{lecture}` ghép chuỗi trực tiếp. Sinh viên lớp `CS101` gọi lecture `sec2:lec1` sẽ đọc được bài của lớp `CS101:sec2`; giảng viên ghi đè được theo cách tương tự. | Thêm module `cache_keys.py` mã hoá từng phần khoá (percent-encode). ID chỉ gồm `[A-Za-z0-9_.-]` giữ nguyên khoá cũ. | E (2 test) |
| F17 | Trung bình | `.github/workflows/deploy.yml`, `deploy/cloud/deploy.sh` | Deploy chạy `git reset --hard origin/main`, tức là build commit mới nhất chứ không phải commit CI đã test. CI xanh của một PR có nhánh tên `main` cũng kích hoạt deploy. | Chỉ deploy khi `workflow_run.event == 'push'`. Truyền `head_sha` vào `DEPLOY_REF`. Script chỉ nhận SHA 40 ký tự nằm trên nhánh deploy. | Kiểm bằng `bash -n` và đọc lại; không có môi trường Actions |
| F18 | Trung bình | `src/drift.py` `_edges` | Đặc trưng liên tục có hơn 90 % giá trị 0 (`velocity_kmh`, `new_sims_on_cell_1h`…) rơi vào nhánh "nhị phân" và sinh khoảng 500 bin. Hai mẫu cùng phân phối đã cho PSI ≈ 0,13, vượt `PSI_WATCH` = 0,10, tức là chính nhiễu nền đã báo "lệch". | Khi số giá trị khác nhau > `n_bins+1`: tách riêng giá trị mode, phần còn lại chia theo quantile (tối đa khoảng 12 bin). Baseline đã lưu giữ nguyên cạnh cũ; chỉ baseline dựng mới bị ảnh hưởng. | T `test_zero_inflated_feature…` |

Bằng chứng thất bại trên HEAD (trích):
- F1: `assert 200 in (400, 429)`. Mã đúng vẫn được nhận sau 5 lần sai.
- F7: `503 == 400`. Sự kiện ở tương lai đã lọt tới engine.
- F12: `'open' == 'closed'`.
- F18: `assert 498 <= 13`.

---

## 3b. Vòng 2: sửa các mục còn mở

Sau vòng 1, chủ dự án yêu cầu sửa tiếp các mục "đã xác nhận nhưng chưa sửa" và các quyết định. Bảng dưới đây liệt kê những gì đã sửa trong vòng 2.

Ký hiệu cột Test:
- **T2** = `tests/test_audit_regressions_round2.py`
- **R2** = `ram_online/test_round2.py`
- **E2** = `edu_dss/backend/tests/test_round2_auth.py`

Cả 29 test (22 + 3 + 4) đều **thất bại trên mã sau vòng 1** (bản sao lưu ở WSL) và **qua trên mã cuối**.

| # | Mức | Vị trí | Vấn đề trước khi sửa | Cách sửa | Test |
|---|---|---|---|---|---|
| G1 | TB–Cao | `resilience.py` `drain_rescore` | Thời gian sao chép và khôi phục toàn bộ state bị tính là timeout của mô hình, có thể làm mở breaker. | Chỉ đo thời gian `score_batch`. | T2 `test_rescore_timeout…` |
| G2 | TB–Cao | `backend_runtime.py` `_loop` | Việc chấm lại chỉ chạy khi hàng đợi rỗng đủ 100 ms. Khi tải đều, nó không bao giờ chạy và hàng 50k âm thầm bỏ các phần tử cũ. | `_maintenance()` chạy cả khi đang bận, tối đa mỗi giây một lần, mỗi lần ≤ 8 sự kiện. | T2 `test_busy_owner…` |
| G3 | TB | `resilience.py`, `server.py` | Ngân sách 30 ms áp cho mọi batch, kể cả batch chỉ có SMS/AUDIO (ngân sách HTTP của chúng là 170 ms). | Thêm `content_budget_ms=SLA_TIER2_MS` cho batch không có CALL. | T2 `test_content_only…` |
| G4 | TB–Thấp | `resilience.py` `learn` | Danh sách chặn đầy thì mỗi BLOCK lại sắp xếp lại 200k mục. | Dùng `heapq.nsmallest` (giữ đúng thứ tự cũ, kể cả khi bằng nhau). | (hiệu năng; hành vi không đổi) |
| G5 | Thấp | `agent.py` | Sau khởi động lại, agent đề xuất lại các cảnh báo hồi tố cũ. Đọc `g.retro` không giữ khoá. | Con trỏ `retro_seen` = max(giá trị đã lưu, bộ đếm khôi phục), có lưu bền. Đọc dưới `det_lock`. | T2 `test_agent_does_not…` |
| G6 | Thấp (demo) | `server.py` `inject` | Các luồng mô phỏng dùng chung `sim.events` không có khoá; event ID có thể trùng. | Thêm `_inject_lock`; ID ngẫu nhiên. | T2 `test_demo_injections…` |
| G7 (D1) | TB | `server.py`, `observability.py`, alerts | Sự kiện LATE/DUPLICATE trả ALLOW không chấm, và bị đếm lẫn vào ALLOW thật. | Ngữ nghĩa giữ nguyên (đã có test khẳng định). Thêm `ingest_unscored` trong `/api/state`, metric `tfs_ingest_unscored{status}` và cảnh báo `TfsLateEventsUnscored`. | T2 `test_late_events…` |
| G8 | Thấp–TB | `security.py`, `backend_state.py` | Khoá tài khoản chỉ xoá phiên trong RAM, phiên trong kho bền vẫn sống lại khi mở khoá. Đổi mật khẩu không thu hồi phiên. | Khoá tài khoản hoặc đổi mật khẩu đều xoá mọi phiên, gồm cả `delete_sessions_for` trong kho bền. | T2 `test_password_change…`, `test_durable_store…` |
| G9 | Thấp | `accounts.py`, `security.py` | Tài khoản không tồn tại thì bỏ qua scrypt, nên thời gian phản hồi lộ email/tên có tồn tại hay không. | `burn_password_check()` chạy scrypt giả. | T2 `test_unknown_account…` |
| G10 | Thấp | `security.py` `verify_audit` | Dòng JSON hỏng gây lỗi 500. | Báo `valid: false` kèm dòng lỗi. | T2 `test_malformed_audit…` |
| G11 | Thấp | `portal.py` | Thêm/xoá chi phí hạ tầng không được ghi audit. | Ghi `infra_cost_add` / `infra_cost_delete`. | T2 `test_infra_cost…` |
| G12 | Thấp | `server.py` | Report/invite nhận chuỗi tuỳ ý và không có rate limit. | Kiểm định dạng `[0-9A-Za-z]{3,15}`. Giới hạn 30 report/giờ, 10 invite/giờ; lý do ≤ 500 ký tự. | T2 `test_reports_and_invites…` |
| G13 (D3) | TB (demo) | `portal.py`, `server.py` | Ba số demo công khai có thể được cấp cho tài khoản thật. | Loại các số đó khỏi kho cấp số. Phiên demo kiểu cũ từ chối số đã có chủ. | T2 `test_public_demo…` |
| G14 (D4) | TB–Thấp | `portal.py` `otp_verify` | Chủ mới của một số thừa kế inbox và người giám hộ của chủ cũ. | `forget_number_history()` xoá cả bản ghi trong DB lẫn trong engine. | T2 `test_new_owner…` |
| G15 | Thấp–TB | `voice_api.py` | Body khoảng 16,8 MB được đọc trọn trước mọi lần kiểm, không giới hạn số upload đồng thời. | Kiểm `Content-Length` và mốc thời gian trước khi đọc. Giới hạn số upload đồng thời (`FRAUD_AUDIO_MAX_UPLOADS`, mặc định 8, quá thì trả 503). | T2 `test_voice_upload…` |
| G16 | Thấp–TB | `drift.py` | Tham chiếu có BLOCK = 0 thì tắt cả hai cảnh báo dải BLOCK. | Tỷ lệ BLOCK > 0,1 % khi tham chiếu bằng 0 tính là đã dịch chuyển. | T2 `test_block_alarm…` |
| G17 | Thấp | `drift.py` | Segment không có dòng nào làm kết quả thành INVESTIGATE. | Ghi `rows: 0`, `NO_ACTION`, thêm vào `empty_segments`. | T2 `test_empty_segment…` |
| G18 (D11) | Thấp | `evals/run_evals.py`, `thresholds.json` | Dung sai 3 điểm % không bao giờ bắt được hồi quy FPR. `smoke_passed` không chặn. Recall `None` gây TypeError. | FPR hồi quy khi > 1,5·baseline + 0,005 điểm %. Thêm `smoke_passed` vào `must_be_true`. Bảo vệ trường hợp None. Test cũ (0,11 → 3,5) vẫn qua. | T2 `test_fpr_regression…` |
| G19 | Thấp | `train.py`, `shadow_run.py` | Segment không có dòng sạch gây lỗi sklearn khó hiểu. Kiểm an toàn shadow dùng `assert` (mất khi chạy `-O`). | Báo lỗi rõ ràng; đổi sang `RuntimeError`. | T2 `test_shadow_safety…` |
| G20 (D8) | TB | `ram_online/streaming.py` | Một bản ghi Kafka hỏng làm consumer dừng mãi mãi. | Thêm tuỳ chọn `--dlq-topic` / `KAFKA_DLQ_TOPIC`: lỗi dữ liệu được đẩy sang DLQ (acks=all, chờ ack) rồi mới commit. Lỗi hạ tầng không vào DLQ. Không cấu hình DLQ thì vẫn dừng như cũ, không mất dữ liệu. | R2 (2 test) |
| G21 | Thấp | `ram_online/production.py` | Exception không bọc trả về client thành lỗi gRPC UNKNOWN. | Trả quyết định fail-open `policy_error` và tính vào breaker. | R2 `test_unexpected_gate…` |
| G22 | Thấp | `spark/*_stream.py`, `event_contract.py`, compose | Không fsync thư mục cha sau `rename`; `source.json` ghi không nguyên tử. Kafka 29092 và cổng 4040 mở trên 0.0.0.0, `secrets/` được mount vào container. | `fsync_directory()`; ghi tạm rồi replace. Chỉ bind 127.0.0.1; che `secrets/` và `telecom_grpc/{secrets,tls}` bằng tmpfs. | T2 `test_dev_compose…` |
| G23 | Thấp | `telecom_grpc/docker-compose.yml` | Mật khẩu Redis hiện trong `ps`; service gRPC không có healthcheck. | Dùng `REDISCLI_AUTH`; thêm healthcheck TCP. | T2 `test_dev_compose…` |
| G24 (D9) | TB | `edu_dss` `main.py`, `ui/app.py` | Mọi giảng viên đều huấn luyện được trên bất kỳ dataset nào. | `TrainingRequest.course_id` là bắt buộc và được kiểm qua `require_course_access`; giá trị này được gửi tới adapter. | E2 `test_automl…` |
| G25 (D10) | Thấp–TB | `edu_dss` `security.py`, `demo_tokens.py`, `config.py`, compose, nginx | Token demo vẫn dùng được khi đã tắt demo. Không kiểm `aud`. Worker giữ `JWT_SECRET`. Giới hạn body nhỏ hơn mức schema cho phép. XFF do client đặt được chuyển tiếp. Trang có thể bị nhúng frame. | Claim `demo=true` bị từ chối khi `EDU_DEMO_MODE≠1`. Tuỳ chọn `JWT_AUDIENCE`. Bỏ secret khỏi worker (nạp lười). Body 1,1 MB ở cả backend và nginx. nginx ghi đè XFF. Thêm `X-Frame-Options DENY` và CSP `frame-ancestors 'none'`. | E2 (3 test) |
| G26 (D5) | Thấp–TB | `deploy/cloud/*`, `.github/workflows/*`, `Dockerfile` | `FORWARDED_ALLOW_IPS="*"`. Backup trước deploy có `|| true`. `curl -k`. Token lộ trên dòng lệnh. Host key không ghim. promtool không kiểm checksum. Base image không ghim. | `proxy_ip.sh` lấy IP của Caddy trên mạng `edge` và truyền vào `FORWARDED_ALLOW_IPS`. Backup thất bại thì dừng deploy (exit 3). smoke kiểm TLS (chỉ dùng `SMOKE_INSECURE=1` khi chờ ACME). Token đọc qua `-H @file`. Secret `DEPLOY_KNOWN_HOSTS` (StrictHostKeyChecking=yes). `sha256sum -c` cho promtool. Ghim `python:3.12.12-slim-bookworm`. | T2 `test_cloud_deploy…` |
| G27 | Thấp | `android/` | Mọi liên kết mở trong WebView đang giữ phiên đăng nhập. Bản release không được ký. | `shouldOverrideUrlLoading` mở liên kết ngoài server bằng trình duyệt. Thêm `signingConfig` khi có `-PreleaseKeystore`. | T2 `test_android…` (tĩnh; **chưa build** vì không có Android SDK) |

**Hai thay đổi đã thử rồi hoàn tác.** Lý do: mỗi thay đổi làm đỏ một test hiện có khẳng định hành vi cũ. Theo nguyên tắc audit, đây là quyết định của chủ dự án:
- Giới hạn `ts` trong hợp đồng Spark: `spark/test_stream_contracts.py:34` khẳng định `ts=1e308` hợp lệ.
- Đồng nhất xử lý bản trùng trong cùng batch giữa hai engine Spark: `test_stateful_stream.py:122-132` khẳng định engine local chấp nhận bản đầu.

---

## 3c. Vòng 3: sửa nốt các mục còn mở

Chủ dự án yêu cầu sửa hết các lỗi còn tồn tại. Họ đã quyết định:
- đổi D1, D12 và D13, dù các test hiện có đang khẳng định hành vi cũ;
- dựng lại mô hình cho D6 và D7;
- D14: chọn phương án tối ưu cho thời điểm này, nhưng vẫn phải sửa sau.

Ký hiệu cột Test:
- **T3** = `tests/test_audit_regressions_round3.py` (12 test, tất cả thất bại trên mã sau vòng 2)
- **S3** = `spark/test_stream_contracts.py::ImeiPartitionKeyTests`
- **E3** = `edu_dss/backend/tests/test_round2_auth.py::test_short_jwt_secret_is_refused`

| # | Mức | Vị trí | Vấn đề trước khi sửa | Cách sửa | Test |
|---|---|---|---|---|---|
| H1 | Thấp–TB | `observability.py` `metrics_authorized` | Docstring ghi "không có token thì chỉ mở cho demo", nhưng tham số `demo_mode` bị bỏ qua. Vì vậy `run_live.py` (chế độ thật) mở `/metrics` cho bất kỳ ai. | Không có `METRICS_TOKEN` thì chỉ mở khi `demo_mode`. | T3 `test_metrics_without_token…` |
| H2 | Thấp | `security.py` `verify_audit`, `/api/audit` | Xoá dòng ở cuối audit không bị phát hiện. | `verify.head` trả `{records, hash}` để chép ra hệ thống ngoài. Gửi lại qua `anchor_records`/`anchor_hash` sẽ phát hiện log bị cắt hoặc viết lại. | T3 `test_external_anchor…` |
| H3 | Thấp | `monitoring/alerts` `TfsDown` | Cảnh báo phụ thuộc tên job scrape. | Thêm `or absent_over_time(tfs_uptime_seconds[5m])`. | T3 `test_down_alert…` |
| H4 | Thấp | `deploy/cloud/setup.sh` | Token DuckDNS nằm rõ trong crontab và trên dòng lệnh. | Ghi token vào file cấu hình curl quyền 600 (`curl -K`). Crontab chỉ trỏ tới file đó. | T3 `test_duckdns…` |
| H5 | Thấp | `.github/workflows/*` | Action ghim theo tag (tag có thể bị dời). | Ghim theo SHA commit, tag ghi trong chú thích. | T3 `test_workflow_actions…` |
| H6 | Thấp | `spark/distributed_features.py` | Mọi dòng không có IMEI dùng chung khoá `""`, nên dồn vào một partition nóng. | `imei_entity()` cho mỗi dòng như vậy một khoá riêng (đặc trưng của chúng luôn bằng 0). | S3 |
| H7 | Thấp | `edu_dss/backend/app/config.py` | `JWT_SECRET` ngắn có thể bị dò offline từ một token bất kỳ. | Bắt buộc ≥ 32 ký tự. Muốn bỏ qua phải đặt rõ `EDU_ALLOW_WEAK_JWT_SECRET=1`. | E3 |
| H8 (D1) | TB | `ingest_guard.py`, `feature_store.py`, `backend_runtime.py` | Watermark toàn cục: hai trung kế lệch nhau 1 ms là sự kiện cũ hơn thành LATE, không được chấm. | Frontier theo từng bên gọi (caller/sender), cộng một sàn toàn cục `max_ts − allowed_lateness_s` (120 s). Các cấu trúc chéo giữa các bên trong FeatureStore chịu được lệch thứ tự có giới hạn:<br>• `in_calls` được chèn đúng thứ tự, chỉ đếm cuộc gọi ≤ ts;<br>• `last_seen`, liên kết IMEI và SIM mới dùng `max`.<br>Checkpoint của guard lên v2, vẫn đọc được v1 (giữ nguyên độ chặt của v1 cho dữ liệu cũ). Mốc compaction là `scoring_floor`. Test cũ ở `test_ingest_guard.py:103` vẫn qua vì cùng một bên gọi. | T3 (3 test) |
| H9 (D12) | TB | `spark/event_contract.py` | Một `ts` sai đơn vị đẩy watermark Spark lên quá xa. | `ts` phải nằm trong [0, 1e11] giây, giống API chính. `test_stream_contracts.py:34` được đổi theo quyết định này. | test đã sửa |
| H10 (D13) | Thấp | `spark/stateful_stream.py` | Engine local giữ bản đến trước khi có xung đột trong cùng batch, khác với engine phân tán. | Cách ly mọi bản xung đột, giống engine phân tán. `test_stateful_stream.py:131-136` được đổi (6 bản bị cách ly, 0 bản trùng). | test đã sửa |
| H11 (D6) | TB | `config.local_hour`, `feature_store.py`, `data_generator.py`, `spark/spark_features.py` | `hour_of_day = (ts/3600) mod 24`, trong khi bộ sinh dữ liệu coi t = 0 là 00:00 giờ Việt Nam. Với giờ Unix thật, đặc trưng lệch 7 giờ. | Dùng chung một quy ước: ts là giây Unix (UTC), giờ trong ngày = `local_hour(ts)` (UTC+7). Bộ sinh phát ts kiểu Unix (ngày 0 = 1970-01-02 theo giờ địa phương). Bundle `runtime/artifacts_v22` được dựng lại thành **tfd-v2.3**. | T3 (2 test) |
| H12 (D7) | TB | `build_dataset.py`, `data_generator.py`, `make_drift_baseline.py`, `ops_pipeline.py`, `server.py` | Tham chiếu drift là cửa sổ đã làm giàu gian lận. PSI vận hành so số đếm luỹ kế của mọi loại sự kiện với tham chiếu chỉ có CALL. | Thêm cửa sổ `ref` (kind `reference`): tỷ lệ gian lận thực tế, chỉ các họ đã biết, không bao giờ dùng để huấn luyện hay hiệu chỉnh. Baseline drift ưu tiên `ref`. Engine đếm riêng `call_decisions`; `kpi_snapshot` tính PSI trên phần chênh lệch kể từ snapshot trước (≥ 500 cuộc gọi). | T3 (2 test) |

**D14 (checkpoint tăng dần), phương án chọn lúc này.** Tôi giữ thiết kế snapshot và chưa viết lại. Lý do:
- F9 (compaction) đã giới hạn kích thước state theo cửa sổ 24 giờ.
- G2 bỏ được việc chấm lại bị bỏ đói.
- Viết lại checkpoint và phục hồi mà không có môi trường tải thật để đo sẽ tạo rủi ro lớn hơn lợi ích ngay lúc này.

Việc này vẫn **bắt buộc phải sửa sau** (§9, ưu tiên 1), với kế hoạch:
1. Ghi delta theo khoá đã chạm (`touched` của giao dịch feature store) vào bảng `state_delta` trong cùng giao dịch.
2. Snapshot đầy đủ chạy ở luồng nền, trên bản sao, tối đa mỗi N phút.
3. Phục hồi = snapshot gần nhất + delta + journal.
4. Đo p99 VERDICT trong lúc checkpoint trước và sau khi đổi.

**Dựng lại bundle (D6/D7).** Lệnh `python src/rebuild_artifacts.py --out-dir … --model-version tfd-v2.3`, chạy xong cả 5 bước:

| Bước | Thời gian |
|---|---|
| build_dataset | 97 s |
| train | 22 s |
| evaluate | 227 s |
| make_drift_baseline | 3 s |
| shadow_run | 25 s |

Kết quả được chép vào `runtime/artifacts_v22`, với manifest `tfd-v2.3`.

- **Chất lượng không đổi đến từng chữ số.** Đây là điều mong đợi, không phải lỗi: bộ mô phỏng chỉ dịch ts đi một hằng số (+86 400 − 25 200 s), và `local_hour` cộng lại đúng 25 200 s, nên đặc trưng giờ trong ngày của dữ liệu mô phỏng giữ nguyên. Thay đổi chỉ có tác dụng với ts Unix thật, ví dụ 02:00 UTC giờ cho ra 9,0 thay vì 2,0.
- **Tham chiếu drift mới (`ref`).**
  - Cửa sổ có 52 407 sự kiện, tỷ lệ gian lận 0,059 %; dải BLOCK chiếm 0,011 %, so với 1,64 % của `cal` cũ.
  - Báo cáo shadow trên cửa sổ test không còn đặc trưng nào bị cảnh báo (trước đó `active_hours_on_network` bị alert). Kết quả là INVESTIGATE vì dải BLOCK gấp 3,4 lần: cửa sổ test cố ý chứa họ zero-day (Wangiri) và nhiều tác nhân hơn. Đây là tín hiệu đúng.
  - Trước sửa, INVESTIGATE xuất hiện vì tỷ lệ BLOCK **giảm** còn 0,06 lần do tham chiếu đã làm giàu gian lận.
- **Phục vụ (cổng SLA).**
  - Lần chạy đầu sau khi chép bundle ra p99 102 ms với 463 fail-open. Lần chạy lại ra **p99 31,1 ms, 98,5 % trong 30 ms, 3 fail-open**.
  - Đo trực tiếp `score_batch` (16 sự kiện): 22,5 ms với bundle cũ, 22,0 ms với bundle mới, tức không chậm hơn.
  - Cùng thời điểm, mã vòng 2 với bundle cũ ra 85 %/99,6 ms. Chênh lệch giữa các lần chạy là nhiễu của laptop.
  - Cổng SLA vẫn trượt nhẹ (31,1 > 30 ms; 3 > 0 fail-open). Đây là môi trường, đúng như `docs/SLA_30MS.md` đã ghi.

---

## 4. Còn mở sau vòng 3

| Mức | Vị trí | Vấn đề | Vì sao chưa sửa |
|---|---|---|---|
| TB–Cao | `backend_runtime.py` (D14) | Checkpoint vẫn là snapshot đầy đủ trong giao dịch ghi. | Chủ dự án chọn phương án tối ưu cho lúc này (xem §3c); **bắt buộc sửa sau**. |
| — | `backend_evidence.py` (D2) | Bộ ghi bằng chứng lỗi thì dừng suy luận. | Fail-closed có chủ đích: không có bằng chứng thì không được ra quyết định. Giữ nguyên. |
| Thấp | Spark so với FeatureStore | Spark chỉ xử lý CALL và không warm-up, nên lệch với FeatureStore cho thuê bao có SMS trước cuộc gọi đầu. | Hợp đồng Spark (`validate_call`) chỉ dành cho CALL; mở rộng sang SMS là tính năng mới. |
| Thấp | Spark | `flush` của hai engine khác nhau khi không còn gì trong bộ đệm. Engine local deep-copy store ở mỗi batch. | Chỉ khác ở trường hợp biên sau khi flush; deep-copy là tối ưu hiệu năng. |
| Thấp | `ram_online/streaming.py` | Cửa sổ có cận dưới mở, neo theo frontier. | Test `test_ram_online.py:99` khẳng định hành vi này. Đây là hợp đồng riêng của hệ thống RAM, khác FeatureStore. |
| Thấp | `portal.py` `register` | Trả 400 khi email đã dùng. | Test `test_portal_accounts.py` khẳng định. Lựa chọn trải nghiệm người dùng; rate limit đã giới hạn việc dò. |
| Thấp | Android | `isMinifyEnabled=false`. | Không có Android SDK để build và kiểm R8; bật mà không build được thì có thể làm hỏng bản release. |
| Thấp | gRPC | Fast-allow (tuỳ chọn); Redis policy dùng `redis://` trên mạng internal. | Có tài liệu, mặc định tắt; mạng internal. |
| Thấp | Triển khai | Caddy đổi IP mà chưa deploy lại thì XFF bị bỏ qua. | Lỗi an toàn (rate limit trở thành chung); chạy lại `deploy.sh` để sửa. |
| — | Dữ liệu | Mọi chỉ số đều trên dữ liệu mô phỏng; SLA 30 ms và 50k TPS chưa đạt trên phần cứng đích. | Cần dữ liệu thật và phần cứng đích. |

---

## 5. Quyết định cho chủ dự án

| # | Trạng thái |
|---|---|
| D1, D6, D7, D12, D13 | **Đã sửa ở vòng 3** theo quyết định ngày 17/11/2026 (H8–H12). |
| D3, D4, D5, D8, D9, D10, D11 | Đã sửa ở vòng 2 (G13, G14, G26, G20, G24, G25, G18). |
| D14 | Hoãn có chủ đích; bắt buộc sửa sau theo kế hoạch ở §3c. |
| D2 | Giữ fail-closed (khuyến nghị). |

**Lưu ý vận hành:**
- Đặt secret `DEPLOY_KNOWN_HOSTS` trong GitHub.
- Client gọi AutoML của EduDSS phải gửi `course_id`.
- `JWT_SECRET` của EduDSS phải dài ≥ 32 ký tự.
- Đặt `KAFKA_DLQ_TOPIC` nếu muốn dùng DLQ.
- Ghi định kỳ `verify.head` của `/api/audit` ra hệ thống ngoài.
- Bundle mô hình nay là **tfd-v2.3**: giờ trong ngày tính theo giờ Việt Nam, áp cho ts Unix. Các số liệu v2.2 trong `docs/` là lịch sử.
- Checkpoint của guard viết bằng bản mới (v2) không đọc được bằng mã cũ.

---

## 6. Vùng đã kiểm và thấy đúng

**Link nhà mạng.**
- HMAC-SHA256 trên chuỗi `ts + "." + body`, so sánh constant-time.
- Cửa sổ ±30 s có kiểm số hữu hạn. Cache chữ ký đã thấy giữ 2× cửa sổ.
- Giới hạn 64 KB được áp *trước* khi kiểm chữ ký.

**Google ID token.** Chỉ RS256, khoá lấy từ JWKS, kiểm `aud`, `iss`, `exp`, `iat`, `sub`, và bắt buộc `email_verified`.

**Mật khẩu và phiên.**
- scrypt (n = 2^14, r = 8, p = 1), salt 16 byte; hash sai định dạng thì fail an toàn.
- Token 32 byte ngẫu nhiên, chỉ lưu SHA-256. Có TTL và thời gian nhàn rỗi; tối đa 10 phiên mỗi tài khoản.

**Cách ly thuê bao.** Mọi `/api/app/*` lấy số từ phiên, không lấy từ request. App thuê bao escape toàn bộ dữ liệu từ server.

**Giao dịch SQLite.**
- Scope lồng nhau dùng chung giao dịch ngoài; lỗi bên trong làm cả giao dịch chỉ còn rollback.
- Ack chỉ bị xoá sau COMMIT.
- Idempotency theo `(kind, event_id)` cộng SHA-256 của JSON chuẩn hoá.

**Outbox bằng chứng.**
- fsync trước khi ack. Chuỗi hash chuẩn hoá ở cả hai phía (`sort_keys`).
- Phục hồi phát lại theo thứ tự journal và không chấm hai lần.

**Đặc trưng.** Cửa sổ đóng `[ts−W, ts]` nhất quán. Không có chia cho 0. Giao dịch feature store bao toàn bộ khoá chạm tới.

**Mô hình.**
- Thứ tự đặc trưng train và serve khớp: LightGBM, IsolationForest theo segment, thang meta của SMS.
- Không rò nhãn.
- `threshold_for_fpr` đúng số FP cho phép, kể cả khi có điểm bằng nhau.
- `fast_iforest` khớp sklearn: c(n), độ sâu + 1 + c − 1, lượng tử hoá float32.

**WAV.**
- Kiểm thời lượng trước khi cấp phát bộ nhớ. Phát hiện dữ liệu bị cắt.
- Giải mã 8 và 24 bit đúng. Từ chối WAV float.
- File im lặng cho đặc trưng hữu hạn và mô hình abstain.

**Thống kê.** Công thức Wilson và `minimum_negatives_if_zero_fp` đúng.

**RAM online.**
- Bloom không có âm tính giả; `b|1` loại bước nhảy 0.
- Rào phiên bản (manifest uuid4 + SCARD) không bị ABA.
- Khoá cùng hash tag (an toàn với Redis Cluster). Kiểm E.164 trước mọi RPC.

**gRPC.** `require_client_auth=True` (đã có test). Chế độ insecure chỉ cho loopback.

**EduDSS.**
- Kiểm chủ sở hữu task trước khi trả kết quả. `extra="forbid"` chặn client tự đặt `requested_by`.
- Mạng `backend` và `data` là internal. Container read-only, chạy non-root.

**Cảnh báo và metrics.** Mọi biểu thức alert và Grafana tham chiếu đúng tên metric mà `observability.py` xuất ra.

**Android release.** `cleartextTrafficPermitted=false`, bắt buộc `https://`, không có JS bridge, `allowBackup=false`.

---

## 7. Kết quả kiểm thử

**Môi trường.**
- WSL Ubuntu, Python 3.12.3, venv dựng từ `requirements-serve.txt` (bản ghim). Thêm `redis 8.1.0`, `grpcio 1.85.0`, `celery 5.6.3`, `fakeredis`, `lupa`.
- Mốc chạy trên bản `git archive HEAD`; sau sửa chạy trên bản sao các file tracked và file mới.
- pytest chạy với `--basetemp` ngắn.

| Bộ test | Mốc (HEAD) | Sau sửa (vòng 1 → 2 → 3) | Ghi chú |
|---|---|---|---|
| `tests/` (app chính) | 426 passed | 442 → 464 → **476 passed** (vòng 3) | 16 + 22 + 12 test hồi quy; mỗi test thất bại trên mã trước vòng của nó. |
| `spark/` | 23 passed, 65 subtests | 23 → 23 → **24 passed**, 65 subtests | Vòng 3 sửa D12/D13 theo quyết định của chủ dự án, nên đổi hai test cũ; thêm 1 test (H6). |
| `edu_dss/backend/tests` | 9 passed, 4 subtests | 11 → 15 → **16 passed**, 4 subtests | 2 + 4 + 1 test mới. |
| `telecom_grpc/tests` | 2 passed | 2 passed (compose kiểm bằng `docker compose config` và YAML) | Phải chạy từ thư mục gốc repo. Chạy từ `telecom_grpc/` báo `ModuleNotFoundError: telecom_grpc` (vấn đề cách gọi, không phải lỗi mã). |
| `ram_online/` | 17/18 trên fakeredis | 19/20 → 22/23 → **22/23** trên fakeredis | **Môi trường:** bộ test cần Redis thật (`RAM_TEST_*_URL`) và không có Redis/Docker daemon, nên chạy với `fakeredis.TcpFakeServer`. Ca thất bại duy nhất (`test_window_rejects_call_id…`) là fakeredis đóng kết nối khi Lua trả `error_reply`; nó thất bại giống hệt trên HEAD. Khi không có URL, cả 8 ca cần Redis đều báo `KeyError`. |
| `evals/run_evals.py` | xem §7.1 | xem §7.1 | Chạy trên cả hai bản. |
| Docker build, promtool, Android, Kafka/Spark cluster | không chạy | không chạy | **Môi trường:** Docker CLI có nhưng daemon không chạy; không có Java hay Android SDK. |

### 7.1 Eval gate

Lệnh: `python -B evals/run_evals.py`, chạy trên WSL, laptop dùng chung (HEAD và vòng 1 chạy liên tiếp; vòng 2 và vòng 3 chạy riêng). Cột vòng 3 là lần chạy lại; lần đầu ngay sau khi chép bundle ra p99 102 ms (nhiễu, xem §3c).

| Chỉ số | Ngưỡng | HEAD | Sau vòng 1 | Sau vòng 2 | Vòng 3 (tfd-v2.3) |
|---|---|---|---|---|---|
| block_recall_pct | ≥ 45 | 54,55 | 54,55 | 54,55 | 54,55 |
| block_precision_pct | ≥ 95 | 100,0 | 100,0 | 100,0 | 100,0 |
| block_fpr_ci_hi_pct | ≤ 0,01 | 0,0077 | 0,0077 | 0,0077 | 0,0077 |
| any_recall_pct | ≥ 80 | 86,36 | 86,36 | 86,36 | 86,36 |
| any_fpr_pct | ≤ 0,2 | 0,1142 | 0,1142 | 0,1142 | 0,1142 |
| family_min_detected_pct | ≥ 75 | 80,0 | 80,0 | 80,0 | 80,0 |
| zero_day_caught_pct | ≥ 80 | 91,7 | 91,7 | 91,7 | 91,7 |
| evidence_chain_valid | true | true | true | true | true |
| smoke_passed (cổng mới, G18) | true | true | true | true | true |
| serving_scored_within_30ms_pct | ≥ 95 | **84,0** ✗ | **91,75** ✗ | 96,0 | 98,5 |
| serving_p99_ms | ≤ 30 | **97,9** ✗ | **47,8** ✗ | **37,5** ✗ | **31,1** ✗ |
| serving_fail_open | 0 | **55** ✗ | **19** ✗ | **11** ✗ | **3** ✗ |

**Chất lượng mô hình:** không đổi đến từng chữ số. F8 (bỏ trí nhớ thu giọng cho số tổng đài) và F7 (giới hạn `ts`) không tác động lên cửa sổ đánh giá.

**Cổng SLA phục vụ:** thất bại trên **cả bốn** bản (vòng 3 chỉ trượt sát ngưỡng). Đây là vấn đề môi trường và hiệu năng đã biết, không phải hồi quy do lần sửa này: HEAD còn tệ hơn bản đã sửa. Baseline 18,6 ms đo trên runner CI; `docs/SLA_30MS.md` ghi rõ 30 ms chưa ổn định trên laptop. Nhiễu giữa các lần chạy lớn, nên chênh lệch giữa các cột **không** được coi là cải thiện hiệu năng. G2/G3 có thể giảm số fail-open giả cho batch SMS/AUDIO, nhưng điều đó chưa được đo trên phần cứng đích.

---

## 8. Refactoring và mã chết

Thay đổi không đổi hành vi:
- Bỏ import không dùng (pyflakes):
  - `clock_description` trong `src/http_loadtest.py`;
  - `MODEL_VERSION` và `EvidenceLog` trong `src/server.py` (server ghi qua `DurableEvidenceLog`);
  - `make_client` trong `ram_online/streaming.py`.
- Gom cách dựng khoá Redis của EduDSS vào `edu_dss/backend/app/cache_keys.py`. Trước đây có bốn chỗ ghép chuỗi lặp nhau ở `main.py` và `services.py`.

Mã chết hoặc trùng **giữ nguyên**, có lý do:
- `Detector.process` và `ActionLedger` không nằm trên đường API chính, nhưng `loadtest.py`, `experiment_guard.py` và test vẫn dùng.
- Tham số `lang` của `reason_codes.for_subscriber` là API công khai. vulture báo không dùng.
- `scaleout.py` không được server dùng, nhưng là lớp scale-out có test và eval riêng (CI job `scaleout`).
- `backup_tfd-v2.0/`, các thế hệ mô hình cũ và output benchmark giữ để rollback và tái lập.

---

## 9. Lộ trình

| Ưu tiên | Việc | Module |
|---|---|---|
| 1 | **D14: checkpoint tăng dần (bắt buộc).** Theo kế hoạch ở §3c: delta theo khoá đã chạm, snapshot nền, phục hồi = snapshot + delta + journal; đo p99 khi checkpoint. | `backend_runtime`, `backend_state`, `feature_store` |
| 1 | Chạy `ram_online` trên Redis thật; build cả ba Docker image; chạy promtool; build Android (`assembleRelease`). | CI |
| 1 | Đặt `DEPLOY_KNOWN_HOSTS`; báo client EduDSS về `course_id` và độ dài `JWT_SECRET`; lưu `verify.head` ra ngoài. | vận hành |
| 2 | Đo lại SLA trên phần cứng đích sau G2/G3/H8. | `backend_runtime`, `resilience` |
| 2 | Spark: SMS và warm-up nếu cần tương đương đầy đủ với FeatureStore. | `spark/` |
| 3 | Dữ liệu thật có nhãn; SLA 30 ms và 50k TPS trên phần cứng đích. | toàn hệ thống |
