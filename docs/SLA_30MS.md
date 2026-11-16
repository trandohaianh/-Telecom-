# SLA 30 ms — kiểm chứng ngày 2026-10-22

Cập nhật tiếp: [tối ưu agent, 426 tests + 50 subtests và soak trên mã cuối](SLA_TARGET_20261025.md). Kết quả lặp lại chưa đạt SLA ổn định; các bảng dưới được giữ làm lịch sử.

**Chưa đạt SLA HTTP đầu cuối p99 <= 30 ms trên môi trường đã đo.** Các bảng 10 giây bên dưới là lịch sử trước đợt sửa lỗi cuối. Bản 2026-10-25 có **407 tests và 50 subtests pass**; xem [báo cáo sửa lỗi, HTTP và giới hạn kiểm chứng](BUGFIX_20261025.md). Không dùng lượt tải ngắn để chứng nhận capacity hoặc enforce trên traffic thật.

## Các tối ưu đã áp dụng

- Admission vào queue chỉ dùng RAM, không chờ SQLite hoặc executor trên event loop. Queue và tổng số jobs đang chạy có giới hạn; timeout/cancel của HTTP không giải phóng chỗ trước khi job thật kết thúc.
- Owner lấy tối đa 16 jobs đã sẵn sàng, vector hóa các verdict liền nhau và reserve + finish trong một transaction SQLite WAL/FULL. Mặc định không đợi gom batch. CDR là ranh giới xử lý; retry cùng ID/nội dung giữ tính idempotent.
- Kết quả chỉ được công bố sau COMMIT; callback chạy sau khi nhả khóa model/database. Cache LRU 4.096 kết quả đã commit kiểm fingerprint đầy đủ. Duplicate không phát lại hành động mạng.
- Evidence JSONL flush/fsync trước khi ACK; ACK chính xác được gộp vào transaction owner hoặc flush khi idle/đóng. Không đổi sang SQLite NORMAL/OFF hay dùng tmpfs để làm số đo đẹp hơn.
- HMAC key được cache trong tiến trình, không đọc file/khóa trên mỗi request đã warm. Đổi file key phải gọi `security.reload_network_key()` trong đúng tiến trình hoặc restart. Reload không hợp lệ giữ cache cũ và báo lỗi; không có endpoint xoay khóa công khai.
- Windows dùng QPC cho clock của event loop trong launcher/verifier; không đổi timer toàn hệ điều hành. `FRAUD_PRECISE_EVENT_LOOP=0` tắt lựa chọn này. Yêu cầu Python >= 3.11 và Uvicorn >= 0.47.0.
- Client dùng `security.network_timestamp()` để giữ timestamp khác nhau khi retry nhanh trên đồng hồ Windows thô. Giá trị được định dạng từ integer nanoseconds, không chuyển qua float trước khi ký. Server vẫn kiểm skew và từ chối chữ ký đã dùng.
- Readiness không lấy khóa SQLite/model; trả 503 nếu owner bị treo quá ngưỡng, bao gồm nhánh chấm bù. Khi COMMIT lỗi, owner dừng để replay dữ liệu đã commit, không checkpoint RAM còn suy đoán.

## Hợp đồng với adapter nhà mạng

`durable=true` chỉ được trả sau khi kết quả có trong journal đã commit. `PENDING`/`durable=false` là job trong RAM, có thể mất khi tiến trình chết. Adapter phải retry **cùng event ID và payload**, với timestamp/chữ ký mới, cho đến kết quả bền vững; không tạo ID mới cho mỗi lần retry. Quá tải hoặc lỗi owner có `retryable=true`. Cùng ID nhưng đổi nội dung trả 409.

Duplicate là kết quả tham khảo và có `recommended_action=NONE`; không khởi động lại một lệnh BLOCK cũ. Kết quả chấm/commit muộn giữ model decision để hậu kiểm, còn phản hồi đã quá deadline phải fail-open. Bản ghi `model_result_only` không phải bằng chứng nhà mạng đã nhận hoặc thực thi hành động. Khi restart không xác nhận được việc giao kết quả, hệ thống chỉ tạo yêu cầu chuyên viên xem lại.

Nhà mạng giữ deadline gốc của cuộc gọi. Retry sau khi mất RAM hoặc sau quá tải không được tạo một deadline cuộc gọi mới. WAV/ASR chạy trên pipeline riêng sau khi có âm thanh; không đặt việc nhận đủ đoạn ghi âm và phiên âm vào budget pre-ring 30 ms.

## Cách đọc phép đo

`scored_before_deadline_pct` đếm verdict được chấm và hoàn thành trong budget runtime, sau COMMIT; thời gian runtime bắt đầu khi submit sau đọc body/kiểm request. Đây chưa phải toàn bộ thời gian HTTP.

`scored_within_http_30ms_pct` còn yêu cầu hoàn tất HTTP trong 30 ms tính từ lịch arrival của load generator. Mẫu số là **tất cả offered events**. PENDING, fail-open, HTTP/client error, thiếu bằng chứng scoring và client shedding đều không được tính thành công.

Các percentile HTTP chỉ có dữ liệu từ attempts đã hoàn tất; xem cùng tỷ lệ thành công trên offered và số shed. Trường commit/queue của phản hồi pending có thể bằng 0 vì chưa có kết quả, nên percentile gồm chúng không phải phân bố fsync độc lập. Muốn profile storage dùng phép đo riêng.

## Kết quả lịch sử trước đợt sửa lỗi cuối

Mỗi mức tải chạy 10 giây, concurrency 32, theo thứ tự 10/50/100 EPS trên cùng instance. Có model thật trong bundle giả lập, HMAC, SQLite WAL/FULL, một owner, evidence writer và shadow mode. Không đổi enforcement để benchmark. Những runs ngắn/fixed-order này chưa chứng nhận capacity, không phải so sánh nhân quả giữa hệ điều hành hoặc riêng từng tối ưu.

| Environment | EPS | Offered | Shed | Fail-open | Scored runtime <=30 ms | Scored scheduled HTTP <=30 ms | HTTP RTT p99 ms | Scheduled p99 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Windows | 10 | 100 | 0 | 0 | 100% | 47% | 31.533 | 43.012 |
| Windows | 50 | 500 | 0 | 21 | 95.8% | 58.8% | 47.931 | 57.671 |
| Windows | 100 | 1000 | 0 | 746 | 25.1% | 18.7% | 136.900 | 141.850 |
| Linux Docker | 10 | 100 | 0 | 83 | 17% | 0% | 62.817 | 64.714 |
| Linux Docker | 50 | 500 | 0 | 363 | 23.8% | 13.2% | 523.171 | 566.128 |
| Linux Docker | 100 | 1000 | 528 | 86 | 7.7% | 0% | 2434.230 | 2514.156 |

Windows: Python 3.12, `PreciseProactorEventLoop`, state trong TEMP trên đĩa host. Linux: Python 3.11.16 trong Docker Desktop/WSL2, 2 CPU/2 GiB, named volume đĩa ext4, source/static/models chỉ đọc, không publish port, network none. Không dùng tmpfs cho SQLite. Tất cả smoke checks đều pass, evidence hợp lệ, không unexpected enforce; chỉ container/volume của phép đo đã được dọn.

Nguồn: [Windows bản cuối](../data/http_windows_sla_final_v22.json), [Linux bản cuối](../data/http_linux_sla_final_v22.json), [regression JUnit](../runtime/artifacts_v22/logs/pytest-sla-final-v2.xml), [môi trường và hashes](../runtime/artifacts_v22/logs/sla-final-environment.json).

Các lượt trước có biến động đáng kể; không chọn riêng lượt tốt nhất làm cam kết SLA. [Windows trước lượt cuối](../data/http_windows_precise_cached_v22.json) và [Linux atomic trước lượt cuối](../data/http_linux_atomic_v22.json) được giữ làm lịch sử. Các con số server cũ trước thay đổi atomic chưa bao gồm final commit nên không so trực tiếp với latency sau COMMIT.

## Chẩn đoán và lỗi còn mở

Profile Linux độc lập trước thay đổi atomic ghi model p50 2,198 ms/p99 9,139 ms; từng commit reserve và finish có p50 khoảng 22,5 ms. Group commit đã bỏ hai lần commit nối tiếp cho mỗi event, nhưng storage, scheduling và transport vẫn chi phối tail. [IO profile lịch sử](../data/http_linux_io_profile_v22.json) chỉ mô tả cấu hình được ghi trong report đó.

Route chẩn đoán không dùng model/SQLite cũng có tail tại bước nhận body. [HTTP/TCP diagnostic](../data/http_transport_tcp_v22.json) xác nhận cả client và server đã có TCP_NODELAY=1; kết quả giữa các lượt biến động nên chưa chứng minh bật lại NODELAY hoặc gom header/body là lời giải. Không sửa package HTTP private hay bật tùy chọn TCP toàn hệ thống từ giả thuyết này.

Một lượt Windows đã gặp native access violation trong khi watchdog định kỳ dump stack. [Log lỗi đã giữ](../runtime/artifacts_v22/logs/http-windows-sla-failed.log). Verifier đã bỏ watchdog định kỳ khỏi timed run, giữ fatal diagnostics và sửa shutdown để lỗi pipe không che mã thoát server. Lượt cuối hoàn tất, nhưng **chưa xác định dứt điểm nguyên nhân native crash**; không coi một lượt pass là đã chữa lỗi native.

## Thứ tự phát triển tiếp

1. Đo trên máy triển khai và adapter gần IMS/SIP thật, tách riêng thời gian receive/auth, queue, feature/model, COMMIT, gửi response và scheduler delay. Chạy steady-state dài, burst và IO stalls; dùng offered events làm mẫu số.
2. Đo storage có fsync tail thấp và tài nguyên CPU ổn định. Giữ WAL/FULL và acknowledgement sau commit; benchmark p99 đầu cuối trước khi chọn capacity hoặc tăng batch wait. Warm model/connections trước pha đo.
3. Khi đã rõ bottleneck, thử transport/event loop hoặc pipeline giao tiếp phù hợp adapter bằng phép đo đối chứng lặp lại. Nếu cần nhiều owner, thiết kế routing/state nhất quán cho MSISDN, IMEI, cell và CDR trước khi shard; không chỉ tăng Uvicorn workers trên một SQLite state.
4. Chạy soak/crash-recovery trên môi trường Linux mục tiêu và điều tra native Windows crash. Thu corpus/traffic có nhãn để kiểm định độ chính xác và pipeline WAV riêng; số đo runtime không chứng minh FPR/recall thực tế.

Cách chạy: [hướng dẫn phát triển](DEVELOPMENT_V22.md), [verifier Linux](../docker/backend/README.md). Số liệu model và Spark: [VALIDATION_V22.md](VALIDATION_V22.md).
