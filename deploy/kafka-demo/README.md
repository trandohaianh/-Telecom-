# Demo Kafka: `carrier.events` → ShardedEngine → nhật ký bền → `fraud.verdicts`

Demo **riêng**, không phải stack production (`deploy/cloud`).

**Luồng xử lý:**
1. Producer phát lại 50 000 sự kiện mô phỏng của cửa sổ test vào topic `carrier.events`, với N partition, key = số chủ gọi.
2. Consumer gom micro-batch, chấm bằng `scaleout.ShardedEngine` sẵn có (router + N tiến trình chấm + side update chéo khoá), rồi ghi **cả lô trong một giao dịch** `BackendState` (`synchronous=FULL`, tức một fsync cho mỗi lô).
3. Phán quyết được ghi vào `fraud.verdicts`. Offset chỉ được commit sau khi lô đã ghi bền (at-least-once; `reserve()` chống chấm lại khi có bản gửi lại).
4. Một sink đọc `fraud.verdicts` và đo độ trễ **đầu-cuối**: từ lúc producer gửi tới lúc nhận được phán quyết, gồm cả thời gian chờ gom lô và linger của Kafka.

Mã nguồn: `src/kafka_pipeline.py`. Test với Kafka giả lập: `tests/test_kafka_pipeline.py`.

## Chạy

```bash
docker compose -p telecom-kafka-demo -f deploy/kafka-demo/docker-compose.yml --profile demo build demo
docker compose -p telecom-kafka-demo -f deploy/kafka-demo/docker-compose.yml up -d kafka
# (tuỳ chọn) mốc đối chiếu trong tiến trình, không qua Kafka, cùng số shard:
docker compose -p telecom-kafka-demo -f deploy/kafka-demo/docker-compose.yml --profile demo run --rm -T demo reference --partitions 2 --out /srv/state/ref2.json
docker compose -p telecom-kafka-demo -f deploy/kafka-demo/docker-compose.yml --profile demo run --rm -T demo bench --partitions 2 --rate 1000 --events 50000 --reference /srv/state/ref2.json
docker compose -p telecom-kafka-demo -f deploy/kafka-demo/docker-compose.yml --profile demo down -v
```

Tham số của `bench`:
- `--partitions N`: số partition của topic, và mặc định cũng là số shard chấm điểm (`--shards` để đặt khác);
- `--rate`: tốc độ gửi mục tiêu (sự kiện/giây);
- `--batch` / `--wait-ms`: kích thước micro-batch tối đa / thời gian chờ tối đa sau sự kiện đầu tiên.

Broker là `apache/kafka:3.8.0` (KRaft, một node), chỉ mở cổng trên `127.0.0.1:19092`.

Kết quả đo ngày 18/11/2026 nằm trong `results/`. Phân tích: `reports/06_Production_Readiness_Report.md` §8.
