> **Bản full-stack (16/11/2026):**
> - Giao diện tách riêng: app thuê bao ở `/app/` (tài khoản email/Google, số giả lập riêng hoặc OTP SMS) và bảng vận hành ở `/admin/` (tên + mật khẩu hoặc Google).
> - Trang quản trị `/admin/manage`: duyệt số bị báo cáo, quản lý tài khoản nhân viên, chi phí, pipeline/KPI.
> - Dữ liệu người dùng lưu bền trong SQLite.
> - `/metrics` cho Grafana Cloud kèm 12 cảnh báo.
> - Bộ eval có cổng chặn hồi quy: [evals/README.md](evals/README.md).
> - Docker cho amd64 và arm64; deploy miễn phí chung VM với FieldOps: [deploy/cloud/README.md](deploy/cloud/README.md).
> - Tầng scale-out chia tải theo số chủ gọi, chất lượng giống chạy 1 tiến trình; ngoại suy khoảng 8 lõi cho 50k TPS: [docs/SCALE_OUT.md](docs/SCALE_OUT.md).

# Telecom Fraud Shield v2 — Real-time two-tier AI fraud detection

> Cập nhật kỹ thuật v2.2: xem [hướng dẫn chạy, pipeline WAV và kiểm chứng](docs/DEVELOPMENT_V22.md). Xem [kết quả kiểm thử v2.2](docs/VALIDATION_V22.md). Bộ mới nằm ở `runtime/artifacts_v22/`; demo/live tự ưu tiên bundle có manifest. Các số liệu v2.1 bên dưới là baseline lịch sử trên dữ liệu giả lập. Live mặc định shadow; pipeline WAV dùng miền đặc trưng riêng và chưa có corpus âm thanh thật được kiểm định.

> Cập nhật 2026-10-25: [407 tests + 50 subtests, sửa lỗi và kiểm chứng](docs/BUGFIX_20261025.md). [SLA HTTP p99 30 ms vẫn chưa đạt](docs/SLA_30MS.md); cần Python >= 3.11/Uvicorn >= 0.47.0.

> Tối ưu SLA tiếp theo: [426 tests + 50 subtests, sửa bão ghi agent và kết quả soak](docs/SLA_TARGET_20261025.md). Các lượt lặp lại chưa đạt 30 ms ổn định, gate tổng vẫn thất bại.

Hệ thống AI phát hiện & ngăn chặn lừa đảo viễn thông thời gian thực (bản 2).
Demo video: https://claude.ai/artifact/1YaojRoGZTyfxCVck7BCy4

## Chạy nhanh / Quick start
```bash
pip install -r requirements.txt
python run_demo.py                 # dashboard -> http://localhost:8080 · app thuê bao -> http://localhost:8080/app/
```
`run_demo.py` in ra token đăng nhập theo vai trò (tạo ngẫu nhiên lần đầu, lưu ở `secrets/users.json`, không chia sẻ file này).

### Phân quyền & nhật ký thao tác / Access control & audit trail
| Vai trò | Được làm |
|---|---|
| `admin` (nhà mạng) | Mọi quyền + xem nhật ký thao tác; tiêm tấn công / gây lỗi **chỉ khi** `FRAUD_DEMO=1` |
| `analyst` | Xem bằng chứng, số điện thoại đầy đủ, kiểm tra chuỗi hash |
| `viewer` | Dashboard chỉ đọc, số thuê bao bị che (`849****4567`) |

Mọi thao tác đặc quyền được ghi vào `logs/audit.jsonl`: chuỗi SHA-256 (sửa/xóa một dòng là gãy chuỗi) **và** chữ ký HMAC
bằng khóa riêng của người thao tác (biết chính xác ai làm; không ai ghi được dòng mang tên người khác).
Giới hạn: khóa đang nằm trên máy chủ; triển khai thật cần HSM / chữ ký bất đối xứng trên thiết bị từng người và neo đầu chuỗi sang hệ thống nhà mạng.

### Chịu lỗi / Resilience
Khi mô hình quá 30 ms hoặc lỗi: **luật dự phòng** (chỉ tra bảng) vẫn chặn số/IMEI đã bị mô hình chặn, số tổng đài được bảo vệ
không đi qua trung kế chính chủ, brandname sai kênh; phần còn lại mới fail-open. Số tổng đài và brandname không bao giờ vào
danh sách chặn (chúng là nạn nhân bị giả mạo). Sự kiện mô hình chưa chấm vào **hàng đợi có giới hạn** (50.000, tràn thì đếm),
được chấm lại khi hồi phục → **cảnh báo hồi tố** (cuộc gọi đã qua: gắn cờ số, báo người nhận). Kết luận đến muộn được giữ lại, không chấm hai lần.

### App thuê bao (PWA) / Subscriber app
`/app/`: cảnh báo cuộc gọi/SMS lừa đảo tới số của mình, lịch sử kèm lý do, tra cứu số lạ, báo cáo số (không tự động chặn),
hướng dẫn tự bảo vệ (gồm cuộc gọi im lặng lấy giọng). Chỉ thấy dữ liệu của chính số mình; không đọc danh bạ.
Điện thoại cùng Wi-Fi mở `http://<IP máy chạy>:8080/app/` → "Thêm vào màn hình chính". Bản demo đăng nhập bằng số giả lập;
bản thật cần OTP. Service worker (mở offline) chỉ chạy trên HTTPS hoặc localhost.

### Tích hợp nhà mạng: đứng song song, không đứng giữa
```
 Cuộc gọi ──► Tổng đài / IMS của nhà mạng ──────────────► Người nhận        (luồng thật, không đi qua hệ thống này)
                     │  bản sao báo hiệu (mirror)    ▲
                     ▼                               │ nhà mạng TỰ quyết định thực thi
              POST /api/v1/verdict  ──► khuyến nghị (BLOCK / FLAG / ALLOW, mã lý do, < 30 ms)
              POST /api/v1/cdr      ◄── thời lượng thật khi cuộc gọi kết thúc
```
- Hệ thống này **không nằm trên đường đi của cuộc gọi**: nó nhận bản sao, trả khuyến nghị. Không trả lời kịp hoặc sập
  thì tổng đài cứ nối cuộc gọi; không có cuộc gọi nào bị rớt vì hệ thống này.
- Mỗi yêu cầu được thiết bị mạng ký **HMAC-SHA256** (khóa chung `secrets/network_link.key`) kèm mốc thời gian:
  sai chữ ký, lệch giờ quá 30 giây hoặc gửi lại một yêu cầu cũ đều bị từ chối.
- Admin nhà mạng chuyển `shadow` (chỉ chấm điểm để so sánh, khuyến nghị luôn là NONE) ↔ `enforce` qua
  `POST /api/network/mode`; mỗi lần chuyển được ghi nhật ký có chữ ký. Mặc định là `shadow`.
- Giao thức thật của tổng đài (SIP/Diameter/SS7, CAMEL) phụ thuộc thiết bị của nhà mạng; API JSON này là lớp chuyển đổi.

### AI agent: máy đề xuất, con người quyết định
`src/agent.py` giám sát hệ thống mỗi 3 giây và chỉ tạo **đề xuất** (thấy gì, vì sao, khuyến nghị gì, hành động sẽ làm gì).
Không có đường code nào để agent tự thay đổi mạng: hành động chỉ chạy trong `decide()` khi **admin bấm duyệt**; từ chối thì
không thay đổi gì và agent không đề xuất lại. Đề xuất được ký bằng khóa của agent, quyết định ký bằng khóa của người duyệt.
| Agent phát hiện | Đề xuất (chờ duyệt) |
|---|---|
| Nhóm số xoay vòng trên cùng thiết bị (SIM box) | Chặn cả nhóm số + thiết bị (SIM mới trên thiết bị đó cũng bị chặn) |
| Thuê bao lâu năm bị chặn | Gỡ chặn, đưa vào danh sách tin cậy (mô hình chỉ còn cảnh báo) |
| Nhiều người dùng báo cáo một số chưa bị chặn | Chặn số đó (báo cáo không bao giờ tự chặn) |
| Sự kiện lọt lúc sự cố nay xác định là lừa đảo | Gửi cảnh báo tới người đã nhận |
| Mô hình không phản hồi > 5 giây | Khôi phục / quay về phiên bản ổn định |
| Tài khoản mở số đầy đủ bất thường | Tạm khóa tài khoản (chống rò rỉ nội bộ) |
| Chuỗi bằng chứng bị sửa | Mở điều tra |

### Mọi nhân viên được xem, không lộ dữ liệu
- Mọi nhân viên tập đoàn có tài khoản (vai trò *Nhân viên*) xem được dashboard phân tích và đề xuất của agent.
- Số điện thoại và IMEI **bị che với mọi vai trò, kể cả admin**. Chuyên viên mở từng số phải **ghi lý do**; mỗi lần mở vào nhật ký có chữ ký và agent đếm số lần mở.
- Watermark tên tài khoản + thời gian phủ toàn màn hình: ảnh chụp bị rò rỉ lộ ra người chụp.
- Không tải hàng loạt: tối đa 40 cảnh báo / 50 bản ghi bằng chứng mỗi lần; tài khoản bị khóa thì bị chặn ngay.
- Đăng nhập bằng mã cá nhân → **phiên có hạn** (8 giờ, hoặc 30 phút không thao tác), lưu trong `sessionStorage` (đóng tab là mất);
  mã cá nhân chỉ dùng để đăng nhập, không dùng trực tiếp cho API. Tạm khóa tài khoản kết thúc ngay mọi phiên của nó.
- Giới hạn còn lại: triển khai thật cần SSO của tập đoàn thay mã cá nhân, và HTTPS.

### Khác biệt so với Truecaller / How this differs from Truecaller
| | Truecaller | Lá chắn (hệ thống này) |
|---|---|---|
| Phát hiện ở đâu | Trên điện thoại, dựa danh bạ cộng đồng | **Tầng nhà mạng**, trước khi đổ chuông |
| Số lừa đảo mới tinh | Chưa ai báo cáo → không biết | Bắt theo **hành vi** (tần suất, thiết bị, trạm) + mô hình bất thường cho kiểu lừa chưa từng thấy |
| Giả mạo số ngân hàng | Hiện **đúng tên ngân hàng** → càng dễ tin | Biết cuộc gọi không đi qua trung kế ngân hàng → chặn |
| SIM box, thay SIM liên tục | Từng số một | **Chặn theo nhóm** số + thiết bị (người duyệt) |
| Giọng nói deepfake trong cuộc gọi | Không | Tầng 2 phân tích âm thanh, ngắt cuộc gọi |
| Danh bạ người dùng | Tải danh bạ lên máy chủ | **Không** đọc danh bạ |
| Bị báo cáo bôi xấu | Báo cáo hàng loạt có thể gắn nhãn sai số sạch | Báo cáo chỉ là tín hiệu, con người quyết định |
| Bảo vệ người thân | Không | Người giám hộ nhận cảnh báo khi bố mẹ gặp lừa đảo (có đồng ý, không xem nội dung) |
| Lọt lúc hệ thống lỗi | — | Chấm lại sau sự cố + cảnh báo hồi tố |
| Cần cài app | Bắt buộc | Chặn ở mạng cho **mọi thuê bao**, app chỉ để xem / nhận cảnh báo |

Truecaller vẫn mạnh hơn ở: hiển thị tên người gọi lạ (caller ID) toàn cầu, kho số khổng lồ nhiều nước, chạy với mọi nhà mạng.
Lá chắn chỉ bảo vệ thuê bao của nhà mạng triển khai nó; đó là đánh đổi để có tín hiệu tầng mạng mà app không thể có.

### App Android (trình giả lập) / Android app (emulator)
`android/` là project Kotlin bọc app thuê bao trong WebView (Android 8+). Trình giả lập gọi server trên máy tính qua `http://10.0.2.2:8080/app/`;
điện thoại thật đổi địa chỉ trong màn "Máy chủ" thành IP Wi-Fi của máy tính. Bộ công cụ (JDK 17, Android SDK, máy ảo Pixel 6 Android 14)
cài ở `D:\AndroidDev`. Chạy server trước (`python run_demo.py`), rồi:
```powershell
powershell -ExecutionPolicy Bypass -File android\run_android.ps1   # build APK, bật máy ảo, cài và mở app
```
APK: `android/app/build/outputs/apk/debug/app-debug.apk`. Bản demo chưa nhận cuộc gọi thật của máy (cần tích hợp nhà mạng + `CallScreeningService`).

Dashboard vận hành có nút gạt **sáng / tối** trên header (nhớ lựa chọn trong trình duyệt).
Tái tạo toàn bộ / Rebuild everything (≈10 min on 2 vCPU):
```bash
python src/build_dataset.py   # simulate train / val / independent test (+24 h warm-up)
python src/train.py           # LightGBM, Isolation Forest, SMS model; thresholds from validation FPR
python src/evaluate.py        # 50k test + 10 x 50k replicates -> data/benchmark_results.json
python src/loadtest.py        # open-loop streaming load test -> data/loadtest_results.json
python -m pytest -q tests     # 64 tests
```
Lớp sẵn sàng vận hành / Production-readiness layer:
```bash
python src/make_drift_baseline.py  # reference distributions + noise floor -> models/drift_baseline.json
python src/shadow_run.py           # score a window, enforce nothing     -> data/shadow_report.json
python src/experiment_guard.py     # duplicates + late events, guard on/off -> data/guard_experiment.json
```

## Kết quả / Results (simulated data, no real subscribers) — model `tfd-v2.1`
| Metric | Target | v2.0 | **v2.1** |
|---|---|---|---|
| Hard-block false-positive rate, 500k events | < 0.01 % | 0.0012 % | **0.0012 %** (6 events; 95 % CI ≤ 0.00262 %) |
| Fraud blocked or flagged (mean of 10 windows) | – | 85.2 % (min 78.6 %) | **84.1 %** (min 75.0 %) |
| Fraud blocked before ringing (mean of 10 windows) | – | 54.2 % | **53.1 %** |
| Warnings on legitimate events (flag, call still connects) | – | 0.039 % | 0.102 % |
| Independent 50k test: block / block+flag recall | – | 48.0 % / 78.0 % | **54.5 % / 87.9 %** (0 wrong blocks) |
| New threats, 5 × 50k: rented old-SIM farms / silent voice-harvesting | – | not modelled | **87.2 % / 87.4 %**, 10/10 numbers each caught, wrong blocks 0.002 % |
| Tier-1 decision, single event p99 | < 30 ms (< 1 ms asked in review) | 0.45 ms (2 vCPU server) | **0.64 ms** (laptop, 8 threads) |
| SMS / audio p99 | < 170 ms | 2.5 / 0.2 ms | 1.1 / 0.24 ms |
| Feature store p99 | < 10 ms | 0.084 ms | 0.10 ms |
| Streaming throughput within 30 ms p99 | ≥ 50,000 TPS | ~25,000 events/s on 2 vCPU server (NOT met) | 10,000 events/s with 4 processes on the laptop (NOT met; ~2,500 per process) |
| Class imbalance (test window) | ~0.1 % | 0.1 % | 0.13 % |

What changed in v2.1 (and why the comparison is fair): real Vietnamese number ranges; ~5 % *occasional* real callers
(new on the network, nobody calls them — they look like fraud SIMs on those two signals); two new fraud families measured
in separate windows; feature `active_hours_on_network` (rented/dormant SIMs); in-call voice features + per-number
memory for voice harvesting; and a **block policy**: a call is hard-blocked only with behavioural evidence (several SIMs
per device, gateway device, bursts, spoofed hotline, unknown cell, many very short calls, a large new-SIM cluster, or
voice harvesting of 2+ people); otherwise it is warned, never blocked, on "new number" alone. v2.0 models and results are
kept in `backup_tfd-v2.0/`.

Performance bugs fixed in v2.1 (scores unchanged, verified event by event): LightGBM / Numba used every core and spun for
each small batch (the 300 events/s demo server burned 4.2 cores); the SMS path spent most of its time in pandas / sklearn
validation (TF-IDF + logistic regression now computed directly, identical to sklearn); each new detector skipped model
warm-up, so its first batches timed out and opened the breaker; the load test allowed 512-event batches (~33 ms > budget)
and logged every fail-open event, a feedback loop under overload (now 128-event batches, fail-open counted in batch
summaries + 1 % sample); cell coordinates came from Python's per-run `hash()`, so training and runtime used different
maps. Throughput is measured on a shared laptop and varies between runs (2 processes at 5,000/s: p99 19 ms in one run,
49 ms in the next); a dedicated multi-core server is needed for a stable 50,000 TPS figure.

### Robustness to an at-least-once feed (`data/guard_experiment.json`)
Same 50k test window, 2 % of events delivered late and a share redelivered byte-identically.
Decisions that differ from the clean stream:

| Redelivery rate | Guard OFF | Guard ON |
|---|---|---|
| 5 % | 152 | 1 |
| 10 % | 348 | 1 |
| 20 % | 793 | 1 |

Guard off, the extra actions all land on *legitimate* subscribers (at FLAG level, not BLOCK).
Guard on, the single difference is one fraud event that arrived >120 s late and is therefore
recorded but not enforced — the call had already ended.

## Cấu trúc / Layout
- `src/data_generator.py` actor-based traffic simulator (overlapping legit/fraud behaviour, zero-day Wangiri only in test)
- `src/feature_store.py` causal streaming features (1m/10m/1h/24h windows, IMEI–SIM graph, mobility)
- `src/train.py` models + thresholds · `src/detector.py` runtime scoring + reason codes · `src/fast_iforest.py` Numba Isolation Forest
- `src/resilience.py` fail-open + circuit breaker · `src/evidence.py` SHA-256 hash-chained evidence log
- `src/ingest_guard.py` deduplication, event-time watermark, idempotent action ledger
- `FeatureStore.save_checkpoint/load_checkpoint` crash recovery without re-running the 24 h warm-up
- `src/drift.py` PSI drift monitor with a measured noise floor; `src/shadow_run.py` score-but-never-enforce mode
- `src/server.py` + `static/index.html` live dashboard (attack injection, fault drills, chain verification)
- `data/test_events_50k.csv` independent test set · `data/benchmark_results.json`, `data/loadtest_results.json` measurements
- `docs/` bilingual report (DOCX/PDF), slides (PPTX/PDF, with speaker notes), portfolio, figures, demo video; `docs/build/` generates them
- `src/agent.py` AI agent (proposes, humans decide) · `src/security.py` roles, sessions, signed audit, operator-link HMAC
- `HUONG_DAN.md` step-by-step guide (Vietnamese): setup, demo, Android, tests, rebuilding data / models / documents

## Giới hạn / Limitations
Simulated data; audio tier uses pre-computed acoustic features (including who-speaks-how-much from a VAD, which the
operator must supply); SIM boxes ~75 % event recall; the first calls of a brand-new fraud number are usually only warned
(block policy); 50,000 TPS not demonstrated (10,000 on the 4-core laptop). The report and slides in `docs/` (v2.1) are
generated from the measurement files: see `HUONG_DAN.md` section 8 to rebuild them. See report section 8.
