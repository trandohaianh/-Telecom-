# Hướng dẫn mở, chạy và cập nhật dự án

Dành cho người mới cầm dự án (kể cả không chuyên lập trình). Mọi lệnh dưới đây gõ trong **Terminal của VS Code**
(menu *Terminal → New Terminal*), đứng ở thư mục `v2`.

---

## 1. Cài đặt lần đầu (làm một lần)

| Cần có | Kiểm tra | Ghi chú |
|---|---|---|
| Python 3.11 trở lên | `python --version` | Máy này đang có 3.12 |
| Thư viện Python | `pip install -r requirements.txt` | Khoảng 5 phút |
| (Tùy chọn) Node.js | `node --version` | Chỉ cần khi tạo lại báo cáo Word / slide |
| (Tùy chọn) bộ Android | đã cài sẵn ở `D:\AndroidDev` | Chỉ cần khi chạy app trên máy ảo Android |

Mở dự án: VS Code → *File → Open Folder* → chọn `D:\Du_An_1_Telecom_AI_Full\v2`.

---

## 2. Chạy demo (dashboard + app)

```
python run_demo.py
```

Chờ khoảng 20 giây đến khi terminal in ra địa chỉ và **mã đăng nhập**:

```
Dashboard vận hành : http://localhost:8080
App thuê bao       : http://localhost:8080/app/
Token đăng nhập dashboard:
  admin    viettel_admin  ....
  analyst  analyst_1      ....
  viewer   viewer_1       ....
  viewer   nhanvien_01    ....
```

- **Dashboard vận hành:** mở `http://localhost:8080`, dán một mã ở trên vào ô đăng nhập.
  - `viettel_admin` (quản trị nhà mạng): thấy mọi thứ, duyệt đề xuất của AI agent, mô phỏng tấn công, diễn tập sự cố.
  - `analyst_1` (chuyên viên): xem bằng chứng, mở số điện thoại đầy đủ (phải ghi lý do).
  - `viewer_1`, `nhanvien_01` (nhân viên): chỉ xem; số điện thoại luôn bị che.
  - Mã chỉ dùng để đăng nhập; sau đó bạn được cấp **phiên** tự hết hạn sau 8 giờ hoặc 30 phút không thao tác.
  - Nút gạt ☾/☀ trên đầu trang: đổi giao diện tối / sáng.
- **App thuê bao:** mở `http://localhost:8080/app/`, chọn một số demo, bấm *Đăng nhập*.
- **App trên điện thoại thật** (cùng Wi-Fi): mở `http://<IP máy tính>:8080/app/` (xem IP bằng lệnh `ipconfig`),
  rồi *Thêm vào màn hình chính*. Nếu không vào được: Windows Firewall đang chặn cổng 8080.
- **Tắt demo:** bấm `Ctrl + C` trong terminal.

Mã đăng nhập được tạo ngẫu nhiên lần đầu và lưu trong `secrets/users.json`. **Không gửi file này cho ai.**

---

## 3. Thử nhanh các tính năng

| Muốn xem | Làm gì |
|---|---|
| Hệ thống chặn lừa đảo | Dashboard (admin) → *Mô phỏng tấn công* → bấm một kiểu (SIM box, giả mạo số tổng đài, SMS lừa đảo, giọng nói deepfake, cuộc gọi im lặng thu giọng, Wangiri) |
| AI agent đề xuất, con người quyết định | Bấm *SIM BOX* → chờ vài giây → ô *AI agent* hiện đề xuất "Chặn cả nhóm" → bấm *DUYỆT* và ghi lý do |
| Chịu lỗi | *Kiểm thử chịu lỗi* → *MÔ HÌNH LỖI* → bấm *GIẢ MẠO SỐ TỔNG ĐÀI* (vẫn bị chặn nhờ luật dự phòng) → *KHÔI PHỤC* |
| Ai đã làm gì | *Nhật ký thao tác* → *XEM & KIỂM TRA NHẬT KÝ* |
| Xem số đầy đủ (chuyên viên) | Đăng nhập `analyst_1` → bấm một thẻ cảnh báo → *XEM SỐ ĐẦY ĐỦ* → nhập lý do |
| Cảnh báo trên điện thoại | App → *Thử nghiệm* → bấm một kiểu cuộc gọi / tin nhắn |
| Bảo vệ người thân | Mở app ở 2 tab với 2 số demo khác nhau → tab 1 mời số của tab 2 → tab 2 bấm *Đồng ý* → tab 2 bấm *SMS lừa đảo* → tab 1 nhận cảnh báo |

---

## 4. Chạy app trên máy ảo Android

Chạy demo trước (mục 2), rồi mở **terminal thứ hai**:

```
powershell -ExecutionPolicy Bypass -File android\run_android.ps1
```

Script tự build file APK, bật máy ảo Pixel 6 (Android 14), cài và mở app *Lá chắn*. Lần đầu mất 5–8 phút
(máy ảo khởi động khoảng 3 phút). File APK để cài lên điện thoại Android thật:
`android\app\build\outputs\apk\debug\app-debug.apk` (trong app, đổi *Máy chủ* thành `http://<IP máy tính>:8080`).

---

## 5. Kiểm tra code (test tự động)

```
python -m pytest -q tests
```

Kết quả mong đợi: tất cả `passed` (khoảng 15 giây).

---

## 6. Dựng lại dữ liệu và mô hình

Chỉ cần khi sửa bộ giả lập, đặc trưng hoặc mô hình. Chạy lần lượt (tổng khoảng 15–20 phút):

```
python src/build_dataset.py        # sinh dữ liệu giả lập (train / val / test)
python src/train.py                # huấn luyện mô hình, chọn ngưỡng
python src/evaluate.py             # đo trên 50k + 10×50k + bài test kiểu lừa mới -> data/benchmark_results.json
python src/make_drift_baseline.py  # mốc theo dõi trôi dữ liệu
python src/shadow_run.py           # chạy song song không thực thi
python src/experiment_guard.py     # thí nghiệm gửi trùng / đến muộn
python src/loadtest.py --partitions 1   # kiểm thử tải (xem mục 7)
```

Trước khi huấn luyện lại, nên sao lưu `models/` và `data/*.json` (đã có bản `backup_tfd-v2.0/` của phiên bản trước).
Khi đang huấn luyện, **tắt demo** (Ctrl + C) để không tranh CPU.

---

## 7. Đo hiệu năng (độ trễ, thông lượng)

Kết quả đo phụ thuộc máy. Để đo đúng:
1. Tắt demo và các chương trình nặng (trình duyệt nhiều tab, máy ảo Android).
2. Chạy: `python src/loadtest.py --partitions 1`, rồi `--partitions 2`, rồi `--partitions 4` (máy 4 nhân).
3. Kết quả ghi vào `data/loadtest_results.json`; báo cáo và dashboard đọc file này.

Mỗi tiến trình chấm điểm dùng **một** nhân CPU; muốn nhanh hơn thì chạy nhiều tiến trình (`--partitions`),
mỗi tiến trình lo một nhóm thuê bao.

---

## 8. Tạo lại báo cáo Word / PDF và slide

Số liệu trong báo cáo được đọc tự động từ `data/*.json`, nên sau khi đo lại chỉ cần tạo lại file:

```
cd docs\build
npm install                    # lần đầu: cài thư viện tạo Word / PowerPoint
node report.js vi              # -> docs/Telecom_Fraud_Report_v2_VI.docx
node report.js en              # -> docs/Telecom_Fraud_Report_v2_EN.docx
node slides.js vi              # -> docs/Telecom_Fraud_Slides_v2_VI.pptx
node slides.js en
python figures.py              # vẽ lại biểu đồ trong docs/fig từ số đo mới
powershell -ExecutionPolicy Bypass -File to_pdf.ps1   # chuyển Word / PowerPoint sang PDF (cần Microsoft Office)
```

Muốn sửa nội dung chữ: mở `docs/build/report.js` (báo cáo) hoặc `slides.js` (slide); mỗi đoạn có hai bản,
`T('tiếng Việt', 'English')`.

---

## 9. Lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| `Address already in use` / cổng 8080 bận | Đã có một demo đang chạy: tắt nó (Ctrl + C ở terminal đó) hoặc khởi động lại máy |
| Dashboard báo *Phiên đăng nhập đã hết hạn* | Bình thường sau 30 phút không thao tác: đăng nhập lại bằng mã trong `secrets/users.json` |
| Dashboard trắng, không có số | Demo chưa chạy hoặc đang khởi động: chờ 20 giây rồi tải lại trang |
| Máy ảo Android tự tắt khi bật | Card đồ họa cũ: script đã dùng chế độ `-gpu swiftshader_indirect`; nếu vẫn lỗi, bật *Windows Hypervisor Platform* trong *Turn Windows features on or off* |
| Chữ tiếng Việt bị lỗi font trong file | Không mở / lưu file bằng lệnh `Get-Content` / `Set-Content` của PowerShell 5; dùng VS Code |
| `pip install` lỗi | Cập nhật pip: `python -m pip install --upgrade pip` rồi chạy lại |

---

## 10. Thư mục trong dự án

| Thư mục / file | Chứa gì |
|---|---|
| `src/` | Toàn bộ mã nguồn hệ thống (giả lập, đặc trưng, mô hình, server, AI agent, bảo mật) |
| `static/` | Giao diện dashboard (`index.html`) và app thuê bao (`app/`) |
| `android/` | Project app Android + script `run_android.ps1` |
| `models/` | Mô hình đã huấn luyện (`tfd-v2.1`) và ngưỡng quyết định |
| `data/` | Dữ liệu giả lập và kết quả đo (`benchmark_results.json`, `loadtest_results.json`, ...) |
| `tests/` | Test tự động |
| `docs/` | Báo cáo, slide, video demo; `docs/build/` là mã tạo ra chúng |
| `secrets/` | Mã đăng nhập, khóa tích hợp nhà mạng (**không chia sẻ**) |
| `logs/` | Nhật ký bằng chứng và nhật ký thao tác của lần chạy demo |
| `backup_tfd-v2.0/` | Mô hình và kết quả của phiên bản trước, để so sánh |
| `README.md` | Tóm tắt kỹ thuật và kết quả |
