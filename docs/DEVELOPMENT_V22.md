# Telecom Fraud Shield — cập nhật kỹ thuật v2.2

Kết quả đo và giới hạn hiện tại: [VALIDATION_V22.md](VALIDATION_V22.md), [sửa lỗi và kiểm chứng 2026-10-25](BUGFIX_20261025.md).

Tối ưu mới nhất, cấu hình `FRAUD_WINDOWS_EVENT_LOOP`, `FRAUD_SQLITE_WAL_AUTOCHECKPOINT_PAGES`, điều kiện SSD và lệnh kiểm gate SLA: [SLA_TARGET_20261025.md](SLA_TARGET_20261025.md). Regression hiện tại có 426 tests + 50 subtests pass; SLA 30 ms chưa ổn định ở tải đã đo.

Pipeline acoustic dùng `voice-acoustic-v2`: clipping đo từ PCM nguồn. Artifact v1 cần trích lại feature, train và calibration; không dùng model v1 với feature v2. Retry kết quả v1 đã hoàn tất vẫn đọc lịch sử qua kiểm canonical metadata, luôn duplicate/NONE. `FRAUD_AUDIO_SHUTDOWN_TIMEOUT_S` mặc định 10 giây; hết timeout báo native worker còn chạy, không giả vờ hủy được thread.

Các artifact v2.1 trong data/ và models/ được giữ làm baseline lịch sử.
Bộ dựng mới nằm ở runtime/artifacts_v22/. Mô hình này vẫn học dữ liệu giả lập;
pipeline nhận WAV thật là một miền đặc trưng và mô hình riêng.

## Chạy hệ thống

Yêu cầu Python >= 3.11 và Uvicorn >= 0.47.0 theo requirements.txt.

```powershell
python run_demo.py
python run_live.py --port 8081 --state-dir runtime/live --require-manifest
```

Demo tự ưu tiên bộ v2.2 khi có đủ manifest. Live khởi động ở shadow, không chạy
replay dữ liệu mô phỏng. Live giữ state.sqlite3, evidence.jsonl, audit.jsonl,
users.json và network_link.key trong --state-dir. Không chia sẻ thư mục này.

GET /health/live kiểm tra tiến trình; GET /health/ready kiểm tra inference owner.
Chạy một inference owner cho mỗi SQLite state; startup từ chối owner thứ hai.
Mỗi lần đổi model bundle phải có quy trình migration/replay hoặc state directory
mới, vì quyết định và lịch sử học từ model cũ không tự chuyển sang model mới.

Có thể chọn bundle khác bằng --model-dir. Các biến FRAUD_DATA_DIR,
FRAUD_MODEL_DIR, FRAUD_LOG_DIR và FRAUD_MODEL_VERSION hỗ trợ môi trường riêng.
Các đường dẫn state/audit/evidence/users/network key cũng có biến FRAUD_* tương
ứng trong src/server.py và src/security.py.

Enforce cần admin chuyển mode và FRAUD_REAL_TRAFFIC_VALIDATED=1 sau khi nhà mạng
đã kiểm định trên traffic có nhãn. Biến này là xác nhận vận hành, không phải bằng
chứng model đạt FPR. Nhà mạng vẫn là bên thực thi và giữ timer của tổng đài.

## Thay đổi về tính đúng và vận hành

- CDR được đưa vào lịch sử theo thời gian hoàn tất, không bị cuộc dài đứng trước
  che một cuộc ngắn đã kết thúc. Cửa sổ gọi đi 24 giờ dùng timestamp chính xác.
- Feature contract là causal-v2-exact-24h-completion-order. Checkpoint v1 có bucket
  phút không đủ thông tin để phục hồi timestamp; cần replay vào namespace mới.
- Ingest giữ thứ tự nhân quả. Duplicate và late không cập nhật lại cửa sổ. API có
  journal riêng cho setup và CDR; retry cùng ID/nội dung không tạo tác động lần hai,
  đổi nội dung cùng ID trả 409.
- HMAC kiểm chữ ký, timestamp hữu hạn và nonce; dữ liệu JSON có kiểm kiểu/giới hạn.
- Suy luận chạy qua queue có giới hạn, ngoài event loop. Hết deadline trả fail-open;
  kết quả đến muộn được lưu riêng. Queue đầy có phản hồi rõ ràng.
- SQLite giữ journal/checkpoint/policy/session/suspension và outbox evidence.
  Evidence tiếp tục chuỗi cũ, có flush khi dừng; không xóa log lúc startup.
- Policy miễn thương mại và quyết định con người áp dụng chung cho kết quả thường
  và đến muộn. Rescore bằng bối cảnh hiện tại chỉ là tín hiệu cần chuyên viên xem;
  nó không tự học denylist hay báo thuê bao là gian lận đã xác nhận.
- Snapshot suy luận có rollback khi lỗi, tránh cập nhật feature một phần.
- Số E.164 có dấu + hoặc định dạng khoảng trắng vẫn được che.

## Delivery, retry và khóa HMAC

Runtime gộp reserve + finish trong một transaction WAL/FULL và chỉ công bố sau
COMMIT. Queue admission dùng RAM; PENDING/durable=false có thể mất khi process
chết. Adapter phải retry cùng event_id/payload với timestamp/chữ ký mới cho đến
kết quả bền vững, giữ deadline gốc của cuộc gọi. Duplicate chỉ trả kết quả tham
khảo, recommended_action=NONE; không dùng retry để thực thi BLOCK cũ.

Client Python có thể dùng security.network_timestamp() rồi sign_network(body,
timestamp, key). Không chuyển timestamp qua float trước khi ký: Windows có thể
cho cùng wall-clock ở các retry rất nhanh. Server vẫn kiểm skew và replay.

network_key() cache key trong tiến trình. Sau khi thay file bằng 64 ký tự hex
hợp lệ, cần gọi security.reload_network_key() trong đúng tiến trình đang phục vụ
hoặc restart; sửa file đơn thuần chưa đổi key đã nạp. Reload lỗi giữ cache cũ và
báo lỗi. Không chạy lệnh reload trong một Python process khác rồi coi server đã đổi.

health/ready trả 503 khi inference owner lỗi/đóng hoặc bận treo quá ngưỡng,
bao gồm nhánh chấm bù; probe không chờ khóa SQLite/model.

Số đo, delivery contract và hướng tối ưu 30 ms: [SLA_30MS.md](SLA_30MS.md).
Hiện chưa đạt SLA HTTP p99 30 ms trên môi trường được đo.

## Dựng lại và đánh giá model

```powershell
python src/rebuild_artifacts.py --out-dir runtime/artifacts_v22_new --model-version tfd-v2.2
python src/rebuild_artifacts.py --out-dir runtime/artifacts_v22_new --model-version tfd-v2.2 --resume
```

Runner mặc định dựng dataset, train, evaluate đầy đủ, drift baseline và shadow
report. Output mới bắt buộc dùng thư mục mới; --resume chỉ tiếp tục build có report,
đúng phiên bản và artifact/hash đã được xác minh.

Train, early-stopping validation và threshold calibration dùng ba cửa sổ độc lập.
Threshold dùng số FP nguyên theo budget. Báo cáo có số negative, khoảng tin cậy
và trạng thái đủ/thiếu mẫu; score chưa được gắn nhãn xác suất đã calibration.

CALL và AUDIO ghép bằng call_id, kiểm cặp thuê bao và khoảng thời gian. Legacy
không có ID chỉ có ghép gần đúng với cuộc trước đó. Báo cáo tách pre-ring,
quyết định cuối phiên và thời gian đến lần phát hiện đầu; AUDIO chunk không được
tính như các cuộc gọi độc lập trong metric theo phiên.

Manifest lưu feature contract, schema, nguồn dataset và hash artifact.
--require-manifest từ chối bundle legacy chưa xác minh. Các báo cáo v2.1 có sẵn
không được dùng làm số liệu mới sau khi đổi hợp đồng đặc trưng.

Drift có active_hours_on_network, peer group, missing/nonfinite và freshness.
Baseline giả lập có fraud prior khác traffic thực, nên dịch chuyển band không tự
đồng nghĩa model xuống cấp.

## Spark/Kafka

Xem [spark/README.md](../spark/README.md) và
[spark/VALIDATION.md](../spark/VALIDATION.md).

Batch mặc định tính đủ 27 đặc trưng qua các shuffle theo MSISDN, IMEI và cell.
Comparator mặc định yêu cầu đủ event ID duy nhất, đủ cột và giá trị hữu hạn.

Các lệnh sau chạy trong container Spark tại /work/spark, sau khi dựng sample và
khởi động Kafka theo spark/README.md. kafka:9092 là hostname trong mạng Docker.

```bash
python spark_features.py --mode batch --engine distributed --events /tmp/sample/sample_events.csv --out /tmp/features.parquet
python spark_features.py --mode stream --engine local-stream --kafka kafka:9092
python spark_features.py --mode stream --engine distributed-stream --kafka kafka:9092 --checkpoint /tmp/distributed-checkpoint
```

local-stream giữ state trên driver. distributed-stream giữ state theo key trên
workers và checkpoint Parquet qua từng microbatch. Mỗi chế độ có namespace riêng.
Distributed snapshot vẫn đọc/ghi state qua batch; cần shared filesystem có commit
rename phù hợp khi chạy cluster. Đây chưa phải chứng nhận throughput/HA.

Có lateness buffer, dedup có thời hạn, quarantine, giới hạn buffer, metrics và
retention snapshot. Không xóa offset Spark riêng khỏi state. Những feature phụ
thuộc first-seen/metadata sẽ đổi ngữ nghĩa nếu bật TTL quên thuê bao; mặc định giữ.

## Pipeline giọng nói thật

```powershell
python src/voice_pipeline.py recording.wav --out observation.json
python src/voice_pipeline.py stereo.wav --caller-channel 0 --out stereo-observation.json
```

Nhận PCM WAV mono/stereo 8/16/24/32 bit, các sample rate được hỗ trợ từ 8 đến
48 kHz, dài 0,1–30 giây và tối đa 12 MiB. Pipeline kiểm header/kích thước, resample
16 kHz, chạy energy VAD, trích phổ/pitch/hoạt động tiếng nói và kiểm clipping/
silence. Không lưu raw WAV. Mono không suy đoán ai là người gọi/người nhận;
stereo chỉ có chỉ số theo bên khi adapter khai báo caller_channel.

pitch_period_variation là độ biến thiên chu kỳ ước lượng theo frame; spectral_drift
là độ đổi phổ. Chúng không được giả làm clinical jitter, định danh speaker hay
điểm vocoder. Pipeline không chuyển các proxy này vào model AUDIO giả lập.

POST /api/v1/audio nhận JSON ký HMAC giống đường nhà mạng:

```json
{
  "event_type": "AUDIO",
  "event_id": "segment-unique-id",
  "call_id": "call-session-id",
  "caller": "84900000001",
  "callee": "84900000002",
  "ts": 1700000001,
  "caller_channel": 0,
  "wav_b64": "<base64 của PCM WAV>"
}
```

ts là thời điểm quan sát segment. Với mono, bỏ caller_channel nếu không có mapping
hai bên. Request có queue giới hạn; xử lý lâu trả 202/PROCESSING, client gửi lại
cùng payload/event_id với chữ ký timestamp mới. Queue đầy trả 429. Retry khác
nội dung trả 409. Kết quả đã hoàn tất được journal idempotent; raw WAV và transcript
không nằm trong bản ghi bền vững. Mỗi response kiểm lại mode hiện tại và artifact
identity, nên cache cũ không mang khuyến nghị enforce sang shadow.

Mặc định trả OBSERVE/NONE khi chưa có model phù hợp âm thanh thật. Đây là pipeline
trích đặc trưng hoạt động được, chưa phải tuyên bố phát hiện deepfake ngoài thực tế.

ASR tùy chọn, chạy cục bộ với model đã có sẵn:

```powershell
pip install -r requirements-voice-asr.txt
python src/voice_pipeline.py recording.wav --asr-model D:/models/faster-whisper-small
```

Server dùng FRAUD_ASR_MODEL. Adapter không tự tải weights. Kiểm thử dùng adapter
ASR giả lập để kiểm contract; chất lượng nhận dạng tiếng Việt cần bộ audio thật.

## Train model cho miền WAV

Manifest CSV:

```csv
audio_path,label,split,group_id,call_id,ts,caller_channel,speaker_id,campaign_id
clean-train.wav,0,train,g1,c1,1,,s1,p1
fraud-train.wav,1,train,g2,c2,2,,s2,p2
clean-cal.wav,0,calibration,g3,c3,11,,s3,p3
fraud-cal.wav,1,calibration,g4,c4,12,,s4,p4
clean-test.wav,0,test,g5,c5,21,,s5,p5
fraud-test.wav,1,test,g6,c6,22,,s6,p6
```

Đây chỉ là schema minh họa, không phải corpus đã cung cấp.

```powershell
python src/train_voice.py --manifest voice_data/manifest.csv --out-dir runtime/voice_model
```

Trainer kiểm split theo thời gian, group/call/speaker/campaign không trùng giữa
split, không trùng nội dung WAV; preprocessing chỉ fit train. Calibration gộp các
nhóm nối qua call/speaker/campaign và lấy điểm lớn nhất mỗi nhóm. Nhiều chunk của
một cuộc không làm tăng số negative độc lập. Upper bound này có đơn vị nhóm; nếu
một nhóm có nhiều record, action vẫn bị tắt vì chưa có bảo đảm FPR theo sự kiện.
Nhóm trộn nhãn bị từ chối. Thiếu negative độc lập thì không bật action; 0 FP trên
vài nghìn mẫu không đủ chứng minh FPR 0,01%. Metadata độc lập cần nhà mạng xác minh.

Cấu hình FRAUD_REAL_VOICE_MODEL tới real_voice_bundle.joblib để server nạp model
này. Bundle và manifest hash phải khớp. Chưa có corpus ghi âm thật có nhãn trong
workspace, nên chưa có model giọng nói thật đã được chứng minh hiệu quả.

## Nhãn chuyên viên và dữ liệu shadow

Live mặc định giữ feature của traffic ALLOW và cảnh báo để hỗ trợ hậu kiểm.
FRAUD_CAPTURE_FEATURES=0 tắt phần này; khi tắt, export sẽ loại các hàng thiếu feature.

```powershell
python src/shadow_feedback.py export-review --state-db runtime/live/state.sqlite3 --out review.csv
python src/shadow_feedback.py build --state-db runtime/live/state.sqlite3 --labels reviewed.csv --out-dir runtime/reviewed_dataset --key-file secrets/feedback_export.key --train-before 1700000000 --calibration-before 1700100000
```

Reviewer điền label, reviewer và group_id. Dùng key pseudonymization riêng 32 byte
hex, không dùng lại khóa HMAC mạng. Output không xuất số thuê bao hay nội dung.
Nhóm trải qua ranh giới thời gian được loại để tránh leak. Cần review cả mẫu ALLOW
đại diện; chỉ gắn nhãn cảnh báo sẽ làm lệch metric.

## Kiểm thử và đo HTTP

```powershell
python -B -m pytest -q -p no:cacheprovider tests spark/test_stateful_stream.py spark/test_distributed_stream.py spark/test_stream_contracts.py
python src/verify_live_http.py --model-dir runtime/artifacts_v22/models --rates 10,50,100 --seconds 10 --out data/http_runtime_verification_new.json
```

verify_live_http tạo server/credential/state/log riêng trong TEMP, kiểm chữ ký,
setup/CDR retry, identity conflict, WAV, enforce gate và restart. Không dùng khóa
hoặc log phiên hiện có. Benchmark báo offered/completed/client-shed, fail-open,
scheduler delay, HTTP roundtrip và latency do server báo; HTTP 200/fail-open không
được coi là đã chấm model thành công.

Trên Windows có thể cần --basetemp là một đường dẫn TEMP mới nếu pytest-current
cũ bị lỗi ACL. Tạo target mới thay vì chỉ vào thư mục có dữ liệu cần giữ.

Có thêm [verifier Linux dùng volume đĩa riêng](../docker/backend/README.md).
Các phép đo đã lưu nằm trong [VALIDATION_V22.md](VALIDATION_V22.md); cấu hình
hiện tại chưa đạt SLA HTTP 30 ms.

SQLite journal và snapshot là triển khai một owner cục bộ. Vẫn cần kiểm định
capacity, HA, SSO/OTP và adapter nhà mạng theo môi trường thực trước vận hành.
