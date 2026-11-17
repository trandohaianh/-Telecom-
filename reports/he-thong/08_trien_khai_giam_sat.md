# 8. Triển khai, giám sát và Android

## Thành phần
| Tệp | Nội dung |
|---|---|
| `Dockerfile`, `.dockerignore` | Image chính. Chỉ copy `src`, `static`, `runtime/artifacts_v22`, `serve.py` và requirements. Chạy `pip check`. |
| `docker/backend/` | Image verifier cô lập (`verify_entrypoint.py` → `verify_live_http`) |
| `deploy/cloud/` | Compose (Caddy + Telecom trên mạng `edge` dùng chung với FieldOps), `telecom.caddy`, `setup.sh`, `deploy.sh`, `smoke.sh` |
| `.github/workflows/ci.yml` | Test, eval có cổng chặn, promtool, scale-out, build image và smoke test |
| `.github/workflows/deploy.yml` | Deploy qua SSH sau khi CI xanh |
| `monitoring/` | 12 luật cảnh báo, dashboard Grafana |
| `android/` | Kotlin WebView bọc `/app/`, SDK 34, min 26 |

## Bản sửa
| # | Lỗi | Sửa |
|---|---|---|
| F13 | Khoá TLS và mật khẩu Redis lọt vào image gRPC. `.env` của deploy nằm trong build context. | Bổ sung các mẫu loại trừ vào cả hai `.dockerignore`. |
| F14 | Image verifier thiếu `prometheus-client`, nên `import server` lỗi. | Thêm `prometheus-client`, `PyJWT[crypto]`, `pyarrow`, ghim theo `requirements-serve.txt`. |
| F15 | `TfsBackupStale` và `TfsKpiStale` im lặng khi job chưa từng thành công. | Thêm `or absent(...)`. |
| F17 | Deploy build commit `origin/main` mới nhất thay vì commit CI đã kiểm. CI xanh của một PR có nhánh tên `main` cũng kích hoạt deploy. | Chỉ deploy khi `event == push`. `DEPLOY_REF=head_sha`. `deploy.sh` chỉ nhận SHA 40 ký tự nằm trên nhánh deploy. |

Tình trạng kiểm chứng:
- F13, F14, F15 được kiểm bằng test tĩnh trong `tests/test_audit_regressions_20261117.py`.
- F17 được kiểm bằng `bash -n` và đọc lại.
- **Chưa** chạy Docker build, promtool hay GitHub Actions, vì môi trường không có Docker daemon và Actions.

## Bản sửa vòng 2
| # | Sửa |
|---|---|
| G26 (D5) | `proxy_ip.sh` lấy IP của Caddy trên mạng `edge` rồi truyền vào `FORWARDED_ALLOW_IPS`; dùng ở cả `setup.sh` và `deploy.sh`. |
| G26 | Backup trước deploy phải thành công, nếu không thì dừng (`exit 3`). Lần cài đầu bỏ qua bước này; ép bỏ qua bằng `SKIP_PREDEPLOY_BACKUP=1`. |
| G26 | `smoke.sh` kiểm chứng chỉ TLS (chỉ dùng `SMOKE_INSECURE=1` khi đang chờ ACME). Token đọc qua `-H @file` với quyền 600. |
| G26 | Secret `DEPLOY_KNOWN_HOSTS` bật `StrictHostKeyChecking=yes`. |
| G26 | CI kiểm `sha256sum` của promtool. |
| G26 | Ghim base image `python:3.12.12-slim-bookworm`. |
| G7 | Thêm cảnh báo `TfsLateEventsUnscored`. |
| G27 | Android: liên kết trỏ ra ngoài server mở bằng trình duyệt hệ thống. Tuỳ chọn ký bản release (`-PreleaseKeystore`…). |

Kiểm chứng:
- `bash -n` cho cả 4 script.
- YAML parse cho workflow, alert và compose.
- `docker compose config` cho gRPC.
- Test tĩnh `test_cloud_deploy…`, `test_android…`.

**Chưa** chạy Actions thật, Docker build hay Android build.

## Bản sửa vòng 3
| # | Sửa |
|---|---|
| H3 | `TfsDown` thêm `absent_over_time(tfs_uptime_seconds[5m])`, nên không còn phụ thuộc tên job scrape. |
| H4 | Token DuckDNS nằm trong file cấu hình curl quyền 600 (`curl -K`), không còn trong crontab hay dòng lệnh. |
| H5 | Mọi action được ghim theo SHA commit. |

## Còn mở
- Android: `isMinifyEnabled=false`. Không có Android SDK để build và kiểm R8.
- Khi Caddy đổi IP mà chưa deploy lại, XFF bị bỏ qua. Đây là lỗi an toàn; chạy lại `deploy.sh` để sửa.

## Đã kiểm và thấy đúng
- Mọi biểu thức alert và Grafana đều tham chiếu đúng metric và label mà `observability.py` xuất ra.
- Compose Telecom bắt buộc `METRICS_TOKEN` và `FRAUD_ADMIN_PASSWORD`, đặt `FRAUD_PUBLIC=1`. Chỉ Caddy publish cổng.
- `secrets/` và `deploy/cloud/.env` nằm trong `.gitignore`. Image chính không copy `.env`.
- Android release:
  - `cleartextTrafficPermitted=false`;
  - bắt buộc `https://`;
  - không có `addJavascriptInterface`;
  - `allowBackup=false`;
  - chỉ activity launcher được export.
