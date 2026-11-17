"""Streaming feature store.

Every behavioural feature is computed *causally* from the event stream itself (nothing is read
from labels or generator-side counters). The same code path is used for training-data extraction
and for live scoring, so there is no train/serve skew.

State is partitioned by key (MSISDN, IMEI, cell); in a deployment each partition would live in a
shard of a low-latency store (e.g. Redis / Aerospike) keyed exactly like a Kafka partition.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import bisect
import heapq
import math
import re
import time
from collections import defaultdict, deque

from config import local_hour
from data_generator import GATEWAY_TACS, OFFICIAL_DOMAINS, PROTECTED_CLI, REGISTERED_CELLS, BRAND_SENDERS

# Các hằng số thời gian xác định window 1 phút, 10 phút, 1 giờ và 24 giờ cho feature.
H1, M10, M1, D1 = 3600.0, 600.0, 60.0, 86400.0
# Thời gian hoạt động trên mạng được cắt ở 72 giờ; dưới 6 giờ được xem là "mới xuất hiện" (dù đăng ký đã lâu).
ACTIVE_CAP_H, FRESH_ACTIVE_H = 72.0, 6.0
# Regex trích domain từ URL trong SMS để phát hiện link không chính thức hoặc lừa đảo.
URL_RE = re.compile(r"https?://([^/\s]+)")
# Từ vựng cảnh báo gian lận dùng để đếm mức độ nguy hiểm của transcript cuộc gọi.
SCAM_LEXICON = ["chuyen gap", "otp", "tam giu", "cap cuu", "cong an", "dieu tra", "chuyen khoan ngay", "xac minh"]
PROTECTED = set(PROTECTED_CLI)
BRANDS = set(BRAND_SENDERS)
OFFICIAL = set(OFFICIAL_DOMAINS) | {"drive.google.com"}
GW_TACS = set(GATEWAY_TACS)

# Danh sách feature CALL gồm nhịp gọi, địa lý, IMEI, network và số nóng/CLI mismatch.
CALL_FEATURES = [
    "calls_1m", "calls_10m", "calls_1h", "callee_dispersion_1h", "in_out_ratio_24h", "incoming_24h",
    "short_call_ratio", "mean_log_duration", "completed_calls_seen", "imei_per_msisdn_24h", "msisdn_per_imei_24h",
    "imei_gateway_tac", "cell_registered", "cell_unknown_radio", "cell_trunk_or_intl", "velocity_kmh",
    "new_sims_on_cell_1h", "caller_age_days", "caller_foreign", "origin_local", "origin_intl_gw", "origin_bank_trunk",
    "cli_protected", "protected_cli_mismatch", "network_2g", "hour_of_day", "active_hours_on_network",
    # B-number novelty: share of this caller's calls in the last 24 h that went to someone it had never
    # called before. People mostly call their contacts again; SIM-box farms dial random subscribers,
    # which a slow farm (3-6 calls/h) never reveals through the 1-hour dispersion feature.
    "new_callee_ratio_24h",
    # SIM-farm co-location: when THIS caller looks like a farm SIM (fresh, no incoming call in 24 h,
    # every call to a distinct never-called number), how many fresh SIMs on the same radio cell made
    # >= 2 calls to >= 2 different numbers in the last hour (itself included). A lone new subscriber
    # gives 1; a farm of n co-located SIMs gives n. 0 when the caller does not match the profile.
    "farm_cell_sims_1h",
]
KNOWN_CALLEE_TTL_S = 30 * 86400.0   # how long a callee stays "known" to a caller
# Danh sách feature SMS dựa trên route, sender, URL và lưu lượng 1 giờ gần nhất.
SMS_META_FEATURES = ["route_brand_gw", "route_p2p", "route_unverified_bts", "sender_is_brand", "brand_not_via_gateway",
                     "url_count", "url_unofficial", "cell_registered", "sender_sms_1h"]
# Danh sách feature audio dùng để phân biệt voice scam, deepfake và call bình thường.
AUDIO_FEATURES = ["vocoder_artifact", "pitch_jitter", "spectral_flatness", "speaker_drift", "codec_2g",
                  "scam_lexicon_hits", "caller_age_days", "caller_known",
                  # Ai nói bao nhiêu (VAD): người gọi im lặng trong khi giọng người nghe bị ghi lại = thu giọng.
                  "caller_speech_ratio", "callee_speech_s"]


# Xóa các phần tử cũ khỏi deque để giữ cửa sổ trượt luôn là dữ liệu trong khoảng thời gian hợp lệ.
_MISSING = object()


def _insort_time(q, ts):
    """Append keeping event-time order; out-of-order inserts are bounded by allowed lateness."""
    if not q or q[-1] <= ts:
        q.append(ts)
    else:
        bisect.insort(q, ts)


def _prune(dq: deque, horizon: float):
    while dq and dq[0][0] < horizon:
        dq.popleft()


class FarmCellWindow:
    """Incremental counts over one cell's (ts, sim, callee) deque for farm_cell_sims_1h.

    Keeps, per SIM, its number of calls and distinct callees in the window, and how many SIMs are
    "bursty" (>= 2 calls to >= 2 numbers). Every add/expire is O(1), so a busy cell (or the first 6 h
    after a fresh deployment, when every SIM is "newly active") does not make each call O(window).
    The deque is the serialised state; these counts are derived and rebuilt from it when needed.
    """
    __slots__ = ("q", "calls", "dialled", "bursty")

    def __init__(self, q):
        self.q, self.calls, self.dialled, self.bursty = q, {}, {}, 0
        for _, sim, callee in q:
            self._count(sim, callee, 1)

    def _is_bursty(self, sim):
        return self.calls.get(sim, 0) >= 2 and len(self.dialled.get(sim, ())) >= 2

    def _count(self, sim, callee, sign):
        before = self._is_bursty(sim)
        n = self.calls.get(sim, 0) + sign
        d = self.dialled.setdefault(sim, {})
        c = d.get(callee, 0) + sign
        if c:
            d[callee] = c
        else:
            d.pop(callee, None)
        if n:
            self.calls[sim] = n
        else:
            self.calls.pop(sim, None)
            self.dialled.pop(sim, None)
        self.bursty += int(self._is_bursty(sim)) - int(before)

    def add(self, item):
        q = self.q
        if not q or q[-1] <= item:
            q.append(item)
        else:
            bisect.insort(q, item)
        self._count(item[1], item[2], 1)

    def expire(self, horizon):
        q = self.q
        while q and q[0][0] < horizon:
            _, sim, callee = q.popleft()
            self._count(sim, callee, -1)

    def count_for(self, caller, ts):
        """|bursty SIMs| if ``caller`` is one of them, counting only entries with t <= ts, else 0."""
        future = []
        for item in reversed(self.q):        # other parties' slightly newer calls (<= allowed lateness)
            if item[0] <= ts:
                break
            future.append(item)
        for _, sim, callee in future:
            self._count(sim, callee, -1)
        value = self.bursty if self._is_bursty(caller) else 0
        for _, sim, callee in future:
            self._count(sim, callee, 1)
        return value


# FeatureStore lưu toàn bộ state lịch sử để tính feature causally trên luồng event.
class FeatureStore:
# Khởi tạo deque, dict và trạng thái theo thuê bao, IMEI, cell để tính feature nhanh O(1).
    def __init__(self):
        self.oc1h = defaultdict(deque)             # msisdn -> deque[(ts, callee)] outgoing, last hour
        self.callee_cnt = defaultdict(dict)        # msisdn -> {callee: n} within the last hour
        self.oc10 = defaultdict(deque)             # msisdn -> deque[ts] outgoing, last 10 min
        self.oc1 = defaultdict(deque)              # msisdn -> deque[ts] outgoing, last minute
        self.in_calls = defaultdict(deque)         # msisdn -> deque[ts] incoming, last 24h
        self.out24 = {}                            # msisdn -> deque[exact event timestamps]
        self.pending_cdr = defaultdict(list)      # msisdn -> min-heap[(end_ts, duration)]
        self.done_durations = defaultdict(lambda: deque(maxlen=20))
        self.imei_by_msisdn = defaultdict(dict)    # msisdn -> {imei: last_ts}
        self.msisdn_by_imei = defaultdict(dict)    # imei -> {msisdn: last_ts}
        self.last_radio = {}                       # msisdn -> (ts, cell)
        self.new_sims_cell = defaultdict(dict)     # cell -> {msisdn: last_ts} for SIMs younger than 60 days
        self.sms_by_sender = defaultdict(deque)
        self.known_callees = defaultdict(dict)     # msisdn -> {callee: last_ts} (30-day memory)
        # D14: keys changed since the last checkpoint (base or delta). Not part of the state itself.
        self._dirty = defaultdict(set)
        self.new24 = defaultdict(deque)            # msisdn -> deque[(ts, 1 if new callee else 0)], last 24 h
        self.fresh_cell_calls = defaultdict(deque)  # radio cell -> deque[(ts, msisdn, callee)] of fresh SIMs, 1 h
        self._farm_agg = {}                        # derived FarmCellWindow per cell (not state; rebuilt lazily)
        self.age_by_msisdn = {}
        # Lần đầu thấy số hoạt động trên mạng (gọi, được gọi, nhắn tin) - khác với tuổi đăng ký SIM.
        # SIM cũ đi thuê / ngủ đông có tuổi đăng ký lớn nhưng vừa mới xuất hiện hoạt động.
        self.first_seen = {}
        self.last_seen = {}

    # ------------------------------------------------------------------ CALL (Tier 1)
# Tính toàn bộ feature của một event CALL dựa trên lịch sử phát sinh từ stream cho đến thời điểm đó.
    def _first(self, party, ts):
        previous = self.first_seen.get(party)
        if previous is None or ts < previous:
            self.first_seen[party] = ts
            return ts
        return previous

    def _touch(self, party, ts):
        # max(): with per-party frontiers (D1) another party's slightly older event may arrive later.
        previous = self.last_seen.get(party)
        self.last_seen[party] = ts if previous is None or ts > previous else previous

    def call_features(self, ev: dict) -> dict:
        ts, caller, callee = ev["ts"], ev["caller"], ev["callee"]
        cell, imei, origin = ev["cell_id"], ev["imei"], ev["origin_trunk"]
        self._touch(caller, ts)
        self._touch(callee, ts)
        age = ev.get("caller_age_days", -1)
        self.age_by_msisdn[caller] = age
        # min(): with per-party frontiers another party's NEWER event may have created the entry first.
        first = self._first(caller, ts)
        self._first(callee, ts)
        active_h = min((ts - first) / 3600.0, ACTIVE_CAP_H)

        # Cửa sổ trượt theo phút/10 phút/1 giờ giúp đếm nhịp gọi và độ phân tán callee hiệu quả.
        oc, cc = self.oc1h[caller], self.callee_cnt[caller]
        h = ts - H1
        while oc and oc[0][0] < h:
            _, old = oc.popleft()
            n = cc[old] - 1
            if n:
                cc[old] = n
            else:
                del cc[old]
        oc.append((ts, callee))
        cc[callee] = cc.get(callee, 0) + 1
        q10, q1 = self.oc10[caller], self.oc1[caller]
        while q10 and q10[0] < ts - M10:
            q10.popleft()
        while q1 and q1[0] < ts - M1:
            q1.popleft()
        q10.append(ts)
        q1.append(ts)
        n1h = len(oc)

        ic = self.in_calls[caller]
        while ic and ic[0] < ts - D1:
            ic.popleft()
        _insort_time(self.in_calls[callee], ts)
        out24 = self._count_out_24h(caller, ts)
        known = self.known_callees[caller]
        novel = 0 if callee in known else 1
        known[callee] = max(ts, known.get(callee, ts))
        nq = self.new24[caller]
        _prune(nq, ts - D1)
        nq.append((ts, novel))
        new_ratio = sum(x[1] for x in nq) / len(nq)
        # Only calls received at or before this event (another party's newer call may already be stored).
        incoming = len(ic) if not ic or ic[-1] <= ts else bisect.bisect_right(ic, ts)

        # Lịch sử call đã xong chỉ xuất hiện sau khi CDR biết duration, vì vậy short_call_ratio và mean_log_duration dựa trên dữ liệu hoàn chỉnh.
        pend = self.pending_cdr[caller]
        hist = self.done_durations[caller]
        while pend and pend[0][0] <= ts:
            hist.append(heapq.heappop(pend)[1])
        if hist:
            short = sum(1 for d in hist if d < 5.0) / len(hist)
            mlog = sum(math.log1p(d) for d in hist) / len(hist)
        else:
            short, mlog = -1.0, -1.0

        # Đồ thị IMEI/MSISDN phát hiện thay đổi thiết bị, SIM mới và TAC gateway bất thường.
        registered = cell in REGISTERED_CELLS
        unknown_radio = cell.startswith("X-")
        if imei:
            im = self.imei_by_msisdn[caller]
            im[imei] = max(ts, im.get(imei, ts))
            ms = self.msisdn_by_imei[imei]
            ms[caller] = max(ts, ms.get(caller, ts))
            d1 = ts - D1
            for links in (im, ms):
                for key in [k for k, last_ts in links.items() if last_ts < d1]:
                    del links[key]
            # Links refreshed by another party's newer event (<= allowed lateness ahead) are not counted.
            imei_cnt = len(im) if max(im.values()) <= ts else sum(1 for v in im.values() if v <= ts)
            msisdn_cnt = len(ms) if max(ms.values()) <= ts else sum(1 for v in ms.values() if v <= ts)
            gw = 1.0 if imei[:8] in GW_TACS else 0.0
        else:
            imei_cnt = msisdn_cnt = 0
            gw = 0.0

        # Tính vận tốc di chuyển và số SIM trẻ trên cell để phát hiện di chuyển bất thường hoặc BTS giả.
        vel = 0.0
        if registered:
            prev = self.last_radio.get(caller)
            if prev and prev[1] != cell:
                dt_h = max((ts - prev[0]) / 3600.0, 1 / 60)
                vel = min(_dist(REGISTERED_CELLS[prev[1]], REGISTERED_CELLS[cell]) / dt_h, 5000.0)
            self.last_radio[caller] = (ts, cell)

        new_sims = 0
        farm_sims = 0
        if registered or unknown_radio:
            nsc = self.new_sims_cell[cell]
            # SIM mới đăng ký HOẶC mới xuất hiện hoạt động (SIM cũ đi thuê không còn lọt qua đặc trưng này)
            fresh = 0 <= age < 60 or (age >= 0 and active_h < FRESH_ACTIVE_H)
            if fresh:
                nsc[caller] = max(ts, nsc.get(caller, ts))
            farm_sims = self._farm_cell(cell, ts, caller, callee, fresh,
                                        fresh and incoming == 0 and len(cc) == n1h and new_ratio == 1.0)
            if nsc:
                stale = [k for k, v in nsc.items() if v < h]
                for k in stale:
                    del nsc[k]
                new_sims = (len(nsc) if not nsc or max(nsc.values()) <= ts
                            else sum(1 for v in nsc.values() if v <= ts))

        # Trả về vector feature cuối cùng sau khi tất cả điều kiện thời gian và trạng thái đã được cập nhật.
        return {
            "calls_1m": len(q1), "calls_10m": len(q10), "calls_1h": n1h,
            "callee_dispersion_1h": len(cc) / n1h,
            "in_out_ratio_24h": (incoming + 1) / (out24 + 1),
            "incoming_24h": incoming,
            "short_call_ratio": short, "mean_log_duration": mlog, "completed_calls_seen": len(hist),
            "imei_per_msisdn_24h": imei_cnt, "msisdn_per_imei_24h": msisdn_cnt, "imei_gateway_tac": gw,
            "cell_registered": 1.0 if registered else 0.0,
            "cell_unknown_radio": 1.0 if unknown_radio else 0.0,
            "cell_trunk_or_intl": 1.0 if cell in ("TRUNK", "INTL") else 0.0,
            "velocity_kmh": vel, "new_sims_on_cell_1h": new_sims,
            "caller_age_days": age, "caller_foreign": 1.0 if age < 0 else 0.0,
            "origin_local": 1.0 if origin == "LOCAL" else 0.0,
            "origin_intl_gw": 1.0 if origin == "INTL_GW" else 0.0,
            "origin_bank_trunk": 1.0 if origin == "BANK_TRUNK" else 0.0,
            "cli_protected": 1.0 if caller in PROTECTED else 0.0,
            "protected_cli_mismatch": 1.0 if (caller in PROTECTED and origin != "BANK_TRUNK") else 0.0,
            "network_2g": 1.0 if ev["network"] == "2G" else 0.0,
            "hour_of_day": local_hour(ts),
            "active_hours_on_network": active_h,
            "new_callee_ratio_24h": new_ratio,
            "farm_cell_sims_1h": farm_sims,
        }

    def _farm_cell(self, cell, ts, caller, callee, fresh, profile):
        """Co-located farm SIMs (see CALL_FEATURES). Only raw-event facts of OTHER SIMs are used, so a
        scale-out shard that sees the other SIMs through side updates computes the same value."""
        w = self._farm_window(cell)
        if fresh:
            w.add((ts, caller, callee))
        w.expire(ts - H1)
        if not w.q:
            del self.fresh_cell_calls[cell]
            self._farm_agg.pop(cell, None)
            return 0
        return w.count_for(caller, ts) if profile else 0

    def _farm_window(self, cell):
        q = self.fresh_cell_calls[cell]
        w = self._farm_agg.get(cell)
        if w is None or w.q is not q:     # restored / rolled back / delta-applied deque: rebuild counts
            w = self._farm_agg[cell] = FarmCellWindow(q)
        return w

    # ------------------------------------------------------------------ scale-out: replicated side state
    # Khi chia tải theo số chủ gọi (mỗi shard sở hữu một phần thuê bao), đa số đặc trưng chỉ
    # cần lịch sử của chính chủ gọi. Ba nhóm đặc trưng lại phụ thuộc sự kiện của SỐ KHÁC:
    #   incoming_24h        <- cuộc gọi ĐẾN số này (do shard của người gọi xử lý)
    #   msisdn_per_imei_24h <- mọi SIM dùng cùng IMEI (SIM box xoay nhiều SIM trên một thiết bị)
    #   new_sims_on_cell_1h <- mọi SIM mới trên cùng cell (trạm BTS giả)
    # Mỗi shard vì thế nhận thêm luồng "side update" của toàn mạng và chỉ cập nhật các trạng thái
    # nhỏ này (vài thao tác dict mỗi sự kiện, không chấm mô hình). Đo trên 50.000 sự kiện test:
    # bỏ bước này làm SIM box tụt từ 82,4% xuống 58,8%; có bước này thì khớp chạy 1 tiến trình.
    def observe_foreign(self, ev: dict, owns) -> None:
        """Áp dụng tác động chéo khoá của một sự kiện mà shard này KHÔNG sở hữu.

        ``owns(msisdn) -> bool`` cho biết số nào thuộc shard này (chỉ những số đó mới cần hàng
        đợi cuộc gọi đến, để bộ nhớ không phình theo toàn mạng)."""
        et = ev.get("event_type")
        ts = ev["ts"]
        if et == "CALL":
            caller, callee = ev["caller"], ev["callee"]
            self._touch(caller, ts)
            self._touch(callee, ts)
            first = self._first(caller, ts)
            self._first(callee, ts)
            if owns(callee):
                _insort_time(self.in_calls[callee], ts)
            # Không dọn mục cũ ở đây (O(1) mỗi side update): call_features của shard sở hữu
            # luôn loại các mục quá 24 h / 1 h trước khi đếm, nên số đếm vẫn chính xác.
            imei = ev.get("imei")
            if imei:
                links = self.msisdn_by_imei[imei]
                links[caller] = max(ts, links.get(caller, ts))
            cell = ev["cell_id"]
            if cell in REGISTERED_CELLS or cell.startswith("X-"):
                age = ev.get("caller_age_days", -1)
                if 0 <= age < 60 or (age >= 0 and min((ts - first) / 3600.0, ACTIVE_CAP_H) < FRESH_ACTIVE_H):
                    cells = self.new_sims_cell[cell]
                    cells[caller] = max(ts, cells.get(caller, ts))
                    # pruned by the owner's call_features / prune_state
                    self._farm_window(cell).add((ts, caller, ev["callee"]))
        elif et == "SMS":
            sender, recipient = ev["sender"], ev.get("recipient")
            self._touch(sender, ts)
            self._first(sender, ts)
            if recipient:
                self._touch(recipient, ts)
                self._first(recipient, ts)
        elif et == "AUDIO":
            self._touch(ev["caller"], ts)

    # ------------------------------------------------------------------ checkpoint / restore
    # The whole store lives in RAM, so a process restart loses every window and the 24 h warm-up
    # starts again. These two methods turn the state into plain data and back, so a deployment can
    # checkpoint periodically and resume from the last checkpoint instead of re-reading a day of
    # traffic. Everything is JSON-shaped on purpose: it survives a version change of this file.
    CHECKPOINT_VERSION = 2
    FEATURE_CONTRACT = "causal-v4-exact-24h-completion-order-bnumber-farmcell"
    STATE_DEQUES = ("oc1h", "oc10", "oc1", "in_calls", "done_durations", "sms_by_sender", "new24", "fresh_cell_calls")
    STATE_DICTS = ("callee_cnt", "imei_by_msisdn", "msisdn_by_imei", "new_sims_cell", "known_callees")
    STATE_PLAIN = ("last_radio", "age_by_msisdn", "first_seen", "last_seen")

    def state_dict(self) -> dict:
        """Versioned snapshot retaining exact event timestamps in outgoing windows."""
        st = {"version": self.CHECKPOINT_VERSION, "feature_contract": self.FEATURE_CONTRACT}
        for name in self.STATE_DEQUES:
            st[name] = {k: list(v) for k, v in getattr(self, name).items() if v}
        for name in self.STATE_DICTS:
            st[name] = {k: dict(v) for k, v in getattr(self, name).items() if v}
        for name in self.STATE_PLAIN:
            st[name] = dict(getattr(self, name))
        st["pending_cdr"] = {k: sorted(v) for k, v in self.pending_cdr.items() if v}
        st["out24"] = {k: list(v) for k, v in self.out24.items() if v}
        return st

    def load_state_dict(self, st: dict) -> None:
        """Restore atomically; old minute counts cannot reconstruct exact timestamps."""
        version = st.get("version")
        if version not in (1, self.CHECKPOINT_VERSION):
            raise ValueError(f"unsupported feature-store checkpoint version: {version!r}")
        if version == 1 and any(v and v[0] for v in st.get("out24", {}).values()):
            raise ValueError("v1 checkpoint contains approximate minute buckets; replay 24 h of history "
                             "to rebuild exact v2 windows before resuming")
        if version == 2 and st.get("feature_contract") != self.FEATURE_CONTRACT:
            raise ValueError("feature-store checkpoint uses a different feature contract")
        restored = FeatureStore()
        for name in self.STATE_DEQUES:
            maxlen = 20 if name == "done_durations" else None
            for k, values in st.get(name, {}).items():
                getattr(restored, name)[k] = deque(
                    (tuple(x) if isinstance(x, list) else x for x in values), maxlen=maxlen)
        for name in self.STATE_DICTS:
            for k, values in st.get(name, {}).items():
                getattr(restored, name)[k] = dict(values)
        for name in self.STATE_PLAIN:
            for k, value in st.get(name, {}).items():
                getattr(restored, name)[k] = tuple(value) if isinstance(value, list) else value
        for k, values in st.get("pending_cdr", {}).items():
            heap = [(float(end), float(duration)) for end, duration in values]
            if any(not math.isfinite(end) or not math.isfinite(duration) or duration < 0
                   for end, duration in heap):
                raise ValueError("invalid pending CDR in checkpoint")
            heapq.heapify(heap)
            restored.pending_cdr[k] = heap
        if version == 2:
            for k, values in st.get("out24", {}).items():
                times = [float(t) for t in values]
                if any(not math.isfinite(t) for t in times) or times != sorted(times):
                    raise ValueError("out24 timestamps must be finite and in event-time order")
                restored.out24[k] = deque(times)
        if not restored.last_seen:
            restored.last_seen.update(restored.first_seen)
            for k, values in restored.oc1h.items():
                if values:
                    restored.last_seen[k] = max(restored.last_seen.get(k, -math.inf), values[-1][0])
        self.__dict__.update(restored.__dict__)

    def prune_state(self, ts: float, *, metadata_ttl_s: float | None = None) -> dict:
        """Compact expired state periodically, outside the scoring hot path.

        Metadata persists by default. An explicit inactivity TTL >= 24 h forgets first_seen and
        completed-call history for idle subscribers: their next event starts fresh history.
        """
        ts = float(ts)
        if not math.isfinite(ts):
            raise ValueError("prune timestamp must be finite")
        if metadata_ttl_s is not None and (not math.isfinite(metadata_ttl_s) or metadata_ttl_s < D1):
            raise ValueError("metadata_ttl_s must be at least 24 h")
        dirty = self._dirty
        for caller, q in list(self.oc1h.items()):
            cc = self.callee_cnt[caller]
            if q and q[0][0] < ts - H1:
                dirty["oc1h"].add(caller)
                dirty["callee_cnt"].add(caller)
            if not q:
                dirty["oc1h"].add(caller)
                dirty["callee_cnt"].add(caller)
            while q and q[0][0] < ts - H1:
                _, callee = q.popleft()
                cc[callee] -= 1
                if not cc[callee]:
                    del cc[callee]
            if not q:
                self.oc1h.pop(caller, None)
                self.callee_cnt.pop(caller, None)
        for name, horizon, tuples in (("oc10", M10, False), ("oc1", M1, False),
                                      ("in_calls", D1, False), ("sms_by_sender", H1, True), ("new24", D1, True),
                                      ("fresh_cell_calls", H1, True),
                                      ("out24", D1, False)):
            mapping = getattr(self, name)
            for key, q in list(mapping.items()):
                if q and (q[0][0] if tuples else q[0]) < ts - horizon:
                    dirty[name].add(key)
                while q and (q[0][0] if tuples else q[0]) < ts - horizon:
                    q.popleft()
                if not q:
                    dirty[name].add(key)
                    del mapping[key]
        self._farm_agg.clear()   # fresh_cell_calls deques were trimmed in place: derived counts are stale
        for name, horizon in (("imei_by_msisdn", D1), ("msisdn_by_imei", D1), ("new_sims_cell", H1),
                              ("known_callees", KNOWN_CALLEE_TTL_S)):
            mapping = getattr(self, name)
            for key, links in list(mapping.items()):
                for link in [k for k, last_ts in links.items() if last_ts < ts - horizon]:
                    del links[link]
                    dirty[name].add(key)
                if not links:
                    dirty[name].add(key)
                    del mapping[key]
        for caller, pending in list(self.pending_cdr.items()):
            if not pending or pending[0][0] <= ts:
                dirty["pending_cdr"].add(caller)
                dirty["done_durations"].add(caller)
            while pending and pending[0][0] <= ts:
                self.done_durations[caller].append(heapq.heappop(pending)[1])
            if not pending:
                del self.pending_cdr[caller]
        evicted = 0
        if metadata_ttl_s is not None:
            for party, last_ts in list(self.last_seen.items()):
                if last_ts < ts - metadata_ttl_s and party not in self.pending_cdr:
                    for name in ("first_seen", "last_seen", "last_radio", "age_by_msisdn", "done_durations"):
                        getattr(self, name).pop(party, None)
                        dirty[name].add(party)
                    evicted += 1
        return {**self.state_stats(), "metadata_evicted": evicted}

    # ------------------------------------------------------------------ D14: incremental checkpoints
    _DELTA_NAMES = STATE_DEQUES + STATE_DICTS + STATE_PLAIN + ("pending_cdr", "out24")

    def take_delta(self) -> dict:
        """Changed keys since the last checkpoint, in state_dict encoding; None marks a deleted key.

        A periodic checkpoint used to serialise the WHOLE store inside the owner's write transaction.
        A delta is proportional to what changed in the interval, not to the number of subscribers.
        """
        out = {}
        for name, keys in self._dirty.items():
            mapping = getattr(self, name)
            entries = {}
            for key in keys:
                if key not in mapping:
                    entries[key] = None
                    continue
                value = mapping[key]
                if name in self.STATE_DEQUES or name == "out24":
                    entries[key] = list(value) if value else None
                elif name in self.STATE_DICTS:
                    entries[key] = dict(value) if value else None
                elif name == "pending_cdr":
                    entries[key] = sorted(value) if value else None
                else:
                    entries[key] = list(value) if isinstance(value, tuple) else value
            if entries:
                out[name] = entries
        self._dirty = defaultdict(set)
        return out

    def apply_delta(self, delta: dict) -> None:
        for name, entries in delta.items():
            if name not in self._DELTA_NAMES:
                raise ValueError(f"unknown feature-store delta section: {name}")
            mapping = getattr(self, name)
            for key, value in entries.items():
                if value is None:
                    mapping.pop(key, None)
                elif name in self.STATE_DEQUES:
                    mapping[key] = deque((tuple(x) if isinstance(x, list) else x for x in value),
                                         maxlen=20 if name == "done_durations" else None)
                elif name == "out24":
                    mapping[key] = deque(float(t) for t in value)
                elif name in self.STATE_DICTS:
                    mapping[key] = dict(value)
                elif name == "pending_cdr":
                    heap = [(float(a), float(b)) for a, b in value]
                    heapq.heapify(heap)
                    mapping[key] = heap
                else:
                    mapping[key] = tuple(value) if isinstance(value, list) else value
        self._dirty = defaultdict(set)

    def snapshot_touched(self, events):
        """Shallow copies of exactly the keys a batch can mutate (see transaction())."""
        touched = defaultdict(set)
        for ev in events:
            et = ev.get("event_type")
            caller, callee = ev.get("caller"), ev.get("callee")
            if et == "CALL":
                for name in ("oc1h", "callee_cnt", "oc10", "oc1", "out24", "pending_cdr",
                             "done_durations", "imei_by_msisdn", "last_radio", "age_by_msisdn",
                             "known_callees", "new24"):
                    touched[name].add(caller)
                touched["in_calls"].update((caller, callee))
                touched["msisdn_by_imei"].add(ev.get("imei"))
                touched["new_sims_cell"].add(ev.get("cell_id"))
                touched["fresh_cell_calls"].add(ev.get("cell_id"))
                for name in ("first_seen", "last_seen"):
                    touched[name].update((caller, callee))
            elif et == "SMS":
                sender, recipient = ev.get("sender"), ev.get("recipient")
                touched["sms_by_sender"].add(sender)
                for name in ("first_seen", "last_seen"):
                    touched[name].update((sender, recipient))
            elif et == "AUDIO":
                touched["last_seen"].add(caller)
        return {name: {key: copy.copy(getattr(self, name)[key]) if key in getattr(self, name) else _MISSING
                       for key in keys if key is not None} for name, keys in touched.items()}

    def restore_touched(self, before):
        for name, values in before.items():
            mapping = getattr(self, name)
            for key, value in values.items():
                if value is _MISSING:
                    mapping.pop(key, None)
                else:
                    mapping[key] = value

    @contextmanager
    def isolated(self, events):
        """Score events WITHOUT keeping their effect (retrospective rescoring).

        Copies only the keys those events touch. The previous implementation copied and reloaded the
        whole store, which with G2 ran every second under load and stalled the owner thread at scale.
        """
        before = self.snapshot_touched(events)
        try:
            yield
        finally:
            self.restore_touched(before)

    @contextmanager
    def transaction(self, events):
        """Rollback keys touched by one scoring batch if inference/policy fails.

        Copies are limited to the batch's subscribers, devices and cells. Queue elements and
        association values are immutable, so a shallow container copy preserves exact state.
        This is still proportional to affected histories, not a global feature-store snapshot.
        """
        before = self.snapshot_touched(events)
        try:
            yield
        except BaseException:
            self.restore_touched(before)
            raise
        for name, values in before.items():     # committed: these keys go into the next delta
            self._dirty[name].update(values)

    def state_stats(self) -> dict:
        """Operational counts without subscriber identifiers."""
        return {"checkpoint_version": self.CHECKPOINT_VERSION, "subscribers": len(self.first_seen),
                "window_events": sum(len(q) for name in ("oc1h", "oc10", "oc1", "in_calls", "sms_by_sender", "out24")
                                     for q in getattr(self, name).values()),
                "pending_cdr": sum(map(len, self.pending_cdr.values())),
                "imei_associations": sum(map(len, self.imei_by_msisdn.values())),
                "cell_associations": sum(map(len, self.new_sims_cell.values()))}


# Ghi checkpoint ra file tạm rồi replace atomically để không để lại file nửa chừng nếu crash.
    def save_checkpoint(self, path) -> None:
        import json as _json
        from pathlib import Path as _Path
        p = _Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(_json.dumps(self.state_dict()), encoding="utf-8")
        tmp.replace(p)          # atomic: a crash mid-write never leaves a half checkpoint

# Đọc file checkpoint JSON và gắn state vào FeatureStore hiện tại.
    def load_checkpoint(self, path) -> None:
        import json as _json
        from pathlib import Path as _Path
        self.load_state_dict(_json.loads(_Path(path).read_text(encoding="utf-8")))

    def _count_out_24h(self, msisdn, ts):
        """Count the closed event-time window [ts - 24 h, ts], including this event."""
        q = self.out24.setdefault(msisdn, deque())
        q.append(float(ts))
        while q and q[0] < ts - D1:
            q.popleft()
        return len(q)

    def commit_cdr(self, ev: dict):
        """Queue CDR by completion time, independently of setup/arrival order.

        Replay supplies setup ts + duration; live feeds may supply end_ts or start_ts explicitly.
        A future completion is invisible until a later call reaches that event time.
        """
        duration = float(ev["duration_s"])
        end_ts = float(ev["end_ts"]) if "end_ts" in ev else float(ev.get("start_ts", ev["ts"])) + duration
        if not math.isfinite(duration) or duration < 0 or not math.isfinite(end_ts):
            raise ValueError("CDR duration must be finite and non-negative; end_ts must be finite")
        heapq.heappush(self.pending_cdr[ev["caller"]], (end_ts, duration))
        self._dirty["pending_cdr"].add(ev["caller"])

    # ------------------------------------------------------------------ SMS
# Tính feature SMS dựa trên route, sender, URL và lưu lượng gần đây của sender đó.
    def sms_features(self, ev: dict) -> dict:
        ts, sender = ev["ts"], ev["sender"]
        self._touch(sender, ts)
        recipient = ev.get("recipient")
        if recipient:
            self._touch(recipient, ts)
        self._first(sender, ts)
        if recipient:
            self._first(recipient, ts)
        q = self.sms_by_sender[sender]
        _prune(q, ts - H1)
        q.append((ts,))
        doms = URL_RE.findall(ev["text"])
        route = ev["sms_route"]
        brand = sender in BRANDS
        return {
            "route_brand_gw": 1.0 if route == "BRAND_GW" else 0.0,
            "route_p2p": 1.0 if route == "P2P" else 0.0,
            "route_unverified_bts": 1.0 if route == "UNVERIFIED_BTS" else 0.0,
            "sender_is_brand": 1.0 if brand else 0.0,
            "brand_not_via_gateway": 1.0 if (brand and route != "BRAND_GW") else 0.0,
            "url_count": float(len(doms)),
            "url_unofficial": 1.0 if any(d.lower() not in OFFICIAL for d in doms) else 0.0,
            "cell_registered": 1.0 if (ev["cell_id"] in REGISTERED_CELLS or ev["cell_id"] == "SMSC") else 0.0,
            "sender_sms_1h": float(len(q)),
        }

    # ------------------------------------------------------------------ AUDIO (Tier 2)
# Tính feature audio từ transcript, codec và age của caller để cảnh báo voice scam.
    def audio_features(self, ev: dict) -> dict:
        self._touch(ev["caller"], ev["ts"])
        tr = ev.get("transcript", "")
        age = self.age_by_msisdn.get(ev["caller"], -2)
        return {
            "vocoder_artifact": ev["vocoder_artifact"], "pitch_jitter": ev["pitch_jitter"],
            "spectral_flatness": ev["spectral_flatness"], "speaker_drift": ev["speaker_drift"],
            "codec_2g": 1.0 if ev["codec"].startswith("AMR") else 0.0,
            "scam_lexicon_hits": float(sum(1 for k in SCAM_LEXICON if k in tr)),
            "caller_age_days": float(age), "caller_known": 0.0 if age == -2 else 1.0,
            "caller_speech_ratio": float(ev.get("caller_speech_ratio", 0.5)),
            "callee_speech_s": float(ev.get("callee_speech_s", 0.0)),
        }


# Tính khoảng cách địa lý giữa hai cell để ước lượng vận tốc di chuyển mỗi lần chuyển cell.
def _dist(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 12742.0 * math.asin(math.sqrt(h))


# Đo thời gian chạy của một hàm feature và trả về cả kết quả và latency tính bằng ms.
def timed(fn, ev):
    t = time.perf_counter()
    out = fn(ev)
    return out, (time.perf_counter() - t) * 1000.0
