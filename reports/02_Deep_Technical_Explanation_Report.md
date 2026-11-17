# Báo cáo giải thích kỹ thuật chuyên sâu

**Ngày:** 17/11/2026.

**Phạm vi:** mã trong `D:\Du_An_1_Telecom_AI_Full\v2` sau các bản sửa F1–F18 (vòng 1), G1–G27 (vòng 2) và H1–H12 (vòng 3) của [01_Technical_Code_Audit_Report.md](01_Technical_Code_Audit_Report.md). Các bản sửa được commit trên nhánh `audit/telecom-v2-fixes` (chưa push).

**Nguồn công thức:** mọi công thức dưới đây được đọc trực tiếp từ mã, kèm vị trí. Nhãn "giới hạn" nói rõ điều mà công thức không chứng minh được.

**Chi tiết theo từng hệ thống:** xem [he-thong/](he-thong/README.md).

---

## 1. Hệ thống giải quyết bài toán gì

Telecom Fraud Shield đứng **song song** với tổng đài, không nằm trên đường đi của cuộc gọi.

- **Đầu vào:** nhà mạng gửi một bản sao báo hiệu, gồm CALL lúc thiết lập, SMS, các đoạn AUDIO trong cuộc gọi, và CDR khi kết thúc.
- **Đầu ra:** khuyến nghị BLOCK, FLAG hoặc ALLOW, kèm mã lý do, trong khoảng 30 ms (CALL) hoặc 170 ms (SMS/AUDIO) (`src/config.py:22-24`).
- **Khi lỗi:** nếu hệ thống chậm hoặc sập, tổng đài cứ nối cuộc gọi (fail-open).
- **Chế độ:** mặc định là `shadow`, tức chấm điểm nhưng khuyến nghị luôn là `NONE`. Chỉ admin mới chuyển được sang `enforce`.

Dữ liệu huấn luyện và đánh giá đều **mô phỏng** (`src/data_generator.py`). Mọi con số chất lượng vì vậy chỉ có giá trị trên phân phối của bộ mô phỏng.

---

## 2. Luồng request

### 2.1 VERDICT từ nhà mạng

1. **Vào `POST /api/v1/verdict`** (`server.py` `network_request`):
   - Đọc body theo luồng, cắt ở **64 KB** (trả 413).
   - `security.verify_network` kiểm `HMAC-SHA256(key, ts + "." + body)` bằng so sánh constant-time.
   - Mốc thời gian phải nằm trong |now − ts| ≤ 30 s. Chữ ký đã thấy bị nhớ trong 60 s để chống phát lại.
   - `validate_event` kiểm: loại sự kiện, trường bắt buộc, trường lạ, số hữu hạn và không âm, độ dài chuỗi (200 ký tự, hoặc 10.000 với `text`), các giá trị nằm trong [0, 1].
   - **Mới (F7):** từ chối `ts` hoặc `end_ts` lớn hơn 1e11. Với link nhà mạng, mốc kiểu Unix (≥ 1e9) vượt đồng hồ thật hơn 300 s cũng bị từ chối.
2. **`InferenceRuntime.submit`** (`backend_runtime.py`):
   - Admission cho phép tối đa 129 request đang xử lý; hàng đợi chứa 128.
   - Kiểm cache RAM 4096 kết quả gần nhất, khoá theo `(kind, event_id)` cộng SHA-256 của JSON chuẩn hoá. Bản lặp y hệt nhận lại kết quả cũ; bản khác nội dung bị 409.
3. **Owner thread** (một luồng duy nhất sở hữu model state) lấy batch ≤ 16 sự kiện. Trong **một** giao dịch `BEGIN IMMEDIATE`, nó chạy lần lượt:
   - `reserve`: ghi journal, đảm bảo idempotent;
   - `IngestGuard.admit`: phân loại DUPLICATE, LATE hoặc ADMITTED;
   - `GuardedDetector.score_batch`: chấm trong deadline, có breaker và luật dự phòng;
   - `_finish`: ghi kết quả.
   - **Mới (F9):** trước checkpoint định kỳ (mỗi 5000 sự kiện hoặc 30 s), `_compact_state` dọn state đã hết hạn.
4. **Sau COMMIT:**
   - Future được giải quyết. `on_result` cập nhật dashboard và đánh thức bộ ghi bằng chứng.
   - Bộ ghi bằng chứng fsync JSONL **trước** khi ack outbox.
5. **Chờ phía HTTP:** `await_precise_deadline` chờ tới deadline. Quá hạn thì trả fail-open, nhưng job vẫn commit và có thể sinh cảnh báo hồi tố.
6. **Trả lời:** `recommended_action = decision` **chỉ khi** thoả cả bốn điều kiện:
   - `mode == enforce`;
   - `ingest == ADMITTED`;
   - `actionable`;
   - không phải bản lặp.

   Ngược lại trả `NONE`.

### 2.2 CDR

Đi qua cùng pipeline với ngân sách 200 ms. `commit_cdr` đẩy `(end_ts, duration)` vào heap theo từng số gọi. Heap này chỉ hiện ra cho những cuộc gọi **có ts ≥ end_ts**, nên đặc trưng luôn có tính nhân quả. Một CDR đối chiếu với setup: khác `caller`, `callee`, `call_id` hoặc `ts` thì bị 409.

### 2.3 Phục hồi sau sự cố

`_restore`:
1. Nạp checkpoint: feature store, guard, deny list, hàng chấm lại, retro.
2. Phát lại `journal_after(seq)` theo đúng thứ tự journal.
3. Chấm lại các VERDICT đã ADMITTED. Bỏ qua các dòng OBSERVATION/RETRO.
4. Dựng lại danh sách chặn từ các chuyển trạng thái đã ghi. Kết luận đến muộn không được chấm hai lần.

Model identity được ghim: checkpoint của bundle khác thì từ chối phát lại.

### 2.4 Thuê bao và nhân viên

**App thuê bao (`/app/`, `portal.py`, `accounts.py`):**
- Đăng ký bằng email và mật khẩu scrypt, hoặc Google ID token.
- Gắn số theo một trong hai cách:
  - cấp số giả lập (chỉ ở chế độ demo);
  - OTP SMS: mã 6 số, TTL 300 s, tối đa 5 lần sai. **Mới (F1):** lần đếm sai được commit thật.
- Mọi `/api/app/*` lấy số từ **phiên**, không lấy từ tham số.

**Bảng vận hành (`/admin/`):**
- Đăng nhập bằng tên và mật khẩu, hoặc Google, hoặc token cá nhân. Phiên có hạn 8 giờ, hoặc 30 phút không thao tác.
- Vai trò theo thứ tự `viewer < analyst < admin`. **Mới (F3):** vai trò đọc lại từ tài khoản ở mỗi request.
- Số điện thoại bị che với **mọi** vai trò. `/api/reveal` cần quyền analyst, phải ghi lý do, và được ghi vào audit có chữ ký.

---

## 3. Ranh giới tin cậy

| Ranh giới | Ai ở phía ngoài | Kiểm soát | Còn hở |
|---|---|---|---|
| Link nhà mạng | Thiết bị mạng giữ khoá HMAC chung | HMAC + ±30 s + chống phát lại + 64 KB + `validate_event` + giới hạn `ts` (F7) | Khoá đối xứng nằm trên server. Nội dung chuỗi (`sender`…) vẫn do bên ngoài kiểm soát; vì vậy F11 escape ở UI. |
| Trình duyệt thuê bao | Bất kỳ ai | Phiên token 32 byte (lưu SHA-256), rate limit theo IP và email, OTP. Từ vòng 2: số đổi chủ thì lịch sử bị xoá (G14); tài khoản không tồn tại vẫn chạy scrypt giả (G9). | `register` báo email đã dùng. |
| Trình duyệt nhân viên | Nhân viên | Phiên ngắn hạn, vai trò hiện tại (F3), che số, watermark, audit HMAC theo người | Token lưu trong `sessionStorage` (XSS sẽ đọc được; F11 đã bịt một đường). Khoá ký nằm trên server. |
| Proxy (Caddy) | Internet | TLS, ghi đè `X-Forwarded-For`. Từ G26, `FORWARDED_ALLOW_IPS` = IP của Caddy (`proxy_ip.sh`). | Caddy đổi IP mà chưa deploy lại thì XFF bị bỏ qua: rate limit theo IP trở thành chung cho mọi người. Không tìm thấy Caddy thì quay về `*` kèm cảnh báo. |
| Agent AI | Mã nội bộ | Chỉ đề xuất. Hành động chỉ chạy trong `decide()` khi admin duyệt; đề xuất và quyết định đều được ký. | — |
| RAM online / gRPC | Adapter tổng đài | mTLS bắt buộc, E.164, API key so constant-time | UNKNOWN thì được ánh xạ thành cho qua (fail-open có chủ đích). |
| EduDSS | IdP phát JWT | HS256 ghim, `iss`, `exp`, quyền theo khoá học | Không kiểm `aud`. Token demo (D10). AutoML không phân quyền dataset (D9). |
| Build/deploy | CI, VM | Chỉ deploy đúng SHA của push lên main (F17). Loại khoá khỏi build context (F13). Ghim host key qua `DEPLOY_KNOWN_HOSTS`, smoke test kiểm TLS, promtool kiểm sha256 (G26). | Khi chưa đặt `DEPLOY_KNOWN_HOSTS` vẫn là `accept-new` (có cảnh báo). |

---

## 4. Thuật toán: công thức, ví dụ và giới hạn

### 4.1 Đặc trưng cửa sổ trượt (`feature_store.py`)

**Cửa sổ:** W ∈ {60, 600, 3600, 86400} s (`M1`, `M10`, `H1`, `D1`). Khoảng đóng `[ts − W, ts]`: phần tử có `t < ts − W` bị bỏ.

**Các đặc trưng chính:**
- `calls_1m`, `calls_10m`, `calls_1h`, `out24` là số phần tử trong cửa sổ tương ứng.
- `callee_dispersion = distinct_callees_1h / calls_1h`. Luôn có calls_1h ≥ 1 vì tính cả cuộc hiện tại.
- `in_out_ratio = (in24 + 1) / (out24 + 1)`.
- `short_call_ratio` và trung bình `log1p(duration)` tính trên 20 CDR gần nhất **đã kết thúc trước ts**.
- `msisdn_per_imei_24h`, `imei_per_msisdn_24h`: đồ thị SIM–máy trong 24 giờ.
- `new_sims_on_cell_1h`: số SIM có `active_h < 6` xuất hiện tại trạm đó trong 1 giờ.
- `active_hours_on_network = min(72, (ts − first_seen)/3600)`.
- `velocity_kmh = haversine(cell_trước, cell_nay) / max(Δt_h, 1/60)`, chặn trên ở 5000 km/h.
- Haversine: `d = 2R·asin(√h)`, với `h = sin²(Δφ/2) + cos φ₁ cos φ₂ sin²(Δλ/2)` và R = 6371 km.

**Ví dụ 1.** Một số gọi 30 cuộc đi trong 24 giờ và không nhận cuộc nào.
- `in_out_ratio = 1/31 ≈ 0,032`.
- Nếu 20 cuộc gần nhất có 15 cuộc dưới ngưỡng ngắn thì `short_call_ratio = 0,75`. Kết hợp với `completed_calls_seen ≥ 3`, đây là một "bằng chứng hành vi" (`detector.py` `has_behavioural_evidence`).

**Ví dụ 2.** Hai trạm cách 1° kinh độ tại xích đạo, tức khoảng 111,2 km, cách nhau 10 phút.
- `Δt = 1/6 h`, nên vận tốc = 111,2 × 6 ≈ 667 km/h.
- Kết quả ≥ 300, nên tính là "di chuyển phi lý".

**Compaction (F9).**
- `prune_state(h)` bỏ các phần tử của mọi cửa sổ có `t < h − W`. Nó cũng chuyển CDR đã kết thúc trước h sang lịch sử, và xoá các khoá rỗng.
- Mốc `h = min(guard.scoring_floor, ts nhỏ nhất trong hàng chấm lại)`, với `scoring_floor = max_ts − 120 s` (từ H8). Lý do an toàn:
  - mọi sự kiện được chấm sau đó đều có ts ≥ `scoring_floor`, vì sự kiện cũ hơn là LATE;
  - các sự kiện chấm lại dùng ts riêng của chúng, và không ts nào nhỏ hơn h.
- Metadata (`first_seen`, tuổi, lịch sử cuộc đã xong) được giữ, nên đặc trưng không đổi.

**Giới hạn.**
- **Giờ trong ngày (H11, D6).** `hour_of_day = local_hour(ts) = ((ts + 25 200)/3600) mod 24`, tức giờ Việt Nam cho ts Unix (UTC).
  - Bộ mô phỏng, FeatureStore và Spark dùng chung quy ước này.
  - Ví dụ: ts = 1 794 880 800 (17/11/2026 02:00 UTC) cho hour_of_day = 9,0.
  - Trước H11 công thức là `(ts/3600) mod 24`, cho ra 2,0 với cùng ts đó, trong khi mô hình học trên giờ địa phương.
- Vị trí trạm là ánh xạ mô phỏng, không phải định vị vô tuyến.

### 4.2 Chống trùng và trễ (`ingest_guard.py`)

- Khoá sự kiện `event_key(ev)` được nhớ 900 s theo thời gian sự kiện (`DEFAULT_DEDUP_TTL_S`).
- **Frontier theo từng bên gọi (H8, D1).** Với p là caller (CALL/AUDIO) hoặc sender (SMS):
  `LATE ⇔ ts < max_ts_p ∨ ts < max_ts − L`, với L = `allowed_lateness_s` = 120 s.
- Ví dụ:
  - A gọi lúc 2000,000, rồi B gọi lúc 1999,999 → **ADMITTED** (trước H8 là LATE).
  - A gọi tiếp lúc 1999 → LATE: lịch sử của chính A không được lùi.
  - C gọi lúc 1850 → LATE: cũ hơn 2000 − 120.
- Frontier của các bên thấp hơn sàn toàn cục bị xoá, nên bộ nhớ giới hạn theo số bên hoạt động trong 120 s gần nhất.
- Các cấu trúc chéo giữa các bên trong FeatureStore chịu được lệch thứ tự ≤ L:
  - `in_calls` được chèn đúng thứ tự (`bisect`), và chỉ đếm cuộc gọi đến có ts ≤ ts hiện tại;
  - `last_seen`, liên kết IMEI và SIM mới dùng `max`.
- Với luồng đúng thứ tự, kết quả giống hệt trước.
- Theo `README` (thí nghiệm `data/guard_experiment.json`), khi lặp lại 20 % sự kiện: guard tắt sinh 793 quyết định sai khác, guard bật chỉ còn 1.

**Giới hạn.**
- Một bên gọi vẫn không được lùi thời gian của chính mình, và mọi sự kiện cũ hơn 120 s so với mốc toàn cục vẫn là LATE.
- Các sự kiện LATE được đếm riêng (G7) trong `ingest_unscored` / `tfs_ingest_unscored`, và có cảnh báo.
- Trước F7, một `ts` sai đơn vị đủ để tắt toàn bộ hệ thống.

### 4.3 Mô hình tầng 1 và tầng 2 (`train.py`, `detector.py`)

**CALL.**
- LightGBM nhị phân trên `CALL_FEATURES`, cho xác suất p.
- Quyết định: `BLOCK nếu p ≥ θ_block`, ngược lại `FLAG nếu p ≥ θ_flag`, còn lại `ALLOW`.
- **Chính sách chặn:** BLOCK mà không có ít nhất một bằng chứng hành vi thì hạ xuống FLAG. Các bằng chứng hợp lệ gồm:
  - giả CLI được bảo vệ;
  - trạm lạ;
  - TAC gateway;
  - ≥ 2 SIM/máy hoặc ≥ 2 máy/SIM;
  - `calls_10m ≥ 5` hoặc `calls_1h ≥ 12`;
  - nhiều cuộc cực ngắn;
  - cụm ≥ 6 SIM mới cùng `calls_10m ≥ 2`;
  - vận tốc ≥ 300 km/h.

**Zero-day.** IsolationForest theo segment (domestic / foreign / trunk), huấn luyện chỉ trên hàng sạch, nâng ALLOW thành FLAG.

**SMS.**
- Chuẩn hoá bỏ dấu, gấp leet (`textnorm.py`), rồi lấy n-gram 1–2 của `[a-z0-9]+`.
- Trọng số `(1 + ln tf)·idf`, chuẩn hoá L2.
- `p = σ(w·x + w_meta·(meta/scale) + b)` với `scale = [1,1,1,1,1,3,1,1,20]`. Logistic regression C = 4, `class_weight=balanced`.

**AUDIO.** LightGBM trên các đặc trưng âm học đã tính sẵn.

**Trí nhớ thu giọng.**
- Đoạn AUDIO có `caller_speech_ratio < 0,15` và `callee_speech_s ≥ 1` được ghi là "một nạn nhân".
- Có ≥ 2 nạn nhân trong 3600 s thì mọi CALL/AUDIO tiếp theo của số đó bị BLOCK (mã A108).
- **Mới (F8):** số trong `PROTECTED_CLI` không bao giờ vào trí nhớ này.

**Ngưỡng theo FPR** (`train.py:57-70`):
- k = ⌊fpr · margin · n⌋ với margin = 0,5.
- θ = nextafter(s₍n−k₎, +∞), trong đó s₍n−k₎ là điểm lớn thứ k+1 của các mẫu hợp lệ.
- Áp dụng với quy tắc `≥`, số FP trên tập hiệu chỉnh luôn ≤ k, kể cả khi có điểm bằng nhau.
- **Ví dụ:** n = 100.000 mẫu hợp lệ, mục tiêu FPR chặn 1e-4: k = ⌊100.000 × 1e-4 × 0,5⌋ = 5. θ nằm ngay trên điểm lớn thứ 6, nên tối đa 5 mẫu hợp lệ bị chặn.
- Sàn sau khi tính: call 0,5 / 0,05; SMS và audio 0,9 / 0,5.

**Giới hạn.**
- Xác suất **không được hiệu chuẩn** (manifest ghi `probabilities_calibrated: false`); ngưỡng chỉ dùng thứ hạng.
- "0 FP trên tập hiệu chỉnh" không có nghĩa là FPR thật bằng 0. Xem Wilson ở §4.5.

### 4.4 Isolation Forest nhanh (`fast_iforest.py`)

**Công thức.**
- `c(n) = 2(ln(n−1) + γ) − 2(n−1)/n` khi n > 2; c(2) = 1; c(n) = 0 khi n ≤ 1. Hằng số γ ≈ 0,5772.
- Giá trị lá = độ sâu + c(n_lá).
- Điểm `s = 2^(−E[h]/c(ψ))`, trong đó E[h] là trung bình trên 100 cây và ψ = min(1024, n).
- Kết quả khớp `-score_samples` của sklearn: cùng lượng tử hoá float32, và trường hợp mẫu số bằng 0 cho 0,5.

**Ví dụ** với ψ = 256: c(256) = 2(ln 255 + 0,5772) − 2·255/256 ≈ 12,237 − 1,992 = 10,245.
- E[h] = 4 cho s = 2^(−0,390) ≈ 0,76: tách rất nhanh, bất thường.
- E[h] = 10 cho s ≈ 0,51: bình thường.

**Giới hạn:** "hiếm" không đồng nghĩa với "gian lận". Ngưỡng zero-day theo từng segment lần lượt là 5e-4, 1e-2 và 5e-3.

### 4.5 Thống kê cho FPR (`statistical_reporting.py`)

**Khoảng Wilson** với p = k/n và z = 1,96:
- `d = 1 + z²/n`
- `c = p + z²/(2n)`
- `m = z·√(p(1−p)/n + z²/(4n²))`
- Khoảng = `[(c − m)/d, (c + m)/d]`. Khi n = 0 thì khoảng là [0, 1].

**Ví dụ** (khớp eval thực tế): k = 0 FP trên n ≈ 50.000 mẫu hợp lệ.
- c = 3,8416/100.000 = 3,84e-5.
- m = 1,96 × √(3,8416/(4 × 2,5e9)) = 1,96 × 1,96e-5 = 3,84e-5.
- Cận trên = 7,68e-5/1,0000768 ≈ **0,0077 %**. Con số này đúng với `block_fpr_ci_hi_pct = 0,0077` ở §7.1 báo cáo 01.

**Cỡ mẫu tối thiểu** để chứng minh FPR < t khi không có FP nào: `N ≥ ⌈z²(1−t)/t⌉`. Với t = 1e-4 thì N ≥ 38.412.

**Giọng nói:** cận trên Clopper–Pearson một phía, `U = Beta⁻¹(0,95; x+1, n−x)`, tính theo nhóm liên thông.

**Giới hạn.**
- Eval chỉ có khoảng 22 sự kiện gian lận. Khoảng Wilson cho recall rộng khoảng ±20 điểm, nên cổng recall (45 / 80) có thể dao động giữa các lần chạy.
- Các mẫu không độc lập: cùng một kẻ gian gọi nhiều lần.

### 4.6 PSI và giám sát drift (`drift.py`)

**Công thức.** `PSI = Σᵢ (aᵢ − eᵢ) · ln(aᵢ / eᵢ)`.
- e là tỷ lệ của bin tham chiếu, a là tỷ lệ hiện tại, cả hai đều được kẹp dưới ở 1e-6.
- Cạnh bin là các thập phân vị (decile) của mẫu tham chiếu; hai đầu ngoài cùng là ±∞.

**Trường hợp đặc biệt:**
- Hằng số: bin `[c, c⁺)` để cô lập giá trị đó.
- Nhị phân hoặc ít giá trị (≤ 11 giá trị): điểm giữa giữa các giá trị.
- **Mới (F18):** khi nhiều giá trị nhưng hơn 90 % là một giá trị duy nhất, ta cô lập giá trị mode bằng `[mode, mode⁺)` và chia phần còn lại theo quantile, tối đa khoảng 12 bin.

**Mức cảnh báo:** watch 0,10; alert 0,25; cần ít nhất 100 giá trị hữu hạn. Nhiễu nền được đo bằng PSI giữa hai nửa ngẫu nhiên của tập tham chiếu.

**Ví dụ.** e = (0,5; 0,5), a = (0,8; 0,2):
- PSI = 0,3·ln 1,6 + (−0,3)·ln 0,4 ≈ 0,141 + 0,275 = **0,416**, tức là alert.
- Trước F18, đặc trưng 95 % số 0 có khoảng 500 bin, và PSI giữa hai mẫu *cùng phân phối* là khoảng 0,13. Sau F18 có tối đa 13 cạnh, PSI nền nhỏ hơn 0,10, còn dịch chuyển thật (30 % thành 50) vẫn lớn hơn 0,25. Test `test_zero_inflated_feature…` kiểm chứng các con số này.

**Giới hạn.**
- **Tham chiếu (H12, D7).** Cửa sổ `ref` (kind `reference`) có tỷ lệ gian lận thực tế và chỉ gồm các họ đã biết; nó không bao giờ dùng để huấn luyện hay hiệu chỉnh.
  - Trước H12 tham chiếu là cửa sổ `cal`, đã làm giàu gian lận (dải BLOCK khoảng 1,64 %), nên mọi báo cáo đều ra INVESTIGATE.
- **PSI vận hành.** Tính trên các quyết định CALL kể từ snapshot trước (≥ 500 cuộc gọi), không còn dùng số luỹ kế của mọi loại sự kiện.
- Từ vòng 2:
  - tham chiếu BLOCK = 0 mà hiện tại > 0,1 % được coi là dải BLOCK đã dịch chuyển (G16);
  - segment không có dòng nào cho kết quả `NO_ACTION` (G17).
- PSI chỉ báo "có lệch", không chỉ ra nguyên nhân hay chất lượng mô hình.

### 4.7 Chịu lỗi (`resilience.py`)

**Circuit breaker.**
- Chuyển CLOSED → OPEN sau 5 lần lỗi hoặc quá hạn liên tiếp (`CB_FAILURE_THRESHOLD`).
- Sau 1 s (`CB_COOLDOWN_S`) chuyển sang HALF_OPEN để thử một lần.

**Luật dự phòng** (khi mô hình lỗi, chỉ tra bảng):
- chặn số và IMEI mà mô hình đã chặn (TTL 24 giờ, tối đa 200k mục);
- chặn số tổng đài được bảo vệ khi không đi qua `BANK_TRUNK`;
- chặn brandname đi sai kênh;
- phần còn lại là fail-open.

Số tổng đài và brandname không bao giờ bị đưa vào danh sách chặn.

**Chấm lại.**
- Sự kiện fail-open được đưa vào hàng đợi (50k). Khi hệ thống hồi phục, chúng được chấm lại trên bản sao state.
- Nếu kết quả là BLOCK/FLAG thì sinh **cảnh báo hồi tố** (gắn cờ số và báo người nhận).

**Từ vòng 2:**
- Chấm lại chạy cả khi owner đang bận, tối đa mỗi giây một lần, mỗi lần ≤ 8 sự kiện (G2).
- Chỉ thời gian `score_batch` được so với ngân sách 20 ms (G1).
- Batch chỉ có SMS/AUDIO dùng ngân sách 170 ms (G3).

**Giới hạn còn lại:** mỗi lượt chấm lại vẫn sao chép toàn bộ state, và checkpoint vẫn là snapshot đầy đủ (D14).

### 4.8 Pipeline WAV (`voice_pipeline.py`, `voice_model.py`)

**Đầu vào.**
- Chỉ PCM 8/16/24/32 bit; tối đa 12 MiB và 30 s; kiểm thời lượng trước khi cấp phát.
- Lấy mẫu lại về 16 kHz bằng `resample_poly(up/gcd, down/gcd)`.

**Khung và VAD.**
- Khung 400 mẫu, bước 160 (25 ms / 10 ms).
- Ngưỡng VAD = `clip(P20(dB) + 8, −55, −35)`. Một đoạn nói phải có ít nhất 8 khung liên tiếp.

**Đặc trưng.**
- Flatness = trung bình nhân / trung bình cộng của phổ công suất (Hann, FFT 512, dải 80–7500 Hz).
- Centroid: trung bình và độ lệch chuẩn.
- Drift = 1 − cos(phổ nửa đầu, phổ nửa sau).
- Pitch: tự tương quan qua FFT 1024, lag 40–266 (60–400 Hz), đỉnh chuẩn hoá ≥ 0,55. `pitch = 1/median(period)`.

**Abstain (OBSERVE)** khi gặp một trong các trường hợp:
- RMS < −50 dBFS;
- clipping > 1 %;
- thời lượng nói < 0,2 s;
- **mới (F10):** file stereo mà không có `caller_channel`.

Từ vòng 2 (G15), trước khi đọc body hệ thống kiểm `Content-Length` (≤ khoảng 16,8 MB) và mốc thời gian. Số upload đồng thời bị giới hạn bởi `FRAUD_AUDIO_MAX_UPLOADS` (mặc định 8); vượt thì trả 503.

**Mô hình.** Imputer median, rồi chuẩn hoá, rồi logistic regression balanced. Ngưỡng chọn theo cận Clopper–Pearson: mục tiêu chặn 1e-4, cảnh báo 5e-3.

**Giới hạn:** chưa có corpus âm thanh thật đã gán nhãn. Mô hình được giao kèm ở trạng thái "insufficient_calibration" sẽ chỉ trả OBSERVE hoặc ALLOW.

### 4.9 Bloom filter có rào phiên bản (`ram_online/core.py`)

**Kích thước.**
- `m = ⌈−n ln p / (ln 2)²⌉` bit.
- `k = max(1, round((m/n) ln 2))`.

**Vị trí bit.** `hᵢ = (a + i·b) mod m`, với a và b là hai nửa 64 bit của BLAKE2b-128; b được ép lẻ (`b|1`).

**Ví dụ.** n = 10⁶, p = 10⁻⁴:
- m = ⌈10⁶ × 9,2103/0,48045⌉ ≈ 19.170.117 bit ≈ 2,40 MB (2,29 MiB).
- k = round(19,17 × 0,693) = 13.

**Quyết định (Lua `CHECK_LUA`).**
- Cho qua mà không tra tập chỉ khi đủ cả ba: Bloom âm, `manifest_live == manifest_snapshot`, và `SCARD == count`.
- Ngược lại: cho qua khi và chỉ khi `¬SISMEMBER`.
- Mọi bất nhất trả UNKNOWN.

**Adapter fail-open** (`production.py`):
- UNKNOWN thì cho cuộc gọi đi tiếp nhưng ẩn huy hiệu.
- Breaker mở sau 2 lỗi phụ thuộc, cooldown 1 s.
- **Mới (F12):** quá tải cục bộ (`OnlineOverloaded`) không được tính là lỗi Redis.

**Từ vòng 2:**
- Exception không bọc trả về quyết định `policy_error` (G21).
- Consumer Kafka có DLQ tuỳ chọn (G20): lỗi dữ liệu được ghi sang DLQ (acks=all, chờ ack) rồi mới commit; lỗi hạ tầng thì không.

**Giới hạn:** chế độ strict vẫn tốn 1 RTT Redis cho mỗi cuộc gọi. Kafka cộng Redis cho at-least-once, không phải exactly-once.

### 4.10 Cửa sổ thời gian thực (`ram_online/streaming.py`)

- `frontier = max(ts)` và `cutoff = frontier − 60.000 ms`.
- `ZADD NX` khi ts > cutoff; đếm các phần tử trong `(cutoff, frontier]`.
- Gắn cờ khi `count > threshold` và `now − frontier ≤ 5 s`.
- Cùng `call_id` mà khác `ts` thì báo lỗi xung đột.

**Khác với FeatureStore:** cận dưới của cửa sổ là mở, và mốc neo là frontier chứ không phải ts của sự kiện.

### 4.11 Spark stateful stream (`spark/stateful_stream.py`, `distributed_stream.py`)

- **Watermark:** `max(prev, max_ts − L)`; khi flush thì bằng `max_ts`.
- **Phát ra:** sự kiện có `ts ≤ watermark`, theo thứ tự `(ts, event_id)`.
- **Late:** `ts < prev_watermark`, hoặc `(ts, id) ≤ last_key`.
- **Khoá phân vùng:** số chủ gọi. Các cập nhật phụ (`in_calls`, `msisdn_per_imei`, `new_sims_cell`) được phát rộng (`scaleout.py`).
- **Từ vòng 3:**
  - `ts` phải nằm trong [0, 1e11] giây (H9, D12);
  - bản trùng có nội dung khác trong cùng batch bị cách ly ở cả hai engine (H10, D13);
  - dòng không có IMEI không còn dồn vào một partition (H6).
- **Giới hạn:** Spark chỉ xử lý CALL (bỏ qua SMS).

### 4.12 EduDSS (`edu_dss/backend/app`)

- **Quyền:** admin toàn cục; sinh viên và giảng viên chỉ truy cập khoá học có trong claim JWT (`course_access.py`).
- **Khoá Redis (F16):** mỗi phần được percent-encode, nên `(CS101, sec2:lec1)` và `(CS101:sec2, lec1)` cho hai khoá khác nhau.
- **Vòng 2:**
  - AutoML bắt buộc `course_id` mà người gọi đang dạy (G24);
  - token có claim `demo=true` bị từ chối khi tắt demo; tuỳ chọn `JWT_AUDIENCE`; worker không còn giữ secret ký (G25).
- **OLS demo** (`grade_baseline.py`):
  - β₁ = Σ(x − x̄)(y − ȳ) / Σ(x − x̄)²;
  - β₀ = ȳ − β₁x̄.
  - Chỉ 7 cặp mô phỏng, nên đây là dữ liệu minh hoạ, không phải mô hình ra quyết định.

---

## 5. Lựa chọn công nghệ và giới hạn

| Công nghệ | Vì sao dùng (theo mã và tài liệu) | Giới hạn |
|---|---|---|
| FastAPI + Uvicorn ≥ 0.47 với event loop riêng (`precise_event_loop.py`) | ASGI; deadline chính xác dưới mili giây (Windows cần loop riêng) | p99 30 ms chưa ổn định (`docs/SLA_30MS.md`; eval §7.1 báo cáo 01) |
| Một owner thread cộng SQLite WAL `synchronous=FULL` | Thứ tự xác định, giao dịch nguyên tử, phục hồi bằng journal | Không mở rộng ngang, không HA; checkpoint là snapshot đầy đủ |
| LightGBM, IsolationForest (Numba), TF-IDF/LR tự cài | Nhanh trên CPU; mô hình SMS tự cài để tránh overhead của sklearn và pandas | Dữ liệu mô phỏng; scikit-learn bị ghim ở 1.8.0 để khớp joblib |
| JSONL chuỗi SHA-256 + HMAC theo người | Bằng chứng không sửa được mà không bị phát hiện | Khoá đặt trên server; xoá ở cuối chuỗi không bị phát hiện |
| Redis + Lua + Bloom (RAM online) | Tra danh sách chặn có rào phiên bản và nguyên tử | Redis một nút trong compose |
| gRPC + mTLS | Giao thức cho adapter tổng đài | Chưa nối với API chính |
| Spark/Kafka | Đặc trưng phân tán, giữ trạng thái theo khoá | Checkpoint trên file chia sẻ; chưa có HA |
| Prometheus/Grafana Cloud | Metrics và 12 cảnh báo | `TfsDown` phụ thuộc tên job scrape |
| Caddy + docker compose trên một VM | Triển khai rẻ, TLS tự động | Một VM, không dự phòng |

---

## 6. Trạng thái kiểm chứng

| Khía cạnh | Trạng thái |
|---|---|
| Hành vi các bản sửa F1–F18 | **Đã kiểm chứng.** 20 test mới (16 cho app chính, 2 cho RAM online, 2 cho EduDSS), tất cả thất bại trên HEAD và qua sau khi sửa. |
| Hành vi các bản sửa G1–G27 | **Đã kiểm chứng.** 29 test mới (22 cho app chính, 3 cho RAM online, 4 cho EduDSS), tất cả thất bại trên mã sau vòng 1. Một số chỉ là test tĩnh trên file cấu hình. |
| Hành vi các bản sửa H1–H12 | **Đã kiểm chứng.** 12 test trong `tests/test_audit_regressions_round3.py`, 1 test Spark và 1 test EduDSS, tất cả thất bại trên mã sau vòng 2. Với D12/D13, hai test cũ được sửa theo quyết định của chủ dự án. |
| Bộ test chính | 426 → 442 → 464 → 476, 0 thất bại. Spark 23 → 24; EduDSS 9 → 16; RAM (fakeredis) 17/18 → 22/23. |
| Spark, EduDSS, gRPC | Qua, không có hồi quy. |
| RAM online | **Một phần:** chạy trên fakeredis (Redis thật không có). Có 1 ca hỏng do giả lập, giống hệt trên HEAD. |
| Chất lượng mô hình (eval) | Giống hệt HEAD đến từng chữ số, kể cả sau khi dựng lại bundle tfd-v2.3 (H11 giữ nguyên giờ địa phương của dữ liệu mô phỏng). |
| SLA phục vụ 30 ms | **Không đạt** trên cả HEAD và bản sửa (môi trường laptop; dự án tự ghi là chưa đạt). |
| Docker image, promtool, Android, deploy | **Chưa kiểm chứng** do môi trường. F13–F17 và G22, G23, G26, G27 chỉ được kiểm bằng test tĩnh, `bash -n`, YAML parse và `docker compose config`. |
| Hiệu năng ở quy mô lớn (checkpoint, chấm lại) | **Chưa tái hiện.** Kết luận dựa trên phân tích mã. |
| Dữ liệu thật | **Không có.** Mọi chỉ số chất lượng chỉ đúng trên dữ liệu mô phỏng. |
