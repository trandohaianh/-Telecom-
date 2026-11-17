# Đánh giá đa vai trò và thuật toán: Telecom Fraud Shield v2

**Ngày:** 17/11/2026
**Nhánh:** `audit/telecom-v2-fixes` (chưa push)
**Bundle mô hình:** `tfd-v2.4` (dựng lại trong vòng này)
**Dữ liệu:** mọi số liệu chất lượng đều đo trên **dữ liệu mô phỏng**, không phải lưu lượng nhà mạng thật.

Báo cáo này nối tiếp:
- [01_Technical_Code_Audit_Report.md](01_Technical_Code_Audit_Report.md): các vòng 1–3 (F1–F18, G1–G27, H1–H12).
- [02_Deep_Technical_Explanation_Report.md](02_Deep_Technical_Explanation_Report.md): giải thích luồng xử lý và công thức.
- [he-thong/](he-thong/README.md): báo cáo cho từng hệ thống con.

Mã các bản sửa vòng 4 bắt đầu bằng **R4-**.

---

## 0. Cách làm

**Năm vai trò review song song**, mỗi vai đọc mã độc lập và chỉ báo phát hiện mới (không lặp lại những gì báo cáo 01 đã ghi):
- Product/CEO;
- Algorithm/ML (có chạy thử nghiệm phản thực tế trên mô hình đã giao);
- Staff engineer;
- Bảo mật;
- Designer/UX.

**Vai QA tôi tự chạy:**
- Khởi động server thật bằng `serve.py`, đúng cấu hình bản public: `FRAUD_PUBLIC=1`, có `METRICS_TOKEN`, mật khẩu admin.
- Chạy 32 luồng end-to-end qua HTTP, gồm các yêu cầu ký HMAC như tổng đài gửi.
- Mở giao diện trong trình duyệt ở kích thước di động (375×812) và desktop.

**Kiểm chứng.** Tôi đọc lại mã cho từng phát hiện trước khi sửa:
- Sửa ngay những lỗi có kịch bản hỏng rõ ràng và không đổi hành vi sản phẩm.
- Những thay đổi làm đổi hành vi mô hình, chính sách chặn, triển khai hoặc giao diện thì **hỏi chủ dự án trước**. Chủ dự án đã chọn **tất cả** các mục được đề xuất (§8).

**Test hồi quy.** Mỗi bản sửa có test trong `tests/test_audit_regressions_round4.py` (28 test). Tôi chạy file này trên `git archive babe2dd` (mã trước vòng 4): 28/28 thất bại. Trên mã hiện tại: 28/28 qua.

---

## 1. Vai Product/CEO

### Sản phẩm có giải quyết đúng bài toán không?

**Một phần.** Ý tưởng cốt lõi đúng và được cài đặt cẩn thận:
- Hệ thống đứng song song với tổng đài, chỉ nhận bản sao báo hiệu. Mặc định ở chế độ `shadow` và fail-open.
- Mọi hành động đều có bằng chứng, có ký HMAC và có người duyệt.
- App thuê bao và bảng vận hành kể cùng một câu chuyện.

Nhưng thứ đang có là **một bộ mô phỏng và demo được kiểm thử kỹ**, chưa phải sản phẩm sẵn sàng cho pilot:
- Mọi cảnh báo trong app đến từ `engine.inject`, tức là sự kiện mô phỏng (`src/server.py`).
- Không có adapter nhà mạng; README tự ghi lớp SIP/Diameter "phụ thuộc thiết bị của nhà mạng".
- App Android không nhận cuộc gọi thật: thiếu `CallScreeningService`.

### Còn thiếu gì cho một pilot thật (theo thứ tự ưu tiên)

1. **Đường dữ liệu nhà mạng** (bản sao báo hiệu và CDR) cho **một** nhà mạng, chạy shadow 4–8 tuần, kèm vòng chuyên viên gán nhãn.
2. **Đánh giá trên dữ liệu thật.** Ngay trên mô phỏng, khi gộp 11 cửa sổ thì recall chặn chỉ đạt 43,5 % và SIM box 68,2 % (§7.2).
3. **Một đường online duy nhất.** Hiện có ba đường song song (API chính, `ram_online`, `telecom_grpc`), hai đường sau chưa nối vào API chính. Kèm theo: đo SLA trên Linux, phần cứng đích, và D14 (checkpoint tăng dần).
4. **Kênh thuê bao bền vững.**
   - Inbox hiện nằm trong RAM: `deque(maxlen=50)` ở `server.py`, khởi động lại là mất.
   - Chưa có push FCM.
   - OTP chỉ qua Twilio/console, chưa qua SMSC của nhà mạng.
5. **Sẵn sàng cho doanh nghiệp:**
   - SSO, HSM/KMS;
   - neo chuỗi audit ra ngoài (vòng 3 đã có API neo, H2);
   - hồ sơ tuân thủ Nghị định 13/2023.

### Làm thừa so với giai đoạn

- **Bảy hệ thống trong một repo.** EduDSS là sản phẩm khác hẳn; nên tách repo.
- **Spark** (khoảng 2,1 nghìn dòng) mới xử lý CALL; `ram_online` và `telecom_grpc` chưa có khách hàng dùng.
- **Agent** có 7 loại đề xuất, cộng theo dõi chi phí, 12 cảnh báo Grafana và bộ sinh slide/báo cáo: phần trau chuốt vận hành đi trước khi có khách hàng.

**Khuyến nghị:** đóng băng Spark ngoài CALL, `ram_online`/gRPC như sản phẩm riêng, và phần mở rộng agent cho tới khi có pilot.

### Chỗ nói quá trong README và giao diện, so với số đo

| Tuyên bố | Thực tế | Đã xử lý |
|---|---|---|
| Header console ghi "(<30 ms)" như một sự thật | `docs/SLA_30MS.md` ghi chưa đạt | **R4-UX2:** đổi thành "mục tiêu <30 ms" |
| Badge `tfd-v2.0` trước khi đăng nhập | Bundle đang chạy là v2.3, nay v2.4 | **R4-UX1:** thành "—"; sau đăng nhập hiện đúng `tfd-v2.4` |
| README: "<0,01 % đạt", "64 tests", bảng v2.1 | Đo một cửa sổ, số cũ | **R4-DOC1:** thêm bảng "Trạng thái đo được" ở đầu README (gộp 11 cửa sổ, ghi rõ chỉ tiêu chưa đạt) |
| "Mọi cặp chữ/nền đạt WCAG AA" | Chữ trắng trên `#ff4d6d` chỉ khoảng 3,2:1 | **R4-UX3:** token `--on-red` (chữ tối trên đỏ ở theme tối) |
| "Chặn ở mạng cho mọi thuê bao" | Mặc định shadow; recall chặn trước khi đổ chuông khoảng 58 % trên test | Ghi trong bảng trạng thái README |
| Watermark toàn màn hình | Không có ở `/admin/manage` | Còn mở (§9) |

---

## 2. Vai Algorithm/ML

### 2.1 Bảng thuật toán

| Thuật toán | Dùng ở đâu | Kết luận | Lý do | Khuyến nghị / đã làm |
|---|---|---|---|---|
| LightGBM nhị phân cho CALL (tầng 1), `scale_pos_weight=10`, dừng sớm trên `val` | `src/train.py` `train_call`; `src/detector.py` | **Phù hợp, có lưu ý** | GBDT trên 27 đặc trưng dạng bảng chấm khoảng 1 ms/batch, hợp với ngân sách 30 ms. Điểm số chưa hiệu chuẩn. Đặc trưng có gain cao nhất là `active_hours_on_network`, một phần do vị trí bắt đầu cửa sổ mô phỏng. | Làm ablation gain/permutation; hiệu chuẩn isotonic nếu sàn tính bằng xác suất. |
| Ngưỡng BLOCK | `train.py` `threshold_for_fpr_bound` | **Đã sửa (R4-M5)** | Trước: chỉ chặn ước lượng điểm (k ≤ fpr·0,5·n), báo cáo tự ghi `upper_95ci_within_target=False` cho CALL và AUDIO. | Nay chọn ngưỡng thấp nhất có cận trên Clopper–Pearson một phía 95 % ≤ 1e-4. Không chứng minh được thì **tắt BLOCK** (AUDIO: 2 965 mẫu âm < 29 957 cần thiết, nên chỉ còn FLAG). |
| Ngưỡng FLAG + sàn | `train.py` `threshold_for_fpr`, `FLOORS` | Phù hợp, có lưu ý | Công thức thống kê thứ tự đúng, kể cả khi điểm bằng nhau. Với SMS, sàn 0,9/0,5 mới là thứ quyết định. | Ghi rõ sàn quyết định điểm vận hành. |
| Chính sách "phải có bằng chứng hành vi" | `detector.py` `has_behavioural_evidence` | Phù hợp, có lưu ý | Chính luật này (không phải ngưỡng) đưa FPR chặn về 0 trên test. Các mốc của nó được đặt tay. | Từ R4-M3, baseline drift tính quyết định sau luật này. Nên đo FPR sau chính sách trên `cal`. |
| IsolationForest theo segment (+ FastIForest) | `train.py`; `fast_iforest.py` | Phù hợp, có lưu ý | Rẻ, không cần nhãn, tách theo segment. Mẫu âm của segment trunk ít (342). Thành công "zero-day" chỉ dựa trên một họ Wangiri. | Đánh giá trên nhiều tác nhân kiểu Wangiri; median/IQR theo segment (M10, còn mở). |
| SMS: TF-IDF 1–2-gram + LogisticRegression | `train.py`; `detector.py`; `textnorm.py` | Đúng loại mô hình; **bằng chứng yếu** | PR-AUC trên val = 1,0 vì các đặc trưng meta (`route_unverified_bts`, `brand_not_via_gateway`) tách lớp theo cách mô phỏng sinh ra. | Ablation chỉ dùng chữ; mẫu lure/domain giữ lại với số lượng đủ lớn. |
| AUDIO LightGBM trên đặc trưng tính sẵn | `train.py` `train_audio` | **Chưa đủ làm bằng chứng** | Không có bộ trích nào trong repo tạo ra `vocoder_artifact`, `speaker_drift`… Đây là đặc trưng chỉ có trong mô phỏng. | BLOCK AUDIO đã bị tắt bởi R4-M5; giữ ở mức demo. |
| Mô hình giọng thật (LR + Clopper–Pearson) | `voice_pipeline.py`, `voice_model.py`, `train_voice.py` | Phù hợp, **đã nới (R4-M8)** | Phương pháp chắc: chia theo thời gian, kiểm rò theo nhóm/người nói/chiến dịch. Nhưng điều kiện "mỗi nhóm đúng một bản ghi" khiến BLOCK không bao giờ bật trên corpus thật. | Nay dùng điểm cao nhất của mỗi nhóm độc lập (có group ID) làm đơn vị. Không có group ID thì vẫn không eligible. |
| Trí nhớ thu giọng (≥ 2 nạn nhân / giờ) | `detector.py` | Phù hợp, **đã chỉnh (R4-HV)** | Khoảng 2 % đoạn im lặng thật cũng qua ngưỡng, nên tổng đài thương mại có thể bị chặn 1 giờ. | Số thương mại đã đăng ký: hạ BLOCK → FLAG (mã C004). |
| Luật dự phòng + circuit breaker | `resilience.py` | Phù hợp, **đã chỉnh (R4-M4)** | Luật giả CLI chặn cả cuộc gọi thật của ngân hàng qua cổng quốc tế (bộ mô phỏng có 3 %). | Qua `INTL_GW` thì chỉ FLAG; qua đường nội địa vẫn BLOCK. |
| PSI đặc trưng | `drift.py` | Phù hợp | Đã sửa bin cho đặc trưng nhiều số 0 (F18) và tham chiếu thực tế (H12). | `null_psi` lấy trong cùng một cửa sổ nên lạc quan; nên lấy từ hai cửa sổ tham chiếu độc lập. |
| PSI quyết định vận hành | `ops_pipeline.py` | **Đã sửa (R4-M3)** | PSI 3 dải bị ALLOW lấn át: tỷ lệ BLOCK phải tăng khoảng 80 lần mới vượt 0,10. Baseline tính quyết định thô, còn số đếm live là sau chính sách. | Thêm kiểm định nhị thức một phía cho tỷ lệ BLOCK (p < 1e-3 → alert). Baseline dùng quyết định sau chính sách. |
| Khoảng Wilson | `statistical_reporting.py` | Công thức đúng | Đang áp cho các sự kiện tương quan (66 sự kiện gian lận chỉ từ khoảng 15 tác nhân). | Bootstrap theo cụm hoặc theo số (M6, còn mở). |
| Cổng eval | `evals/run_evals.py` | **Đã sửa (R4-M2)** | Trước: một seed; seed 204 và 206 trượt cổng. | Nay gộp cửa sổ test + 10 cửa sổ lặp trải đều 24 giờ, thêm `any_recall_min_window_pct`. |
| Dựng dữ liệu | `build_dataset.py`, `data_generator.py` | Phù hợp, **đã chỉnh (R4-M7, M1, M9)** | Lừa đảo sinh không theo nhịp ngày nên mô hình học "đêm = rủi ro". Thử phản thực tế đổi sang 02:00 làm FLAG tăng 33 → 73. TAC gateway không bao giờ có trong train. ID của `val` và `cal` trùng nhau. | Lừa đảo theo nhịp ngày; cửa sổ lặp bắt đầu ở 0, 2,4, …, 21,6 giờ; farm SIM box luân phiên TAC gateway; ID theo tên cửa sổ. |
| So sánh mô hình | `compare_models.py` | **Thiếu baseline** | Chỉ so các ứng viên ML, không có baseline toàn luật, và chọn mô hình trên cửa sổ test. | Thêm baseline luật; chọn trên `val`/`cal` (còn mở). |
| Đặc trưng Spark | `spark/` | Phù hợp | Dùng lại FeatureStore theo thực thể. | H9 giới hạn `ts`, H6 bỏ partition nóng. |
| Bloom + Redis | `ram_online/core.py` | Phù hợp | Công thức m, k đúng; không có âm tính giả; có rào phiên bản. | — |
| Scale-out theo người gọi | `scaleout.py` | Phù hợp, có lưu ý | Nhìn trước tối đa một batch, đã ghi trong tài liệu. | — |

### 2.2 Vòng lặp với bộ mô phỏng (chưa sửa được nếu không có dữ liệu thật)

Một số đặc trưng tách lớp chủ yếu vì bộ mô phỏng sinh chúng như vậy:
- `cell_unknown_radio`: 45/45 dòng là gian lận.
- `route_unverified_bts` và `url_unofficial`.
- `scam_lexicon_hits` trùng với `DEEPFAKE_SCRIPTS` của bộ sinh.
- Khoảng tuổi thuê bao của kẻ gian.
- `active_hours_on_network` phụ thuộc điểm bắt đầu warm-up.

Các tác nhân "mới" ở cửa sổ test đến từ cùng mã sinh dữ liệu; chỉ Wangiri và 4 lure test là thật sự mới.

**Kết luận:** mọi con số chất lượng chỉ đo **đường ống**, không chứng nhận hiệu quả trên mạng thật.

---

## 3. Vai Staff engineer

| # | Mức | Vấn đề (vị trí) | Đã sửa |
|---|---|---|---|
| S1 | Cao | Link nhà mạng nhận `ts` tới now + 300 s, nhưng sàn LATE là max_ts − 120 s (`server.py`, `ingest_guard.py`). Một tổng đài lệch đồng hồ +130 s sẽ làm mọi trung kế khác thành LATE. | **R4-S1:** `NETWORK_MAX_FUTURE_S = 60` (< 120). |
| S2 | Cao | Từ G2, bảo trì chạy mỗi giây và sao chép rồi nạp lại **toàn bộ** feature store (`resilience.drain_rescore`). Ở quy mô lớn, owner bị treo, fail-open tăng, hàng chấm lại lại đầy thêm. | **R4-S2:** `FeatureStore.snapshot_touched`/`restore_touched`/`isolated`: chỉ sao chép các khoá mà batch chạm tới. |
| S3 | TB | Dòng audit cuối bị cắt dở khiến `import server` lỗi, không khởi động được. Ghi audit không fsync. | **R4-S3:** bỏ qua dòng hỏng khi khởi động (giữ nguyên làm bằng chứng; `verify_audit` báo `malformed`), thêm xuống dòng, ghi có fsync. |
| S4 | TB | HMAC, `json.loads` và base64 trên khoảng 17 MB chạy ngay trên event loop dùng chung với verdict 30 ms. | **R4-S4:** `asyncio.to_thread(_parse_signed_upload)`. |
| S5 | TB-Thấp | Đọc body upload không có hạn (slowloris). | **R4-N1:** hạn tổng 15 s (`FRAUD_AUDIO_READ_TIMEOUT_S`), quá hạn trả 408. |
| S6 | TB-Thấp | PSI vận hành đếm quyết định đã bị thay bằng fail-open ALLOW khi trả lời trễ. | **R4-S6:** đếm `model_decision` của sự kiện đã chấm. |
| S7 | TB | Kafka: sự kiện lệch đồng hồ hơn 2 s bị coi là "poison" và đẩy vào DLQ vĩnh viễn. | **R4-S7:** lớp `EventFromFuture` (thử lại được, không vào DLQ). |
| S8 | TB-Thấp | Sót trong H8: `first_seen` có thể bị một sự kiện mới hơn tạo trước; các liên kết IMEI/cell đếm cả liên kết "ở tương lai". | **R4-S8:** `first_seen = min`; chỉ đếm liên kết có ts ≤ sự kiện hiện tại. |
| S10 | Thấp | `checkpoint_every=5000`: khoảng 5 snapshot đầy đủ mỗi giây ở 25k eps. | **R4-S10:** 100 000 (bộ hẹn giờ 30 s vẫn giữ). |
| S11 | Thấp | `anchor_records` âm gây 500; `legacy_frontier` không được kiểm. | **R4-N9 / R4-S11.** |
| S9 | Thấp-TB | Số nhận rất nhiều cuộc gọi đến (tổng đài) bị sao chép deque mỗi batch; `bisect` trên deque. | **Còn mở** (§9): cần ring buffer theo phút. |
| — | Thấp | Đếm `runtime.stats` từ hai luồng (chỉ số đo có thể mất vài lần cộng). Bỏ `_party_max` theo thứ tự cập nhật (bộ nhớ có giới hạn). | Còn mở; không ảnh hưởng đúng/sai. |
| — | Vận hành | Nâng bundle (tfd-v2.3 → v2.4) khiến `FRAUD_STATE_DB` cũ từ chối khởi động (model identity khác). | Ghi ở §8: cần thư mục state mới và 24 giờ warm-up. |

---

## 4. Vai bảo mật

| # | Mức | Vấn đề | Đã sửa |
|---|---|---|---|
| N1 | TB | Slowloris giữ cả 8 slot upload âm thanh mà không cần bí mật nào. | R4-N1 (hạn đọc 15 s, trả 408). |
| N2 | TB-Thấp | Nhãn `method` của Prometheus không giới hạn; method tự chế làm phình `/metrics` cho tới khi OOM. | R4-N2: chỉ các method chuẩn, còn lại là `OTHER`. |
| N3 | TB-Thấp | G14 chỉ dọn lịch sử khi có người **nhận** số, không dọn khi **nhả** số; số giả lập tái cấp mang theo người giám hộ cũ. | R4-N3: dọn cả số cũ khi đổi sang số mới và khi cấp số giả lập. |
| N4 | TB-Thấp | Deploy thiếu `DEPLOY_KNOWN_HOSTS` thì chỉ cảnh báo rồi tin mọi host key. | R4-N4 (**quyết định chủ dự án**): dừng deploy; khoá SSH được xoá bằng `trap`. |
| N5 | TB-Thấp | `FORWARDED_ALLOW_IPS` vẫn quay về `*` ở `proxy_ip.sh`, compose và `serve.py`. | R4-N5 (**quyết định**): không thấy Caddy thì dừng; mặc định chỉ tin `127.0.0.1`; đặt `TELECOM_PROXY_CONTAINER` nếu tên khác. |
| N6 | TB-Thấp | EduDSS không truyền `JWT_AUDIENCE` vào compose, nên kiểm `aud` (G25) không bao giờ bật. | R4-N6 (**quyết định**): compose truyền biến này; ngoài demo, API không khởi động khi thiếu. |
| N7 | Thấp | AutoML kiểm `course_id` nhưng `dataset_ref` là chữ tự do. | Còn mở: hợp đồng với adapter (adapter không nằm trong repo). |
| N8 | Thấp | App chính không có `X-Frame-Options`/`frame-ancestors` (clickjacking). | R4-N8: thêm `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`, `nosniff`. |
| N9 | Thấp | Tham số neo audit âm hoặc thiếu một nửa. | R4-N9: `ge=1`, hash 64 hex, phải gửi đủ cả hai. |
| N10 | Thấp | Healthcheck Redis của EduDSS dùng `--pass`; `deploy.sh` tự sửa chính nó khi đang chạy; sw.js cache cả phản hồi lỗi và script bên thứ ba; `/api/app/lookup` không giới hạn tần suất. | R4-N10: `REDISCLI_AUTH`; chạy từ bản sao tạm; cache chỉ phản hồi `ok` cùng nguồn; lookup 120 lần/giờ. |
| — | Thấp | `run_demo.py` mở `0.0.0.0` ở chế độ demo. | **Giữ nguyên có chủ đích:** README hướng dẫn mở app từ điện thoại cùng Wi-Fi. |
| — | Thấp | Chưa có CSP đầy đủ cho script nội tuyến. | Còn mở: cần hash/nonce cho từng script nội tuyến. |

**Đã kiểm và thấy đúng:**
- `metrics_authorized` (H1).
- DLQ có mang cấu hình TLS, acks=all, idempotence.
- Regex của report/invite.
- Claim demo của EduDSS.
- Header của `smoke.sh` nằm trong file quyền 600.
- Mọi điểm `innerHTML` nhận dữ liệu từ bên ngoài đều qua `esc()`.
- Service worker không cache `/api/*`.
- Android: liên kết ngoài mở trình duyệt, release bắt buộc HTTPS, không có JS bridge.

---

## 5. Vai Designer/UX

| Giao diện | Điểm trước | Điểm sau vòng 4 | Lý do chính |
|---|---:|---:|---|
| App thuê bao `static/app/index.html` | **7/10** | **7,5/10** | Mạnh: phân cấp rõ, tiếng Việt dễ hiểu, theme sáng/tối, vùng chạm 46–56 px. Đã sửa: `aria-label` cho mọi ô nhập, toast `aria-live`, độ tương phản `.v-info`, ẩn nút DEMO ở chế độ thật, ngừng gọi server khi tab ẩn. Còn mở: hàng lịch sử là `div` bấm được, overlay chưa là dialog thật, chữ 11 px, tab đang chọn tô đỏ. |
| Bảng vận hành `static/admin/index.html` | **5,5/10** | **6,5/10** | Đã sửa: feed và đề xuất không làm mới khi đang trỏ chuột (nút Duyệt không dịch chỗ); lý do xem số và ghi chú duyệt gửi trong body POST và bắt buộc nhập; chữ tối trên đỏ; nhãn ô đăng nhập; nút đóng có nhãn; bỏ `<a><button>` lồng nhau; "mục tiêu <30 ms"; badge phiên bản. Còn mở: chữ IN HOA giãn khó đọc dấu, nhãn 10,5 px, biểu đồ canvas không có mô tả, phụ thuộc Google Fonts. |
| Trang quản trị `static/admin/manage.html` | **5/10** | **6/10** | Đã sửa: hỏi xác nhận khi xoá chi phí, bắt buộc ghi căn cứ khi duyệt, `esc()` cho mọi giá trị nội suy, bỏ `<a><button>`. Còn mở: chưa có watermark (README có hứa), tab chưa là `tablist`, khoá nhân viên không hỏi xác nhận. |
| Android `android/` | **5/10** | **5,5/10** | Đã có từ vòng 2: liên kết ngoài mở trình duyệt, cấu hình ký release. Còn mở: màu chủ đạo đỏ `#E11D48` (lệch với brand xanh lơ), chuỗi hiển thị cho người dùng còn lời dặn dev ("python run_demo.py"), chưa có thanh tiến trình lần tải đầu, chưa có push. |
| EduDSS Streamlit `edu_dss/ui/app.py` | **3,5/10** | **3,5/10** | Đăng nhập bằng cách dán JWT, bộ chọn vai trò chỉ để trang trí, kết quả là JSON thô, nhãn lẫn Anh–Việt. Chỉ thêm ô `course_id` cho AutoML. Cần làm lại (§9). |

---

## 6. Vai QA (chạy thật)

**Môi trường:**
- `serve.py` trong WSL, cổng 18080.
- `FRAUD_PUBLIC=1`, `METRICS_TOKEN` và mật khẩu admin sinh ngẫu nhiên (không in ra).
- Bundle `tfd-v2.4`, manifest `verified`.

**Kết quả: 32/32 luồng qua** (`qa_flows.py`, chạy cả trên tfd-v2.3 và tfd-v2.4).

| Nhóm | Luồng đã kiểm |
|---|---|
| Trang | `/`, `/app/`, `/admin/`, `/admin/manage`, `/health/live` đều 200. Header `X-Frame-Options: DENY` và CSP `frame-ancestors` có mặt. |
| Metrics | Không token: 401. Có token: 200, có `tfs_ingest_unscored`. Đăng nhập demo kiểu cũ: 403. |
| Thuê bao | Đăng ký; email trùng 400; sai mật khẩu 401; nhận số giả lập; inbox; mô phỏng giả CLI tới số mình và inbox nhận đúng; tra số tổng đài trả mức `info`; số sai định dạng 422; báo cáo số; chấp nhận giám hộ khi không có lời mời: 404. |
| Vận hành | Đăng nhập mật khẩu; `/api/state` (số liệu được che); audit hợp lệ, có `head`, kiểm theo neo hợp lệ; chế độ mạng `shadow`. |
| Link nhà mạng | Verdict có ký: `recommended_action = NONE` ở shadow. Gửi lại: trả kết quả đã lưu (`duplicate`). Hai người gọi lệch 1 ms: cả hai `ADMITTED` và được chấm (H8). Không ký: 401. ts tính bằng ms: 400. CDR: 202. Upload WAV có ký: 200. |

**Quan sát thêm khi QA.**
- Một cuộc gọi QA "bình thường" lần thứ hai ra **BLOCK**. Mã lý do R202, R305, R306 và Z001 cho thấy: số mới hoạt động, dùng chung IMEI với SIM QA khác, gọi dồn. Đó đúng là mẫu SIM box, không phải lỗi.
- Cuộc gọi đầu tiên sau khi khởi động bị fail-open vì lần chấm đầu chậm trên laptop.

---

## 7. Kết quả kiểm thử

### 7.1 Bộ test

Chạy trên WSL, Python 3.12.3, venv ghim theo `requirements-serve.txt`, trên bản sao sạch của cây làm việc.

| Bộ | Trước vòng 4 (`babe2dd`) | Sau vòng 4 | Ghi chú |
|---|---|---|---|
| `tests/` | 476 passed | **504 passed** | +28 test vòng 4 (28/28 thất bại trên `babe2dd`). Hai test cũ được sửa theo quyết định chủ dự án: `test_commercial_registry::test_thu_giong_*` (C004) và `test_security_resilience::test_spoofed_hotline_*` (INTL_GW → FLAG). |
| `spark/` | 24 passed, 65 subtests | **24 passed**, 65 subtests | |
| `edu_dss/backend/tests` | 16 passed, 4 subtests | **16 passed**, 4 subtests | |
| `telecom_grpc/tests` (chạy từ gốc repo) | 2 passed | **2 passed** | |
| `ram_online/` (fakeredis) | 22/23 | **22/23** | Ca hỏng duy nhất `test_window_rejects_call_id…` là do giả lập fakeredis xử lý `error_reply` của Lua; hỏng y hệt trên HEAD gốc. **Môi trường**, không phải lỗi mã. |
| QA end-to-end | — | **32/32** | §6 |
| pyflakes | — | sạch | Chỉ còn cảnh báo fixture `env` và một f-string cũ trong test. |

### 7.2 Eval (bundle tfd-v2.4, gộp cửa sổ test + 10 cửa sổ lặp trải đều 24 giờ)

Lệnh: `python -B evals/run_evals.py` (mặc định `--windows 10`).

| Chỉ số | Ngưỡng | tfd-v2.3 (1 cửa sổ test) | **tfd-v2.4 (gộp 11 cửa sổ)** |
|---|---|---|---|
| block_recall_pct | ≥ 45 | 54,55 | **43,5 ✗** |
| block_precision_pct | ≥ 95 | 100,0 | 99,6 |
| block_fpr_pct / cận trên 95 % | ≤ 0,01 (cận trên) | 0,0 / 0,0077 | **0,0003 / 0,0017** |
| any_recall_pct | ≥ 80 | 86,36 | 81,11 |
| any_recall_min_window_pct (mới) | ≥ 70 | — | **68,57 ✗** |
| any_fpr_pct | ≤ 0,2 | 0,1142 | 0,0703 |
| family_min_detected_pct | ≥ 75 | 80,0 | **68,2 ✗** (SIM_BOX) |
| zero_day_caught_pct | ≥ 80 | 91,7 | 90,2 |
| serving p99 / trong 30 ms / fail-open | ≤ 30 / ≥ 95 / 0 | 31,1 / 98,5 / 3 | **28,1 / 99,0 / 1 ✗** |
| evidence_chain_valid, smoke_passed | true | true | true |

Theo họ lừa đảo (gộp):

| Họ | Phát hiện | Ghi chú |
|---|---|---|
| CLI_SPOOF | 100 % | |
| SMS_PHISH | 100 % | |
| ZERO_DAY_WANGIRI | 90,2 % | |
| DEEPFAKE_VOICE | 87,1 % | Chỉ còn FLAG, vì BLOCK AUDIO đã bị tắt |
| SIM_BOX | 68,2 % | Họ yếu nhất |

**Cách đọc.**
- Cổng **đỏ ở 3 chỉ tiêu chất lượng**. Lý do: cổng giờ đo trung thực trên 11 cửa sổ trải cả ngày đêm, thay vì một seed may mắn. Chủ dự án đã được báo trước điều này khi chọn M2.
- Baseline hồi quy phần chất lượng được chốt lại theo định nghĩa gộp. Baseline phục vụ (đo trên runner CI) giữ nguyên.
- Các ngưỡng mục tiêu **không** bị hạ để "cho xanh".
- Ở cửa sổ ban đêm, số FLAG nhầm thấp (11 ở 00:00, 5 ở 21:36). Mô hình không còn mặc định coi "đêm = rủi ro" (M7).
- Hiệu chuẩn CALL: ngưỡng BLOCK theo cận Clopper–Pearson một phía 95 % ≤ 1e-4 (k = 5 FP trên 105 427 mẫu âm). Báo cáo huấn luyện vẫn ghi `upper_95ci_within_target=False` vì nó dùng khoảng Wilson **hai phía**, chặt hơn (1,11e-4).

---

## 8. Danh sách bản sửa vòng 4 và quyết định của chủ dự án

**Quyết định ngày 17/11/2026.** Chủ dự án đã chọn tất cả các mục:
- ML: M5, M7 + M1, M4, M2.
- Fail-closed: N4, N5, N6.
- Giọng nói: miễn trí nhớ thu giọng cho số thương mại; nới điều kiện eligible (M8).
- UI: lý do qua form; feed không nhảy; ẩn DEMO.

| Nhóm | Mã | File chính |
|---|---|---|
| Bảo mật | R4-N1, N2, N3, N4, N5, N6, N8, N9, N10 | `voice_api.py`, `observability.py`, `portal.py`, `server.py`, `.github/workflows/deploy.yml`, `deploy/cloud/*`, `serve.py`, `edu_dss/*`, `static/app/sw.js` |
| Staff | R4-S1, S2, S3, S4, S6, S7, S8, S10, S11 | `server.py`, `feature_store.py`, `resilience.py`, `security.py`, `voice_api.py`, `ram_online/streaming.py`, `backend_runtime.py`, `ingest_guard.py` |
| ML / dữ liệu | R4-M1, M2, M3, M4, M5, M7, M8, M9, R4-HV | `data_generator.py`, `evals/run_evals.py`, `thresholds.json`, `ops_pipeline.py`, `make_drift_baseline.py`, `resilience.py`, `train.py`, `evaluate.py`, `voice_model.py`, `build_dataset.py`, `commercial_registry.py`, `reason_codes.py` |
| UX | R4-UX1…UX3 và các sửa nêu ở §5 | `static/admin/index.html`, `static/admin/manage.html`, `static/app/index.html` |
| Tài liệu | R4-DOC1 | `README.md` (bảng trạng thái đo được) |
| Bundle | tfd-v2.4 | `runtime/artifacts_v22/*` (dựng lại bằng `rebuild_artifacts.py`) |

**Lưu ý vận hành mới:**
- Deploy cần secret `DEPLOY_KNOWN_HOSTS`.
- Nếu Caddy không tên `fieldops-caddy-1` hay `telecom-caddy-1`, đặt `TELECOM_PROXY_CONTAINER`.
- EduDSS ngoài demo cần `JWT_AUDIENCE` (xem `edu_dss/.env.example`).
- Nâng lên tfd-v2.4 cần thư mục `FRAUD_STATE_DB` mới và 24 giờ warm-up.
- API `/api/reveal` và `/api/agent/{id}/decide` nhận JSON body; query cũ vẫn được nhận để tương thích.

---

## 9. Còn lại và lý do

| Mức | Mục | Lý do chưa làm |
|---|---|---|
| **TB–Cao, bắt buộc sửa sau** | **D14: checkpoint tăng dần.** Checkpoint vẫn là snapshot đầy đủ trong giao dịch ghi. R4-S2 đã bỏ việc sao chép toàn store khi chấm lại; R4-S10 giảm tần suất checkpoint theo số sự kiện. | Viết lại checkpoint và phục hồi cần môi trường tải thật để đo. Chủ dự án chọn "phương án tối ưu lúc này, sau vẫn phải sửa". Kế hoạch ở báo cáo 01 §3c. |
| Chiến lược | Adapter nhà mạng, dữ liệu thật có nhãn, inbox bền + FCM, OTP qua SMSC, gộp về một đường online, tách EduDSS | Quyết định sản phẩm và đầu tư (§1), không phải lỗi mã. |
| TB | ML: hiệu chuẩn xác suất, ablation, baseline toàn luật trong `compare_models.py`, CI theo cụm tác nhân (M6), median/IQR theo segment cho mã lý do zero-day (M10), mô hình riêng cho thu giọng im lặng | Cần dữ liệu thật hoặc thiết kế đánh giá mới; không đổi được kết luận trên dữ liệu mô phỏng. |
| TB | Cổng eval đang đỏ (recall chặn 43,5; SIM box 68,2; min-window 68,6) | Đây là kết quả đo trung thực. Cải thiện cần nghiên cứu mô hình SIM box, không nên hạ ngưỡng. |
| Thấp–TB | S9: số nhận rất nhiều cuộc gọi đến bị sao chép deque mỗi batch | Cần đổi cấu trúc dữ liệu (ring buffer theo phút) và đo hiệu năng. |
| Thấp | N7: ràng buộc dataset ↔ khoá học trong EduDSS | Thuộc hợp đồng adapter bên ngoài repo. |
| Thấp | CSP đầy đủ cho script nội tuyến | Cần tách script hoặc thêm hash cho từng khối. |
| Thấp | UX: dialog/focus trap, `tablist`, watermark ở `/admin/manage`, chữ ≥ 12 px, màu Android, EduDSS UI | Thay đổi giao diện lớn hơn; nên làm cùng nhà thiết kế. |
| Thấp | Android: `isMinifyEnabled=false` | Không có Android SDK để build và kiểm R8. |
| Thấp | Đếm `runtime.stats` không khoá; thứ tự dọn `_party_max` | Chỉ ảnh hưởng số đo và bộ nhớ (có giới hạn), không ảnh hưởng quyết định. |
| Môi trường | Docker build, Redis/Kafka thật, Android build, GitHub Actions thật | Máy không có Docker daemon, Java hay Android SDK; các phần này mới chỉ được kiểm tĩnh (YAML parse, `bash -n`, `docker compose config`). |
