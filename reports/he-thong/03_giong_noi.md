# 3. Pipeline giọng nói (WAV)

## Vai trò
Hệ thống nhận một đoạn WAV đã ký và phân tích nó thành:
- đặc trưng âm học;
- thời lượng nói theo từng bên (nếu có ánh xạ kênh);
- transcript, nếu bật ASR cục bộ.

Sau đó một mô hình riêng chấm điểm và trả về OBSERVE, ALLOW, FLAG hoặc BLOCK. Kết quả được đưa vào runtime chính dưới dạng AUDIO.

## Thành phần
| Module | Nội dung |
|---|---|
| `voice_api.py` | Route upload, kiểm HMAC, slot xử lý đồng thời, job, ASR tuỳ chọn (`FRAUD_ASR_MODEL`, không tự tải weights) |
| `voice_pipeline.py` | `decode_wav`: chỉ nhận PCM 8/16/24/32 bit, tối đa 12 MiB và 30 s. Chuẩn hoá về 16 kHz (`resample_poly`). Khung 25 ms với bước 10 ms. VAD năng lượng. Tính flatness, centroid, drift, pitch và clipping. |
| `voice_model.py` | Imputer median, StandardScaler, logistic regression. Ngưỡng chọn theo Clopper–Pearson. Abstain khi chất lượng kém hoặc khi đầu ra không hợp lệ. |
| `train_voice.py` | Huấn luyện có kiểm rò rỉ theo group, call, speaker và campaign, chia theo thứ tự thời gian. |

## Bản sửa
**F10:** file stereo không có `caller_channel` bị chấm trên kênh 0, mà kênh này có thể là người nghe. Pipeline nay thêm cảnh báo `caller_channel_required`, và mô hình trả OBSERVE với lý do `party_attribution_abstention`.

Test `test_stereo_audio_without_caller_channel_abstains` thất bại trên HEAD.

## Bản sửa vòng 2
**G15.** Trước khi đọc body, hệ thống từ chối các request có:
- `Content-Length` vượt giới hạn (413);
- mốc thời gian hết hạn (401).

Số upload đang đọc cùng lúc bị giới hạn bởi `FRAUD_AUDIO_MAX_UPLOADS` (mặc định 8); vượt thì trả 503 kèm `Retry-After`. HMAC vẫn cần đọc toàn bộ body mới kiểm được.

Test: `test_voice_upload_rejects_oversized_declared_length_before_reading`.

## Còn mở
- Job retry vẫn hiện "expired". Hành vi này an toàn.
- Chưa có corpus WAV thật đã gán nhãn.

## Đã kiểm và thấy đúng
- Kiểm thời lượng trước khi cấp phát bộ nhớ.
- Phát hiện file bị cắt; xử lý byte lẻ ở cuối.
- Giới hạn clipping đúng theo từng độ sâu bit.
- Từ chối file float.
- Âm thanh im lặng cho ra đặc trưng hữu hạn và mô hình abstain.
- Resample stereo đúng với `axis=0`.
- Đặc trưng NaN hoặc vô hạn bị từ chối (riêng pitch được impute).
