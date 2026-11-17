# Thuật toán của 3 dự án: dùng ở đâu, vì sao chọn, công thức, đã phù hợp chưa

**Ngày:** 17/11/2026
**Nhánh:** `audit/telecom-v2-fixes` (chưa push)
**Phạm vi:**
1. Fraud Telecom (Telecom Fraud Shield, `src/`).
2. Spark/Kafka streaming, kèm hai nhánh online thử nghiệm `ram_online/` và `telecom_grpc/`.
3. EduDSS (`edu_dss/`).

**Lưu ý chung:** mọi số liệu chất lượng ở đây đo trên **dữ liệu mô phỏng** (`src/data_generator.py`), không phải lưu lượng nhà mạng thật. Mỗi thuật toán được trình bày theo bốn ý:
- **Dùng ở đâu**: file và hàm.
- **Vì sao chọn**: so với phương án thay thế.
- **Công thức**: kèm ví dụ số khi cần.
- **Đánh giá**: đã phù hợp chưa, còn giới hạn gì.

Báo cáo này bổ sung, không thay thế:
- [02_Deep_Technical_Explanation_Report.md](02_Deep_Technical_Explanation_Report.md): luồng xử lý chi tiết.
- [04_Danh_gia_da_vai_tro_va_thuat_toan.md](04_Danh_gia_da_vai_tro_va_thuat_toan.md): đánh giá đa vai trò, các vòng sửa R4/R5.

---

## 0. Ba dự án đã ổn chưa

Kiểm lại lần cuối ngày 17/11/2026 trên WSL (Python 3.12.3), bản sao sạch của cây làm việc, bundle **tfd-v2.6**.

| Dự án | Bộ test | Kết quả | Trạng thái |
|---|---|---|---|
| Fraud Telecom | `tests/` | **574 passed** | Chạy được đầu-cuối: QA 32/32 trên server thật ký HMAC. Mọi chỉ tiêu **chất lượng** trên mô phỏng đã đạt (§5). Riêng SLA phục vụ trên máy WSL này chưa ổn định (§5). |
| Spark/Kafka | `spark/` + `telecom_grpc/tests` | **26 passed**, 65 subtests | Đặc trưng khớp `FeatureStore`, kể cả đặc trưng farm mới. `ram_online/` (fakeredis): 22/23; ca hỏng duy nhất do fakeredis giả lập Lua, hỏng y hệt trên mã gốc. |
| EduDSS | `edu_dss/backend/tests` | **16 passed**, 4 subtests | Khung API/RBAC/hàng đợi chạy đúng. AutoML, LLM và graph là adapter, thuật toán thật nằm ngoài repo. |

**"Ổn" ở đây nghĩa là:** mã chạy đúng, test đạt, và các thuật toán phù hợp với bài toán. **Không** có nghĩa là đã chứng minh hiệu quả trên mạng nhà mạng thật. Phần đó cần dữ liệu shadow có nhãn (báo cáo 04 §10.4).

---

## 1. Fraud Telecom

### 1.1 Đặc trưng cửa sổ trượt nhân quả (`src/feature_store.py`)

**Dùng ở đâu.** `FeatureStore.call_features`, `sms_features`, `audio_features`. Mỗi sự kiện chỉ thấy lịch sử **trước** nó.

**Vì sao chọn.**
- Quyết định chặn phải ra trước khi đổ chuông (ngân sách 30 ms), nên đặc trưng phải tính tăng dần O(1) trung bình cho mỗi sự kiện. Không thể truy vấn lại cơ sở dữ liệu.
- Cửa sổ giữ **dấu thời gian chính xác** (deque) chứ không gộp theo phút. Bản v1 gộp theo phút gây sai số ở biên 24 giờ, và không phục hồi đúng được sau checkpoint.
- Tính "nhân quả" (chỉ dùng quá khứ) để tránh rò nhãn: đặc trưng lúc huấn luyện giống hệt lúc phục vụ. Cả Spark lẫn scale-out đều dùng lại đúng lớp này, nên ba đường cho cùng một con số.

**Công thức.** Cửa sổ W ∈ {60, 600, 3600, 86400} s, giữ phần tử có `t ≥ ts − W`.

| Đặc trưng | Công thức | Ý nghĩa nghiệp vụ |
|---|---|---|
| `calls_1m`, `calls_10m`, `calls_1h` | số cuộc gọi đi trong cửa sổ | gọi dồn dập |
| `callee_dispersion_1h` | `#số_B_khác_nhau_1h / calls_1h` | gọi rải cho người lạ |
| `in_out_ratio_24h` | `(in24 + 1) / (out24 + 1)` | chỉ gọi đi, không ai gọi lại (làm trơn Laplace +1 để không chia cho 0) |
| `short_call_ratio`, `mean_log_duration` | trên 20 CDR **đã kết thúc trước ts**: `#(d < 5 s)/n`, `Σ ln(1+d)/n` | đổ chuông rồi tắt (Wangiri) |
| `imei_per_msisdn_24h`, `msisdn_per_imei_24h` | bậc của đồ thị hai phía SIM–IMEI trong 24 giờ | SIM box xoay SIM trên một máy |
| `velocity_kmh` | `haversine(cell₁, cell₂) / max(Δt_h, 1/60)`, chặn ở 5000 | di chuyển phi lý (nhân bản SIM) |
| `active_hours_on_network` | `min(72, (ts − first_seen)/3600)` | SIM cũ đi thuê / ngủ đông mới hoạt động |
| `new_sims_on_cell_1h` | số SIM "tươi" trên cell trong 1 giờ | cụm SIM mới / BTS giả |
| `hour_of_day` | `((ts + 25 200)/3600) mod 24` | giờ Việt Nam |

Haversine: `d = 2R·asin(√h)`, với `h = sin²(Δφ/2) + cos φ₁·cos φ₂·sin²(Δλ/2)` và R = 6371 km.

**Đánh giá: phù hợp.** Test `test_scaleout_features`, `test_feature_contracts` và `spark/compare_features.py` khẳng định ba đường tính ra giống nhau. Giới hạn: toạ độ cell là ánh xạ mô phỏng.

### 1.2 Độ mới của số B: `new_callee_ratio_24h` (vòng 5)

**Vì sao.** Người thật chủ yếu gọi lại số quen. Farm SIM box quay số ngẫu nhiên. Một farm chậm (3–6 cuộc/giờ) không lộ qua `callee_dispersion_1h`, vì trong 1 giờ nó chỉ gọi vài cuộc.

**Công thức.** Mỗi người gọi có một trí nhớ 30 ngày các số đã gọi, ký hiệu K. Cuộc gọi tới b là "mới" khi `novel = 𝟙[b ∉ K]`. Khi đó:

`new_callee_ratio_24h = Σ_{cuộc trong 24 h} novel / #cuộc trong 24 h`

**Đánh giá.** Đúng hướng cho lưu lượng thật. Trên mô phỏng thì yếu: thuê bao thật có trung bình 0,93, farm là 1,0, vì trong 24 giờ warm-up gần như mọi số B đều "mới". Đặc trưng này một mình không cứu được farm ẩn (xem §1.3).

### 1.3 Đồng vị trí farm SIM: `farm_cell_sims_1h` + luật sàn FLAG (vòng 6, **mới**)

**Bài toán.** Ở vòng 5, cả 89 sự kiện SIM box bị bỏ sót trong eval gộp đều thuộc **một** tác nhân là `SB_T2_STEALTH`. Đó là farm 2 SIM, mỗi SIM một IMEI riêng, TAC điện thoại thường, 3–6 cuộc/giờ. Xét từng SIM, nó trông y như một thuê bao mới bình thường.

Thử luật đơn giản "SIM mới + không có cuộc gọi đến + toàn gọi số lạ" trên `cal` và `val` thì gắn cờ nhầm 264–1 341 sự kiện sạch, vì thuê bao mới thật cũng hay chỉ gọi đi.

**Vì sao chọn đồng vị trí.** Tín hiệu mà farm ngoài đời không giấu được: các SIM nằm trong **cùng một hộp, cùng một trạm**, và cùng chạy một kiểu gọi. Đây cũng là cách nhà mạng thật hay dùng để phát hiện SIM box (cụm SIM theo vị trí). Thuê bao mới thật thì rải ra nhiều trạm.

**Công thức.**
- **SIM "tươi":** `0 ≤ age < 60` ngày, hoặc `active_hours_on_network < 6`.
- **Hồ sơ farm của người gọi** (chỉ shard sở hữu người gọi biết):
  `P = tươi ∧ incoming_24h = 0 ∧ #số_B_khác_1h = calls_1h ∧ new_callee_ratio_24h = 1`.
- **SIM "dồn dập" trên cell c trong 1 giờ:**
  `B(c, ts) = { s : s tươi, Σ cuộc gọi của s trên c trong [ts−3600, ts] ≥ 2, #số_B khác nhau ≥ 2 }`.
  Tập này chỉ dùng sự kiện thô, nên một shard scale-out thấy các SIM kia qua side update vẫn tính ra đúng con số. Test `test_farm_feature_is_exact_under_scale_out` kiểm điều này.
- **Đặc trưng:**
  `farm_cell_sims_1h = |B(c, ts)|` nếu `P ∧ người_gọi ∈ B(c, ts)`, ngược lại `0`. Chỉ tính trên cell vô tuyến (cell đã đăng ký hoặc trạm lạ), không tính trên TRUNK/INTL.
- **Luật sàn:** `farm_cell_sims_1h ≥ 2` thì ALLOW được nâng thành **FLAG** (mã R308). Luật **không** tự chặn.
- **Bằng chứng hành vi:** `farm_cell_sims_1h ≥ 3` được tính là bằng chứng hành vi, để mô hình được phép BLOCK.

**Chọn ngưỡng trên `cal`/`val`, không nhìn cửa sổ test.** Số sự kiện bị bắt ở từng ngưỡng:

| Cửa sổ | Sạch (≥ 2) | SIM box (≥ 2) | Sạch (≥ 3) | SIM box (≥ 3) |
|---|---|---|---|---|
| `cal` (105 427 sạch / 679 SIM box) | 11 | 470 | 0 | 467 |
| `val` (108 594 / 348) | 7 | 305 | 0 | 282 |
| `train` (211 299 / 2 630) | 14 | 1 829 | 0 | 1 824 |

Chi phí của luật FLAG ước khoảng **0,008 %** sự kiện sạch (18/214 021 trên `cal`+`val`). Cổng an toàn cho FLAG là 0,2 %.

**Đánh giá.** Hợp lý về nghiệp vụ, có số liệu chọn ngưỡng độc lập. Giới hạn: farm chỉ có **một** SIM trên mỗi trạm, hoặc trải SIM ra nhiều trạm, vẫn né được. Khi đó cần đặc trưng theo IMEI/TAC cùng lô hoặc dữ liệu thật.

### 1.4 LightGBM cho CALL (tầng 1) (`src/train.py: train_call`, `src/detector.py`)

**Vì sao chọn GBDT (LightGBM).**
- Dữ liệu dạng bảng, 29 đặc trưng khác thang đo (đếm, tỷ lệ, cờ 0/1). Cây quyết định không cần chuẩn hoá và tự bắt được tương tác như "SIM mới **và** gọi dồn dập".
- Logistic regression không bắt được tương tác nếu không tự thêm.
- Mạng nơ-ron cần nhiều dữ liệu hơn và chậm hơn trên CPU.
- So với Random Forest, GBDT chính xác hơn với cùng số cây, và mô hình nhỏ hơn.
- LightGBM dự đoán một batch mất khoảng 1 ms, nằm gọn trong ngân sách 30 ms. Nó còn trả đóng góp từng đặc trưng (`pred_contrib`) để sinh lý do giải thích.

**Công thức.**
- Mô hình cộng: `F(x) = Σ_{t=1..T} f_t(x)`, xác suất `p = σ(F) = 1/(1 + e^{−F})`.
- Hàm mất có trọng số lớp, với w₊ = `scale_pos_weight` = 10:
  `L = −Σᵢ [ w₊·yᵢ·ln pᵢ + (1 − yᵢ)·ln(1 − pᵢ) ]`
- Mỗi vòng dùng đạo hàm bậc 1 và 2: `gᵢ = wᵢ(pᵢ − yᵢ)`, `hᵢ = wᵢ·pᵢ(1 − pᵢ)`.
- Trọng số lá tối ưu: `w* = −G/(H + λ)`.
- Độ lợi khi tách:
  `Gain = ½ [ G_L²/(H_L+λ) + G_R²/(H_R+λ) − (G_L+G_R)²/(H_L+H_R+λ) ]`
  với G, H là tổng g, h trong nút và `λ = lambda_l2 = 1`.
- **Tham số:** `learning_rate=0.05`, `num_leaves=31`, `min_child_samples=40`, `feature_fraction=0.8`, `bagging_fraction=0.8`, tối đa 600 vòng, dừng sớm sau 40 vòng trên `val`.
- **Vì sao `scale_pos_weight=10`:** tỷ lệ gian lận trong train khoảng 1,5 %. Trọng số 10 tăng độ nhạy mà không làm điểm số dồn hết về 1. Ngưỡng sau đó được đặt bằng thống kê (§1.5), nên điểm không cần là xác suất đã hiệu chuẩn.

**Đánh giá: phù hợp, có lưu ý.**
- Điểm **không** được hiệu chuẩn thành xác suất, và chủ dự án đã quyết định không hiệu chuẩn. Mọi ngưỡng chỉ dựa vào thứ hạng.
- Một số đặc trưng tách lớp vì bộ mô phỏng sinh chúng như vậy (báo cáo 04 §2.2).

### 1.5 Ngưỡng theo FPR có bảo đảm thống kê

**BLOCK: cận trên Clopper–Pearson một phía** (`threshold_for_fpr_bound`).
- **Vì sao:** "0 FP trên tập hiệu chỉnh" không có nghĩa FPR thật bằng 0. Ta muốn **với độ tin cậy 95 %**, FPR chặn ≤ 1e-4, tức ≤ 1 trong 10 000 cuộc gọi sạch.
- **Công thức:** sắp điểm của n mẫu sạch (cửa sổ `cal`) giảm dần: s₍₁₎ ≥ s₍₂₎ ≥ … Với k là số FP cho phép,
  `U(k, n) = Beta⁻¹(0,95; k + 1, n − k)`.
  Chọn k* lớn nhất sao cho `U(k*, n) ≤ 1e-4`, rồi đặt `θ_block = nextafter(s₍k*+1₎, +∞)`. Quy tắc `p ≥ θ` cho tối đa k* mẫu sạch vượt ngưỡng, kể cả khi điểm bằng nhau.
- **Cỡ mẫu tối thiểu:** khi k = 0, `U = 1 − 0,05^{1/n}`. Muốn U ≤ 1e-4 thì cần `n ≥ ln(20)/1e-4 ≈ 29 957`. AUDIO chỉ có 2 965 mẫu sạch, nên **BLOCK AUDIO bị tắt** (`BLOCK_DISABLED`) thay vì bật trên một bảo đảm chưa chứng minh được.
- **Ví dụ CALL:** n = 105 427, cho k* = 5 (cận một phía ≈ 1,0e-4).

**FLAG: thống kê thứ tự** (`threshold_for_fpr`).
- `k = ⌊fpr·0,5·n⌋`, `θ_flag = nextafter(s₍k+1₎, +∞)`, với fpr = 1e-3 cho CALL và 5e-3 cho SMS/AUDIO.
- Hệ số 0,5 là biên an toàn. FLAG không chặn, nên không cần cận chặt như BLOCK.

**Sàn (FLOORS).** CALL 0,5 / 0,05; SMS và AUDIO 0,9 / 0,5. Sàn chống trường hợp ngưỡng quá thấp khi tập sạch quá "dễ".

**Đánh giá: phù hợp.** Đây là cách chuẩn khi điểm chưa hiệu chuẩn và cần bảo đảm FPR một phía.

### 1.6 Chính sách "phải có bằng chứng hành vi" mới được BLOCK (`has_behavioural_evidence`)

**Vì sao.** "Mới" (SIM mới, ít lịch sử) không phải là tội. Chặn cứng một thuê bao thật gây hại lớn hơn nhiều so với bỏ lọt một cuộc gọi đã được FLAG. Nên BLOCK chỉ giữ lại khi có ít nhất một hành vi cụ thể:
- giả CLI tổng đài;
- trạm lạ;
- TAC gateway;
- ≥ 2 SIM/máy hoặc ≥ 2 máy/SIM;
- `calls_10m ≥ 5` hoặc `calls_1h ≥ 12`;
- ≥ 3 CDR với ≥ 60 % cuộc cực ngắn;
- cụm ≥ 6 SIM mới kèm `calls_10m ≥ 2`;
- vận tốc ≥ 300 km/h;
- **mới:** ≥ 3 SIM farm cùng trạm.

Không có bằng chứng nào thì BLOCK được hạ xuống FLAG (mã P001).

**Đánh giá.** Phù hợp, và chính luật này giữ FPR chặn ≈ 0. Các mốc được đặt tay; đã kiểm trên `cal`/`val`.

### 1.7 IsolationForest theo segment, phát hiện zero-day (`train.py`, `fast_iforest.py`)

**Vì sao.**
- Kiểu tấn công mới chưa có nhãn, nên cần một mô hình **không giám sát** chỉ học "bình thường trông thế nào".
- IsolationForest rẻ (100 cây, ψ ≤ 1024), không cần chuẩn hoá, và chịu được dữ liệu nhiều chiều.
- Học riêng ba segment domestic / foreign / trunk, vì phân phối của chúng khác hẳn nhau. Học chung thì mọi cuộc gọi quốc tế đều "bất thường".
- Bản cài lại bằng Numba (`fast_iforest.py`) cho điểm khớp sklearn nhưng nhanh hơn nhiều với batch nhỏ.

**Công thức.**
- `c(n) = 2(ln(n−1) + γ) − 2(n−1)/n` khi n > 2; c(2) = 1; c(n ≤ 1) = 0; γ ≈ 0,5772.
- Điểm bất thường `s(x) = 2^{−E[h(x)]/c(ψ)}`, với h là độ sâu tại lá cộng c(n_lá).
- **Ví dụ** ψ = 256: c(256) ≈ 10,245. E[h] = 4 cho s ≈ 0,76 (bất thường); E[h] = 10 cho s ≈ 0,51 (bình thường).
- Ngưỡng zero-day theo segment: 5e-4, 1e-2 và 5e-3 trên tập sạch. Chỉ nâng ALLOW lên FLAG, không bao giờ chặn.
- **Lý do giải thích (vòng 5, M10):** `zⱼ = |xⱼ − median_seg,j| / IQR_seg,j`; hai đặc trưng có z lớn nhất được đưa vào lý do. Dùng median/IQR vì bền với ngoại lai hơn mean/std.

**Đánh giá.** Phù hợp cho vai trò lưới an toàn. Giới hạn: thành công "zero-day" chỉ được đo trên một họ (Wangiri, 90,2 %). "Hiếm" không đồng nghĩa với "gian lận".

### 1.8 SMS: TF-IDF 1–2-gram + Logistic Regression (`train.py`, `detector.py`, `textnorm.py`)

**Vì sao.**
- Tin nhắn ngắn, từ vựng lừa đảo lặp lại ("trúng thưởng", "xác minh tài khoản"). Mô hình tuyến tính trên n-gram thưa là chuẩn mực cho lọc spam: nhanh, ít dữ liệu, và giải thích được (trọng số từng n-gram).
- Transformer chính xác hơn trên văn bản khó, nhưng nặng, chậm trên CPU và khó giải thích.
- Bản suy luận được cài tay (khớp sklearn, có test so khớp) để bỏ chi phí cố định của sklearn/pandas với batch nhỏ.

**Công thức.**
- Chuẩn hoá: bỏ dấu, gấp leet ("0"→"o"…), tách token `[a-z0-9]+`, lấy n-gram 1–2, `min_df = 2`, tối đa 40 000 đặc trưng.
- Trọng số: `w(t, d) = (1 + ln tf)·idf(t)`, với `idf(t) = ln((1+N)/(1+df(t))) + 1`. Sau đó chuẩn hoá L2: `x = w/‖w‖₂`.
- Xác suất: `p = σ(β·x + β_meta·(meta/scale) + b)`. Logistic regression với C = 4 (phạt L2 là 1/C), `class_weight="balanced"`, tức trọng số lớp = `N/(2·N_lớp)`.

**Đánh giá: đúng loại mô hình, bằng chứng còn yếu.** PR-AUC trên val bằng 1,0 vì các đặc trưng meta của tuyến gửi tách lớp theo cách mô phỏng sinh ra. Cần ablation chỉ dùng chữ trên dữ liệu thật.

### 1.9 Giọng nói

**AUDIO LightGBM (demo)** trên đặc trưng âm học tính sẵn: `num_leaves=15`, `min_child_samples=20`. BLOCK đã bị tắt vì không đủ mẫu sạch (§1.5). Mô hình này chỉ dùng cho demo.

**Pipeline WAV thật** (`voice_pipeline.py`, `voice_model.py`, `train_voice.py`):
- **Tiền xử lý:** PCM, lấy mẫu lại 16 kHz (`resample_poly`), khung 25 ms / bước 10 ms. Ngưỡng VAD = `clip(P20(dB) + 8; −55; −35)`.
- **Đặc trưng:**
  - spectral flatness = trung bình nhân / trung bình cộng của phổ công suất;
  - centroid;
  - drift = `1 − cos(phổ_nửa_đầu, phổ_nửa_sau)`;
  - pitch bằng tự tương quan trong dải 60–400 Hz.
- **Mô hình:** imputer median, chuẩn hoá, rồi logistic regression balanced. Ngưỡng theo Clopper–Pearson, và **đơn vị là nhóm độc lập** (group ID, R4-M8), vì nhiều đoạn của cùng một cuộc gọi không độc lập.
- **Vì sao:** chưa có corpus thật lớn, nên chọn mô hình tuyến tính ít tham số, kiểm rò theo người nói/chiến dịch. Không đủ bằng chứng thì mô hình chỉ trả OBSERVE.

**Trí nhớ thu giọng:**
- Đoạn có `caller_speech_ratio < 0,15` và `callee_speech_s ≥ 1` được ghi là một "nạn nhân".
- Có ≥ 2 nạn nhân khác nhau trong 3600 s thì mọi cuộc tiếp theo của số đó bị BLOCK (A108).
- Số tổng đài được bảo vệ không bao giờ vào trí nhớ này. Số thương mại đã đăng ký chỉ bị FLAG (C004).
- **Vì sao:** kẻ gian im lặng để thu giọng nạn nhân nhằm giả giọng. Một nạn nhân có thể là trùng hợp, hai nạn nhân trong một giờ thì đã là một mẫu hình.

### 1.10 Chịu lỗi: circuit breaker + luật dự phòng (`resilience.py`)

**Vì sao.** Nếu mô hình lỗi hoặc quá hạn, cuộc gọi vẫn phải đi tiếp (fail-open). Nhưng những gì **chắc chắn** thì vẫn phải chặn được.

**Công thức.**
- **Breaker:** CLOSED → OPEN sau 5 lỗi liên tiếp. Sau 1 s chuyển HALF_OPEN để thử một lần, thành công thì về CLOSED.
- **Luật dự phòng** (chỉ tra bảng, O(1)):
  - danh sách chặn số/IMEI do mô hình đã chặn (TTL 24 giờ, ≤ 200 nghìn mục);
  - giả CLI tổng đài đi đường nội địa thì BLOCK, qua `INTL_GW` thì FLAG (R4-M4);
  - brandname đi sai kênh.
- **Chấm lại:** sự kiện fail-open được đưa vào hàng đợi (50 nghìn) và chấm lại khi hồi phục, có cảnh báo hồi tố.

**Đánh giá: phù hợp.**

### 1.11 Chống trùng và trễ (`ingest_guard.py`)

**Vì sao.** Nhà mạng gửi lại (at-least-once), và sự kiện có thể tới lệch thứ tự. Nếu tính trùng hoặc cho "lùi thời gian", đặc trưng sẽ sai và quyết định bị nhân đôi.

**Công thức.**
- Khoá sự kiện được nhớ 900 s theo thời gian sự kiện.
- Với bên p (caller hoặc sender): `LATE ⇔ ts < max_ts_p ∨ ts < max_ts − L`, với L = 120 s.
- Frontier của từng bên cho phép hai người khác nhau lệch nhau ≤ 120 s, nhưng không cho lịch sử của chính một người bị lùi.

**Đánh giá: phù hợp.**

### 1.12 Giám sát drift: PSI + kiểm định nhị thức (`drift.py`, `ops_pipeline.py`)

**Vì sao cần hai phép đo.** PSI đo lệch phân phối từng đặc trưng và quyết định. Nhưng tỷ lệ BLOCK rất nhỏ (≈ 0,1 %), nên PSI trên 3 dải bị ALLOW lấn át: tỷ lệ BLOCK phải tăng khoảng 80 lần thì PSI mới vượt 0,10. Vì vậy có thêm một kiểm định nhị thức một phía cho riêng tỷ lệ BLOCK (R4-M3).

**Công thức.**
- `PSI = Σᵢ (aᵢ − eᵢ)·ln(aᵢ/eᵢ)`, với bin là decile của tham chiếu, tỷ lệ kẹp dưới ở 1e-6, và giá trị mode chiếm > 90 % được tách riêng (F18). Mức watch 0,10, alert 0,25.
- **Ví dụ:** e = (0,5; 0,5), a = (0,8; 0,2) cho PSI ≈ 0,416, tức alert.
- **Kiểm định BLOCK:** `p = P(X ≥ x | n, p₀) = 1 − F_Binom(x − 1; n, p₀)`, cảnh báo khi p < 1e-3.

**Đánh giá: phù hợp.**

### 1.13 Thống kê báo cáo: Wilson, bootstrap theo cụm, gộp cửa sổ (`statistical_reporting.py`, `evals/run_evals.py`)

**Wilson** (cho FPR, khi các mẫu sạch gần như độc lập), với p = k/n và z = 1,96:
- `d = 1 + z²/n`
- `c = p + z²/(2n)`
- `m = z·√(p(1−p)/n + z²/(4n²))`
- Khoảng `[(c − m)/d, (c + m)/d]`.
- **Vì sao Wilson:** khoảng Wald `p ± z√(p(1−p)/n)` sụp về [0, 0] khi k = 0.

**Bootstrap theo cụm** (cho recall, R5-M6):
- **Vì sao:** các sự kiện của cùng một kẻ gian không độc lập. Lấy mẫu lại theo **tác nhân**, không theo sự kiện.
- **Công thức:** lấy ngẫu nhiên có hoàn lại m tác nhân, B = 2000 lần. Mỗi lần tính `recall* = Σ k_j / Σ n_j`, rồi lấy phân vị 2,5 % và 97,5 %.

**Gộp 11 cửa sổ** (R4-M2):
- **Vì sao:** một cửa sổ 50 nghìn sự kiện chỉ có khoảng 15 kẻ gian, nên kết quả nhảy hàng chục điểm giữa các seed.
- **Cách gộp:** cộng ma trận nhầm lẫn của cửa sổ test với 10 cửa sổ lặp bắt đầu ở 0; 2,4; …; 21,6 giờ.

### 1.14 Bảo mật

| Thuật toán | Dùng ở đâu | Vì sao | Công thức |
|---|---|---|---|
| Chuỗi bằng chứng SHA-256 + HMAC theo người | `evidence.py`, `security.py` | Sửa hay xoá giữa chuỗi đều bị phát hiện; biết ai ký | `hᵢ = SHA256(hᵢ₋₁ ‖ payloadᵢ)`, `sigᵢ = HMAC-SHA256(k_người, hᵢ)` |
| Chữ ký mạng HMAC | `security.sign_network` | Xác thực nhà mạng, chống phát lại | `HMAC-SHA256(key, ts ‖ "." ‖ body)`, so sánh thời gian hằng (`compare_digest`), kèm cửa sổ thời gian |
| Mật khẩu scrypt | `accounts.py` | Hàm băm "tốn bộ nhớ", chống brute-force bằng GPU; có sẵn trong stdlib | `scrypt(pw, salt₁₆, N=2¹⁴, r=8, p=1, dkLen=32)` |
| OTP 6 số | `accounts.otp_start` | Ngẫu nhiên mật mã; lưu bản băm, không lưu mã | `code = randbelow(10⁶)`; lưu `H(phone:code)`; TTL 300 s; giới hạn số lần thử |
| Phiên, token | `security.py` | Không đoán được | `secrets.token_urlsafe(32)` (256 bit) |
| Web Push VAPID | `push.py` | Đẩy tới PWA mà không cần Firebase | JWT ký ECDSA P-256 (RFC 8292); payload mã hoá aes128gcm (RFC 8291) |

### 1.15 Hạ tầng thuật toán

| Thuật toán | Dùng ở đâu | Vì sao | Công thức |
|---|---|---|---|
| Chia shard theo người gọi | `scaleout.py` | Trạng thái nặng nhất là theo người gọi, nên chia theo người gọi thì tăng tuyến tính theo số lõi | `shard = BLAKE2b₃₂(caller) mod n`. Các đặc trưng chéo khoá (cuộc đến, IMEI, cell, farm) được cập nhật qua side update rút gọn |
| Checkpoint tăng dần (R5-D14) | `backend_runtime.py`, `feature_store.take_delta` | Snapshot đầy đủ tỷ lệ với số thuê bao. Delta chỉ tỷ lệ với số khoá vừa đổi | Ghi delta mỗi chu kỳ, cứ 20 delta ghi một base. Phục hồi = base ⊕ Δ₁ ⊕ … ⊕ Δₖ (tombstone = xoá khoá) |
| Một owner thread + SQLite WAL | `backend_runtime.py`, `backend_state.py` | Thứ tự xác định, giao dịch nguyên tử, journal phục hồi | — |

### 1.16 Đường phục vụ: ba nguồn làm owner thread dừng (vòng 6, **mới**)

Đo từng request phía server cho thấy các fail-open trong lượt SLA **không** do mô hình, mà do ba việc phụ chạy chung với owner thread.

| Nguồn | Đo được | Vì sao xảy ra | Sửa | Vì sao chọn cách này |
|---|---|---|---|---|
| Checkpoint WAL tự động của SQLite | COMMIT ≈ 20 ms, cứ khoảng 136 phán quyết một lần ở 50 EPS | COMMIT làm WAL vượt 1000 trang phải chép WAL vào file DB và fsync, ngay trên owner thread | Kết nối ghi đặt `wal_autocheckpoint=0`. Một thread nền với kết nối riêng chạy `PRAGMA wal_checkpoint(PASSIVE)` mỗi 0,25 s. COMMIT tối đa giảm 22 → 12 ms | PASSIVE không chặn người ghi. Mỗi COMMIT vẫn `synchronous=FULL`, nên **độ bền không đổi**. Tăng ngưỡng (đã thử 8 192 trang ngày 25/10) chỉ làm cú dừng thưa hơn mà dài hơn |
| Kiểm chuỗi bằng chứng trong tiến trình API | 54–71 ms với ~300 bản ghi, lần đầu đọc file **hai** lần | Parse JSON và băm lại từng dòng là Python thuần, tranh GIL với owner | Chạy trong **tiến trình con** (`verify_in_subprocess`), một lượt đọc. Áp cho cả agent và endpoint `/api/evidence/verify` | Tiến trình con có GIL riêng. Log thật hàng triệu dòng sẽ cần vài giây CPU, không được nằm trong tiến trình phục vụ |
| GC thế hệ 2 của Python | cú dừng ~85 ms lác đác | Server không đóng băng heap khởi động (mô hình, từ điển 40 nghìn n-gram), trong khi load test trong tiến trình thì có | `gc_tuning.tune_gc_for_latency()`: `gc.collect(); gc.freeze(); gc.set_threshold(50 000, 50, 1000)`, dùng chung cho server và load test. Tắt bằng `FRAUD_GC_TUNING=0` | Cách chuẩn cho dịch vụ Python cần độ trễ thấp. Rác vòng tạo sau đó vẫn được dọn ở thế hệ trẻ |

**Độ phức tạp của đặc trưng farm.** Bản đầu duyệt cả hàng đợi của cell ở mỗi cuộc gọi, tức O(N). Ở trạm đông, hoặc trong 6 giờ đầu sau một lần triển khai mới khi mọi SIM đều "tươi", N có thể lên hàng nghìn. Bản cuối (`FarmCellWindow`) giữ số đếm tăng dần, O(1) cho mỗi lần thêm/hết hạn. Test khẳng định nó khớp đúng định nghĩa brute-force, và thời gian mỗi cuộc gọi không tăng khi cửa sổ lớn gấp 16 lần.

---

## 2. Spark/Kafka streaming (+ `ram_online/`, `telecom_grpc/`)

### 2.1 Spark stateful stream (`spark/stateful_stream.py`, `spark/distributed_stream.py`)

**Vì sao Spark/Kafka.**
- Lịch sử cuộc gọi và đặc trưng liên thực thể (IMEI, cell) của cả mạng lớn hơn một máy.
- Kafka giữ thứ tự theo partition và cho phát lại.
- Spark cho tính toán phân tán, có checkpoint.
- Đường chặn 30 ms **không** đi qua đây. Đây là đường phân tích / cập nhật chính sách.

**Công thức.**
- **Watermark:** `wm = max(wm_cũ, max_ts − L)`; khi flush thì `wm = max_ts`.
- **Phát ra:** sự kiện có `ts ≤ wm`, theo thứ tự `(ts, event_id)`. Thứ tự này là nhân quả và xác định.
- **Late:** `ts < wm_cũ` hoặc `(ts, id) ≤ last_key`. Sự kiện late bị cách ly (quarantine) hoặc làm hỏng batch, tuỳ `late_policy`.
- **Chống trùng:** theo `event_id`, kèm băm nội dung. Cùng id mà khác nội dung thì cách ly cả hai (H10).
- **Đặc trưng phân tán:** ba "thực thể" (thuê bao, IMEI, cell) được tính trong ba lần repartition-and-sort riêng, rồi ghép theo `event_id`.
- **Đặc trưng farm (vòng 6):**
  - partition cell tính `|B(c, ts)|` và `người_gọi ∈ B`;
  - partition thuê bao cho biết người gọi có khớp hồ sơ P hay không;
  - giá trị cuối = `|B|` nếu P, ngược lại 0.
  Test `test_spark_cell_partition_matches_feature_store_for_a_farm` khẳng định kết quả khớp `FeatureStore`.
- **Checkpoint:** snapshot Parquet ghi vào thư mục tạm rồi đổi tên nguyên tử, kèm `source.json` để ràng buộc cấu hình và hợp đồng đặc trưng.

**Đánh giá.** Phù hợp cho phân tích. Giới hạn:
- chỉ xử lý CALL;
- checkpoint là snapshot đầy đủ;
- chưa đo throughput nhiều máy.

### 2.2 Bloom filter có rào phiên bản (`ram_online/core.py`)

**Vì sao.** Tra danh sách chặn trong RAM với 1 phép băm, không cần RTT tới Redis cho đa số cuộc gọi sạch. Bloom không bao giờ âm tính giả, nên "Bloom âm" là bằng chứng chắc chắn số đó **không** có trong danh sách.

**Công thức.**
- `m = ⌈−n·ln p / (ln 2)²⌉` bit; `k = max(1, round((m/n)·ln 2))`.
- Vị trí bit: `hᵢ = (a + i·b) mod m` (băm kép Kirsch–Mitzenmacher), với a, b là hai nửa 64 bit của BLAKE2b-128 và b ép lẻ.
- **Ví dụ:** n = 10⁶, p = 10⁻⁴ cho m ≈ 19,17 triệu bit (≈ 2,4 MB) và k = 13.
- **Rào phiên bản (Lua, nguyên tử):** chỉ được tin "Bloom âm" khi `manifest_live = manifest_snapshot` và `SCARD = count`. Ngược lại phải tra tập chính xác (`SISMEMBER`). Mọi bất nhất trả UNKNOWN (fail-open).

**Đánh giá.** Đúng công thức. Là nhánh thử nghiệm, chưa nối API chính (quyết định vòng 5).

### 2.3 Cửa sổ thời gian thực Redis (`ram_online/streaming.py`)

- `cutoff = frontier − 60 000 ms`. `ZADD NX` khi `ts > cutoff`, rồi đếm các phần tử trong `(cutoff, frontier]`.
- Gắn cờ khi `count > ngưỡng` và `now − frontier ≤ 5 s`.
- **Vì sao:** ZSET cho phép đếm cửa sổ và xoá phần tử cũ trong O(log n), nguyên tử trong một script Lua.

### 2.4 gRPC edge (`telecom_grpc/`)

- Dùng protobuf và mTLS.
- **Vì sao:** tổng đài cần giao thức nhị phân, khung dữ liệu cố định, xác thực hai chiều bằng chứng chỉ.
- Không chứa thuật toán học máy; là adapter thử nghiệm.

---

## 3. EduDSS (`edu_dss/`)

| Thuật toán | Dùng ở đâu | Vì sao | Công thức / tham số | Đánh giá |
|---|---|---|---|---|
| JWT HS256 + RBAC theo khoá học | `security.py`, `course_access.py` | Không trạng thái; claim mang danh sách khoá học nên không cần tra DB mỗi request | `sig = HMAC-SHA256(secret, base64url(header) ‖ "." ‖ base64url(payload))`; kiểm `exp`, tuỳ chọn `aud`; sinh viên và giảng viên chỉ vào khoá có trong claim | Phù hợp. Cần IdP/OIDC thật khi triển khai |
| Hồi quy tuyến tính một biến (OLS) | `grade_baseline.py` | Baseline minh bạch, giải thích được, không cần thư viện | `β₁ = Σ(x−x̄)(y−ȳ)/Σ(x−x̄)²`, `β₀ = ȳ − β₁x̄`, kẹp về [0, 4]; đánh giá `MAE = mean|ŷ−y|`, `RMSE = √mean(ŷ−y)²` trên 2 dòng giữ lại theo thời gian | **Chỉ là demo**: 7 cặp dữ liệu tổng hợp, không dùng để ra quyết định học vụ |
| Giới hạn tốc độ LLM: cửa sổ cố định trên Redis | `services.reserve_llm_slot` | Một lệnh Lua nguyên tử, không cần khoá phân tán | `bucket = ⌊t/60⌋`; `count = INCR(key:bucket)`; `EXPIRE 120`; cho phép khi `count ≤ limit`; chờ `60 − (t mod 60)` s | Phù hợp. Biên cửa sổ có thể cho tối đa 2×limit trong 60 s (đặc tính của cửa sổ cố định) |
| Cắt văn bản bài giảng có chồng lấn | `services.precompute_cms` | Giữ ngữ cảnh ở biên đoạn khi tóm tắt / giải thích | đoạn 3000 ký tự, bước 2800 (chồng 200) | Phù hợp |
| Khoá cache percent-encode | `cache_keys.py` | Chống va chạm khoá: `(CS101, sec2:lec1)` ≠ `(CS101:sec2, lec1)` (F16) | `key = "edu:" + quote(seg₁, safe="") + ":" + …` | Phù hợp |
| Hàng đợi Celery/Redis | `celery_app.py`, `tasks/` | Tác vụ dài (AutoML, tiền xử lý) không chặn API | at-least-once, có tiến độ | Phù hợp. AutoML, LLM và graph đều là **adapter**, thuật toán thật nằm ngoài repo |

---

## 4. Tổng hợp: thuật toán nào phù hợp, cái nào chưa

| Mức | Thuật toán |
|---|---|
| **Phù hợp, đã kiểm chứng bằng test** | Cửa sổ trượt nhân quả; IngestGuard; ngưỡng Clopper–Pearson; chính sách bằng chứng hành vi; circuit breaker và luật dự phòng; PSI và kiểm định nhị thức; Wilson và bootstrap theo cụm; chuỗi bằng chứng HMAC; scrypt; scale-out; checkpoint tăng dần; Bloom và Lua; watermark Spark; JWT/RBAC EduDSS; cache key |
| **Phù hợp về phương pháp, bằng chứng chỉ trên mô phỏng** | LightGBM CALL; IsolationForest zero-day; TF-IDF + LR cho SMS; đặc trưng B-number; đặc trưng farm đồng vị trí |
| **Chưa đủ làm bằng chứng** | AUDIO LightGBM (đặc trưng chỉ có trong mô phỏng; BLOCK đã tắt); OLS EduDSS (7 điểm tổng hợp); mô hình giọng thật (chưa có corpus) |
| **Không làm, theo quyết định chủ dự án** | Hiệu chuẩn xác suất (isotonic / Platt) |

---

## 5. Các chỉ tiêu từng đỏ: đã sửa thế nào

Đo bằng `python -B evals/run_evals.py`: gộp cửa sổ test + 10 cửa sổ lặp trải đều 24 giờ. Mục tiêu **không** bị hạ.

| Chỉ số | Ngưỡng | Vòng 4 (tfd-v2.4) | Vòng 5 (tfd-v2.5) | **Vòng 6 (tfd-v2.6)** |
|---|---|---|---|---|
| block_recall_pct | ≥ 45 | 43,5 ✗ | 45,06 | **45,58** ✓ |
| block_precision_pct | ≥ 95 | 99,6 | 99,62 | **99,62** ✓ |
| block FPR / cận trên 95 % | ≤ 0,01 % (cận trên) | 0,0003 / 0,0017 | 0,0003 / 0,0017 | **0,0003 / 0,0017** ✓ |
| any_recall_pct | ≥ 80 | 81,11 | 80,76 | **90,29** ✓ |
| any_fpr_pct | ≤ 0,2 | 0,0703 | 0,0718 | **0,0839** ✓ |
| **any_recall_min_window_pct** | ≥ 70 | 68,57 ✗ | 68,42 ✗ | **83,33** ✓ |
| **family_min_detected_pct** | ≥ 75 | 68,2 ✗ (SIM box) | 67,5 ✗ (SIM box) | **87,1** ✓ (họ thấp nhất giờ là DEEPFAKE_VOICE) |
| SIM box phát hiện | — | 68,2 | 67,5 | **87,6** |
| zero_day_caught_pct | ≥ 80 | 90,2 | 90,2 | **90,2** ✓ |
| evidence_chain_valid, smoke_passed | true | true | true | **true** ✓ |
| serving ≤ 30 ms % | ≥ 95 | 99,0 | 26,5 ✗ | **98,5** ✓ |
| serving p99 ms | ≤ 30 | 28,1 | 221 ✗ | **33,4** ✗ (100 EPS; 50 EPS là 17,0) |
| serving fail-open | 0 | 1 ✗ | 210 ✗ | **4** ✗ (100 EPS; 50 EPS là 0) |

**SIM box từ 67,5 % lên 87,6 %:** 34 sự kiện còn bỏ sót, so với 89 trước đó; tất cả thuộc farm ẩn `SB_T2_STEALTH` (từ 22,6 % lên 70,4 %). 26 trên 34 là cuộc gọi thứ 1 hoặc 2 của SIM (17 + 9), khi chưa đủ SIM nào "dồn dập" (≥ 2 cuộc) để thành bằng chứng đồng vị trí. Đây là giới hạn tự nhiên của một luật nhân quả: không thể biết là farm trước khi farm kịp gọi.

**Cái giá:** FLAG nhầm tăng từ 0,0718 % lên 0,0839 %, tức khoảng 1 cảnh báo nhầm thêm trên 8 000 cuộc gọi sạch, vẫn dưới cổng an toàn 0,2 %. FPR chặn **không đổi**, vì luật farm chỉ FLAG.

**Còn đỏ: p99 và fail-open của đường phục vụ ở 100 EPS.**
- Đã gỡ ba nguồn dừng đo được (§1.16). Cùng máy này, vòng 5 đo p99 221 ms với 210 fail-open; nay là 33,4 ms với 4 fail-open.
- Phần còn lại là độ trễ fsync của đĩa ảo WSL: một commit `synchronous=FULL` dao động 4–25 ms tuỳ lúc. Tám lần đo A/B liên tiếp có cùng mã mà kết quả chênh nhau từ 0 tới 132 fail-open, nên **máy này không đủ yên để chốt SLA**.
- Baseline đo trên runner CI là p99 18,6 ms và vẫn được giữ.
- **Group commit: đã có sẵn, không phải lời giải.** Owner loop vốn lấy hết các request đang chờ (tối đa 16) vào **một** giao dịch, tức một lần fsync. Đề xuất "làm group commit" ở lượt trước của tôi là sai, vì nó đã tồn tại.
- **Thí nghiệm đã chạy** (100 EPS, 600 sự kiện mỗi lần, đọc `commit_ms` phía server):

  | Cấu hình | commit p50 | commit p99 | fail-open (3 lần) |
  |---|---|---|---|
  | Hiện tại: `FULL`, checkpoint nền 0,25 s, evidence gom 20 ms | 4,6–4,9 ms | 11–16 ms | 28 / 9 / 69, lần sau 56 / 216 / 64 |
  | Evidence gom 200 ms | 4,8–5,0 ms | 14–16 ms | 108 / 94 / 11 |
  | Evidence gom 200 ms + checkpoint 1 s | 4,5–4,7 ms | 11–16 ms | 10 / 145 / 60 |
  | Checkpoint ngay trong COMMIT (như cũ) | 2,2–2,3 ms | 11–16 ms | 45 / 69 / 82 |
  | **`synchronous=NORMAL`** | **0,17 ms** | **1–4 ms** | **0 / 34 / 1** |

- **Kết luận đo được:** nút thắt là **fsync bắt buộc ở mỗi COMMIT** trên đĩa ảo WSL (4–25 ms). Giãn các fsync phụ không làm p99 của COMMIT giảm. Chỉ bỏ fsync mỗi COMMIT mới đưa đường phục vụ về trong 30 ms trên máy này.
- **Đã thêm (mặc định không đổi):**
  - `FRAUD_SQLITE_SYNCHRONOUS=FULL|NORMAL`, mặc định FULL, được hiện ở `/health/ready` qua trường `journal_durability`;
  - `FRAUD_SQLITE_CHECKPOINT_INTERVAL_S` (0,05–10 s);
  - `FRAUD_EVIDENCE_COALESCE_S` (0–5 s).
  Mọi giá trị sai đều báo lỗi ngay lúc khởi động.
- **Cần chủ dự án quyết:**
  1. **Giữ `FULL`** và chốt SLA trên phần cứng đích. SSD trung tâm dữ liệu có tụ chống mất điện thường fsync dưới 0,5 ms. Lệnh đo: `python -B src/verify_live_http.py --rates 50,100 --seconds 30`, chạy 3 lần.
  2. **Đổi mặc định sang `NORMAL`.** Tiến trình sập không mất gì. Mất điện hoặc OS sập có thể mất các phán quyết commit sau lần checkpoint gần nhất (khoảng dưới 1 giây). Khi đó nhà mạng gửi lại (at-least-once) thì sự kiện được chấm lại, nhưng bản ghi bằng chứng của những phán quyết đã trả lời trước đó sẽ không còn.
