"""Danh bạ số thương mại đã đăng ký gọi ra hàng loạt (tổng đài bán hàng, CSKH, khảo sát).

Vì sao cần: ở tầng báo hiệu, một tổng đài telesales hợp lệ và một SIM box trông
giống nhau về khối lượng — đo trên dữ liệu mô phỏng thì tổng đài còn gọi NHIỀU
HƠN: 65 cuộc/giờ so với 4,5. Thứ tách hai bên là "có ai gọi lại không" và "số này
đã hoạt động trên mạng bao lâu". Nên một tổng đài ĐANG chạy thì an toàn (đo được:
0 trên 15.952 cuộc bị chặn hoặc gắn cờ), nhưng một tổng đài VỪA MỞ, chưa ai gọi
lại bao giờ, thì bị chặn sạch — 120 trên 120 cuộc, điểm rủi ro 0,999.

Cổng `has_behavioural_evidence` không cứu được trường hợp đó, vì một tổng đài
thật sự CÓ bằng chứng hành vi: 81 cuộc một giờ. Cổng ấy bảo vệ người dùng thường,
không bảo vệ doanh nghiệp gọi ra nhiều. Đây là việc của vận hành, không phải của
mô hình: doanh nghiệp đăng ký trước thì số của họ được miễn.

Nguyên tắc của việc miễn, và đây là phần quan trọng nhất của file này:

1. Chỉ miễn các tín hiệu THUỘC VỀ BẢN CHẤT GỌI RA HÀNG LOẠT — gọi nhiều, gọi
   nhiều số khác nhau, ít ai gọi lại, mới hoạt động. Đó đúng là mô tả công việc
   của một tổng đài.
2. KHÔNG BAO GIỜ miễn các tín hiệu chỉ đích danh gian lận — nhiều SIM trên một
   máy, thiết bị dòng GSM gateway, trạm phát lạ, giả mạo số tổng đài ngân hàng,
   im lặng thu giọng, hay bất thường do mô hình không giám sát phát hiện. Một số
   đã đăng ký mà dính những thứ này thì hoặc đăng ký bị lợi dụng, hoặc số bị
   chiếm — cả hai đều nguy hiểm HƠN một số lạ, nên vẫn chặn và đánh dấu ưu tiên.
3. Đăng ký có trần tốc độ. Vượt xa mức đã khai thì thôi miễn: một tổng đài khai
   100 cuộc/giờ mà đang gọi 5.000 cuộc/giờ không còn là cái đã được duyệt nữa.

Đây là danh bạ trong bộ nhớ, đủ cho bản demo. Chạy thật thì nó là một bảng có
kiểm soát truy cập, và mỗi lần thêm/sửa/xoá đều phải vào nhật ký kiểm toán.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import reason_codes as rc

# Mã lý do mô tả "đang gọi ra hàng loạt" — đúng công việc của một tổng đài đã đăng ký.
# Giữ bằng mã (R102...) chứ không bằng tên đặc trưng: mã là thứ đi vào sổ bằng chứng
# và hiện ra cho chuyên viên, nên quy tắc miễn đọc được bằng đúng ngôn ngữ đó.
COMMERCIAL_PATTERN_CODES = frozenset(
    {
        "R101",  # tần suất gọi đột biến trong 1 phút
        "R102",  # nhiều cuộc gọi đi trong 10 phút
        "R103",  # lưu lượng gọi đi lớn trong 1 giờ
        "R104",  # gọi tới rất nhiều số khác nhau
        "R105",  # tỉ lệ gọi đi / gọi đến lệch
        "R106",  # tỷ lệ cuộc cực ngắn cao (khách không nhấc máy)
        "R107",  # thời lượng cuộc gọi bất thường
        "R108",  # lịch sử cuộc gọi bất thường
        "R109",  # gần như không có cuộc gọi đến
        "R306",  # mới bắt đầu hoạt động trên mạng — chính là ngày đầu của chiến dịch
        "R401",  # thuê bao mới kích hoạt
        "R601",  # hoạt động vào khung giờ bất thường
        "P001",  # ghi chú chính sách về mức bằng chứng, không phải tín hiệu gian lận
    }
)


@dataclass(frozen=True)
class Registration:
    number: str
    enterprise: str
    max_calls_per_hour: int
    registered_at: float
    expires_at: float | None = None

    def active_at(self, ts: float) -> bool:
        if ts < self.registered_at:
            return False
        return self.expires_at is None or ts < self.expires_at


class CommercialRegistry:
    """Tra cứu O(1) trên đường quyết định; thêm/xoá hiếm nên dùng một khoá chung."""

    def __init__(self):
        self._by_number: dict[str, Registration] = {}
        self._lock = threading.Lock()
        self.stats = {"exempted": 0, "kept_fraud_signal": 0, "kept_over_rate": 0, "harvest_downgraded": 0}

    # ------------------------------------------------------------------ quản trị
    def register(
        self,
        number: str,
        enterprise: str,
        max_calls_per_hour: int,
        registered_at: float = 0.0,
        expires_at: float | None = None,
    ) -> Registration:
        reg = Registration(str(number), enterprise, int(max_calls_per_hour), float(registered_at), expires_at)
        with self._lock:
            self._by_number[reg.number] = reg
        return reg

    def revoke(self, number: str) -> bool:
        with self._lock:
            return self._by_number.pop(str(number), None) is not None

    def lookup(self, number: str, ts: float) -> Registration | None:
        reg = self._by_number.get(str(number))
        return reg if reg is not None and reg.active_at(ts) else None

    def __len__(self) -> int:
        return len(self._by_number)

    # ------------------------------------------------------------------ áp dụng
    def apply(self, ev: dict, result: dict) -> None:
        """Hạ quyết định xuống CHO QUA nếu số đã đăng ký và chỉ dính tín hiệu thương mại.

        Sửa tại chỗ ``result``. Mọi trường hợp đều để lại một mã lý do, kể cả khi
        KHÔNG miễn — chuyên viên cần thấy số này có đăng ký, nhất là lúc nó bị chặn.
        """
        if result.get("decision") == "ALLOW" or result.get("event_type") != "CALL":
            return
        reg = self.lookup(ev.get("caller") or "", ev.get("ts", 0.0))
        if reg is None:
            return

        reasons = result.get("reasons") or []
        codes = {r.get("code") for r in reasons}

        # 3. Trần tốc độ: vượt xa mức đã khai thì đăng ký không còn mô tả đúng số này nữa.
        rate = (result.get("evidence_features") or {}).get("calls_1h")
        if rate is not None and rate > 2 * reg.max_calls_per_hour:
            self.stats["kept_over_rate"] += 1
            self._prepend(
                result,
                rc.COMMERCIAL_OVER_RATE,
                extra=f"{reg.enterprise}: {int(rate)} > 2 × {reg.max_calls_per_hour} cuộc/giờ",
            )
            return

        # Trí nhớ thu giọng (A108): ~2 % cuộc gọi thật cũng có đoạn im lặng, nên một tổng đài đã đăng ký
        # gọi hàng loạt sẽ chạm ngưỡng "2 nạn nhân / giờ" rất nhanh. Với số đã đăng ký: hạ BLOCK → FLAG
        # (vẫn cảnh báo, vẫn để chuyên viên xem), không cắt mọi cuộc gọi của doanh nghiệp trong 1 giờ.
        if "A108" in codes and result.get("decision") == "BLOCK":
            self.stats["harvest_downgraded"] += 1
            result["decision"] = "FLAG"
            from detector import ACTIONS

            result["action_vi"], result["action_en"] = ACTIONS.get(
                (result.get("event_type"), "FLAG"), (result.get("action_vi"), result.get("action_en"))
            )
            self._prepend(result, rc.COMMERCIAL_HARVEST, extra=reg.enterprise)
            return

        # 2. Tín hiệu chỉ đích danh gian lận: giữ nguyên quyết định, đánh dấu ưu tiên.
        fraud_codes = codes - COMMERCIAL_PATTERN_CODES
        if fraud_codes:
            self.stats["kept_fraud_signal"] += 1
            self._prepend(
                result, rc.COMMERCIAL_FRAUD_SIGNAL, extra=f"{reg.enterprise}: {', '.join(sorted(fraud_codes))}"
            )
            return

        # 1. Chỉ có tín hiệu thương mại: miễn.
        self.stats["exempted"] += 1
        result["decision"] = "ALLOW"
        result["commercial_exempt"] = reg.enterprise
        self._prepend(result, rc.COMMERCIAL_REGISTERED, extra=reg.enterprise)
        from detector import ACTIONS

        result["action_vi"], result["action_en"] = ACTIONS[("CALL", "ALLOW")]

    @staticmethod
    def _prepend(result: dict, code_tuple, extra: str = "") -> None:
        code, vi, en = code_tuple
        suffix = f" [{extra}]" if extra else ""
        result["reasons"] = [{"code": code, "vi": vi + suffix, "en": en + suffix, "feature": "commercial_registry"}] + (
            result.get("reasons") or []
        )
