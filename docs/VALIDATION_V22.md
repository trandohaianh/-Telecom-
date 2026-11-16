# Kiểm chứng Telecom Fraud Shield v2.2 — cập nhật 2026-10-25

Cập nhật mới nhất: [426 tests + 50 subtests và đợt tối ưu SLA/soak](SLA_TARGET_20261025.md). Các con số 407 tests và HTTP bên dưới thuộc snapshot trước đợt tối ưu này.

Bản này ghi kết quả đo cục bộ và giới hạn đã biết. Không dùng số liệu v2.1 làm kết quả cho feature contract mới. Dataset, models và checkpoint cũ được giữ nguyên; bundle mới ở `runtime/artifacts_v22/`.

## Regression và artifact

**407 tests và 50 subtests passed** trong 59,34 giây; không fail/skip. Bao gồm recovery journal/CDR, atomic commit/restart, deadline, evidence ACK, HMAC, readiness, voice và Spark. [JUnit bản cuối](../runtime/artifacts_v22/logs/pytest-bugfix-20261025-final-r4.xml), [chi tiết các lỗi đã sửa và source hashes](BUGFIX_20261025.md).

Kết quả AST parse và hashes các Python files được ghi trong [manifest kiểm chứng](../runtime/verification_20261025/manifest.json).

Cả năm bước build dataset, train, evaluate, drift baseline và shadow đã hoàn tất với exit code 0. Manifest và checksum được xác minh; version `tfd-v2.2`, feature contract `causal-v2-exact-24h-completion-order`.

Nguồn: [rebuild report](../runtime/artifacts_v22/rebuild_report.json), [model manifest](../runtime/artifacts_v22/models/manifest.json), [benchmark](../runtime/artifacts_v22/data/benchmark_results.json), [training report](../runtime/artifacts_v22/models/training_report.json).

## Kết quả trên dữ liệu giả lập

Main window có 50.000 events, 66 fraud và 158.787 warm-up events:

| Metric theo event | BLOCK | BLOCK+FLAG |
|---|---:|---:|
| TP / FP / FN | 36 / 0 / 30 | 57 / 57 / 9 |
| Recall | 54,55% | 86,36% |
| Precision | 100% | 50% |
| FPR | 0% | 0,1142% |

Sau sửa lượng tử float32 của Isolation Forest, [main window 50k được chạy lại](../runtime/verification_20261022/evaluation_50k.json): 0 raw decisions thay đổi, giữ các số liệu main window trên. Nghiên cứu nhiều cửa sổ sau đây là lịch sử trước bản sửa, chưa chạy lại toàn bộ.

Qua 10 cửa sổ × 50.000 events, recall BLOCK+FLAG trung bình là 82,74%, thấp nhất 75%. BLOCK có 4 FP / 499.432 legitimate events: FPR 0,0008%, CI 95% 0,00031–0,00206%. BLOCK+FLAG FPR là 0,0889%.

Event metrics gồm AUDIO chunks có tương quan. Riêng 35.136 CALL sessions, 53 fraud: pre-ring BLOCK recall 49,06%; final BLOCK 52,83%; final BLOCK+FLAG 86,79%. Zero CALL FP trên 35.083 negatives vẫn có upper FPR 95% 0,01095%.

Calibration dùng cửa sổ độc lập seed 33:

| Nhánh | FP / negatives | Upper FPR 95% | Target | Đạt bound trong mẫu giả lập |
|---|---:|---:|---:|---|
| CALL BLOCK | 5 / 105.427 | 0,01110% | 0,01% | Không |
| SMS BLOCK | 0 / 51.038 | 0,00753% | 0,01% | Có |
| AUDIO BLOCK | 0 / 2.965 | 0,12940% | 0,01% | Không |
| Anomaly trunk FLAG | 0 / 342 | 1,11080% | 0,5% | Không |

Khoảng tin cậy dựa trên giả định mẫu độc lập; simulator và chunks không chứng minh giả định này cho traffic thật. Báo cáo có `certifies_deployment_fpr=false`. Live vẫn mặc định shadow.

## Spark/Kafka

- Bounded distributed replay: 8.939 events, 27/27 features khớp Python tại atol/rtol 1e-9.
- Persistent distributed state: 8.939 events qua hai Python/Spark processes độc lập; kiểm tra restart, lateness tail, retry đổi thứ tự, conflict và late/malformed quarantine.
- Kafka actual CLI distributed-stream: 8.932 events, bốn partitions, ba data batches và hai tail-drain commits, ba process starts; 27/27 features khớp. Một duplicate bị bỏ qua, một malformed record được quarantine.
- Local-stream cũng qua Kafka/restart trên 8.932 events và bốn partitions.

Nguồn và lệnh chạy lại: [spark/VALIDATION.md](../spark/VALIDATION.md). Distributed stream dùng full Parquet snapshots mỗi batch; multi-host cần shared POSIX filesystem cùng đường dẫn. Chưa chứng minh incremental state-store, HA hay throughput production.

## HTTP và độ bền state

Bảng dưới là lượt lịch sử trước đợt sửa lỗi cuối, dùng group commit WAL/FULL, công bố sau COMMIT, cache HMAC và event loop QPC trên Windows. Mỗi mức tải chạy 10 giây, concurrency 32, một owner và shadow mode. Linux Docker Desktop/WSL2 giới hạn 2 CPU/2 GiB, SQLite trên named volume đĩa ext4. Lượt Linux nguồn cuối và Windows soak được mô tả riêng trong [báo cáo 2026-10-25](BUGFIX_20261025.md).

| Environment | EPS | Offered | Shed | Fail-open | Scored runtime <=30 ms | Scored scheduled HTTP <=30 ms | HTTP RTT p99 ms | Scheduled p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Windows | 10 | 100 | 0 | 0 | 100% | 47% | 31.533 | 43.012 |
| Windows | 50 | 500 | 0 | 21 | 95.8% | 58.8% | 47.931 | 57.671 |
| Windows | 100 | 1000 | 0 | 746 | 25.1% | 18.7% | 136.900 | 141.850 |
| Linux Docker | 10 | 100 | 0 | 83 | 17% | 0% | 62.817 | 64.714 |
| Linux Docker | 50 | 500 | 0 | 363 | 23.8% | 13.2% | 523.171 | 566.128 |
| Linux Docker | 100 | 1000 | 528 | 86 | 7.7% | 0% | 2434.230 | 2514.156 |

**Chưa đạt SLA HTTP p99 30 ms.** Tỷ lệ thành công lấy tất cả offered events làm mẫu số; percentile chỉ gồm attempts hoàn tất. PENDING/fail-open/shed không phải scoring thành công. Cả tám smoke checks/restart đều pass và evidence hợp lệ trên hai môi trường; tài nguyên Docker riêng của test đã dọn.

Nguồn, contract retry/durability, giới hạn phép đo và hướng tối ưu: [SLA_30MS.md](SLA_30MS.md). Một lượt Windows trước đó gặp native access violation; log được giữ và nguyên nhân chưa xác định dứt điểm. Lượt cuối pass chưa chứng minh native crash đã được chữa.

## WAV và ASR

Đã kiểm thử decoder PCM WAV 8/16/24/32 bit, mono/stereo, resampling, VAD, acoustic features, silence/clipping, giới hạn input, API HMAC, bounded queue, idempotency, cache theo mode/artifact và trainer chia dữ liệu theo thời gian/nhóm.

Default là OBSERVE/NONE. Proxy âm học không được đưa vào model AUDIO giả lập. ASR dùng adapter local-only và model có sẵn; chưa cung cấp weights ASR hay corpus ghi âm thật có nhãn, nên chưa đo chất lượng tiếng Việt hoặc độ chính xác phát hiện deepfake. Trainer/model loader và calibration đã triển khai, nhưng chưa có model WAV thật được chứng nhận.

Calibration WAV gộp nhóm liên kết qua call/speaker/campaign, từ chối mixed-label groups; chỉ cho phép action khi đủ bound và mỗi nhóm độc lập có một record. Tính độc lập của metadata vẫn cần kiểm định ngoài hệ thống.

## Việc cần môi trường nhà mạng

Thu traffic có nhãn đại diện cả ALLOW, kiểm định theo nhóm/cuộc và thời gian, bổ sung corpus WAV thật, kiểm thử tải dài/HA và tích hợp adapter mạng cùng OTP/SSO. Giới hạn hiện tại là SQLite một owner và Spark full snapshots; capacity/retention/migration cần thiết kế theo môi trường triển khai.

Hướng dẫn chạy và quy trình dữ liệu: [DEVELOPMENT_V22.md](DEVELOPMENT_V22.md).
