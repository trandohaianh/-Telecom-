"""Human-readable reason codes (bilingual) mapped from model feature contributions."""

# CALL_REASONS ánh xạ tên feature sang mã lý do tiếng Việt / Anh để giải thích risk của cuộc gọi.
CALL_REASONS = {
    "calls_1m": ("R101", "Tần suất gọi đột biến trong 1 phút", "Call burst within 1 minute"),
    "calls_10m": ("R102", "Số cuộc gọi đi cao bất thường trong 10 phút", "Unusually many outgoing calls in 10 minutes"),
    "calls_1h": ("R103", "Lưu lượng gọi đi lớn trong 1 giờ", "High outgoing call volume in 1 hour"),
    "callee_dispersion_1h": ("R104", "Gọi tới rất nhiều số khác nhau (không có danh bạ quen)", "Calls spread over many distinct numbers"),
    # Hai tín hiệu khác nhau, hai mã riêng: tỉ lệ gọi đi/gọi đến lệch, và số cuộc gọi đến tuyệt đối quá ít.
    "in_out_ratio_24h": ("R105", "Gọi đi rất nhiều nhưng gần như không ai gọi lại (tỉ lệ đi/đến lệch)",
                         "Calls out far more than it receives (skewed out/in ratio)"),
    "incoming_24h": ("R109", "Gần như không nhận cuộc gọi đến trong 24 giờ", "Almost no incoming calls in 24 hours"),
    "short_call_ratio": ("R106", "Tỷ lệ cuộc gọi cực ngắn cao (đổ chuông rồi tắt)", "High share of very short calls (ring-and-drop)"),
    "mean_log_duration": ("R107", "Thời lượng cuộc gọi bất thường", "Unusual call durations"),
    "completed_calls_seen": ("R108", "Lịch sử cuộc gọi bất thường", "Unusual call history"),
    "imei_per_msisdn_24h": ("R201", "Một số thuê bao dùng nhiều IMEI trong 24h", "One number used on several IMEIs in 24h"),
    "msisdn_per_imei_24h": ("R202", "Nhiều SIM cùng chạy trên một IMEI (dấu hiệu SIM Box)", "Many SIMs on one IMEI (SIM-box pattern)"),
    "imei_gateway_tac": ("R203", "Thiết bị thuộc dòng GSM Gateway", "Device model is a GSM gateway"),
    "cell_registered": ("R301", "Phát từ cell không có trong danh mục nhà mạng", "Originates from an unregistered cell"),
    "cell_unknown_radio": ("R302", "Trạm phát sóng lạ (nghi BTS giả)", "Unknown radio cell (possible fake BTS)"),
    "cell_trunk_or_intl": ("R303", "Đi vào qua trung kế / quốc tế", "Enters via trunk / international route"),
    "velocity_kmh": ("R304", "Di chuyển giữa các trạm nhanh phi lý", "Impossible travel between cells"),
    "new_sims_on_cell_1h": ("R305", "Nhiều SIM mới (mới kích hoạt hoặc mới xuất hiện) cùng hoạt động tại một cell",
                            "Many new SIMs (newly activated or newly seen) active on one cell"),
    "active_hours_on_network": ("R306", "Số mới bắt đầu hoạt động trên mạng dù đăng ký đã lâu (nghi SIM ngủ đông / đi thuê)",
                                "Number only just started being active despite an old registration (dormant / rented SIM)"),
    "caller_age_days": ("R401", "Thuê bao mới kích hoạt", "Newly activated subscription"),
    "caller_foreign": ("R402", "Số nước ngoài", "Foreign number"),
    "origin_local": ("R403", "Tuyến gốc bất thường", "Unusual origin route"),
    "origin_intl_gw": ("R404", "Đi vào qua cổng quốc tế", "Arrives through an international gateway"),
    "origin_bank_trunk": ("R405", "Không đi qua trung kế ngân hàng", "Not via the bank trunk"),
    "cli_protected": ("R501", "Hiển thị số tổng đài ngân hàng/cơ quan", "Presents a bank / agency hotline"),
    "protected_cli_mismatch": ("R502", "Giả mạo số tổng đài: số được bảo vệ nhưng không đến từ trung kế chính chủ", "Spoofed hotline: protected number not from its owner's trunk"),
    "network_2g": ("R503", "Kết nối qua 2G (dễ bị BTS giả lợi dụng)", "2G attach (exploitable by fake BTS)"),
    "hour_of_day": ("R601", "Hoạt động vào khung giờ bất thường", "Activity at an unusual hour"),
}

# SMS_REASONS tập mã lý do riêng cho SMS, tập trung vào tuyến gửi và dấu hiệu lừa đảo.
SMS_REASONS = {
    "route_unverified_bts": ("S101", "Tin phát từ trạm chưa xác thực (nghi BTS giả)", "Broadcast from an unverified cell (possible fake BTS)"),
    "brand_not_via_gateway": ("S102", "Mang tên thương hiệu nhưng không qua cổng Brandname", "Brand sender name not delivered via the brandname gateway"),
    "url_unofficial": ("S103", "Chứa đường link ngoài tên miền chính thức", "Contains a link outside official domains"),
    "sender_sms_1h": ("S104", "Số gửi phát tán nhiều tin trong 1 giờ", "Sender is blasting many SMS in 1 hour"),
    "route_p2p": ("S105", "Gửi từ thuê bao cá nhân", "Sent from a personal number"),
    "cell_registered": ("S106", "Cell phát không có trong danh mục", "Cell not in the operator registry"),
    "text": ("S201", "Nội dung giống mẫu lừa đảo", "Text matches phishing patterns"),
}

# AUDIO_REASONS lưu mã lý do cho audio / giọng nói, gắn với vocoder và scam script.
AUDIO_REASONS = {
    "vocoder_artifact": ("A101", "Dấu vết vocoder của giọng tổng hợp AI", "Vocoder artefacts typical of synthetic speech"),
    "pitch_jitter": ("A102", "Độ rung cao độ phẳng bất thường", "Unnaturally flat pitch jitter"),
    "spectral_flatness": ("A103", "Phổ âm bất thường", "Unusual spectral flatness"),
    "speaker_drift": ("A104", "Đặc trưng giọng trôi giữa các đoạn", "Speaker embedding drifts between chunks"),
    "codec_2g": ("A105", "Codec 2G", "2G codec"),
    "scam_lexicon_hits": ("A201", "Hội thoại có kịch bản lừa đảo (OTP, chuyển tiền gấp, công an...)", "Scam script keywords (OTP, urgent transfer, police...)"),
    "caller_age_days": ("A301", "Số gọi là thuê bao mới", "Caller is a new subscription"),
    "caller_known": ("A302", "Số gọi chưa có lịch sử", "Caller has no history"),
    "caller_speech_ratio": ("A106", "Người gọi im lặng suốt cuộc gọi (nghi gọi để thu giọng người nghe)",
                            "Caller stays silent through the call (suspected voice harvesting)"),
    "callee_speech_s": ("A107", "Giọng người nghe bị ghi lại nhiều giây trong khi bên gọi không nói",
                        "Several seconds of the callee's voice captured while the caller says nothing"),
}

# NEED_EVIDENCE: nguyên tắc chặn - "mới" thôi thì không đủ để chặn cứng, phải có bằng chứng hành vi đi kèm.
NEED_EVIDENCE = ("P001", "Chưa đủ bằng chứng hành vi để chặn cứng (chỉ là số mới / ít lịch sử): cho qua kèm cảnh báo",
                 "Not enough behavioural evidence for a hard block (only new / little history): allowed with a warning")
# HARVEST_MEMORY: số đã bị bắt im lặng thu giọng ở nhiều người khác nhau -> chặn các cuộc tiếp theo trước khi đổ chuông.
HARVEST_MEMORY = ("A108", "Số này đã im lặng thu giọng ở ít nhất 2 người khác nhau trong 1 giờ: chặn / ngắt cuộc gọi",
                  "This number silently recorded 2+ different people within an hour: call blocked / cut")
# ZERO_DAY cảnh báo khi phát hiện hành vi rất lạ nhưng không thuộc mẫu đã biết.
ZERO_DAY = ("Z001", "Mô hình không giám sát: hành vi lệch xa lưu lượng bình thường (nghi tấn công mới)",
            "Unsupervised model: behaviour far from normal traffic (possible new attack)")
# FAIL_OPEN nhấn mạnh việc cho qua lưu lượng để giữ SLA khi detector timeout hoặc lỗi.
FAIL_OPEN = ("F001", "Fail-open: bộ phát hiện quá hạn/ lỗi, cho qua để không gián đoạn dịch vụ",
             "Fail-open: detector timed out / failed, traffic allowed to protect service continuity")
# Luật dự phòng chạy khi mô hình không trả lời kịp: chỉ tra cứu bảng, không cần mô hình.
FALLBACK_DENYLIST = ("F002", "Chế độ dự phòng: số / thiết bị thuộc nhóm đã bị chặn trước đó",
                     "Fallback mode: number / device belongs to a group already blocked")
FALLBACK_SPOOF = ("F003", "Chế độ dự phòng: hiển thị số tổng đài được bảo vệ nhưng không đi qua trung kế chính chủ",
                  "Fallback mode: protected hotline presented but not from its owner's trunk")
FALLBACK_BRAND = ("F004", "Chế độ dự phòng: tên thương hiệu nhưng không qua cổng Brandname",
                  "Fallback mode: brand sender name not delivered via the brandname gateway")
# Quyết định của con người (do AI agent đề xuất, chuyên viên phê duyệt) ghi đè kết quả mô hình.
GROUP_BLOCK = ("G001", "Chặn theo nhóm: chuyên viên đã phê duyệt chặn nhóm số / thiết bị này",
               "Group block: an analyst approved blocking this group of numbers / devices")
HUMAN_ALLOW = ("G002", "Chuyên viên đã xác nhận số hợp lệ: hạ từ chặn xuống cảnh báo",
               "An analyst confirmed this number is legitimate: downgraded from block to warning")
# Số thương mại đã đăng ký gọi ra hàng loạt (xem commercial_registry.py). Ba mã, vì ba
# kết cục khác nhau phải phân biệt được trong sổ bằng chứng.
COMMERCIAL_REGISTERED = ("C001", "Số thương mại đã đăng ký, chỉ có dấu hiệu gọi ra hàng loạt: cho qua",
                         "Registered commercial caller, only bulk-outbound signals: allowed")
COMMERCIAL_FRAUD_SIGNAL = ("C002", "Số thương mại đã đăng ký NHƯNG dính dấu hiệu gian lận ngoài phạm vi đăng ký: giữ nguyên quyết định, ưu tiên xem xét",
                           "Registered commercial caller BUT hit a fraud signal outside the registration: decision kept, priority review")
COMMERCIAL_HARVEST = ("C004", "Số thương mại đã đăng ký có nhiều đoạn im lặng (trí nhớ thu giọng): chỉ cảnh báo, không chặn 1 giờ",
                      "Registered commercial number with silent segments (harvest memory): flagged, not blocked for an hour")
COMMERCIAL_OVER_RATE = ("C003", "Số thương mại đã đăng ký nhưng gọi vượt xa tốc độ đã khai: không áp dụng miễn trừ",
                        "Registered commercial caller far above its declared rate: exemption not applied")
# RETRO đánh dấu kết quả chấm lại sau sự cố: cuộc gọi đã qua, hành động là gắn cờ số và báo người nhận.
RETRO = ("F010", "Chấm lại sau sự cố: sự kiện đã được cho qua lúc fail-open, nay mô hình kết luận nghi lừa đảo",
         "Re-scored after an outage: the event was allowed during fail-open; the model now flags it")


# render lọc và sắp xếp các feature có đóng góp lớn thành chuỗi lý do không trùng mã.
def render(catalog, names):
    out, seen = [], set()
    for n in names:
        if n in catalog and catalog[n][0] not in seen:
            seen.add(catalog[n][0])
            code, vi, en = catalog[n]
            out.append({"code": code, "vi": vi, "en": en, "feature": n})
    return out


# --------------------------------------------------------------------- thuê bao
# Lý do hiển thị trong app của thuê bao khác với lý do hiển thị cho chuyên viên
# trực ca. Chuyên viên cần biết bộ phát hiện đã quá hạn 31,7 ms hay luật dự phòng
# nào đã chạy; khách hàng thì không — với họ đó là chuyện nội bộ của nhà mạng, và
# đọc được nó chỉ gây hoang mang chứ không giúp họ an toàn hơn. Hai bảng dưới đây
# quyết định mã nào không nói với thuê bao, và mã nào nói lại bằng lời thường.
SUBSCRIBER_HIDE = {
    "F001",   # fail-open: bộ phát hiện quá hạn / lỗi — chuyện vận hành
    "P001",   # chính sách nội bộ về mức bằng chứng cần có để chặn cứng
    "C001",   # số thương mại đã đăng ký — chuyện giữa nhà mạng và doanh nghiệp
    "C003",   # vượt tốc độ đã khai — như trên
}

SUBSCRIBER_TEXT = {
    "F002": ("Số hoặc thiết bị này nằm trong nhóm đã bị chặn trước đó",
             "This number or device is in a group that was blocked earlier"),
    "F003": ("Cuộc gọi hiện số tổng đài, nhưng không xuất phát từ tổng đài đó",
             "The call shows a hotline number but did not come from that hotline"),
    "F004": ("Tin nhắn mang tên thương hiệu nhưng không gửi qua kênh chính thức",
             "The message carries a brand name but was not sent through the official channel"),
    "F010": ("Sự kiện này lọt qua lúc hệ thống gặp sự cố; kiểm tra lại cho thấy có dấu hiệu lừa đảo",
             "This event slipped through during an outage; on re-check it shows signs of fraud"),
    "Z001": ("Cách số này hoạt động khác hẳn một thuê bao bình thường",
             "This number behaves very differently from an ordinary subscriber"),
    "G001": ("Chuyên viên của nhà mạng đã chặn nhóm số / thiết bị này",
             "An operator analyst blocked this group of numbers / devices"),
    "G002": ("Chuyên viên của nhà mạng đã xác nhận số này hợp lệ",
             "An operator analyst confirmed this number is legitimate"),
    "C002": ("Số này có đăng ký gọi quảng cáo, nhưng cuộc gọi còn có dấu hiệu lừa đảo khác",
             "This number is registered for outbound campaigns, but the call also shows signs of fraud"),
}


def for_subscriber(reasons, lang="vi"):
    """Các lý do một khách hàng được đọc, bằng lời của khách hàng.

    Bỏ hẳn mã thuộc SUBSCRIBER_HIDE, và thay lời của mã nào có bản viết lại.
    Mã còn lại giữ nguyên: chúng vốn đã nói về chính cuộc gọi đó.
    """
    out = []
    for r in reasons or []:
        code = r.get("code")
        if code in SUBSCRIBER_HIDE:
            continue
        if code in SUBSCRIBER_TEXT:
            vi, en = SUBSCRIBER_TEXT[code]
            out.append({**r, "vi": vi, "en": en})
        else:
            out.append(r)
    return out
