# Technical Defense — workspace-backed projects
Cập nhật 06/11/2026. Tài liệu phân biệt năng lực đã có mã/bằng chứng với fixture và hướng thiết kế tiếp theo.

## Ranh giới danh mục

Workspace hiện có Telecom Fraud Shield, pipeline Spark/Kafka CALL Analytics và khung dịch vụ Edu DSS. Không tìm thấy mã nguồn Field Repair RAG, Edge-based Multi-sensor Object Tracking hoặc Django Employee Management. Module Android trong repo chỉ là WebView của FraudShield, không phải object tracker. Nếu cần dự án thứ ba có thể chứng minh ngay bằng mã, dùng Spark/Kafka CALL Analytics và nói rõ đây là nhánh analytics phân tán bổ trợ cho Telecom. Không tự nhận các dự án vắng source.

Các số trong Portfolio Telecom v2.1 là số giả lập lịch sử. Dùng bằng chứng v2.2 ở các báo cáo mới hơn. Edu DSS là service integration scaffold; repo có một OLS baseline trên fixture tổng hợp, chưa có grade model huấn luyện hoặc đánh giá trên cohort thật.

## 1. Telecom Fraud Detection — tách đường online và analytics

Luồng online: signaling mirror → signed FastAPI request → HMAC/schema/idempotency → bounded admission → một inference/state owner → causal RAM features → LightGBM/anomaly/policy/evidence gate → SQLite WAL/FULL journal → phản hồi advisory cho phần tử mạng.

Luồng analytics: CDR/Kafka → Spark CALL features → dedup/watermark/quarantine/checkpoint → phân tích lịch sử, đầu vào huấn luyện hoặc policy publication.

API nhận bản sao signaling và trả khuyến nghị; phần tử mạng giữ quyền thực thi cuộc gọi. Runtime khởi động ở shadow, không nằm inline thay SIP/Diameter/SS7. Spark/Kafka không ở trong request đồng bộ Call Setup nên không tính vào ngân sách RPC 30ms. Nhánh gRPC/protobuf là adapter tra policy riêng, chưa thay FastAPI và chưa tích hợp toàn bộ scoring flow.

Một inference owner tuần tự hóa cập nhật feature state, đổi lại giảm khả năng scale ngang trên một SQLite writer. Outcome chỉ được xác nhận sau commit để giữ durability; do đó queue và fsync tail tham gia latency. Tham chiếu: [server.py](../src/server.py), [detector.py](../src/detector.py), [feature_store.py](../src/feature_store.py), [resilience.py](../src/resilience.py).

### Imbalanced data và model

CALL features chỉ dùng thông tin đã có tại event time: số cuộc 1m/10m/1h/24h, callee dispersion, tỷ lệ incoming/outgoing, SIM/IMEI sharing, SIM mới theo cell, mobility, CLI/trunk và duration của cuộc đã hoàn tất. Duration chỉ nhập feature sau khi CDR hoàn tất, tránh leakage vào quyết định pre-ring.

Training CALL dùng LightGBM với scale_pos_weight=10; SMS dùng TF-IDF và Logistic Regression với balanced class weights. Threshold được chọn từ điểm của legitimate calibration samples theo ngân sách FPR riêng cho BLOCK và FLAG. Isolation Forest theo segment fit trên legitimate traffic và chỉ phát FLAG. Hard BLOCK còn phải qua behavioral evidence gate. Code không dùng SMOTE. Điểm thô chưa calibrated thành xác suất production.

Khi fraud chỉ khoảng 0,1%, accuracy đánh lừa: mô hình luôn ALLOW có thể đạt xấp xỉ 99,9% accuracy nhưng bắt được 0 fraud. Vì vậy phải báo riêng BLOCK và FLAG: precision=TP/(TP+FP), recall=TP/(TP+FN), FPR=FP/(FP+TN) kèm cỡ mẫu/upper confidence bound; đồng thời báo toàn bộ offered requests đúng hạn, fail-open, pending, errors và shedding. Chọn ngưỡng là bài toán tối ưu dưới ràng buộc FPR và chi phí vận hành, không phải chọn accuracy cao nhất. Calibration trên synthetic negatives không chứng nhận carrier traffic.

### Metrics có thể trích dẫn

Trên 50.000 event giả lập, 66 fraud và 158.787 warm-up events: BLOCK TP/FP/FN=36/0/30, recall 54,55%, precision 100% trong mẫu, sample FPR 0%; BLOCK+FLAG=57/57/9, recall 86,36%, precision 50%, FPR 0,1142%. Đây là event-level simulator, không phải dữ liệu khách hàng. Trong 35.136 CALL sessions/53 fraud, pre-ring BLOCK recall 49,06%, final BLOCK 52,83%, BLOCK+FLAG 86,79%. Calibration CALL BLOCK giả lập có 5 FP/105.427 negatives; upper 95% FPR là 0,01110%, cao hơn target 0,01%. Deployment FPR chưa được chứng nhận. Xem [VALIDATION_V22.md](VALIDATION_V22.md).

HTTP soak mới nhất: 50 EPS, ba cửa sổ 30 giây, scheduled p99 = 31,424 / 41,106 / 46,436 ms; tỷ lệ offered request được chấm trong 30ms là 98,6% / 96% / 87,733%. Gate tổng thất bại. Redis microbenchmark hoặc protobuf image build không chứng minh gRPC/IMS SLA. Xem [SLA_TARGET_20261025.md](SLA_TARGET_20261025.md).

**Script giải trình luồng realtime:** “Em tách online decision API khỏi analytics path. Online service xác thực signaling mirror, đưa vào một state owner, tính causal features, score và lưu journal/evidence rồi mới trả advisory. Network element sở hữu enforcement. Kafka/Spark xử lý CDR, replay và entity features bất đồng bộ; micro-batch không nằm trong deadline pre-ring. Service mặc định shadow và end-to-end p99 hiện chưa qua gate.”

**Script giải trình imbalance:** “Em không tối ưu accuracy vì fraud hiếm. Training dùng class weights cho CALL và SMS, sau đó chọn threshold theo ngân sách FPR riêng cho BLOCK/FLAG. Em báo precision, recall và confidence bound theo hành động. Synthetic calibration chưa đủ chứng nhận FPR 0,01% trên carrier; upper bound CALL hard-block là 0,01110%, vì thế vẫn shadow cho tới khi có dữ liệu nhà mạng gán nhãn đại diện.”

Câu hỏi khó: class weights tránh nhân bản ví dụ fraud gần giống nhau; threshold vẫn quyết định operating point. Precision 100% trên 36 true blocks không chứng minh an toàn ngoài mẫu. Fail-open giữ call không bị treo nhưng là policy unavailable, không phải model ALLOW.

## 2. Edu DSS — System Design/System Integration, không nhận là advanced ML

Topology hiện có: Streamlit view → Nginx HTTPS/WebSocket → FastAPI JWT/RBAC → Redis cache/broker → Celery worker → domain adapters. API xếp job AutoML/CMS/advice, đọc explanation từ cache và polling task. Có một route dự đoán điểm baseline OLS dùng fixture tổng hợp trong demo; dữ liệu và model thật vẫn thuộc external adapter contract. Repo không có grade dataset, real-data trained predictor, recommendation algorithm hay model-quality metric. Tham chiếu: [Edu README](../edu_dss/README.md), [FastAPI routes](../edu_dss/backend/app/main.py), [service adapters](../edu_dss/backend/app/services.py), [Streamlit view](../edu_dss/ui/app.py), [OLS baseline](../edu_dss/backend/app/grade_baseline.py).

Local demo trả fixture có nhãn demo_fixture_only. Hai dòng leaderboard 0,50 và 0,68 là hard-coded; CMS demo trả excerpt; advice trả ba bước tĩnh. OLS baseline dùng bảy cặp GPA tổng hợp, đầu vào mẫu cố định 2.5 và holdout hai dòng tổng hợp; metric này chỉ kiểm tra wiring/toán học, không đo chất lượng học vụ. Advice JSON được nén khi đưa vào queue rồi giải nén trước adapter; không có bằng chứng giảm prompt/token 13,23 lần.

**Script trả lời:** “Edu DSS em định vị là phần system integration. Em xây role-gated API, async job lifecycle, cache-first read, rate limiting và adapter boundary để AutoML/CMS/LLM không chặn UI. Em bổ sung OLS một biến làm baseline demo trên fixture tổng hợp để kiểm tra prediction contract; nó không được train trên hồ sơ sinh viên và metric holdout hai mẫu không đại diện chất lượng thật. Model và recommendation của trường vẫn cần cohort data, leakage-safe evaluation và adapter nghiệp vụ.”

Nếu được hỏi mô hình tiếp theo: xác định target và prediction timestamp; thay fixture bằng cohort đã được phép sử dụng; split theo term/time và student/cohort để tránh leakage; so sánh naive với linear baseline; báo MAE/RMSE/R2 cùng subgroup error cho regression. Early-warning classifier cần PR-AUC, recall ở intervention capacity và calibration. Recommendation có thể kết hợp mastery gap, prerequisite graph, mục tiêu và catalog được duyệt; đánh giá Recall@K/NDCG@K, rồi prospective study trước khi khẳng định cải thiện kết quả học tập.

Một thiếu sót quyền truy cập tài nguyên đã được vá: Student token phải có course_ids, Instructor token phải có teaching_course_ids; Admin được vận hành toàn cục. Demo token chỉ được vào demo-course. Đây là claim-based boundary, chưa thay IdP lifecycle, tenant policy, enrollment freshness hay audit. Tham chiếu: [course authorization](../edu_dss/backend/app/course_access.py), [JWT validation](../edu_dss/backend/app/security.py).

Câu hỏi RMSE: “Repo có metric OLS trên hai dòng synthetic holdout, nhưng chưa có RMSE trên cohort thật. Em trình bày đó là smoke-test của baseline contract; chỉ báo chất lượng học vụ sau khi có labeled cohort và split leakage-safe.” Câu hỏi leakage: “Chốt prediction time, chỉ materialize feature đã có tại thời điểm đó, split theo thời gian/student và audit target-derived fields.” Recommendation có hiệu quả không: “Offline ranking không chứng minh nhân quả; cần prospective controlled pilot.”

## 3. Thay thế có bằng chứng trong workspace — Spark/Kafka CALL Analytics

Edge tracking và Django HR không có source trong workspace. Dùng Spark/Kafka chỉ như lựa chọn workspace-backed; trình bày là analytics phân tán cùng domain Telecom, không nói đó là sản phẩm độc lập đã vận hành production.

Pipeline: CSV/Kafka CALL → schema contract → deterministic order theo (ts,event_id) → distributed state subscriber/IMEI/cell → 27 causal features → dedup/watermark/late quarantine → atomic checkpoint/output.

Batch tính feature trên workers và join theo event_id. Kafka path xử lý bounded state, duplicate payload hash, conflict quarantine, CDR completion time và checkpoint gắn engine/schema. State được ghi bằng full Parquet snapshot mỗi batch, chưa phải incremental state store; multi-host/HA/throughput production chưa được chứng minh. Tham chiếu: [Spark CLI](../spark/spark_features.py), [distributed features](../spark/distributed_features.py), [distributed stream](../spark/distributed_stream.py), [Spark validation](../spark/VALIDATION.md).

Bằng chứng: distributed replay 8.939 events khớp 27/27 features với Python tolerance 1e-9. Kafka CLI 8.932 events, 4 partitions, 3 data batches và 2 tail drains qua process restart, cũng khớp 27/27; duplicate bị bỏ, malformed record quarantine. Đây là kiểm tra function/recovery trên một máy, không phải scale/HA/30ms.

**Script trả lời:** “Spark/Kafka là analytical pipeline, không phải Call Setup RPC. Em dùng nó để chứng minh deterministic feature parity qua distributed replay và restart, đồng thời xử lý event-time, dedup và quarantine. Bản hiện tại snapshot Parquet toàn state mỗi batch; em sẽ đo tăng trưởng state và chuyển incremental state trước khi nói production scale.”

Câu hỏi watermark: đó là lateness boundary của application theo observed event time, không tạo global ordering giữa Kafka partitions. Câu hỏi exactly-once: không claim cross-system exactly-once; mô tả idempotency và commit-after-success đúng theo implementation. Câu hỏi scale: chỉ có sample một máy/process restart, chưa có multi-host throughput hay HA.

## Checklist trước phỏng vấn

1. Dùng số liệu v2.2 từ validation/SLA report; bảng v2.1 ở [Portfolio_VI.md](Portfolio_VI.md) và [Portfolio_EN.md](Portfolio_EN.md) chỉ là historical simulation. Không nêu p99 0,45 ms hay 25k/s như end-to-end SLA hiện hành.
2. Không giới thiệu Field Repair RAG, Edge tracker hay Django HR là project đã triển khai từ repo này.
3. Không trình bày synthetic OLS holdout như model metric thật hoặc recommendation quality.
