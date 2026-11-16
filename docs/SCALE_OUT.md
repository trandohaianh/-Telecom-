# Mở rộng tới 50.000 TPS: chia tải theo số chủ gọi

## Vấn đề

Một tiến trình nhận từng sự kiện qua HTTPS (`/api/v1/verdict`) chỉ giữ được SLA 30 ms đến khoảng
300 sự kiện/giây. Chi phí nằm ở mỗi request: TLS, HMAC, FastAPI, ghi nhật ký bằng chứng. Phần chấm
mô hình chỉ tốn khoảng 0,09 ms CPU cho mỗi sự kiện.

## Giải pháp (`src/scaleout.py`)

1. **Gom lô ở biên mạng.** Nhà mạng gửi sự kiện theo lô nhỏ (64 sự kiện ≈ 1–2 ms ở 50k TPS) qua
   Kafka hoặc gRPC stream, thay vì mỗi cuộc gọi một request HTTPS.
2. **Chia shard theo số chủ gọi.** Mỗi tiến trình (một lõi) sở hữu một phần thuê bao và giữ riêng
   cửa sổ đặc trưng cùng mô hình của phần đó, nên phần nặng tăng tuyến tính theo số lõi.
3. **Nhân bản trạng thái chéo khoá.** Ba nhóm đặc trưng phụ thuộc sự kiện của số khác: cuộc gọi
   đến, số SIM trên một IMEI, SIM mới trên một cell. Mọi shard nhận thêm luồng "side update" của
   toàn mạng (`FeatureStore.observe_foreign`), mỗi sự kiện chỉ tốn khoảng 1,6 µs.

## Bằng chứng

**Đặc trưng.** `tests/test_scaleout_features.py` cho thấy với 2, 3 và 5 shard, mọi đặc trưng của
mọi sự kiện **giống hệt** chạy 1 tiến trình. Test đối chứng xác nhận: bỏ side update thì hơn 100
sự kiện bị sai đặc trưng.

**Chỉ số trên 50.000 sự kiện test** (`evals/shard_equivalence.py --replicated`):

| Cấu hình | Recall chặn | Precision | SIM box |
|---|---|---|---|
| 1 tiến trình | 54,55% | 100% | 82,4% |
| 2/4/8 shard, không nhân bản | 40,91% | 100% | 58,8% |
| 2/4/8 shard, có nhân bản | **54,55%** | **100%** | **82,4%** |

**Thông lượng** (`evals/bench_scaleout.py`, máy 2 lõi):
- 1 shard: khoảng **10.000 sự kiện/giây** end-to-end.
- Độ trễ mỗi lô 64 sự kiện: p50 5 ms, p99 11 ms, vẫn dưới SLA 30 ms.
- Máy 2 lõi không đo được tăng tuyến tính, vì router và 2 shard tranh nhau CPU. CI chạy lại trên
  runner 4 lõi.

**Năng lực ngoại suy:**
- Mỗi shard tốn 0,089 ms cho sự kiện mình sở hữu và 0,0016 ms cho mỗi side update.
- Để mỗi lõi ≤ 70%, cần **8 shard ≈ 8 lõi** cho 50.000 TPS, cộng lõi cho consumer/producer: tức
  2–3 VM loại 4–8 vCPU.
- Đây là ngoại suy từ số đo; cần đo lại trên phần cứng và lưu lượng đích trước khi cam kết.

## Còn phải làm khi triển khai thật

- **Kết nối luồng.** Nối consumer Kafka (topic chính chia partition theo số chủ gọi, cộng topic side
  update) vào `ShardedEngine`. Router trong `ShardedEngine` chỉ để benchmark trên 1 máy.
- **Phục hồi.** Checkpoint trạng thái từng shard (`FeatureStore.state_dict`) và phát lại offset khi
  khởi động lại.
- **Thay đổi số shard.** Muốn đổi số shard phải chia lại khoá, theo kiểu consistent hashing có di
  chuyển trạng thái.
