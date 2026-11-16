# HỒ SƠ SẢN PHẨM & KIẾN TRÚC
## Fraud Telecom · Spark/Kafka Streaming · Edu DSS

**Bản trình bày cho lãnh đạo, khách hàng viễn thông và hội đồng kỹ thuật**  
Ngày lập: 28/10/2026 · Phạm vi: mã nguồn, báo cáo kiểm chứng và prototype hiện có trong workspace

> **Thông điệp bán hàng có trách nhiệm:** Bộ giải pháp hướng tới ba năng lực bổ sung nhau: ra quyết định chống gian lận ở luồng mạng, phân tích lịch sử cuộc gọi trên luồng dữ liệu phân tán, và dịch vụ hỗ trợ quyết định học tập theo vai trò. Hồ sơ này phân biệt rõ năng lực đã đo, năng lực đã kiểm tra chức năng và phần còn là thiết kế/scaffold. Không có cam kết SLA, độ chính xác trên mạng thật hay năng lực production nào được suy ra chỉ từ mã nguồn.

---

## 1. Tóm tắt dành cho buổi trình bày

### Câu chuyện 60 giây

Telecom Fraud Shield xử lý tín hiệu định tuyến và hành vi thuê bao để hỗ trợ ba hành động: cho phép, chặn trước khi đổ chuông, hoặc cho qua kèm cảnh báo/chuyển chuyên viên. Trạng thái thuê bao và đặc trưng hành vi được tính theo thời gian thực hoặc từ CDR; mô hình LightGBM, Isolation Forest và luật bảo vệ quyết định mức rủi ro. Kafka/Spark tách khối lượng lịch sử và đặc trưng liên thực thể khỏi đường phản hồi cuộc gọi. Một nhánh gRPC dùng protobuf, Bloom Filter trong RAM và Redis Lua đã được bổ sung làm hướng thay thế cho REST lookup.

Kết quả hiện tại: feature pipeline Spark đã được đối chiếu trên dữ liệu kiểm thử và khôi phục checkpoint qua restart; lookup Bloom/Redis có phép đo thực trên Redis cục bộ; nhưng **API Telecom hiện hành chưa đạt SLA HTTP p99 30 ms ổn định**. Edu DSS có khung FastAPI/Streamlit/Celery, JWT/RBAC và Compose; do không có pipeline Edu DSS gốc trong workspace, AutoML, sinh giải thích, tư vấn LLM và graph vẫn cần adapter nghiệp vụ thật.

### Ba hệ thống và mức độ trưởng thành

| Hệ thống | Giá trị sản phẩm | Hiện trạng chứng cứ | Điều chưa được tuyên bố |
|---|---|---|---|
| Fault Telecom / Fraud Shield | Giảm cuộc gọi lừa đảo, SIM box, CLI spoof, SMS phishing và tín hiệu voice scam | Mã scoring, shadow, evidence, resilience, regression; 426 tests + 50 subtests theo báo cáo 25/10/2026; dữ liệu đánh giá phần lớn giả lập | Chưa đạt ổn định HTTP p99 ≤30 ms; chưa xác nhận FPR/recall trên mạng nhà mạng; gRPC mới là adapter độc lập, chưa thay API cũ |
| Spark/Kafka CALL pipeline | Tính 27 đặc trưng CALL có thứ tự nhân quả; lưu checkpoint, dedup, lateness và quarantine | Batch phân tán/Kafka thật trên mẫu; 27/27 đặc trưng khớp; chạy qua restart và kiểm tra lỗi dữ liệu | Chưa đo throughput production đa máy; full snapshot Parquet, không phải incremental state store; không phải luồng chặn cuộc gọi 30 ms |
| Edu DSS | Đóng gói dịch vụ học tập đa vai trò, xếp hàng tác vụ và phục vụ giải thích/cache | Có mã khung API/UI, JWT role checks, Celery/Redis, Nginx/TLS và Compose; `compose config` hợp lệ | Chưa có pipeline nghiệp vụ trong repo; adapter, graph, IdP/OIDC, quyền lớp học và chất lượng khuyến nghị chưa tích hợp |

### Sơ đồ tổng thể

```text
TELECOM CALL SETUP (đường quyết định)
SIP/IMS/MSC ── gRPC + mTLS ──> CallPolicy (protobuf)
                                ├─ Bloom snapshot (RAM, mỗi replica)
                                └─ Redis Lua: version/cardinality + exact set
                                     └─ ALLOW / BLOCK / unavailable→Fail-Open

TELECOM DATA / ANALYTICS (không nằm trong deadline pre-ring)
Switch setup copy + CDR ──> Kafka ──> Spark CALL features / sliding windows
                                     ├─ state theo caller / IMEI / cell
                                     ├─ dedup, watermark, quarantine, checkpoint
                                     └─ policy publisher ──> Redis deny policy + Bloom refresh

EDU DSS (sản phẩm độc lập)
Browser ── HTTPS 443 ──> Nginx ──┬─ /  ──> Streamlit (view)
                                  └─ /api ─> FastAPI ── JWT/RBAC
                                                  ├─ Redis cache
                                                  └─ Celery broker ──> worker ──> domain adapters
```

Ba luồng trên không phải một hệ thống đã kết nối xuyên suốt. Spark có bộ kiểm chứng riêng; gRPC đọc Redis policy theo hợp đồng của `ram_online`; Edu DSS là stack độc lập.

---

## 2. Hệ thống 1 — Fault Telecom / Fraud Shield

### 2.1 Vấn đề khách hàng và mục tiêu sản phẩm

Nhà mạng cần quyết định khi nhận bản tin thiết lập cuộc gọi (Call Setup), trước khi cuộc gọi đổ chuông. Quyết định chậm có thể làm tăng post-dial delay hoặc khiến phần tử mạng tự timeout. Quyết định sai có hai loại chi phí khác nhau:

- **False negative:** cuộc gọi/SMS gian lận được giao tới thuê bao; tăng tổn thất và khiếu nại.
- **False positive:** thuê bao thật bị từ chối hoặc bị cảnh báo; đây là rủi ro trải nghiệm, pháp lý và thương hiệu.

Do đó mục tiêu không đơn giản là tối đa recall. Hệ thống tách **BLOCK** có tác động mạnh khỏi **FLAG** (cho qua, giám sát hoặc chuyển người xem xét). Deadline 30 ms là ngân sách từ bộ điều khiển/adapter mạng; độ chính xác, FPR, độ bền và latency phải được nghiệm thu riêng.

### 2.2 Các lớp cấu tạo trong phiên bản hiện có

```text
Telecom adapter / REST FastAPI hiện hành
        │ signed CALL / SMS / AUDIO observation
        ▼
Validation + authentication + idempotency + bounded admission
        ▼
Inference owner (một chủ sở hữu state/model)
        ├─ FeatureStore causal trong RAM
        ├─ CALL: LightGBM + Isolation Forest + behavioural evidence gate
        ├─ SMS: TF-IDF + Logistic Regression + route/URL metadata
        └─ AUDIO: audio/text features + Tier-2 model/heuristic
        ▼
Policy override + fallback/circuit breaker + deadline accounting
        ▼
SQLite WAL/FULL transaction → durable evidence/journal → HTTP response
        └─ agent/dashboard/audit/shadow/drift chạy tách biệt theo chức năng
```

`src/server.py` là ranh giới FastAPI và vận hành; `src/detector.py` ghép feature với model; `src/feature_store.py` tính lịch sử nhân quả; `src/resilience.py` áp fallback, circuit breaker và rescoring. `models/` chứa artifact cũ; bộ báo cáo v2.2 tham chiếu bundle trong `runtime/artifacts_v22/`. `src/security.py` có vai trò viewer, analyst, admin; AI agent không phải tài khoản người dùng.

**Đường hiện hành có durability trên online path:** theo hợp đồng đã tối ưu, một kết quả chỉ được xác nhận bền vững sau COMMIT. Queue trong RAM không phải kết quả bền; nếu trả `PENDING`, adapter phải retry với cùng event ID và nội dung, không được coi 200/ALLOW là model đã chấm thành công. Điều này bảo vệ audit/idempotency nhưng khiến thời gian COMMIT/storage tham gia latency.

### 2.3 Đặc trưng CALL — dữ liệu nào biến thành tín hiệu

Feature contract v2.2 là `causal-v2-exact-24h-completion-order`. Một sự kiện chỉ được dùng thông tin đã nhìn thấy trước hoặc tại thời điểm sự kiện; nhãn gian lận không đi vào feature.

| Nhóm | Ví dụ feature | Ý nghĩa vận hành |
|---|---|---|
| Tốc độ | `calls_1m`, `calls_10m`, `calls_1h` | Thuê bao phát sinh burst hoặc gọi hàng loạt |
| Phân tán đích | `callee_dispersion_1h = unique_callee_1h / calls_1h` | Gọi tới nhiều đích khác nhau có thể gợi ý wangiri/robocall |
| Cân bằng luồng | `in_out_ratio_24h = (incoming_24h+1)/(outgoing_24h+1)` | Tách SIM nhận ít cuộc so với số gọi đi |
| Kết quả cuộc gọi | `short_call_ratio = #duration<5s / #completed_calls`; `mean_log_duration = mean(log(1+d))` | Nhiều cuộc rất ngắn; `log1p` giảm độ lệch của duration dài |
| Quan hệ SIM/thiết bị | `imei_per_msisdn_24h`, `msisdn_per_imei_24h` | Một SIM đổi nhiều máy hoặc một máy chạy nhiều SIM/SIM box |
| Cell và định vị | `new_sims_on_cell_1h`, `velocity_kmh`, cell chưa đăng ký | Cụm SIM mới hoặc di chuyển không hợp lý |
| Danh tính định tuyến | `protected_cli_mismatch`, trunk, network, giờ, tuổi SIM | CLI được bảo vệ nhưng không đi qua trunk hợp lệ, SIM nước ngoài/mới, 2G |

Sliding queue/deque giúp cập nhật và loại sự kiện cũ theo cửa sổ. Thời lượng cuộc gọi chưa có trong setup; CDR đến sau và được đưa vào heap theo completion time. Như vậy một cuộc gọi chưa hoàn tất không bị tính như đã có duration. Lịch sử chính xác tạo feature tốt hơn nhưng cần state, thứ tự sự kiện, checkpoint và recovery.

### 2.4 Mô hình, ngưỡng và chính sách quyết định

**CALL/Tier 1.** LightGBM cho điểm rủi ro `p = f_LGBM(x)`; ngưỡng cấu hình `t_flag`, `t_block` tạo ba dải:

```text
p < t_flag                 → ALLOW
t_flag ≤ p < t_block       → FLAG
p ≥ t_block               → BLOCK, nếu qua cổng bằng chứng hành vi
```

Một Isolation Forest riêng theo segment domestic/foreign/trunk chấm bất thường trên một tập feature chuẩn hóa bằng median/IQR. Nếu rủi ro supervised thấp nhưng anomaly vượt ngưỡng segment, kết quả ít nhất là FLAG (zero-day path). Mô hình anomaly không tự động biến bất thường thành BLOCK.

**Cổng bằng chứng trước khi chặn:** nếu LightGBM đề xuất BLOCK nhưng thiếu bằng chứng hành vi, chính sách hạ xuống FLAG. Ví dụ bằng chứng: CLI spoof có bảo vệ; cell/radio không tin cậy; nhiều SIM/IMEI; gọi dồn dập; nhiều cuộc rất ngắn đã hoàn tất; nhiều SIM mới cùng cell và caller gọi tiếp; hoặc vận tốc di chuyển bất thường. Đây là lớp bảo vệ false positive có thể giải thích cho NOC.

**SMS.** Text được chuẩn hóa, vector hóa TF-IDF rồi Logistic Regression `p=σ(wᵀx+b)`; metadata gồm route brand/P2P/BTS, domain URL và tốc độ gửi. Hiện thực tính thẳng sparse dot product để giảm overhead framework cho batch nhỏ.

**AUDIO/Tier 2.** Dùng feature âm học/transcript như jitter, spectral flatness, codec, lexical hit, VAD caller/callee. Đây là đường sau khi đã có audio, không thể đặt vào ngân sách Call Setup pre-ring. Báo cáo ghi rõ chưa có corpus WAV tiếng Việt thật có nhãn và chưa chứng nhận accuracy/deepfake; mặc định OBSERVE/NONE.

Các artifact và thresholds được gắn version/manifest để giảm nguy cơ nạp nhầm model. SHADOW chấm và ghi bằng chứng nhưng không thực thi chặn. Chỉ có thể chuyển sang enforce sau đánh giá calibration, rollback, phê duyệt con người và kiểm chứng mạng thật.

### 2.5 Hiệu năng: vì sao chưa chốt SLA 30 ms

Độ trễ cần xét theo tổng ngân sách, không chỉ inference:

```text
T_total = T_network + T_auth/parse + T_queue/scheduler + T_feature
        + T_model + T_policy + T_durable_commit + T_response
```

Nếu SLA đo từ MSC tới adapter, RTT/serialization/queue/DB/evidence đều nằm trong đường đo. Phép đo server-local không thay được end-to-end. Trên code cũ, một owner state, SQLite WAL/FULL, evidence bền và HTTP transport vẫn là thành phần đáng kể. Thay sang gRPC giảm chi phí framing/JSON; không loại bỏ network RTT, tính toán feature hay fsync nếu vẫn nằm trên đường đồng bộ.

**Số đo xác thực mới nhất của HTTP API cũ:** 3 cửa sổ 50 EPS, mỗi cửa sổ 30 giây; concurrency 32; tất cả 1.500 offered event/cửa sổ được tính. Scheduled HTTP p99 là **31.424 / 41.106 / 46.436 ms**; tỷ lệ scored HTTP trong 30 ms là **98.6% / 96.0% / 87.733%**. Gate tổng không đạt. Cửa sổ đầu p99 server 21.502 ms nhưng p99 cả scheduled HTTP 31.424 ms cho thấy nhìn riêng server timing sẽ che phần adapter/transport/scheduler. Tải 100 EPS còn tệ hơn trong các đợt soak.

Có một lượt trước sửa tối ưu agent với 50 EPS cho p99 20.850 ms, nhưng hai lượt sau 31.250 và 38.317 ms. Không chọn lượt tốt nhất làm cam kết SLA. Linux Docker/WSL2 cũng chưa đạt trong lượt kiểm tra ngắn. Một lượt Windows trước đó gặp native access violation; nguyên nhân chưa được chứng minh là đã xử lý dứt điểm.

### 2.6 Online gRPC + RAM Bloom + Redis — prototype bổ sung

`telecom_grpc/proto/telecom.proto`, `telecom_grpc/server.py` và `ram_online/core.py` tạo một service độc lập; hiện chưa thay `src/server.py` hoặc được tích hợp vào adapter nhà mạng.

1. gRPC dùng HTTP/2 + protobuf nhị phân; server mTLS kiểm danh tính client. RPC nhận E.164 và trả enum ALLOW/BLOCK cùng trạng thái known/unavailable.
2. Replica giữ Bloom snapshot bất biến trong RAM; tạo snapshot từ Redis ngoài request path. BLAKE2 double hashing sinh các vị trí bit.
3. Redis Lua đọc manifest (version/cardinality) và kiểm tra set nguyên tử. Nếu snapshot khớp và Bloom âm tính, Lua bỏ qua `SISMEMBER`; nếu có thể có hoặc manifest đã đổi thì exact membership.
4. Redis client pool có retry tắt, connect/socket timeout cấu hình không quá 10 ms. Online budget mặc định 25 ms; client ví dụ giữ deadline 30 ms. Quá tải, Redis lỗi, policy thiếu hoặc quá deadline tạo trạng thái unavailable và circuit breaker Fail-Open.

Bloom mathematics:

```text
m = -n ln(p) / (ln 2)^2       số bit tối ưu
k ≈ (m/n) ln 2                số hàm/vị trí băm
P(false positive) ≈ (1 - e^(-kn/m))^k
```

Với capacity `n=1,000,000`, false-positive mục tiêu `p=10^-4`: khoảng 19.17 triệu bit (~2.40 MB thập phân / 2.29 MiB), `k≈13`. Bloom có thể báo “có” nhầm, nhưng Redis exact set quyết định cuối nên không chặn nhầm chỉ vì false positive. Bloom có thể âm tính sai nếu chưa chứa item mới; manifest fencing buộc exact check khi policy đổi.

**Trade-off bắt buộc:** chế độ an toàn hiện vẫn có một lượt Redis Lua cho mọi yêu cầu, kể cả Bloom âm tính, để xác nhận policy không đổi. Bloom giảm công việc exact-set trên cache hit, **không xóa RTT Redis**. Bỏ lượt Redis âm tính chỉ khi thiết kế và chứng minh được publication ACK/fencing barrier cùng giới hạn độ trễ cập nhật policy. Không được gọi một Bloom local stale là consistent.

Lookup prototype đo 1.000 yêu cầu tuần tự, tập deny nhỏ, Python + Redis thật: Linux client cùng bridge với Redis p50 **0.409 ms**, p99 **1.024 ms**, max **1.681 ms**, 1.000 đúng trong ngân sách 5 ms; Windows host tới Docker Redis p50 **1.300 ms**, p99 **6.711 ms**, max **15.720 ms**, 15/1.000 degraded, 0 sai. Đây **không** phải HTTP/gRPC/SIP offered-load benchmark, capacity test hay carrier SLA.

### 2.7 Kafka/near-real-time feature đối với deny policy

`ram_online/streaming.py` là detector thử nghiệm độc lập: Kafka key phải là E.164 canonical caller; mỗi cuộc có `call_id` duy nhất và `setup_ts_ms` nguyên bản. Lua Sorted Set đặt frontier `max(event_ts)`; xóa score `≤ frontier-60s`, chỉ thêm event mới hơn cutoff, dedup bằng `ZADD NX`, đếm `ZCARD`. Ngưỡng demo là `count > threshold` (mặc định 100). Nếu xử lý thành công Redis mới commit offset Kafka, do đó ngữ nghĩa là at-least-once với update idempotent chứ không phải giao dịch exactly-once Kafka+Redis.

Flagging theo call rate là tín hiệu sàng lọc, không phải bằng chứng gian lận. Tự động bật block bị tắt mặc định. CDR đến sau không được dùng timestamp hoàn tất thay setup_ts cho live trigger; backfill lịch sử không nên kích hoạt policy live.

### 2.8 Kết quả chất lượng và giới hạn dữ liệu

Trên main window giả lập 50.000 event, 66 fraud và warm-up 158.787 event, báo cáo v2.2 cho:

| Hành động | TP / FP / FN | Recall | Precision | FPR |
|---|---:|---:|---:|---:|
| Chỉ BLOCK | 36 / 0 / 30 | 54.55% | 100% | 0% trong mẫu |
| BLOCK + FLAG | 57 / 57 / 9 | 86.36% | 50% | 0.1142% |

Đây là event-level simulator, gồm nhiều loại event; 0 FP trên một mẫu không chứng minh FPR production bằng 0. Riêng CALL session, 53 fraud/35.136 cuộc: pre-ring BLOCK recall 49.06%, final BLOCK 52.83%, BLOCK+FLAG 86.79%. Calibration độc lập cho hard BLOCK CALL có 5 FP trên 105.427 negatives; upper 95% FPR 0.01110%, hơi vượt target 0.01%. Vì vậy tài liệu đánh giá ghi rõ `certifies_deployment_fpr=false`; live mặc định shadow. Corpus voice thật chưa đủ.

### 2.9 Người dùng, quy trình và quản trị Telecom

| Vai trò | Việc làm | Quyền/quy trình nên áp dụng |
|---|---|---|
| NOC / network operator | Theo dõi latency, health, fail-open, throughput, alarm; kiểm tra hệ thống call-control | Không tự sửa model threshold; dùng runbook rollback/bypass |
| Fraud analyst | Xem FLAG/evidence, tra lý do, xác nhận false positive, gắn disposition | Quyết định con người có audit; không sửa raw evidence |
| Fraud policy owner | Quản lý allow/deny, threshold, rollout shadow/canary/enforce | Dual approval, expiry, kiểm tra ảnh hưởng thuê bao |
| ML/data scientist | Huấn luyện, calibration, drift, version artifact/feature contract | Không đẩy model trực tiếp live; ký manifest, báo cáo theo cohort |
| SRE/platform | Redis/Kafka/Spark/SQLite storage, backup, dashboards, restore, capacity | On-call, SLO dashboard, failover/recovery diễn tập |
| Privacy/security | PII, retention, HMAC/mTLS/JWT keys, data access | Least privilege, key rotation, audit, masking |

Mọi BLOCK nên có reason code, model/policy version, event ID, state/durability status và audit trail. Giao diện phải phân biệt `scored`, `pending`, `fail-open`, `blocked`, `duplicate` và `late result`; HTTP 200 không đồng nghĩa với scoring thành công.

---

## 3. Hệ thống 2 — Spark/Kafka CALL Analytics

### 3.1 Vai trò trong sản phẩm

Spark/Kafka không nhận quyết định đồng bộ khi SIP đang chờ. Nó tính lại đặc trưng trên lịch sử, gom quan hệ caller–callee/IMEI/cell, xử lý CDR trễ, đối chiếu feature contract với Python và cung cấp dữ liệu/insight cho scoring, model training hoặc policy publisher. Đây là analytical/near-real-time path. Dùng micro-batch cho Call Setup sẽ không đạt SLA 30 ms theo thiết kế.

### 3.2 Pipeline cấu tạo

```text
CSV batch hoặc Kafka CALL JSON
       ▼
Schema/contract validation + chuẩn hoá + dedup/quarantine
       ▼
Deterministic order (ts, event_id) + bounded lateness / watermark
       ▼
┌──────────────┬──────────────┬─────────────┐
│ MSISDN state │ IMEI state   │ Cell state  │  Spark workers / sorted shuffle
│ in + out     │ shared SIMs  │ fresh SIMs  │
└──────┬───────┴──────┬───────┴───────┬─────┘
       └──────── feature join by event_id ────────┘
                            ▼
27 causal features + quarantine + receipt + per-key snapshots
                            ▼
Atomic directory commit/checkpoint → Parquet outputs / downstream consumers
```

`spark/spark_features.py` là CLI batch/stream; `event_contract.py` kiểm tra CALL; `distributed_features.py` tính batch phân tán; `stateful_stream.py` là engine local trên driver; `distributed_stream.py` lưu per-entity snapshots phân tán; `FeatureStore` từ `src/feature_store.py` dùng chung ngữ nghĩa.

### 3.3 Thuật toán phân tán

**Deterministic causality.** Mọi record được xếp theo `(ts,event_id)`. Incoming call được phát tán tới subscriber người nhận, outgoing tới caller. Subscriber stage hợp nhất cả hai luồng để cập nhật first-seen, incoming count và trạng thái. IMEI stage tính thuê bao cùng thiết bị trong 24h. Cell stage tính SIM mới theo cửa sổ 1h và thời gian active. Partial feature vector được join bằng event ID duy nhất; batch engine mặc định thực thi trên worker, không gom cả tập lên driver.

**Watermark theo hợp đồng ứng dụng.** Nếu `max_ts` là thời điểm lớn nhất đã nhận và lateness cho phép `L`, cutoff=`max_ts-L`; record `ts≤cutoff` có thể được emit sau khi cả stage hoàn tất. Event cũ hơn committed watermark/order bị quarantine hoặc làm query fail theo cấu hình. Một Kafka partition bị trễ có thể phá giả định completeness; số partition không tự tạo thứ tự toàn cục.

**Dedup và replay.** Khóa nghiệp vụ là `event_id`, kèm hash payload. Exact duplicate được bỏ qua; cùng ID nhưng payload khác được quarantine/từ chối. Input của retry được so sánh hai chiều bằng `exceptAll`, nên đổi thứ tự hàng không gây sai lệch, nhưng thay nội dung không được xem là retry hợp lệ. Retention mặc định tới 24 giờ cho dedup; buffer giới hạn 100.000 events.

**Commit/recovery.** Mỗi batch ghi input, state, features, dedup, buffer, quarantine và receipt vào thư mục tạm; chỉ rename sang commit directory sau khi các thành phần hoàn tất. Restart nạp receipt/state mới nhất. Checkpoint bị gắn source, engine, schema/feature contract; dùng nhầm local/distributed hoặc v1/v2 bị từ chối thay vì đoán/migrate mất dữ liệu.

### 3.4 Hợp đồng sự kiện và feature semantics

CSV/Kafka phải có schema cố định gồm event id/type, timestamp, caller/callee, cell, IMEI, trunk, network, tuổi SIM, duration, label/fraud type. CALL replay dùng `ts + duration_s` làm completion time. Các cột `start_ts`/`end_ts` ngoài contract bị quarantine; trường thiếu, số vô hạn, duration âm, completion overflow, CSV extra/reordered columns và malformed JSON đều được xử lý rõ thay vì lặng lẽ parse sai.

Tie timestamp được phân xử bằng event ID. Cửa sổ 24h giữ timestamp chính xác, không làm tròn phút. CDR completion được cập nhật theo event time. Exact feature contract quan trọng vì train/serve mismatch làm mô hình trông chính xác trên batch nhưng sai trong live.

### 3.5 Kết quả kiểm chứng Spark

| Phép kiểm | Kết quả đã ghi nhận | Ý nghĩa |
|---|---|---|
| Distributed batch replay | 8.939 events; 27/27 features khớp Python, tolerance 1e-9 | Worker computation nhất quán với reference trên mẫu |
| Persistent distributed state | 8.939 events qua hai process; khớp 27/27 | Kiểm tra restart/lateness/retry |
| Kafka + distributed stream CLI | 8.932 events, 4 partitions, 3 data batches + 2 tail-drain commits; 27/27 khớp | Tích hợp thật ở môi trường thử nghiệm |
| Data quality | duplicate bị bỏ; malformed/late/conflict vào quarantine hoặc bị từ chối | Hành vi lỗi có thể kiểm toán |
| Contract restart kiểm tra bổ sung | Hai Spark process; 5 event accepted, 3 quarantine; extra CSV column và foreign checkpoint bị từ chối | Bảo vệ biên schema và resume |

Các kiểm chứng chạy trên single-host test container/Spark, Kafka 3.9.0 và PySpark 4.2.0 theo báo cáo. **Không chứng minh multi-host throughput, HA, vận hành object storage, hot-key capacity hay online 30 ms.**

### 3.6 Điểm mạnh, điểm yếu, người sử dụng

**Điểm mạnh:** feature logic dùng chung giữa huấn luyện và tính toán; xử lý liên thực thể caller/IMEI/cell; checkpoint isolation; retry có hash; record xấu có lý do; batch phân tán tránh `collect/toPandas`; các engine local và distributed có ranh giới rõ.

**Điểm yếu:** distributed streaming ghi full snapshot Parquet mỗi batch (đọc/shuffle/ghi state), không dùng Spark incremental state-store; shared POSIX path phải giống nhau giữa worker/driver; 1 hot entity vẫn có thể quá lớn; commit protocol chưa thay thế HDFS/object-store atomic semantics/HA; Kafka+filesystem/Redis không phải một transaction chung. PII như MSISDN/IMEI cần policy masking/retention/ACL độc lập.

| Vai trò | Dùng hệ thống ra sao |
|---|---|
| Data engineer | Provision topic, chọn engine/checkpoint, cấu hình partitions/lateness/dedup, theo dõi receipts và quarantine |
| Data scientist | Dùng output 27 feature đúng version để huấn luyện và kiểm tra drift |
| Fraud analyst | Xem aggregate/flag và dữ liệu đã lọc; không dùng raw Kafka trực tiếp |
| Platform/SRE | Kafka lag, Spark batch duration, state sizes, checkpoint storage, restart và DLQ/quarantine |
| Data governance | Hợp đồng dữ liệu, retention, masking, kiểm soát truy cập và replay approvals |

---

## 4. Hệ thống 3 — Edu DSS Microservices

### 4.1 Product concept và trạng thái thực

Edu DSS hướng tới dịch vụ hỗ trợ dạy/học: giảng viên xếp hàng huấn luyện và chuẩn bị giải thích bài giảng; sinh viên đọc nội dung đã cache và gửi dữ liệu feature tối thiểu để xin gợi ý lộ trình; Admin xem sức khỏe queue/graph. Mã nằm dưới `edu_dss/` được dựng ở lượt đóng gói vừa qua.

**Ranh giới quan trọng:** không tìm thấy pipeline Edu DSS gốc trong workspace. Vì vậy đây là **service scaffold có adapter contract**, không phải chuyển đổi hoàn tất của model, diagnosis engine, kho khóa học hay knowledge graph đã có. AutoML, CMS explanation, advice và graph chưa trả kết quả domain thật nếu adapter chưa được cấu hình.

### 4.2 Microservice topology và triển khai

| Service | Trách nhiệm | Mạng/quyền |
|---|---|---|
| `nginx-gateway` | HTTPS 443; redirect 80→443; route `/api` và UI; WebSocket upgrade Streamlit | Chỉ service publish host port 80/443 |
| `streamlit-ui` | Stateless view, forms, HTTPX AsyncClient pool, task polling | Không tính toán model; gọi backend qua Docker network |
| `fastapi-backend` | JWT verification, role authorization, validation, cache read, enqueue/status | Backend/data networks nội bộ; Redis không publish port |
| `redis-cache` | Cache bài giảng, Celery broker/result, global rate counter | ACL secret, AOF volume; network `data` internal |
| `celery-worker` | AutoML/CMS/advice calls và tác vụ dài | Internal data + worker-only egress tới adapter; không mở port |

Các Docker image chạy non-root/read-only với tmpfs cho scratch. Compose Redis là một node AOF đơn lẻ, chỉ dùng integration/small deploy; không phải HA, backup hay managed production Redis.

### 4.3 FastAPI routes và RBAC

JWT hiện xác minh HS256, issuer, expiry, subject, và role. Chức năng role thực thi ở backend; chọn “vai trò hiển thị” trong UI không cấp quyền.

| Role | API | Kết quả |
|---|---|---|
| Instructor | `POST /api/instructor/automl` | 202 + task ID; Celery gọi AutoML adapter, sau đó lưu leaderboard theo subject/model |
| Instructor | `GET /api/instructor/leaderboard/{model_ref}` | Đọc leaderboard cache theo caller/tenant key |
| Instructor | `POST /api/instructor/cms/precompute` | Chunk nội dung, xếp tác vụ, lưu explanations vào Redis theo course/lecture |
| Student | `GET /api/student/explanations/{course}/{lecture}` | Đọc cache-first; 404 khi chưa precompute |
| Student | `POST /api/student/advice` | Kiểm tra pseudonymous ID, nén feature JSON rồi xếp vào queue |
| Student/Instructor/Admin | `GET /api/tasks/{task_id}` | State/progress/result; owner check giới hạn task riêng, Admin giám sát |
| Admin | `GET /api/admin/queue` | Active/reserved worker count |
| Admin | `GET /api/admin/knowledge-graph` | Hiện trả `adapter_required`; graph chưa kết nối |

Task ownership được ghi Redis trước khi publish task để tránh race khi worker chạy rất nhanh. Result/progress đọc ngoài event loop bằng worker thread. Model request bị validate strict, từ chối field thừa; input tư vấn chỉ gồm student ref, mục tiêu và các số feature, sau đó nén zlib.

### 4.4 Celery, cache, polling và LLM rate limit

Tác vụ nặng trả 202 ngay, Celery worker thực hiện bên ngoài HTTP request. Redis `task_acks_late`, `reject_on_worker_lost`, `prefetch=1`, time limit và result expiry đặt ranh giới vận hành. Streamlit dùng `@st.cache_resource` tạo một HTTPX AsyncClient trên một event loop nền cố định; các lời gọi đi qua cùng connection pool. `st.fragment(run_every=2)` polling trạng thái tác vụ, giới hạn chờ ngắn để UI có thể cập nhật.

LLM có hai limiter: Celery per-worker rate và Redis Lua fixed-minute counter dùng chung mọi replicas. Với nhiều worker, Redis counter mới là giới hạn toàn cụm; ngưỡng phải căn theo quota/key/vendor và có dashboard, dead-letter, retry policy. Mặc định code 60 request/minute toàn cụm và 5/minute mỗi worker là cấu hình minh họa, không phải quota nhà cung cấp.

### 4.5 Adapter contracts và tích hợp còn phải làm

Các biến `EDU_AUTOML_ADAPTER`, `EDU_EXPLANATION_ADAPTER`, `EDU_ADVICE_ADAPTER` cần URL nội bộ HTTPS và adapter phải kiểm tra bearer credential từ Docker secret. AutoML adapter nhận dataset reference/target/metric, trả model ref/leaderboard; explanation adapter nhận course/lecture/chunks; advice adapter nhận feature nén đã giải mã và trả đề xuất có cấu trúc. Secrets phải rotate; không được đưa token hoặc nội dung học tập vào log.

Chưa có: user registry/IdP JWKS/OIDC; database migration; phân quyền giảng viên theo lớp/môn; sinh viên chỉ đọc course đang đăng ký; graph DB; kho file/dataset; queue DLQ; audit sink; notification; content retention; recommendation quality/evaluation; adapter egress allowlist đầy đủ. Cần triển khai authorization theo tài nguyên trước khi dùng dữ liệu học tập thật. Local bootstrap cấp secret/cert self-signed chỉ để chạy thử.

### 4.6 Người dùng và quy trình

| Người dùng | Luồng làm việc |
|---|---|
| Giảng viên | Đăng nhập qua IdP → chọn dataset/target/metric → xếp AutoML → theo dõi task → xem leaderboard; tải bài giảng để chunk và pre-generate giải thích |
| Sinh viên | Mở course/lecture được cấp quyền → đọc cache; nhập mục tiêu và feature học tập tối thiểu → nhận task ID → xem tiến độ/kết quả tư vấn |
| Admin/IT | Quan sát queue/worker/cache/graph; quản lý user role ở IdP; rotate secret, kiểm log/audit, xử lý retry/DLQ |
| Product owner/Academic governance | Phê duyệt nội dung/logic khuyến nghị; giám sát bias, quyền riêng tư, opt-out và giới hạn vai trò AI |

Giảng viên không nên xem hồ sơ cá nhân ngoài lớp mình phụ trách; Admin có quyền vận hành không đồng nghĩa được truy cập mọi nội dung học tập. Những luật này cần tích hợp IAM và database policy thật, không thể giải quyết chỉ bằng ba tên role.

---

## 5. Cách kể chuyện sản phẩm khi gặp khách hàng

### 5.1 Giá trị kinh doanh

1. **Ngăn chặn sớm và giải thích được:** trả về BLOCK/FLAG/ALLOW, reason code và evidence thay vì black-box score.
2. **Quyết định có kiểm soát rủi ro:** hard block đòi hỏi hành vi bổ trợ; shadow/canary trước enforce; fail-open tránh giữ cuộc gọi khi model hỏng.
3. **Học từ lịch sử quy mô lớn:** Spark/Kafka tính trạng thái subscriber/device/cell, replay và dữ liệu sạch hơn cho feature/model.
4. **Tái sử dụng nền tảng dữ liệu an toàn:** Edu DSS dùng pattern tương tự cho async jobs/cache/RBAC nhưng vẫn tách workload và dữ liệu.

### 5.2 Demo đề xuất 8 phút

1. Cho xem kiến trúc và nói rõ online vs analytical path.
2. Trình một cuộc gọi giả lập: score, feature nổi bật, lý do FLAG/BLOCK; nhấn mạnh không thực thi production.
3. Cho xem shadow dashboard/report, tỷ lệ miss/false action và SLA fail-open.
4. Replay một CDR duplicate/malformed trong Spark; chỉ ra dedup/quarantine và checkpoint restart.
5. Mở Edu DSS với Instructor queue, Student cache hit/task polling, Admin queue; giải thích adapter stub cần backend khách hàng.
6. Kết bằng pilot scope, KPI, dữ liệu cần khách hàng cấp và những gì chưa cam kết.

Không demo chặn thuê bao thật trước khi có văn bản phê duyệt, rollback, allowlist, traffic shadow và nghiệm thu FPR. Không trình số liệu synthetic như kết quả khách hàng.

### 5.3 Gói pilot và KPI nghiệm thu

| Workstream | Đầu vào khách hàng | KPI/exit gate |
|---|---|---|
| Telecom shadow | SIP event sample, CDR, label/complaint dispositions, routing metadata, test trunk | p50/p95/p99 end-to-end tại adapter; scored-success trên offered; fail-open; p99 commit; call setup delay; recall/FPR theo cohort |
| Policy trial | allowlist, fraud team reviewer, appeals process | hard-block FPR upper confidence bound ≤ mục tiêu; review latency; rollback dưới ngưỡng thời gian; không unexpected enforce |
| Spark scale | Kafka schemas, ordering/keying, retention, storage | throughput and backlog drain; state bytes/key; batch duration; checkpoint restore; quarantine rate; replay determinism |
| Edu adapter proof | SSO/JWKS, SIS/LMS, AutoML/CMS/LLM/graph endpoint | role/resource authorization; recommendation quality and bias; API p95; cache hit; queue lag; cost/request; data deletion |

SLA agreement phải ghi start/end timestamp, percentile, offered-load, concurrency/burst, measurement point, error budget, fail-open accounting, topology/HA, exclusions và chế tài. Không dùng “average <30 ms” thay p99 end-to-end.

---

## 6. Bản đồ rủi ro xuyên hệ thống

| Rủi ro | Hệ quả | Cách giảm thiểu/điều kiện go-live |
|---|---|---|
| HTTP API p99 >30 ms | Call setup delay hoặc fail-open tăng | Đo ở IMS adapter; profile phase; xác định storage/network tail; gRPC canary; deadline gốc; p99 gate dài |
| Fail-open khi Redis/model down | Fraud vẫn lọt qua | Bounded outage budget, carrier fallback policy, alarm, forensic journal; không tính là successful scoring |
| Bloom snapshot stale | Sai ALLOW nếu bỏ version fence | Lua manifest guard hiện tại; hoặc chứng minh publication barrier, lag bound và replay |
| Synthetic-data bias | Thật giả khác traffic/segment; FPR không đại diện | Dữ liệu có nhãn đại diện, time/group split, calibration theo cohort, human review |
| SQLite single writer / FULL fsync | Tail latency và throughput bị giới hạn | Benchmark storage mục tiêu, workload partitions/state ownership, transaction/durability design trước scale |
| Full Spark snapshots | I/O/shuffle tăng mỗi micro-batch, recovery lâu | Benchmark state growth, incremental state store, filesystem commit protocol/shared storage HA |
| Kafka event ordering/late partition | Cửa sổ thiếu hoặc đảo thứ tự | Key by caller, bounded lateness, lag monitors, quarantine, replay; no global watermark assumption |
| Edu adapters absent | UI/API chạy nhưng nghiệp vụ không trả kết quả thật | Build/secure domain services and acceptance tests before pilot |
| IAM/resource authorization | Lộ điểm/khoá học/model qua tenant | OIDC/JWKS, membership checks per course/data/model, least privilege, audit |
| Redis single node | Cache/queue outage or data loss | Managed HA, backup/restore drills, TLS/ACL, memory sizing and eviction review |
| Sensitive telecom/education data | Privacy/legal exposure | Data minimization, pseudonymization, retention, encryption, access logging, DPIA/compliance review |

---

## 7. Lộ trình phát triển có thứ tự

### P0 — Chốt hợp đồng và đo đường thật (0–4 tuần)

- Cùng nhà mạng xác nhận deadline bắt đầu/kết thúc, offered EPS/burst, concurrency, SLA percentile và fail-open policy.
- Deploy gRPC shadow sidecar cạnh adapter; không bật enforce; instrument timestamps từng phase.
- Thu traffic synthetic + sanitized representative; time/group splits; đánh giá hard-block false positives và subgroup fairness.
- Edu: xác định vị trí pipeline, IdP, SIS/LMS, course authorization, graph, LLM vendor, retention và owner cho từng adapter.
- Spark: chốt Kafka key/schema, replay horizon, partition count, state/retention và storage.

### P1 — Tăng độ tin cậy và bảo vệ dữ liệu (1–2 tháng)

- MTLS/service identity, secret rotation, policy manifest changelog, Redis HA/restore, SLO metrics, alerting và audit sink.
- Split policy, fraud, call evidence, analyst disposition; support block expiry/appeal/undo và dual approval.
- Spark checkpoint/commit shared-storage protocol, DLQ, idempotent publisher, rebalance/recovery fencing.
- Edu OIDC/JWKS, course-level RBAC/ABAC, durable task ownership, DLQ, adapter auth, migrations, egress policy.

### P2 — Nghiệm thu hiệu năng và mô hình (2–4 tháng)

- Repeatable load test tại topology mục tiêu, soak qua checkpoint/rotation, Redis outage/packet loss/CPU contention; gate p99 and offered-success, not averages.
- Nếu fsync tail là bottleneck, thiết kế tách durable audit/action outbox với call response protocol được nhà mạng chấp nhận; không hạ SQLite FULL một cách mù quáng.
- Test gRPC p50/p95/p99/error/circuit under realistic load, benchmark exact Redis membership and sharding; local microbench chỉ chỉ đường, không signoff.
- Mở rộng Spark state incrementally, test shared storage/object storage committer, hot-key skew và multi-host failures.
- Edu evaluate advice factuality, learning outcome, bias, privacy leakage, human override and cost/latency per provider.

### P3 — Controlled production rollout (sau khi gates đạt)

- Internal employee/test-number canary → small opt-in cohort → low-risk traffic → gradual ramp with automated rollback.
- Tách control plane (policy/model/config) khỏi data plane (per-call scoring); versioning, blue/green, circuit and rollback rehearsals.
- SRE on-call, fraud operations, academic support, security/privacy owner; tabletop drills và post-incident review.

---

## 8. Kết luận kiểu hội đồng đầu tư

Đây là một **portfolio hướng sản phẩm có nền kỹ thuật đáng kể**, mạnh nhất ở feature engineering có kiểm chứng, model explainability/policy gate, resilience và Spark recovery semantics. Giá trị thương mại là cách kết hợp thời gian thực, phân tích lịch sử và quy trình con người để quyết định có trách nhiệm.

Nhưng maturity không đồng đều: Fraud Shield đã chạy được trong môi trường kiểm soát nhưng SLA và mô hình chưa được chứng nhận trên carrier traffic; Spark/Kafka đã vượt proof-of-concept chức năng nhưng còn full snapshots/shared-storage giới hạn scale; Edu DSS mới là microservice scaffold chờ tích hợp domain. Đề xuất bán **pilot có điều kiện và các milestone đo được**, không bán như giải pháp đã được chứng nhận 30 ms/production-ready. Sau khi đạt các gate tải, FPR, HA, IAM và dữ liệu thật, mới nâng thành cam kết SLA và hợp đồng production.

---

## Phụ lục A — Những con số và nguồn kiểm chứng

- Telecom model/scoring/synthetic quality: `docs/VALIDATION_V22.md`, `runtime/artifacts_v22/`, `runtime/verification_20261025/`.
- Telecom SLA gate: `docs/SLA_TARGET_20261025.md`, `data/http_sla_target_shipped_20261025.json`, `runtime/sla_target_20261025/pytest-shipped.xml`.
- Spark feature/recovery: `spark/VALIDATION.md`, `spark/README.md`, `data/spark_contract_verification_20261022.json`.
- RAM Bloom/Redis lookup: `ram_online/README.md`, `data/ram_online_lookup_strict_20261026.json`, `data/ram_online_lookup_linux_20261026.json`, `runtime/ram_online_junit_verified_20261026.xml`.
- gRPC and Edu DSS scaffold: `telecom_grpc/README.md`, `edu_dss/README.md` and their source/Compose files. The latter two have only source/compose syntax checks recorded here, not runtime load results.

## Phụ lục B — Thuật ngữ

| Thuật ngữ | Nghĩa |
|---|---|
| Call Setup / pre-ring | Giai đoạn mạng đang thiết lập cuộc gọi, trước khi thiết bị nhận đổ chuông |
| CDR | Call Detail Record, bản ghi cuộc gọi thường có sau khi cuộc gọi hoàn tất |
| MSISDN / IMEI | Định danh số thuê bao / thiết bị |
| Bloom Filter | Cấu trúc bit xác suất: không có false negative với tập đã nạp, có thể false positive |
| FPR / recall / precision | Tỷ lệ dương tính giả / tỷ lệ bắt fraud / tỷ lệ cảnh báo đúng trong cảnh báo |
| p99 | 99% mẫu có latency không cao hơn giá trị đó; phải nêu đúng mẫu số và điểm đo |
| Watermark | Mốc event-time cho biết pipeline đã đủ dữ liệu theo giới hạn lateness đã chọn |
| Idempotency | Retry cùng business ID không tạo hành động/ghi kết quả lặp |
| Fail-open | Khi quyết định không sẵn sàng, cho phép lưu lượng đi tiếp và báo rõ trạng thái suy giảm |
| Shadow mode | Ghi nhận quyết định giả định nhưng không thực thi tác động thật |
| RBAC / ABAC | Phân quyền theo vai trò / thuộc tính như tổ chức, lớp học hoặc tài nguyên |

