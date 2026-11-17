"""Danh bạ số thương mại: miễn đúng thứ phải miễn, giữ đúng thứ phải giữ.

Hồi quy cho một chỗ yếu đo được: tổng đài bán hàng VỪA MỞ, chưa ai gọi lại bao
giờ, bị chặn 120 trên 120 cuộc với điểm rủi ro 0,999 — trong khi tổng đài đang
chạy thì 0 trên 15.952 cuộc bị động tới. Cổng `has_behavioural_evidence` không
cứu được, vì một tổng đài thật sự CÓ bằng chứng hành vi.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commercial_registry import COMMERCIAL_PATTERN_CODES, CommercialRegistry  # noqa: E402
from resilience import FallbackRules, GuardedDetector  # noqa: E402

TONG_DAI = "02873001234"


def call(ts=1000.0, caller=TONG_DAI, imei="35391110000001"):
    return {"event_id": "e1", "event_type": "CALL", "ts": ts, "caller": caller,
            "callee": "0900000001", "imei": imei}


def verdict(decision="BLOCK", codes=("R103", "R104"), calls_1h=80):
    """Kết quả như detector trả về: quyết định, mã lý do, và đặc trưng kèm theo."""
    return {"event_id": "e1", "event_type": "CALL", "ts": 1000.0, "decision": decision,
            "risk": 0.99, "action_vi": "x", "action_en": "x",
            "reasons": [{"code": c, "vi": "", "en": "", "feature": ""} for c in codes],
            "evidence_features": {"calls_1h": calls_1h}}


def fresh():
    reg = CommercialRegistry()
    reg.register(TONG_DAI, "Công ty A", max_calls_per_hour=100, registered_at=0.0)
    return reg


def codes_of(r):
    return [x["code"] for x in r["reasons"]]


# --------------------------------------------------------------- miễn đúng chỗ
def test_chi_co_dau_hieu_goi_hang_loat_thi_cho_qua():
    reg, r = fresh(), verdict("BLOCK", ("R103", "R104", "R109", "R306"))
    reg.apply(call(), r)
    assert r["decision"] == "ALLOW"
    assert r["commercial_exempt"] == "Công ty A"
    assert codes_of(r)[0] == "C001"
    assert reg.stats["exempted"] == 1


def test_gan_co_cung_duoc_ha_xuong_cho_qua():
    reg, r = fresh(), verdict("FLAG", ("R102",))
    reg.apply(call(), r)
    assert r["decision"] == "ALLOW"


def test_moi_hoat_dong_tren_mang_la_tin_hieu_duoc_mien():
    """R306 chính là ngày đầu của chiến dịch — lý do cả 120 cuộc bị chặn."""
    assert "R306" in COMMERCIAL_PATTERN_CODES
    reg, r = fresh(), verdict("BLOCK", ("R306", "R109"))
    reg.apply(call(), r)
    assert r["decision"] == "ALLOW"


# ------------------------------------------------- không miễn tín hiệu gian lận
def test_sim_box_thi_van_chan_du_da_dang_ky():
    reg, r = fresh(), verdict("BLOCK", ("R103", "R202"))      # R202 = nhiều SIM một máy
    reg.apply(call(), r)
    assert r["decision"] == "BLOCK"
    assert codes_of(r)[0] == "C002"
    assert "R202" in r["reasons"][0]["vi"]
    assert reg.stats["kept_fraud_signal"] == 1


def test_gia_mao_so_tong_dai_van_chan():
    reg, r = fresh(), verdict("BLOCK", ("R103", "R502"))
    reg.apply(call(), r)
    assert r["decision"] == "BLOCK"


def test_bat_thuong_khong_giam_sat_van_giu():
    reg, r = fresh(), verdict("FLAG", ("Z001",))
    reg.apply(call(), r)
    assert r["decision"] == "FLAG"


def test_thu_giong_chi_canh_bao_voi_so_da_dang_ky():
    # Quyết định chủ dự án 17/11/2026: số thương mại đã đăng ký chạm trí nhớ thu giọng (A108) chỉ bị
    # gắn cờ, không bị chặn 1 giờ (khoảng 2 % cuộc gọi thật cũng có đoạn im lặng).
    reg, r = fresh(), verdict("BLOCK", ("A108",))
    reg.apply(call(), r)
    assert r["decision"] == "FLAG" and r["reasons"][0]["code"] == "C004"
    assert reg.stats["harvest_downgraded"] == 1


# ------------------------------------------------------------- trần tốc độ
def test_vuot_xa_toc_do_da_khai_thi_thoi_mien():
    reg, r = fresh(), verdict("BLOCK", ("R103",), calls_1h=5000)   # khai 100
    reg.apply(call(), r)
    assert r["decision"] == "BLOCK"
    assert codes_of(r)[0] == "C003"
    assert reg.stats["kept_over_rate"] == 1


def test_trong_bien_do_gap_doi_thi_van_mien():
    reg, r = fresh(), verdict("BLOCK", ("R103",), calls_1h=180)    # <= 2 × 100
    reg.apply(call(), r)
    assert r["decision"] == "ALLOW"


# ----------------------------------------------------- không đăng ký / hết hạn
def test_so_khong_dang_ky_thi_khong_doi_gi():
    reg, r = fresh(), verdict("BLOCK", ("R103",))
    reg.apply(call(caller="0999999999"), r)
    assert r["decision"] == "BLOCK"
    assert codes_of(r) == ["R103"]


def test_dang_ky_het_han_thi_khong_con_mien():
    reg = CommercialRegistry()
    reg.register(TONG_DAI, "Công ty A", 100, registered_at=0.0, expires_at=500.0)
    r = verdict("BLOCK", ("R103",))
    reg.apply(call(ts=1000.0), r)
    assert r["decision"] == "BLOCK"


def test_thu_hoi_dang_ky():
    reg = fresh()
    assert reg.revoke(TONG_DAI) is True
    r = verdict("BLOCK", ("R103",))
    reg.apply(call(), r)
    assert r["decision"] == "BLOCK"


def test_quyet_dinh_cho_qua_thi_khong_dong_vao():
    reg, r = fresh(), verdict("ALLOW", ())
    reg.apply(call(), r)
    assert r["decision"] == "ALLOW"
    assert "commercial_exempt" not in r


# --------------------------------------------- người vẫn thắng đăng ký
def test_chan_nhom_do_chuyen_vien_duyet_thang_dang_ky():
    """Đăng ký không được biến một số thành không thể chặn."""
    class Fake:
        def score_batch(self, evs):
            return [verdict("BLOCK", ("R103",)) for _ in evs]

    fb = FallbackRules()
    fb.block_manual([TONG_DAI], ref="PROP-1")
    gd = GuardedDetector(Fake(), budget_ms=1e9, fallback=fb, registry=fresh())
    r = gd.score_batch([call()])[0]
    assert r["decision"] == "BLOCK"
    assert r.get("human_override") == "group_block"


def test_chay_qua_guarded_detector_thi_duoc_mien():
    class Fake:
        def score_batch(self, evs):
            return [verdict("BLOCK", ("R103", "R109")) for _ in evs]

    gd = GuardedDetector(Fake(), budget_ms=1e9, fallback=FallbackRules(), registry=fresh())
    r = gd.score_batch([call()])[0]
    assert r["decision"] == "ALLOW"
    assert codes_of(r)[0] == "C001"


def test_khong_truyen_registry_thi_van_chay_binh_thuong():
    class Fake:
        def score_batch(self, evs):
            return [verdict("BLOCK", ("R103",)) for _ in evs]

    gd = GuardedDetector(Fake(), budget_ms=1e9, fallback=FallbackRules())
    assert len(gd.registry) == 0
    assert gd.score_batch([call()])[0]["decision"] == "BLOCK"


# ------------------------------------------------------------- phía khách hàng
def test_khach_hang_khong_doc_ma_noi_bo_nhung_van_doc_duoc_C002():
    import reason_codes as rc
    reg, r = fresh(), verdict("BLOCK", ("R103", "R202"))
    reg.apply(call(), r)
    seen = {x["code"] for x in rc.for_subscriber(r["reasons"])}
    assert "C002" in seen            # "số có đăng ký nhưng còn dấu hiệu lừa đảo"
    r2 = verdict("BLOCK", ("R103",))
    fresh().apply(call(), r2)
    assert "C001" not in {x["code"] for x in rc.for_subscriber(r2["reasons"])}
