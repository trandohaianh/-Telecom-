"""Live engine behind the HTTP service: replays the demo stream or serves live network events.

Owns the guarded two-tier detector, the durable journal (BackendState), the evidence log, the
inference runtime thread and the operations agent. src/server.py creates exactly one instance in
its lifespan and exposes it through the FastAPI routes; this module has no HTTP routes of its own.
Split out of server.py (which had grown past 1 400 lines) without changing behaviour.
"""

from __future__ import annotations

import collections
import os
import queue
import secrets
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import numpy as np
from fastapi import HTTPException

import security
from agent import OpsAgent
from backend_evidence import DurableEvidenceLog
from backend_model_identity import loaded_model_identity
from backend_runtime import InferenceRuntime
from backend_state import BackendState
from config import DATA_DIR, LOG_DIR, MODEL_DIR, SLA_TIER1_MS, SLA_TIER2_MS
from data_generator import TrafficSimulator
from detector import Detector
from events_io import load_events
from evidence import verify_in_subprocess
from resilience import GuardedDetector
from security import DEMO_MODE, AuditLog, mask_imei, mask_number

# Tốc độ phát sự kiện mẫu trên giao diện; đây là tốc độ demo, không phải năng lực cực đại của engine.
DEMO_RATE = 300  # sự kiện/giây hiển thị trên dashboard; engine đạt khoảng 15k/s mỗi lõi
# Đặt file log bằng chứng trong thư mục log do cấu hình v2 quản lý.
EVIDENCE_PATH = Path(
    os.environ.get("FRAUD_EVIDENCE_FILE", LOG_DIR / ("evidence_demo.jsonl" if DEMO_MODE else "evidence_live.jsonl"))
)


def _demo_evidence_cap() -> int:
    raw = os.environ.get("FRAUD_DEMO_EVIDENCE_MAX_MB") or "256"
    try:
        mb = int(raw)
    except ValueError:
        raise ValueError("FRAUD_DEMO_EVIDENCE_MAX_MB must be an integer") from None
    if not 8 <= mb <= 65536:
        raise ValueError("FRAUD_DEMO_EVIDENCE_MAX_MB must be within [8, 65536]")
    return mb << 20


class LiveEngine:
    # Chuẩn bị dữ liệu, mô hình, hàng đợi và các bộ đếm dùng chung cho phiên dashboard.
    def __init__(
        self,
        *,
        demo_mode=None,
        state_path=None,
        evidence_path=None,
        detector_factory=Detector,
        start_workers=True,
        audit_log=None,
    ):
        self.demo_mode = DEMO_MODE if demo_mode is None else demo_mode
        # Nhật ký thao tác dùng chung với API (server.audit); test tạo engine riêng thì nhận một AuditLog mặc định.
        self.audit_log = audit_log if audit_log is not None else AuditLog()
        self._stop = threading.Event()
        self._stream_thread = None
        self.lock = threading.RLock()
        self.det_lock = threading.RLock()
        self._inject_lock = threading.Lock()
        self.events = []
        if detector_factory is Detector:
            det = Detector(require_manifest=os.environ.get("FRAUD_REQUIRE_MANIFEST", "0") == "1")
        else:
            det = detector_factory()
        det.runtime_model_identity = loaded_model_identity(det, MODEL_DIR if detector_factory is Detector else None)
        det.keep_features = not self.demo_mode and os.environ.get("FRAUD_CAPTURE_FEATURES", "1") == "1"
        if self.demo_mode:
            self.events = load_events(DATA_DIR / "test_events_50k.csv")
            det.prime(load_events(DATA_DIR / "test_warmup_24h_events.csv.gz"))
        det.warm_up()
        self.g = GuardedDetector(det, budget_ms=SLA_TIER1_MS, content_budget_ms=SLA_TIER2_MS)
        self.store = BackendState(
            None if self.demo_mode else (state_path or os.environ.get("FRAUD_STATE_DB", LOG_DIR / "live_state.sqlite3"))
        )
        # The runtime acquires the exclusive owner before any live-state replay.
        self.evidence_path = evidence_path or EVIDENCE_PATH
        try:
            self.store.acquire_owner()
            # Demo evidence rotates (simulated traffic, ~1.25 GB/day otherwise); live evidence never does.
            self.log = DurableEvidenceLog(
                self.evidence_path, self.store, rotate_bytes=_demo_evidence_cap() if self.demo_mode else None
            )
        except BaseException:
            self.store.close()
            raise
        self.sim = (
            TrafficSimulator(seed=777, n_personal=300, n_callcentres=2, window="test") if self.demo_mode else None
        )
        self.inject_q: queue.Queue = queue.Queue(maxsize=64)
        self.network_mode = "shadow"  # An operator must deliberately re-enable enforcement after restart.
        self.network_stats = collections.Counter()
        self.counts = collections.Counter()
        # LATE/DUPLICATE trả ALLOW không chấm điểm; đếm riêng để không lẫn vào ALLOW thật (D1).
        self.ingest_counts = collections.Counter()
        # Quyết định của riêng CALL đã chấm điểm (ALLOW gồm cả FAIL_OPEN): cùng miền với baseline drift (D7).
        self.call_decisions = collections.Counter()
        self.by_type = collections.Counter()
        self.alerts = collections.deque(maxlen=60)
        self.lat = collections.deque(maxlen=3000)
        self.rate_hist = collections.deque(maxlen=120)
        self.processed = 0
        self.truth = collections.Counter()
        self.pos = 0
        self.offset, self.loops = 0.0, 0
        self.started = time.time()
        self.reputation = collections.defaultdict(collections.Counter)
        self.reports = collections.defaultdict(set)
        self.inbox = collections.defaultdict(lambda: collections.deque(maxlen=50))
        self.app_sessions = {}
        self.demo_numbers = [sub.msisdn for sub in self.sim.subs[:3]] if self.sim else []
        self.recent_hits = collections.deque(maxlen=3000)
        self.guardians = collections.defaultdict(set)
        self.invites = collections.defaultdict(set)
        try:
            self.runtime = InferenceRuntime(
                self.g,
                self.store,
                on_result=self._record_live_result,
                on_retro=self._record_retro,
                execution_lock=self.det_lock,
                start=False,
            )
        except BaseException:
            self.log.close()
            self.store.close()
            raise
        if not self.demo_mode:
            security.configure_store(self.store)
        self.agent = OpsAgent(self, self.audit_log, state_store=self.store if not self.demo_mode else None)
        if start_workers:
            self.runtime.start()
            self.agent.start()
            if self.demo_mode:
                self._stream_thread = threading.Thread(target=self._run, name="demo-replay", daemon=True)
                self._stream_thread.start()

    async def submit_event(self, event, budget_ms=None):
        budget = (
            budget_ms
            if budget_ms is not None
            else (SLA_TIER1_MS if event.get("event_type") == "CALL" else SLA_TIER2_MS)
        )
        return await self.runtime.submit("VERDICT", event, budget_ms=budget)

    async def record_observation(self, event, result):
        return await self.runtime.record_observation(event, result)

    def _record_live_result(self, event, result):
        self.log.submit(result)
        if "decision" not in result:
            return
        with self.lock:
            self.network_stats[result["decision"]] += 1
            ingest = result.get("ingest")
            if ingest and ingest != "ADMITTED":
                self.ingest_counts[ingest] += 1
            if result.get("record_type") == "audio_observation":
                # Real acoustic observations have their own schema and model domain.
                return
            self.processed += 1
            self.by_type[event["event_type"]] += 1
            if event["event_type"] == "CALL" and (not ingest or ingest == "ADMITTED"):
                # A late delivery rewrites decision to fail-open ALLOW; drift compares MODEL decisions.
                model = result.get("model_decision") if result.get("scored") else None
                self.call_decisions[model or result["decision"]] += 1
            self.counts[
                "FAIL_OPEN" if result.get("fail_open") and result["decision"] == "ALLOW" else result["decision"]
            ] += 1
            self.lat.append(result.get("latency_ms", 0.0))
            self._track(event, result)
            if result["decision"] != "ALLOW" or result.get("fail_open"):
                self._alert(result, event)

    def _record_retro(self, results):
        # Reuse primary outbox entries; only legacy/rescore entries need a write.
        prepared = []
        legacy = []
        for source in results:
            result = dict(source)
            event = {"event_id": result["event_id"], "event_type": result["event_type"]}
            if result.get("journal_ref"):
                prepared.append((event, result))
            else:
                legacy.append((event, result))
        if legacy:
            with self.store.transaction():
                for event, result in legacy:
                    seq, previous = self.store.reserve("RETRO", event)
                    if previous is not None:
                        continue
                    result["journal_ref"] = f"{self.store.get('store_id')}:{seq}"
                    self.store.finish(seq, result)
                    prepared.append((event, result))
        with self.lock:
            for event, result in prepared:
                self.log.submit(result)
                self._alert(result, event)
                if not result.get("requires_review"):
                    self.reputation[result.get("party")]["RETRO"] += 1

    def close(self):
        self._stop.set()
        self.agent.close()
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=5)
        self.runtime.close()
        self.log.close()
        if getattr(security, "_session_store", None) is self.store:
            security.configure_store(None)
        self.store.close()

    # --------------------------------------------------------------- stream loop
    # Phát dữ liệu định kỳ, tính điểm, cập nhật KPI/cảnh báo và trả kết quả cho request đang chờ.
    def _run(self):
        # Chờ server khởi động xong trước khi bắt đầu vòng xử lý có đo thời gian.
        if self._stop.wait(2.0):
            return
        # Khởi tạo mốc tính tốc độ và số sự kiện xử lý kể từ lần cập nhật gần nhất.
        last_tick, done_in_tick = time.time(), 0
        # Thread phục vụ dashboard chạy liên tục đến khi tiến trình server dừng.
        while not self._stop.is_set():
            # Gom các batch injection đang đợi và Future tương ứng để trả kết quả về API.
            batch, futures = [], []
            # Rút hết các yêu cầu đang có trong queue để chấm cùng batch dữ liệu nền.
            while not self.inject_q.empty():
                # Mỗi phần tử chứa danh sách sự kiện và Future của request gọi endpoint.
                evs, fut = self.inject_q.get()
                # Ghép các sự kiện được tiêm thành một batch duy nhất.
                batch += evs
                # Ghi số lượng sự kiện để chia lại kết quả sau khi chấm điểm.
                futures.append((len(evs), fut))
            # Số sự kiện nền mỗi nhịp 50 ms, tối thiểu là một sự kiện.
            n_stream = max(1, int(DEMO_RATE * 0.05))
            # Tạo danh sách sự kiện nền cho nhịp hiện tại.
            stream = []
            # Đọc liên tiếp n_stream sự kiện từ dataset kiểm thử.
            for _ in range(n_stream):
                # Sao chép dict để không sửa trực tiếp dữ liệu đã nạp trong bộ nhớ.
                ev = dict(self.events[self.pos])
                # Dịch thời gian sự kiện nếu đã phát lại dataset một vòng trở lên.
                ev["ts"] += self.offset  # replay loops keep time moving forward
                # Đổi ID ở các vòng lặp sau để tránh trùng khóa với lần phát đầu.
                if self.offset:
                    ev["event_id"] = f'{ev["event_id"]}-L{self.loops}'
                # Tiến sang sự kiện tiếp theo trong dataset.
                self.pos += 1
                # Khi hết dataset, quay lại đầu và tăng offset để timestamp vẫn đi tới trước.
                if self.pos == len(self.events):
                    self.pos, self.loops = 0, self.loops + 1
                    self.offset += self.events[-1]["ts"] - self.events[0]["ts"] + 1.0
                # Đưa sự kiện vừa đọc vào batch nền.
                stream.append(ev)
            # Bắt đầu đo thời gian chấm cả sự kiện nền và sự kiện tiêm vào.
            t = time.perf_counter()
            # Chấm toàn batch qua detector có bảo vệ (khóa: feature store không an toàn khi nhiều luồng ghi cùng lúc,
            # và API tích hợp với nhà mạng cũng chấm qua chính detector này).
            with self.det_lock:
                res = self.g.score_batch(batch + stream)
            # Tính tổng độ trễ của batch theo mili giây.
            dt = (time.perf_counter() - t) * 1000
            # Bảo vệ các trạng thái mà API khác có thể đọc trong lúc thread cập nhật.
            with self.lock:
                # Ghép từng sự kiện đầu vào với kết quả quyết định tương ứng.
                for ev, r in zip(batch + stream, res):
                    # Tăng tổng số sự kiện đã xử lý.
                    self.processed += 1
                    # Đếm fail-open tách riêng khỏi quyết định thông thường.
                    # (quyết định của luật dự phòng lúc mô hình lỗi vẫn được tính là BLOCK / FLAG).
                    self.counts["FAIL_OPEN" if r.get("fail_open") and r["decision"] == "ALLOW" else r["decision"]] += 1
                    # Đếm loại sự kiện để dashboard hiển thị tỷ lệ cuộc gọi/SMS/âm thanh.
                    self.by_type[ev["event_type"]] += 1
                    # Cập nhật thống kê bắt gian lận nếu event có nhãn mô phỏng.
                    if ev.get("label"):
                        self.truth["fraud_seen"] += 1
                        # Xem FLAG hoặc BLOCK là đã bắt được sự kiện gian lận.
                        if r["decision"] != "ALLOW":
                            self.truth["fraud_caught"] += 1
                    # Ghi nhận trường hợp hệ thống chặn nhầm một sự kiện hợp lệ.
                    elif r["decision"] == "BLOCK":
                        self.truth["legit_blocked"] += 1
                    # Gửi quyết định tới logger bằng chứng (logger tự áp dụng chính sách lấy mẫu).
                    self.log.submit(r)
                    # Ghi uy tín của số gọi/gửi và đưa sự kiện vào hộp thư nếu người nhận đang dùng app.
                    self._track(ev, r)
                    # Chỉ đưa sự kiện cần chú ý vào danh sách cảnh báo trực tiếp.
                    if r["decision"] != "ALLOW" or r.get("fail_open"):
                        self._alert(r, ev)
                # Ước lượng độ trễ trung bình mỗi sự kiện của batch.
                per_event = dt / max(len(res), 1)
                # Thêm tối đa 20 mẫu cho mỗi nhịp để lịch sử độ trễ phản ánh gần đúng hoạt động.
                for _ in range(min(len(res), 20)):
                    self.lat.append(per_event)
                # Cộng sản lượng của batch vào bộ đếm nhịp hiện tại.
                done_in_tick += len(res)
                # Đến mốc ít nhất một giây thì cập nhật thông lượng rồi đặt lại cửa sổ đo.
                now = time.time()
                if now - last_tick >= 1.0:
                    self.rate_hist.append(round(done_in_tick / (now - last_tick), 1))
                    last_tick, done_in_tick = now, 0
            # Phân đoạn kết quả theo kích thước ban đầu và hoàn tất các request injection.
            i = 0
            for n, fut in futures:
                fut.set_result(res[i : i + n])
                i += n
            # Sau khi hồi phục, chấm lại các sự kiện đã lọt lúc fail-open (ngoài đường quyết định, có giới hạn thời gian).
            with self.det_lock:
                retro = self.g.drain_rescore(max_n=128)
            if retro:
                with self.lock:
                    for r in retro:
                        self._alert(r, {"event_type": r["event_type"]})
                        self.log.submit(r)
                        self.reputation[r.get("party")]["RETRO"] += 1
            # Giới hạn nhịp phát để dashboard chạy ở tốc độ demo đã cấu hình.
            self._stop.wait(0.05)

    # _alert đưa một quyết định cần chú ý lên danh sách cảnh báo của dashboard (gọi khi đang giữ self.lock).
    def _alert(self, r, ev):
        # Chọn các trường phục vụ hiển thị, không sao chép toàn bộ kết quả.
        a = {
            k: r.get(k)
            for k in (
                "event_id",
                "event_type",
                "decision",
                "risk",
                "action_vi",
                "action_en",
                "reasons",
                "party",
                "counterparty",
                "zero_day",
                "fail_open",
                "degraded",
                "retro",
                "anomaly_score",
                "requires_review",
                "retro_context",
                "model_decision",
            )
        }
        # Đánh dấu sự kiện tiêm thử, loại gian lận mô phỏng (để đối chiếu) và đặc trưng làm căn cứ.
        a["injected"] = ev.get("injected", False)
        a["truth"] = ev.get("fraud_type", "")
        a["features"] = r.get("evidence_features", {})
        a["time"] = time.strftime("%H:%M:%S")
        a["imei"], a["cell"] = ev.get("imei"), ev.get("cell_id")
        a["human_override"] = r.get("human_override")
        self.alerts.appendleft(a)
        if r["decision"] != "ALLOW" and not r.get("requires_review"):
            self.recent_hits.append(
                {
                    "wall": time.time(),
                    "type": r["event_type"],
                    "decision": r["decision"],
                    "party": r.get("party"),
                    "imei": ev.get("imei"),
                    "cell": ev.get("cell_id"),
                    "event_id": r["event_id"],
                    "human_override": r.get("human_override"),
                    "caller_age_days": (r.get("evidence_features") or {}).get("caller_age_days"),
                }
            )
        self._to_inbox(r, ev)

    # _track ghi uy tín số gọi/gửi; chỉ quyết định khác ALLOW mới được đếm để bộ nhớ không phình theo lưu lượng sạch.
    def _track(self, ev, r):
        if r["decision"] != "ALLOW":
            self.reputation[r.get("party")][r["decision"]] += 1
        elif r.get("counterparty") in self.inbox:
            self._to_inbox(r, ev)

    # _push_inbox thêm một mục vào hộp thư và báo cho hook lưu bền (portal ghi CSDL ở luồng riêng).
    def _push_inbox(self, who, item):
        self.inbox[who].appendleft(item)
        hook = getattr(self, "inbox_hook", None)
        if hook is not None:
            hook(who, item)

    # _to_inbox đưa sự kiện vào hộp thư của người nhận, chỉ khi người đó đang dùng app (không lưu dữ liệu của người khác).
    def _to_inbox(self, r, ev):
        if r.get("requires_review"):
            return
        who = r.get("counterparty")
        if who not in self.inbox:
            return
        self._push_inbox(
            who,
            {
                "id": r["event_id"],
                "type": r["event_type"],
                "from": r.get("party"),
                "time": time.strftime("%H:%M:%S"),
                "decision": r["decision"],
                "action": r.get("action_vi"),
                "risk": r.get("risk"),
                "reasons": [x["vi"] for x in r.get("reasons", [])][:4],
                "text": ev.get("text", ""),
                "retro": bool(r.get("retro")),
                "degraded": bool(r.get("degraded")),
                "simulated": ev.get("app_kind", ""),
            },
        )
        # Bảo vệ người thân: báo cho người giám hộ khi người được bảo vệ gặp lừa đảo.
        # Chỉ gửi loại sự kiện và kết quả, không gửi nội dung tin nhắn (giữ riêng tư cho người được bảo vệ).
        if r["decision"] != "ALLOW" and r["event_type"] in ("CALL", "SMS", "AUDIO"):
            what = {"CALL": "cuộc gọi", "SMS": "tin nhắn", "AUDIO": "cuộc gọi (giọng nói)"}[r["event_type"]]
            verdict = "đã bị nhà mạng chặn" if r["decision"] == "BLOCK" else "nghi lừa đảo"
            for g in self.guardians.get(who, ()):
                if g in self.inbox:
                    self._push_inbox(
                        g,
                        {
                            "id": f"FAM-{r['event_id']}",
                            "type": "FAMILY",
                            "from": who,
                            "time": time.strftime("%H:%M:%S"),
                            "decision": r["decision"],
                            "action": f"Người thân của bạn vừa nhận một {what} {verdict}",
                            "reasons": [
                                f"Số gọi / gửi: {r.get('party')}",
                                "Hãy gọi hỏi thăm người thân, nhắc không chuyển tiền, không đọc OTP",
                            ],
                            "text": "",
                            "retro": False,
                            "degraded": False,
                            "simulated": "",
                        },
                    )

    # notify gửi cảnh báo do chuyên viên phê duyệt tới các thuê bao (người dùng app nhận ngay trong hộp thư).
    def notify(self, recipients, message):
        n = 0
        with self.lock:
            for who in recipients:
                if who in self.inbox:
                    self._push_inbox(
                        who,
                        {
                            "id": f"WARN-{int(time.time() * 1000)}-{n}",
                            "type": "WARN",
                            "from": "Nhà mạng",
                            "time": time.strftime("%H:%M:%S"),
                            "decision": "FLAG",
                            "action": message,
                            "reasons": [],
                            "text": "",
                            "retro": True,
                            "degraded": False,
                            "simulated": "",
                        },
                    )
                    n += 1
        return n

    # verify_evidence: AI agent gọi định kỳ. Thường chỉ kiểm phần mới ghi (nhẹ); 30 phút một lần kiểm toàn bộ
    # để bắt cả trường hợp sửa vào những dòng cũ đã kiểm.
    def verify_evidence(self):
        if not self.evidence_path.exists():
            return True, 0, None
        now = time.time()
        full = now - getattr(self, "_ev_full_at", 0.0) > 1800
        state = {} if full else dict(getattr(self, "_ev_state", {}))
        if state and self.evidence_path.stat().st_size < state.get("pos", 0):
            state = {}  # the demo log rotated: start again from the new file's first line
        # Child process: the hash-chain re-check must not compete with the inference owner for the GIL.
        ok, n, bad, state = verify_in_subprocess(self.evidence_path, state)
        if ok is None:
            self.evidence_verify_errors = getattr(self, "evidence_verify_errors", 0) + 1
            return True, n, None  # could not check: never report tampering without evidence
        if full and ok:
            self._ev_full_at = now
        if ok:
            self._ev_state = state
        return ok, n, bad

    # --------------------------------------------------------------- attack injection
    # Tạo một kiểu tấn công tổng hợp, gửi qua cùng detector rồi trả quyết định cho dashboard.
    def inject(self, kind: str, target: str | None = None):
        # Các luồng simulate-stream của nhiều người dùng chạy song song nhưng dùng chung self.sim.events.
        with self._inject_lock:
            return self._inject_unlocked(kind, target)

    def _inject_unlocked(self, kind: str, target: str | None = None):
        # Chọn timestamp sau sự kiện nền mới nhất để sự kiện thử nghiệm nằm ở tương lai gần.
        last_ts = self.events[(self.pos - 1) % len(self.events)]["ts"] + self.offset
        # Dùng simulator riêng cho thao tác injection để không đụng luồng dữ liệu nền.
        sim = self.sim
        # Xóa các sự kiện thử nghiệm còn lại từ lần bấm trước.
        sim.events = []
        # Khoảng thời gian sinh sự kiện của phần lớn kịch bản tấn công.
        t0, t1 = last_ts + 1, last_ts + 120
        # Mỗi nhánh gọi bộ sinh tương ứng với loại tấn công trên giao diện.
        if kind == "LEGIT":
            # Cuộc gọi bình thường: lấy một cuộc gọi hợp lệ có thật trong luồng (số gọi đã có lịch sử).
            legit = [e for e in self.events[:5000] if e["event_type"] == "CALL" and not e["label"]]
            ev = dict(legit[int(time.time() * 1000) % len(legit)])
            ev["ts"] = t0
            sim.events = [ev]
        elif kind == "SIM_BOX":
            sim.simbox(
                t0, t1, 3, (600, 900), gateway_tac_p=1.0, tag="DEMO", active_h=(0.05, 0.05), dedicated_imei=False
            )
        elif kind == "CLI_SPOOF":
            sim.cli_spoof(t0, t1, 1, tag="DEMO")
        elif kind == "SMS_PHISH":
            from data_generator import SCAM_DOMAINS_TEST, TEST_LURES

            sim.sms_phish(t0, t1, 1, TEST_LURES, SCAM_DOMAINS_TEST, tag="DEMO")
        elif kind == "DEEPFAKE_VOICE":
            sim.deepfake(t0, t1, 3, tag="DEMO")
        elif kind == "WANGIRI":
            sim.wangiri(t0, t0 + 400, 1, (1200, 1500), tag="DEMO", active_h=0.1)
        elif kind == "VOICE_HARVEST":
            sim.voice_harvest(t0, t0 + 400, 1, (700, 900), tag="DEMO", active_h=(0.05, 0.05), aged_p=1.0)
        else:
            # Từ chối loại tấn công không được hỗ trợ thay vì trả kết quả demo giả.
            raise HTTPException(400, f"unknown attack {kind}")
        # Sắp xếp theo thời gian và giới hạn số event gửi vào hệ thống.
        evs = sorted(sim.events, key=lambda e: e["ts"])[:25]
        # Gán ID riêng của phiên demo và cờ để giao diện nhận biết event do người dùng tiêm.
        for k, e in enumerate(evs):
            e["event_id"] = f"INJ-{kind[:3]}-{secrets.token_hex(6)}-{k}"
            e["injected"] = True
        # Nhắm vào số của người dùng app. Với kiểu tấn công "rải" (SIM box, Wangiri) chỉ cuộc cuối tới người
        # dùng: họ là một trong nhiều người nhận, nên đổi mọi người nhận thành một số sẽ làm sai hành vi.
        if target:
            spread = kind in ("SIM_BOX", "WANGIRI")
            mine = [e for e in evs if e["event_type"] in ("CALL", "AUDIO", "SMS")]
            if kind == "VOICE_HARVEST":
                # one of many victims: the last answered call that got in-call audio, plus that audio
                audio = [e for e in evs if e["event_type"] == "AUDIO"]
                a = audio[-1] if audio else None
                mine = [e for e in evs if a and e["callee"] == a["callee"] and e["caller"] == a["caller"]] or mine[-1:]
            for e in mine[-1:] if spread else mine:
                e["callee" if e["event_type"] != "SMS" else "recipient"] = target
                e["app_kind"] = kind
        # Tạo Future rồi chuyển batch sang thread nền xử lý đồng bộ với dữ liệu đang phát.
        fut: Future = Future()
        try:
            self.inject_q.put_nowait((evs, fut))
        except queue.Full:
            raise HTTPException(503, "demo injection queue is full") from None
        # Chờ tối đa 10 giây; timeout sẽ báo lỗi thay vì giả vờ injection thành công.
        res = fut.result(timeout=10)
        # Chỉ trả các trường cần thiết để giao diện thống kê và hiển thị lý do.
        return [
            {
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "decision": r["decision"],
                "risk": r.get("risk"),
                "reasons": r.get("reasons", []),
                "zero_day": r.get("zero_day", False),
                "to_target": bool(target) and r.get("counterparty") == target,
                "party": r.get("party"),
                "action": r.get("action_vi"),
                "text": e.get("text", ""),
            }
            for e, r in zip(evs, res)
        ]

    # Tạo bản chụp nhất quán các bộ đếm và số liệu mà API trạng thái trả về.
    def state(self, mask: bool = False):
        # Khóa các cấu trúc đang được thread stream cập nhật.
        with self.lock:
            alerts = list(self.alerts)[:40]
            # Vai trò người xem chỉ thấy số đã che, đủ để theo dõi cảnh báo nhưng không định danh được thuê bao.
            if mask:
                alerts = [
                    dict(
                        a,
                        party=mask_number(a.get("party")),
                        counterparty=mask_number(a.get("counterparty")),
                        imei=mask_imei(a.get("imei")),
                    )
                    for a in alerts
                ]
            # Chuyển mẫu latency thành mảng để tính các phân vị; dùng 0 nếu chưa có mẫu.
            lat = np.array(self.lat) if self.lat else np.array([0.0])
            # Lấy trạng thái circuit breaker hiện tại từ lớp bảo vệ.
            cb = self.g.cb
            # Trả JSON-friendly values để API có thể mã hóa thành JSON.
            return {
                "model_version": self.runtime.model_version,
                "model_identity": self.runtime.model_identity,
                "model_manifest": getattr(self.g.det, "model_manifest", {"status": "legacy_unverified"}),
                "uptime_s": round(time.time() - self.started),
                "processed": self.processed,
                "decisions": dict(self.counts),
                "by_type": dict(self.by_type),
                "ingest_unscored": dict(self.ingest_counts),
                "call_decisions": dict(self.call_decisions),
                "eps": self.rate_hist[-1] if self.rate_hist else 0,
                "eps_history": list(self.rate_hist),
                "latency_ms": {
                    "p50": round(float(np.percentile(lat, 50)), 3),
                    "p99": round(float(np.percentile(lat, 99)), 3),
                },
                "breaker": {"state": cb.state, **cb.stats},
                "chaos": {"delay_ms": self.g.chaos_delay_ms, "error": self.g.chaos_error},
                "resilience": {
                    **self.g.stats,
                    "rescore_pending": len(self.g.rescore_queue),
                    "rescore_capacity": self.g.rescore_queue.maxlen,
                    "denylist_size": len(self.g.fallback.deny),
                },
                "evidence_written": self.log.written,
                "truth": dict(self.truth),
                "alerts": alerts,
                "demo_mode": self.demo_mode,
                "inference": {
                    **self.runtime.stats,
                    "queue_size": self.runtime.queue.qsize(),
                    "queue_capacity": self.runtime.queue.maxsize,
                },
            }
