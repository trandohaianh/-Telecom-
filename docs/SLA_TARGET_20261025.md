# Tối ưu SLA 30 ms — 2026-10-25

**Chưa đạt p99 đầu cuối ≤30 ms ổn định trên máy hiện tại.** Đã sửa một nút thắt lớn trong agent và giảm độ trễ điển hình, nhưng lượt lặp lại vẫn có cửa sổ vượt ngưỡng. Không dùng riêng cửa sổ tốt nhất để công bố SLA.

## Bản mã cuối

**426 tests + 50 subtests pass**. Với WAL/FULL và ngưỡng checkpoint mặc định 1.000 trang, ba cửa sổ 50 EPS × 30 giây có scheduled HTTP p99 **31,424 / 41,106 / 46,436 ms**; tỷ lệ scored qua HTTP trong 30 ms là **98,6 / 96 / 87,733%**. Gate tổng thất bại. [Report nguồn cuối](../data/http_sla_target_shipped_20261025.json), [JUnit](../runtime/sla_target_20261025/pytest-shipped.xml).

Đã quan sát 40 lần thay salt của WAL trong tải, với synchronous=FULL. Observer lấy một mẫu mỗi 16 owner batches, chỉ đọc header, không gọi checkpoint thủ công. Lượt này kiểm API HTTP loopback, không bao gồm đường truyền tới tổng đài.

Thử ngưỡng 8.192 trang ghi nhận bốn chu kỳ WAL, p99 43,497 / 44,522 / 42,887 ms và vẫn không đạt. Không đủ bằng chứng để đổi mặc định nên đã giữ 1.000 trang. [Report thử 8.192](../data/http_sla_target_wal8192_r2_20261025.json). Tần suất observer giữa hai lượt khác nhau; không gán toàn bộ chênh lệch cho riêng checkpoint threshold. Một lần tổng hợp trước đó lỗi ở child restart không có owner batch; chỉ các quan sát WAL của lần lỗi được lưu, không dùng làm kết quả latency.

`FRAUD_SQLITE_WAL_AUTOCHECKPOINT_PAGES` cho phép đối chứng có kiểm soát, nhận integer 1–65.536, mặc định 1.000. Mỗi COMMIT vẫn fsync theo FULL; ngưỡng này không phải giới hạn cứng dung lượng WAL khi có reader giữ snapshot.

## Thay đổi đã áp dụng

- Agent gộp lưu snapshot thành một lần mỗi vòng scan, thay vì ghi lại toàn bộ danh sách sau từng đề xuất. Test tái hiện 64 đề xuất gây 65 lần ghi trước sửa; sau sửa còn một lần, đủ 64 audit records và khôi phục đầy đủ sau restart. Trong lượt soak trước sửa đã tích lũy 1.013 đề xuất, snapshot khoảng 609 KB.
- Khi watcher lỗi, các đề xuất đã ghi audit thành công vẫn được lưu. Đề xuất có audit thất bại không được đưa vào RAM/snapshot. Quyết định của chuyên viên ngoài scan vẫn persist ngay.
- Metadata không đổi không tạo database changes/WAL frames mới. Metadata từ agent được mã hóa trước khi lấy khóa; giải mã evidence cũng chạy ngoài khóa SQLite để giảm cản trở inference owner.
- Route verdict trả trực tiếp JSONResponse từ các trường JSON-native, giảm phần FastAPI phải duyệt/copy lại model identity trên event loop. Nội dung hợp đồng và kiểm tra quyền giữ nguyên.
- Bộ phát tải Windows chờ bằng timer trên một executor worker thay cho timeout IOCP thô. Vẫn lấy lịch arrival gốc làm mốc, ghi rõ `arrival_pacing`, tính mọi offered event và không tính pending/fail-open/errors/shedding là thành công. Đây là cải thiện công cụ phát tải, không tự chứng minh server nhanh hơn.
- Có lựa chọn `FRAUD_WINDOWS_EVENT_LOOP=selector` cho profile Windows được đo; mặc định vẫn là proactor. Không thay timer toàn Windows, không busy-spin. Thử GIL 1 ms không cho kết quả đủ thuyết phục nên không đưa vào cấu hình mặc định.

Giữ SQLite **WAL/FULL**, công bố sau COMMIT, model/threshold/feature contract và deadline 30 ms. Không thay đổi Spark vì đường Spark batch/stream không phải đường RPC pre-call này.

## Kết quả đo

Mỗi dòng trong bảng là một cửa sổ 30 giây liên tiếp trên cùng instance. CALL dùng model bundle giả lập tfd-v2.2, HMAC, evidence writer, ops agent, một inference owner, shadow và concurrency 32. Có CDR/WAV smoke check trước tải và kiểm tra restart sau tải; chưa đo tải CDR/WAV/ASR đồng thời. Đây là tải đã warm model/kết nối, không đo cold startup.

| Lượt trước khi thêm tùy chọn checkpoint | EPS | Offered | Scheduled HTTP p99 ms | Scored qua HTTP ≤30 ms / mọi offered | Fail-open |
|---|---:|---:|---:|---:|---:|
| Mixed, cửa sổ 1 | 50 | 1.500 | 22,533 | 99,467% | 2 |
| Mixed, cửa sổ 2 | 100 | 3.000 | 45,196 | 94,8% | 126 |
| Mixed, cửa sổ 3 | 100 | 3.000 | 50,046 | 93,467% | 153 |
| Lặp 50 EPS, cửa sổ 1 | 50 | 1.500 | 20,850 | 99,467% | 2 |
| Lặp 50 EPS, cửa sổ 2 | 50 | 1.500 | 31,250 | 98,8% | 8 |
| Lặp 50 EPS, cửa sổ 3 | 50 | 1.500 | 38,317 | 98,2% | 9 |

Nguồn: [mixed 50/100/100 EPS](../data/http_sla_target_soak_fixed_20261025.json), [lặp 50 EPS](../data/http_sla_target_capacity50_20261025.json). Cả hai đều có **gate tổng = false**, dù một số cửa sổ đạt. Tám smoke checks pass và evidence hợp lệ trong cả hai lượt. [Lặp 25 EPS × 40 giây](../data/http_sla_target_capacity25_20261025.json) cũng không đạt: p99 38,052 / 37,113 / 43,447 ms.

Trước sửa bão ghi agent, [ba cửa sổ 50 EPS](../data/http_sla_target_soak_20261025.json) có p99 43,430 / 58,593 / 78,593 ms. Đây là quan sát trước/sau trên máy dùng chung, không phải phép chứng minh nhân quả tách mọi biến môi trường. Bằng chứng trực tiếp cho lỗi ghi lặp nằm ở regression test 65 → 1 lần ghi.

[Baseline đầu lượt](../data/http_sla_target_baseline_20261025.json), [pacing](../data/http_sla_target_pacing_20261025.json), [GIL experiment](../data/http_sla_gil1_20261025.json) và [Selector experiment ngắn](../data/http_sla_selector_20261025.json) được giữ nguyên để đối chiếu. Các lượt 10 giây không được chọn thay cho soak khi công bố kết quả.

[Linux trước tùy chọn checkpoint](../data/http_sla_target_linux_20261025.json) qua 8/8 smoke checks và evidence hợp lệ; scheduled p99 ở 10/50 EPS là 39,779/46,106 ms. [Linux nguồn cuối](../data/http_sla_target_linux_final_20261025.json) cũng qua 8/8 smoke checks và evidence hợp lệ, p99 39,083/41,722 ms; container/volume riêng đã dọn. Container dùng 2 CPU/2 GiB, volume đĩa riêng và nguồn/models chỉ đọc. Lượt ngắn dùng kiểm chức năng, không chứng nhận capacity; SQLite không đặt trên tmpfs.

## Kiểm chứng và môi trường

- **426 tests + 50 subtests pass**, không fail/skip: [JUnit](../runtime/sla_target_20261025/pytest-shipped.xml). Có test rollback, restart, audit failure, scan failure, giải mã evidence chậm không giữ khóa writer và cấu hình checkpoint không làm mất FULL/recovery.
- [Manifest cuối](../runtime/sla_target_20261025/manifest.json) ghi hashes source/test/report, AST parse, phiên bản Python/packages và điều kiện lưu trữ. Model/dataset gốc và các báo cáo trước được giữ nguyên.
- Windows benchmark dùng state TEMP trên **C: SSD** (WDC WDS240G2G0B-00EPW0). Workspace ở **D: HDD** (TOSHIBA MQ02ABD100H), theo Get-Partition/Get-PhysicalDisk. Không suy diễn số đo trên C: thành SLA cho state mặc định trong workspace D:.
- Profile timer trước sửa agent: model p50/p99 khoảng 2,84/8,58 ms; COMMIT có mẫu tới 90,63 ms. Đây là diagnostic riêng, không phải số đo latency cuối. [Phase summary](../runtime/sla_target_20261025/phase_summary.json). File cProfile `owner.pstats` bị lần restart ghi đè và không được dùng để kết luận về workload.
- [Đối chứng wakeup socket](../runtime/sla_target_20261025/wakeup_comparison.json): 4.000 callbacks, p99 0,235–0,283 ms; tắt Nagle trên self-socket không cho lợi ích rõ nên không thay đổi cấu hình socket nội bộ.

SQLite có thể làm checkpoint trong COMMIT, khiến một số commit chậm hơn; [tài liệu WAL chính thức](https://www.sqlite.org/wal.html#performance_considerations) mô tả cơ chế này. Mẫu COMMIT chậm trên máy hiện tại chưa đủ để gán mọi outlier cho checkpoint. Bản này giữ cơ chế checkpoint SQLite hiện có.

## Chạy lại profile và gate

Chọn tên output mới; công cụ bảo toàn báo cáo đã có:

```powershell
python -B runtime/sla_target_20261025/verify_target.py --rates 50,50,50 --seconds 30 --loop selector --wal-pages 1000 --out data/http_sla_check_new.json
```

Gate yêu cầu từng cửa sổ: scheduled HTTP p99 ≤30 ms, ít nhất 99% mọi offered calls được chấm/trả trong 30 ms, accounting hợp lệ, không unexpected enforce; cửa sổ ít nhất 30 giây và 1.000 offers. Tỷ lệ gate tính từ integer counts, không dùng phần trăm đã làm tròn. Phải quan sát ít nhất hai chu kỳ tái sử dụng WAL và xác nhận FULL. Gate không đạt trả mã thoát khác 0 và vẫn lưu đầy đủ report. Đây là quy tắc kiểm thử, không phải chứng nhận production.

Profile phục vụ Windows có thể chọn bằng `FRAUD_WINDOWS_EVENT_LOOP=selector` trước `python run_live.py`. Cấu hình `--state-dir` phải trỏ tới storage đã đo phù hợp, có migration nhất quán nếu chuyển state đang dùng. Selector Windows có giới hạn 512 sockets và không hỗ trợ asyncio subprocess theo [tài liệu Python](https://docs.python.org/3.12/library/asyncio-platforms.html#windows); cần kiểm tra phù hợp với adapter và số kết nối thực tế.

Để chốt SLA cần xác định EPS/burst/concurrency và máy triển khai mục tiêu. Bước còn lại là đo fsync tail và tài nguyên CPU ổn định trên máy đó, chạy lại gate dài qua nhiều checkpoint và dưới tải audio dự kiến. Hiện chưa có cơ sở công bố 30 ms ổn định cho 50 hoặc 100 EPS trên máy này.
