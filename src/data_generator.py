"""Synthetic telecom traffic simulator (v2).

Design goals, in contrast to v1:
* Events are produced by *actors* (subscribers, call centres, banks, fraud rings) over simulated
  time. The detector never receives behavioural counters such as "calls in the past hour" from the
  generator: every behavioural feature is computed causally by the feature store from the stream.
* Legitimate and fraudulent behaviour overlap on purpose (call centres look like SIM boxes,
  travellers look like "impossible travel", banks really do send SMS with URLs, 2G codecs look like
  vocoder artefacts ...), so a perfect score is not achievable by construction.
* Fraud is rare (~0.1 % of events in the evaluation window).
* The evaluation window contains *new* fraud actors, new phishing lures and one attack family that
  never appears in training (Wangiri one-ring callback fraud) to measure zero-day behaviour.

Everything here is synthetic. No real subscriber data is used.
"""
from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass, field

import numpy as np

# Dữ liệu tham chiếu của thành phố, cell và số hotline dùng để mô phỏng mạng và actor lừa đảo.
# --------------------------------------------------------------------------- reference data
CITIES = {"HN": (21.0285, 105.8542), "HCM": (10.8231, 106.6297), "DN": (16.0544, 108.2022)}
CELLS_PER_CITY = 120
REGISTERED_CELLS = {}
for _city, (_lat, _lon) in CITIES.items():
    # crc32 thay vì hash(): hash() của chuỗi đổi theo mỗi lần chạy Python (PYTHONHASHSEED), làm tọa độ trạm và
    # đặc trưng tốc độ di chuyển khác nhau giữa lúc huấn luyện và lúc chạy, và kết quả không lặp lại được.
    _rng = random.Random(zlib.crc32(_city.encode()) & 0xFFFF)
    for _i in range(CELLS_PER_CITY):
        REGISTERED_CELLS[f"{_city}-{_i:04d}"] = (_lat + _rng.uniform(-0.15, 0.15), _lon + _rng.uniform(-0.15, 0.15))

# Danh sách số nóng ngân hàng và cơ quan mà cuộc gọi giả mạo cố gắng sao chép.
from config import LOCAL_UTC_OFFSET_S, local_hour  # noqa: E402

SIM_DAY0_S = 86400.0   # simulated day 0 starts at local midnight of 1970-01-02 (Unix-style seconds)

PROTECTED_CLI = ["1900545413", "1800599928", "02439741234", "1900558868", "02838221234", "113"]  # bank / agency hotlines
BRAND_SENDERS = ["Vietcombank", "Techcombank", "BIDV", "VPBank", "MoMo", "VNPAY", "EVNHANOI", "Shopee", "Viettel"]
OFFICIAL_DOMAINS = ["vietcombank.com.vn", "techcombank.com", "bidv.com.vn", "vpbank.com.vn", "momo.vn",
                    "vnpay.vn", "evnhanoi.vn", "shopee.vn", "viettel.vn"]
GATEWAY_TACS = ["86001204", "86252103", "35873605"]          # TACs of known GSM-gateway modules
CONSUMER_TACS = ["35391110", "35332509", "35290611", "86769004", "35847631", "35614210", "86887903"]

# Phishing lures. TRAIN_LURES are seen in training; TEST_LURES only appear in the evaluation window.
# Bộ lời lừa đảo dùng trong training, gồm các template SMS phishing thường gặp trong thực tế.
TRAIN_LURES = [
    "[{brand}] Tai khoan cua quy khach bi tam khoa do dang nhap bat thuong. Xac thuc ngay tai {url}",
    "[{brand}] Giao dich {amt} VND dang cho duyet. Neu khong phai ban, huy lenh tai {url}",
    "{brand} thong bao: cap nhat sinh trac hoc truoc 24h de tranh bi khoa the. Truy cap {url}",
    "Ban da trung thuong {amt}d tu chuong trinh tri an {brand}. Nhan thuong tai {url}",
    "[{brand}] Diem thuong cua ban sap het han, doi qua ngay: {url}",
    "Cuc canh sat: ban co lien quan vu an rua tien, khai bao truc tuyen tai {url} truoc 17h",
]
TEST_LURES = [
    "[BHXH] So BHXH cua ban chua dong bo VNeID. Cap nhat tai {url} de khong bi gian doan quyen loi",
    "[{brand}] Thẻ của quý khách sắp hết hạn, gia hạn miễn phí tại {url} trong hôm nay",
    "Thong bao hoan thue TNCN {amt}d. Nhan tien qua {url} (Tong cuc Thue)",
    "{brand}: Phat hien dang nhap tu thiet bi la o Campuchia. Neu khong phai ban bam {url}",
]
SCAM_DOMAINS_TRAIN = ["vcb-xacthuc.net", "techcom-baomat.info", "bidv-khoataikhoan.com", "vpb-uudai.top",
                      "momo-quatang.xyz", "evn-thanhtoan.online", "congan-khaibao.org"]
SCAM_DOMAINS_TEST = ["vneid-dongbo.site", "the-giahan.click", "hoanthue-tncn.store", "baomat-taikhoan.live"]

LEGIT_SMS = [
    "Ma OTP cua quy khach la {otp}. Tuyet doi KHONG cung cap ma nay cho bat ky ai.",
    "[{brand}] TK {acct} -{amt}VND luc {hh}:{mm}. SD: {bal}VND. Chi tiet: https://{dom}/tb",
    "[{brand}] Uu dai cuoi tuan giam 20% khi thanh toan qua app. Xem tai https://{dom}/km",
    "[{brand}] Quy khach luu y: {brand} KHONG BAO GIO yeu cau cung cap OTP qua dien thoai. Tim hieu: https://{dom}/canhbao",
    "Don hang {num} dang giao, shipper se goi trong 30 phut.",
    "Me oi toi nay con ve muon, me an com truoc nhe",
    "Hop nhom 14h phong 402 nhe, nho mang laptop",
    "Anh gui lai link tai lieu: https://drive.google.com/{num}",
    "Cuoi tuan di da ngoai khong, bao tao truoc thu 6 nhe",
    "Tai khoan cua ban bi khoa roi a? goi lai cho minh nhe",   # benign text with scary words
]

# Đầu số di động Việt Nam theo nhà mạng (sau mã quốc gia 84) và tỉ trọng thuê bao gần đúng để mô phỏng.
VN_PREFIXES = {
    "Viettel": [86, 96, 97, 98, 32, 33, 34, 35, 36, 37, 38, 39],
    "Vinaphone": [88, 91, 94, 81, 82, 83, 84, 85],
    "MobiFone": [89, 90, 93, 70, 76, 77, 78, 79],
    "Vietnamobile": [92, 52, 56, 58],
    "Gmobile": [99, 59],
}
NETWORK_SHARE = {"Viettel": 0.53, "Vinaphone": 0.22, "MobiFone": 0.20, "Vietnamobile": 0.04, "Gmobile": 0.01}

DEEPFAKE_SCRIPTS = ["chuyen gap tien vao tai khoan tam giu", "doc ma otp de xac minh", "con dang cap cuu can tien gap",
                    "cong an dang dieu tra tai khoan cua anh", "sep can chuyen khoan ngay cho doi tac"]
NORMAL_SCRIPTS = ["toi nay may gio ve", "gui lai hop dong giup em", "mai hop luc chin gio", "con an com chua",
                  "chuyen khoan tien nha thang nay roi nhe", "cong an phuong goi lam can cuoc", "nho mua sua cho be"]


# Tính khoảng cách Haversine giữa hai điểm địa lý để ước lượng tốc độ di chuyển giả.
def _haversine(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


# Tạo mô hình hoạt động theo giờ để traffic tăng ở khung giờ cao điểm trong ngày.
def day_profile(t: float) -> float:
    """Relative activity by local (Vietnam) hour of day; t is Unix-style UTC seconds."""
    h = local_hour(t)
    if h < 6:
        return 0.12
    if h < 8:
        return 0.6
    if h < 12:
        return 1.3
    if h < 14:
        return 0.9
    if h < 18:
        return 1.3
    if h < 22:
        return 1.1
    return 0.4


# Sinh thời điểm event theo Poisson không đồng nhất và có thể lọc theo profile ngày hoặc giờ làm việc.
def poisson_times(rng: np.random.Generator, rate_per_s: float, t0: float, t1: float, profile=True, work_hours=False):
    """Inhomogeneous Poisson arrivals by thinning."""
    if rate_per_s <= 0 or t1 <= t0:
        return []
    peak = rate_per_s * (1.3 if profile else 1.0)
    n = rng.poisson(peak * (t1 - t0))
    ts = np.sort(rng.uniform(t0, t1, n))
    out = []
    for t in ts:
        if work_hours:
            h = local_hour(t)
            if not (8 <= h < 18):
                continue
            if rng.random() < rate_per_s / peak:
                out.append(float(t))
        elif profile:
            if rng.random() < rate_per_s * day_profile(t) / peak:
                out.append(float(t))
        else:
            out.append(float(t))
    return out


# Mô hình dữ liệu của từng subscriber, lưu home city, home cell, IMEI và danh bạ nội bộ.
@dataclass
class Sub:
    msisdn: str
    home_city: str
    home_cell: str
    imeis: list
    age_days: int
    contacts: list = field(default_factory=list)
    kind: str = "personal"
    # Mức hoạt động: phần lớn gọi đều, một phần ít gọi (vài ngày một cuộc) -> có người thật "mới xuất hiện" trên mạng.
    calls_per_day: float = 7.0
    # Thời điểm bắt đầu hoạt động (None = từ đầu): SIM mới mua, người vừa đi nước ngoài về.
    active_from: float | None = None


# Simulator chính xây dựng chuỗi event theo thời gian cho một cửa sổ traffic nhất định.
class TrafficSimulator:
    """Builds a time-ordered event stream for one window of simulated traffic."""

# Khởi tạo RNG, cell map, thuê bao cá nhân, call centre và trạng thái chung của window hiện tại.
    def __init__(self, seed: int, n_personal: int = 12000, n_callcentres: int = 60, window: str = "train"):
        self.rng = np.random.default_rng(seed)
        self.r = random.Random(seed)
        self.window = window
        self.seed = seed
        self._call_seq = 0
        self.cells_by_city = {c: [k for k in REGISTERED_CELLS if k.startswith(c)] for c in CITIES}
        self.subs = [self._new_sub("personal") for _ in range(n_personal)]
        self.callcentres = [self._new_sub("callcentre") for _ in range(n_callcentres)]
        pool = [s.msisdn for s in self.subs]
        for s in self.subs:
            s.contacts = self.r.sample(pool, 25)
        self.events = []
        self.n_relatives = 900
        self.foreign_biz = [R_ + str(self.r.randint(10 ** 8, 10 ** 9)) for R_ in ["1", "65", "852", "44", "81"] * 3]

    # ------------------------------------------------------------------ helpers
# Tạo MSISDN mới theo đầu số di động thật của Việt Nam (84 + đầu số 2 chữ số + 7 chữ số), có tỉ trọng nhà mạng.
    def _msisdn(self, prefix="84"):
        net = self.r.choices(list(VN_PREFIXES), weights=list(NETWORK_SHARE.values()))[0]
        return prefix + str(self.r.choice(VN_PREFIXES[net])) + f"{self.r.randint(0, 9_999_999):07d}"

# Sinh IMEI mới từ TAC đã cho để mô phỏng thiết bị cá nhân hoặc gateway GSM.
    def _imei(self, tacs):
        return self.r.choice(tacs) + f"{self.r.randint(0, 999999):06d}" + str(self.r.randint(0, 9))

# Sinh một subscriber mới với city, cell, IMEI, age và kiểu vai trò phù hợp.
    def _new_sub(self, kind):
        city = self.r.choices(list(CITIES), weights=[0.45, 0.45, 0.10])[0]
        imeis = [self._imei(CONSUMER_TACS)]
        if self.r.random() < 0.04:
            imeis.append(self._imei(CONSUMER_TACS))
        if kind == "callcentre":
            age = self.r.randint(200, 3000)
        else:
            age = self.r.randint(1, 30) if self.r.random() < 0.03 else self.r.randint(31, 4000)
        u = self.r.random()
        rate = 7.0 if u < 0.85 else (1.0 if u < 0.97 else 0.2)     # 12 % ít gọi, 3 % rất ít gọi
        return Sub(self._msisdn(), city, self.r.choice(self.cells_by_city[city]), imeis, age, kind=kind, calls_per_day=rate)

# Ghi event vào danh sách internal events để sắp xếp theo thời gian khi build xong.
    def _emit(self, **ev):
        self.events.append(ev)

# Tạo event CALL chuẩn với định danh, network, thời lượng và nhãn fraud.
    def _call(self, t, caller, callee, cell, imei, origin, net, age, duration, label=0, ftype="CLEAN", actor="", **extra):
        self._call_seq += 1
        call_id = extra.pop("call_id", f"{self.window}-{self.seed}-call-{self._call_seq:09d}")
        self._emit(event_type="CALL", call_id=call_id, ts=t, caller=caller, callee=callee, cell_id=cell, imei=imei,
                   origin_trunk=origin, network=net, caller_age_days=age, duration_s=round(duration, 1),
                   label=label, fraud_type=ftype, actor=actor, **extra)
        return call_id

# Tạo audio segment để mô phỏng giọng thật, clone hay artifact trong deepfake.
    def _audio(self, t, caller, callee, label, ftype, deepfake, codec_2g, actor="", silent_caller=False, call_id=None):
        r = self.rng
        # Ai nói bao nhiêu (từ bộ phát hiện giọng nói / VAD): bình thường cả hai bên đều nói; kẻ thu giọng
        # gọi tới rồi im lặng để ghi lại giọng người nghe ("Alo? Alo? Ai đấy?") rồi cúp máy.
        if silent_caller:
            caller_ratio = float(np.clip(r.normal(0.03, 0.03), 0, 1))
            callee_s = float(np.clip(r.normal(4.5, 1.8), 0.5, 12))
        else:
            caller_ratio = float(np.clip(r.beta(5, 5) if not deepfake else r.beta(7, 3), 0, 1))
            if r.random() < 0.02:            # genuine silent moments: bad signal, pocket dial, child on the phone
                caller_ratio = float(r.uniform(0, 0.12))
            callee_s = float(np.clip(r.normal(6.0, 2.5), 0.3, 20))
        if silent_caller:
            vocoder = float(np.clip(r.normal(0.20 + (0.22 if codec_2g else 0), 0.12), 0, 1))
            jitter = float(np.clip(r.normal(0.024, 0.007), 0.0005, 0.06))
            flat = float(np.clip(r.normal(0.30, 0.09), 0, 1))
            drift = float(np.clip(r.normal(0.22, 0.12), 0, 1))
            script = ""                      # nothing said by the caller -> nothing to transcribe
        elif deepfake:
            quality = r.beta(2, 2)  # better generators -> fewer artefacts
            vocoder = float(np.clip(r.normal(0.62 - 0.25 * quality, 0.14), 0, 1))
            jitter = float(np.clip(r.normal(0.010 + 0.012 * quality, 0.005), 0.0005, 0.06))
            flat = float(np.clip(r.normal(0.42, 0.10), 0, 1))
            drift = float(np.clip(r.normal(0.48, 0.17), 0, 1))
            script = self.r.choice(DEEPFAKE_SCRIPTS) if r.random() < 0.75 else self.r.choice(NORMAL_SCRIPTS)
        else:
            vocoder = float(np.clip(r.normal(0.20 + (0.22 if codec_2g else 0), 0.12), 0, 1))
            jitter = float(np.clip(r.normal(0.024 - (0.006 if codec_2g else 0), 0.007), 0.0005, 0.06))
            flat = float(np.clip(r.normal(0.30 + (0.08 if codec_2g else 0), 0.09), 0, 1))
            drift = float(np.clip(r.normal(0.22, 0.12), 0, 1))
            script = self.r.choice(NORMAL_SCRIPTS) if r.random() < 0.97 else self.r.choice(DEEPFAKE_SCRIPTS)
        # ASR noise: drop / garble words
        words = [w for w in script.split() if r.random() > 0.12]
        if not call_id:
            raise ValueError("audio segments require an explicit call_id")
        self._emit(event_type="AUDIO", call_id=call_id, ts=t, caller=caller, callee=callee, codec="AMR-NB-2G" if codec_2g else "EVS-VoLTE",
                   vocoder_artifact=round(vocoder, 4), pitch_jitter=round(jitter, 5), spectral_flatness=round(flat, 4),
                   speaker_drift=round(drift, 4), caller_speech_ratio=round(caller_ratio, 4),
                   callee_speech_s=round(callee_s, 2), transcript=" ".join(words), label=label, fraud_type=ftype, actor=actor)

# Tạo event SMS với route, cell và nội dung để mô phỏng spam hoặc phishing.
    def _sms(self, t, sender, recipient, route, cell, text, label=0, ftype="CLEAN", actor=""):
        self._emit(event_type="SMS", ts=t, sender=sender, recipient=recipient, sms_route=route, cell_id=cell,
                   text=text, label=label, fraud_type=ftype, actor=actor)

    # ------------------------------------------------------------------ legitimate traffic
# Sinh traffic hợp pháp: cuộc gọi, SMS và di chuyển của người dùng bình thường theo mô hình mạng thực.
    def legit(self, t0, t1):
        r, R = self.rng, self.r
        travellers = set(R.sample(range(len(self.subs)), int(0.015 * len(self.subs))))
        # 1 % thuê bao chỉ bắt đầu hoạt động giữa chừng (SIM mới mua / vừa về nước): người thật nhưng "mới xuất hiện".
        late = set(R.sample(range(len(self.subs)), int(0.01 * len(self.subs))))
        for i in late:
            self.subs[i].active_from = R.uniform(t0, t1)
        for i, s in enumerate(self.subs):
            cur_cell, cur_city = s.home_cell, s.home_city
            switch_t = R.uniform(t0, t1) if i in travellers else None
            dest_city = R.choice([c for c in CITIES if c != s.home_city]) if switch_t else None
            for t in poisson_times(r, s.calls_per_day / 86400, s.active_from or t0, t1):
                if switch_t and t > switch_t:
                    cur_city, cur_cell = dest_city, R.choice(self.cells_by_city[dest_city])
                cell = cur_cell if R.random() < 0.8 else R.choice(self.cells_by_city[cur_city])
                callee = R.choice(s.contacts) if R.random() < 0.85 else R.choice(self.subs).msisdn
                answered = R.random() > 0.15
                dur = float(r.lognormal(4.1, 1.0)) if answered else 0.0
                origin = "LOCAL" if R.random() > 0.004 else "INTL_GW"   # a few roaming / OTT breakouts
                net = "2G" if R.random() < 0.05 else R.choice(["4G", "VoLTE", "VoLTE", "3G"])
                imei = s.imeis[0] if len(s.imeis) == 1 or R.random() < 0.8 else s.imeis[1]
                call_id = self._call(t, s.msisdn, callee, cell, imei, origin, net, s.age_days, dur)
                if answered and R.random() < 0.06:   # in-call scoring is sampled / triggered for a subset of calls
                    self._audio(t + min(2, dur / 2), s.msisdn, callee, 0, "CLEAN", False, net == "2G", call_id=call_id)
            for t in poisson_times(r, 4 / 7 * s.calls_per_day / 86400, s.active_from or t0, t1):
                txt = R.choice(LEGIT_SMS[4:]).format(num=R.randint(1000, 99999))
                self._sms(t, s.msisdn, R.choice(s.contacts), "P2P", s.home_cell, txt)
        # Occasional numbers: real people who only ever CALL OUT and are in nobody's contacts (secondary SIMs, an
        # elderly parent's phone, a just-bought SIM, someone back from abroad, a person phoning shops / job ads).
        # They are new on the network AND receive no calls, exactly like a fraud SIM on those two signals, so the
        # model has to use behaviour rather than "never seen before" alone. ~5 % of the population.
        for _ in range(int(0.05 * len(self.subs))):
            num, age = self._msisdn(), (R.randint(1, 30) if R.random() < 0.5 else R.randint(31, 4000))
            city = R.choice(list(CITIES))
            cell, imei = R.choice(self.cells_by_city[city]), self._imei(CONSUMER_TACS)
            start = R.uniform(t0, t1)
            if R.random() < 0.2:      # a burst: several calls within the hour (shops, job ads, a delivery)
                times = sorted(R.uniform(start, start + 3600) for _ in range(R.randint(3, 8)))
            else:
                times = poisson_times(r, R.uniform(1, 4) / 86400, start, t1)
            for t in times:
                if t > t1:
                    continue
                answered = R.random() > 0.25
                self._call(t, num, R.choice(self.subs).msisdn, cell, imei, "LOCAL", R.choice(["4G", "VoLTE", "3G"]), age,
                           float(r.lognormal(4.0, 1.0)) if answered else float(R.uniform(0, 20)))
        # call centres / telesales: heavy, dispersed, mostly outgoing, short calls  (look like SIM boxes)
        for c in self.callcentres:
            rate = R.uniform(25, 110) / 3600
            for t in poisson_times(r, rate, t0, t1, work_hours=True):
                answered = R.random() > 0.45
                dur = float(r.lognormal(3.6, 0.8)) if answered else float(r.uniform(0, 4))
                self._call(t, c.msisdn, R.choice(self.subs).msisdn, c.home_cell, c.imeis[0], "LOCAL",
                           R.choice(["4G", "VoLTE"]), c.age_days, dur)
            for t in poisson_times(r, 6 / 3600, t0, t1, work_hours=True):   # customers calling back
                s = R.choice(self.subs)
                self._call(t, s.msisdn, c.msisdn, s.home_cell, s.imeis[0], "LOCAL", "VoLTE", s.age_days,
                           float(r.lognormal(4.0, 0.7)))
        # banks / agencies: real calls from protected numbers through their trunks
        for t in poisson_times(r, 40 / 3600, t0, t1, work_hours=True):
            cli = R.choice(PROTECTED_CLI)
            origin = "BANK_TRUNK" if R.random() > 0.03 else "INTL_GW"    # some outsourced/offshore centres
            self._call(t, cli, R.choice(self.subs).msisdn, "TRUNK", "", origin, "VoLTE", 3650,
                       float(r.lognormal(4.3, 0.6)))
        # brand / bank SMS through the official brandname gateway
        for t in poisson_times(r, 900 / 3600, t0, t1):
            b = R.randrange(len(BRAND_SENDERS))
            tpl = R.choice(LEGIT_SMS[:4])
            txt = tpl.format(otp=R.randint(100000, 999999), brand=BRAND_SENDERS[b], acct=R.randint(1000, 9999),
                             amt=f"{R.randint(1, 900) * 10000:,}", hh=R.randint(0, 23), mm=R.randint(10, 59),
                             bal=f"{R.randint(1, 9000) * 10000:,}", dom=OFFICIAL_DOMAINS[b])
            self._sms(t, BRAND_SENDERS[b], R.choice(self.subs).msisdn, "BRAND_GW", "SMSC", txt)
        # international traffic: relatives abroad (repeat callers) + foreign businesses (airlines, hotels,
        # overseas banks) that call many customers in bursts -> gives the "foreign" peer group real variance
        for _ in range(self.n_relatives):
            num = R.choice(["1", "61", "81", "82", "886", "33", "49"]) + str(R.randint(10 ** 8, 10 ** 9))
            family = R.sample(self.subs, R.randint(1, 3))
            for t in poisson_times(r, R.uniform(1, 4) / 86400, t0, t1):
                answered = R.random() > 0.2
                self._call(t, num, R.choice(family).msisdn, "INTL", "", "INTL_GW", "VoLTE", -1,
                           float(r.lognormal(5.0, 0.8)) if answered else float(r.uniform(0, 25)))
        for num in self.foreign_biz:
            for t in poisson_times(r, R.uniform(3, 15) / 3600, t0, t1, work_hours=True):
                answered = R.random() > 0.25
                self._call(t, num, R.choice(self.subs).msisdn, "INTL", "", "INTL_GW", "VoLTE", -1,
                           float(r.lognormal(4.4, 0.6)) if answered else float(r.uniform(0, 20)))

    # ------------------------------------------------------------------ fraud actors
# "Nuôi" SIM: vài cuộc gọi trông bình thường trong 1-12 giờ trước khi ra tay, để số không còn "mới xuất hiện".
    def _warm_up_sim(self, num, cell, imei, age, start, ftype, tag):
        R, r = self.r, self.rng
        for _ in range(R.randint(2, 5)):
            t = start - R.uniform(1, 12) * 3600
            self._call(t, num, R.choice(self.subs).msisdn, cell, imei, "LOCAL", R.choice(["4G", "VoLTE"]), age,
                       float(r.lognormal(4.0, 0.8)), 1, ftype, tag)

# Mô phỏng farm SIM box chuyển lưu lượng quốc tế về nội địa bằng SIM và IMEI đặc trưng.
    def simbox(self, t0, t1, n_sims, rate_range, gateway_tac_p=0.5, tag="SB", active_h=(2, 8), dedicated_imei=None,
               aged=False):
        """A GSM-gateway farm terminating bypassed international traffic on local SIMs.

        aged=True: the farm runs on old, properly registered SIMs bought or rented from real subscribers
        (what fraudsters switch to once new SIM registration is locked down), so "new SIM" stops helping."""
        R, r = self.r, self.rng
        city = R.choice(list(CITIES))
        cell = R.choice(self.cells_by_city[city])
        # M1: a per-farm coin flip left whole training windows without a single gateway-TAC farm, so the
        # model never saw imei_gateway_tac = 1. Explicit 0/1 keep their meaning; otherwise farms alternate.
        if gateway_tac_p in (0.0, 1.0):
            gateway = gateway_tac_p == 1.0
        else:
            self._farms = getattr(self, "_farms", 0) + 1
            gateway = self._farms % 2 == 1
        tacs = GATEWAY_TACS if gateway else CONSUMER_TACS
        if dedicated_imei is None:
            dedicated_imei = R.random() < 0.3
        modules = [self._imei(tacs) for _ in range(max(2, n_sims // 4))]
        start = R.uniform(t0, t0 + 0.5 * (t1 - t0))
        dur_active = R.uniform(*active_h) * 3600
        for _ in range(n_sims):
            sim, age = self._msisdn(), (R.randint(200, 3000) if aged else R.randint(1, 45))
            own = self._imei(tacs)   # "IMEI-changer" boxes give every SIM its own identity
            if aged and R.random() < 0.5:   # half of the rented SIMs are "warmed up" with ordinary calls first
                self._warm_up_sim(sim, cell, own if dedicated_imei else R.choice(modules), age, start, "SIM_BOX", tag)
            for t in poisson_times(r, R.uniform(*rate_range) / 3600, start, min(t1, start + dur_active), profile=True):
                answered = R.random() > 0.3
                dur = float(r.lognormal(4.0, 0.9)) if answered else float(r.uniform(0, 5))
                self._call(t, sim, R.choice(self.subs).msisdn, cell, own if dedicated_imei else R.choice(modules), "LOCAL",
                           R.choice(["3G", "4G"]), age, dur, 1, "SIM_BOX", tag)

# Mô phỏng cuộc gọi lừa đảo bằng số nóng ngân hàng/cơ quan qua gateway quốc tế hoặc BTS 2G giả.
    def cli_spoof(self, t0, t1, n_calls, tag="CS"):
        """Calls presenting a bank/agency number, injected via foreign gateways or a fake 2G BTS."""
        R, r = self.r, self.rng
        for t in sorted(r.uniform(t0, t1, n_calls)):
            fake_bts = R.random() < 0.5
            cell = f"X-{R.randint(0, 99):02d}" if (fake_bts and R.random() < 0.7) else (
                R.choice(list(REGISTERED_CELLS)) if fake_bts else "INTL")
            self._call(float(t), R.choice(PROTECTED_CLI), R.choice(self.subs).msisdn, cell, self._imei(CONSUMER_TACS),
                       "INTL_GW" if not fake_bts else "LOCAL", "2G" if fake_bts else R.choice(["VoLTE", "3G"]),
                       3650, float(r.lognormal(4.8, 0.6)), 1, "CLI_SPOOF", tag)

# Tạo tin nhắn phishing với URL giả, thương hiệu và cảnh báo che giấu để bẫy người nhận.
    def sms_phish(self, t0, t1, n_msgs, lures, domains, tag="PH"):
        R = self.r
        times = sorted(self.rng.uniform(t0, t1, n_msgs))
        bts = f"X-{R.randint(0, 99):02d}" if R.random() < 0.7 else R.choice(list(REGISTERED_CELLS))
        for t in times:
            brand = R.choice(BRAND_SENDERS[:6])
            txt = R.choice(lures).format(brand=brand, url="https://" + R.choice(domains) + f"/{R.randint(10, 999)}",
                                         amt=f"{R.randint(5, 90) * 1_000_000:,}")
            if R.random() < 0.3:   # obfuscation tricks
                txt = txt.replace("o", "0", 1).replace("i", "1", 1)
            if R.random() < 0.7:   # fake BTS broadcast, sender id set to the brand
                self._sms(float(t), brand, R.choice(self.subs).msisdn, "UNVERIFIED_BTS", bts, txt, 1, "SMS_PHISH", tag)
            else:                  # spam SIMs sending person-to-person
                self._sms(float(t), self._msisdn(), R.choice(self.subs).msisdn, "P2P", R.choice(list(REGISTERED_CELLS)),
                          txt, 1, "SMS_PHISH", tag)

# Mô phỏng cuộc gọi clone giọng và chia audio thành nhiều chunk để feature deepfake hoạt động.
    def deepfake(self, t0, t1, n_calls, tag="DF"):
        """Scam calls using a cloned voice; the signalling looks like an ordinary new prepaid SIM."""
        R, r = self.r, self.rng
        for t in sorted(r.uniform(t0, t1, n_calls)):
            caller = self._msisdn()
            callee = R.choice(self.subs).msisdn
            city = R.choice(list(CITIES))
            net2g = R.random() < 0.3
            call_id = self._call(float(t), caller, callee, R.choice(self.cells_by_city[city]), self._imei(CONSUMER_TACS),
                       "LOCAL", "2G" if net2g else "VoLTE", R.randint(1, 400), float(r.lognormal(5.0, 0.5)),
                       1, "DEEPFAKE_VOICE", tag)
            call_end = float(t) + self.events[-1]["duration_s"]
            for k in range(R.randint(1, 3)):   # several 2-second chunks are scored during the call
                self._audio(min(float(t) + 2 + 2 * k, call_end), caller, callee, 1, "DEEPFAKE_VOICE", True, net2g, tag, call_id=call_id)

# Mô phỏng cuộc gọi im lặng thu giọng: gọi hàng loạt, người nghe nói "Alo" thì im lặng ghi âm rồi cúp máy.
    def voice_harvest(self, t0, t1, n_numbers, rate_range, tag="VH", active_h=(0.5, 2), aged_p=0.5):
        """Silent calls that record the victim's voice for later cloning (the 'just say Alo' scam).

        Signalling: many short answered calls to unrelated people. In-call audio: the caller never speaks
        while the callee's voice is captured for several seconds. Half the numbers are old rented SIMs."""
        R, r = self.r, self.rng
        for _ in range(n_numbers):
            num = self._msisdn()
            age = R.randint(200, 3000) if R.random() < aged_p else R.randint(1, 45)
            city = R.choice(list(CITIES))
            cell, imei = R.choice(self.cells_by_city[city]), self._imei(CONSUMER_TACS)
            start = R.uniform(t0, t0 + 0.6 * (t1 - t0))
            if R.random() < 0.5:
                self._warm_up_sim(num, cell, imei, age, start, "VOICE_HARVEST", tag)
            for t in poisson_times(r, R.uniform(*rate_range) / 3600, start, min(t1, start + R.uniform(*active_h) * 3600),
                                   profile=True):
                answered = R.random() > 0.3
                dur = float(r.uniform(3, 9)) if answered else float(r.uniform(0, 15))
                callee = R.choice(self.subs).msisdn
                call_id = self._call(t, num, callee, cell, imei, "LOCAL", R.choice(["4G", "VoLTE"]), age, dur, 1, "VOICE_HARVEST", tag)
                if answered and R.random() < 0.6:
                    self._audio(t + 2, num, callee, 1, "VOICE_HARVEST", False, False, tag, silent_caller=True, call_id=call_id)

# Sinh cuộc gọi one-ring zero-day từ dải số premium nhằm kiểm tra phát hiện family mới.
    def wangiri(self, t0, t1, n_numbers, rate_range, tag="WG", active_h=1.5):
        """ZERO-DAY family (never in training): one-ring calls from premium-rate foreign ranges."""
        R, r = self.r, self.rng
        start = R.uniform(t0, t0 + 0.6 * (t1 - t0))
        for _ in range(n_numbers):
            num = R.choice(["882", "881", "252", "247", "690"]) + str(R.randint(10 ** 7, 10 ** 8))
            for t in poisson_times(r, R.uniform(*rate_range) / 3600, start, min(t1, start + active_h * 3600), profile=True):
                self._call(t, num, R.choice(self.subs).msisdn, "INTL", "", "INTL_GW", "VoLTE", -1,
                           float(r.uniform(0.5, 3.0)), 1, "ZERO_DAY_WANGIRI", tag)

    # ------------------------------------------------------------------ build
# Xây dựng toàn bộ event stream cho cửa sổ hiện tại và thêm traffic warm-up hợp pháp trước khi đo.
    def build(self, hours: float, start_hour: float = 0.0, warmup_hours: float = 0.0):
        """warmup_hours of legitimate-only traffic are simulated BEFORE start_hour so that the
        feature store has realistic history when the measured window begins (no cold start)."""
        # start_hour is LOCAL time. Simulated ts are UTC-style seconds (same convention as live traffic),
        # placed one day after the epoch so even a 24 h warm-up stays at non-negative times.
        t0 = SIM_DAY0_S + start_hour * 3600 - LOCAL_UTC_OFFSET_S
        t1 = t0 + hours * 3600
        self.legit(t0 - warmup_hours * 3600, t1)
        R = self.r
        days = hours / 24
        if self.window in ("train", "val"):
            # Training/validation windows are ENRICHED with fraud (more positives to learn from).
            for k in range(max(1, round(1.5 * days))):
                self.simbox(t0, t1, R.randint(6, 16), (3, 40), tag=f"SB{k}", active_h=(1, 5))
            # farms on old rented SIMs: the model must not rely on "new SIM" alone
            for k in range(max(1, round(1.0 * days))):
                self.simbox(t0, t1, R.randint(4, 10), (3, 30), tag=f"SBA{k}", active_h=(1, 4), aged=True)
            self.cli_spoof(t0, t1, int(60 * days))
            self.sms_phish(t0, t1, int(120 * days), TRAIN_LURES, SCAM_DOMAINS_TRAIN)
            self.deepfake(t0, t1, int(60 * days))
            self.voice_harvest(t0, t1, max(1, round(6 * days)), (15, 45))
        elif self.window == "reference":
            # Drift reference (D7): realistic prevalence of the KNOWN families only, never used for
            # training or thresholds, and without the zero-day family reserved for the test window.
            self.simbox(t0, t1, 2, (5, 10), tag="SB_R1", active_h=(0.75, 1.25), dedicated_imei=False)
            self.cli_spoof(t0, t1, 4)
            self.sms_phish(t0, t1, 6, TRAIN_LURES, SCAM_DOMAINS_TRAIN)
            self.deepfake(t0, t1, 3)
        elif self.window == "threats":
            # New-threat evaluation: farms on old rented SIMs and silent voice-harvesting numbers, nothing else.
            self.simbox(t0, t1, 2, (5, 10), tag="SB_AGED", active_h=(0.75, 1.25), aged=True)
            self.voice_harvest(t0, t1, 2, (8, 16), tag="VH", active_h=(0.5, 1.0))
        else:
            # Evaluation window at realistic prevalence (~0.1 %): new actors, a stealthier farm,
            # unseen phishing lures and a zero-day family (Wangiri) that never appears in training.
            self.simbox(t0, t1, 2, (5, 10), tag="SB_T1", active_h=(0.75, 1.25), dedicated_imei=False)
            self.simbox(t0, t1, 2, (3, 6), gateway_tac_p=0.0, tag="SB_T2_STEALTH", active_h=(1, 1.5), dedicated_imei=True)
            self.cli_spoof(t0, t1, 4)
            self.sms_phish(t0, t1, 6, TRAIN_LURES[:2] + TEST_LURES, SCAM_DOMAINS_TRAIN[:2] + SCAM_DOMAINS_TEST)
            self.deepfake(t0, t1, 3)
            self.wangiri(t0, t1, 1, (15, 25), active_h=0.5)
            # Same actors as v2.0 so the headline numbers stay comparable; the new threats are measured in
            # their own "threats" windows (evaluate.py new_threats_study).
        self.events.sort(key=lambda e: e["ts"])
        for i, e in enumerate(self.events):
            e["event_id"] = f"{self.window[:2].upper()}{i:07d}"
            e["warmup"] = int(e["ts"] < t0)
        return self.events
