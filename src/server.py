"""FastAPI service + live dashboard for the Telecom Fraud Detection MVP.

A background thread replays the independent test stream (after priming the feature store with the
24 h warm-up) through the guarded two-tier detector, writes evidence, and keeps live counters.
Attack injection and chaos (latency / failure) endpoints drive the demo.

Run:  python run_demo.py   ->  http://localhost:8080
"""
# Docstring trên mô tả thread replay dữ liệu, detector có bảo vệ và các API demo.
# Bật cú pháp annotation hiện đại mà không cần đánh giá một số kiểu ngay khi import module.
from __future__ import annotations

# collections cung cấp bộ đếm và hàng đợi có giới hạn cho trạng thái dashboard.
import asyncio
import collections
import os
from pathlib import Path
# contextlib tạo lifespan (khởi động / tắt) cho FastAPI.
import contextlib
# gc dùng để dọn rác và đóng băng các đối tượng dùng chung trước khi stream chạy.
# json đọc các file số liệu đã lưu và giải mã bản ghi bằng chứng.
import json
import re
# queue chuyển các sự kiện thử tấn công từ request API sang thread xử lý.
import queue
# threading chạy luồng giả lập song song với máy chủ web.
import threading
# time đo thời gian, tạo mốc sự kiện và hiển thị thời gian trên cảnh báo.
import time
# Future cho phép request chờ thread nền xử lý xong sự kiện được tiêm.
from concurrent.futures import Future

# NumPy tính phân vị độ trễ từ mẫu được giữ trong bộ đệm.
import numpy as np
# secrets tạo token phiên cho app thuê bao.
import secrets
# FastAPI định nghĩa ứng dụng và lỗi HTTP; JSONResponse trả JSON trực tiếp.
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Nạp cấu hình đường dẫn và giới hạn SLA của hệ thống.
from config import DATA_DIR, LOG_DIR, MODEL_DIR, SLA_TIER1_MS, SLA_TIER2_MS, STATIC_DIR
from backend_state import BackendState, EventConflict
from backend_model_identity import loaded_model_identity
from backend_runtime import InferenceRuntime, REQUIRED, validate_event
from backend_evidence import DurableEvidenceLog
# TrafficSimulator tạo các sự kiện tấn công nhân tạo cho các nút demo; PROTECTED_CLI là số tổng đài chính thức.
from data_generator import PROTECTED_CLI, TrafficSimulator
# Detector trích xuất đặc trưng và chấm điểm sự kiện bằng các mô hình đã huấn luyện.
from detector import Detector
# Hàm load_events đọc các tệp sự kiện đã tạo trước.
from events_io import load_events
# verify kiểm tra tính toàn vẹn của chuỗi bằng chứng (ghi qua DurableEvidenceLog).
from evidence import verify_in_subprocess
# GuardedDetector bọc detector để áp dụng timeout, luật dự phòng, fail-open và circuit breaker.
from resilience import GuardedDetector
# Phân quyền theo vai trò, che số điện thoại cho người xem và nhật ký thao tác có chữ ký.
import accounts
import security
from security import DEMO_MODE, AuditLog, mask_imei, mask_number, require, verify_audit
# AI agent vận hành: giám sát và đề xuất, con người quyết định.
from agent import OpsAgent
# Tài khoản thuê bao, quản trị, chi phí, pipeline vận hành và /metrics.
import portal

# Tốc độ phát sự kiện mẫu trên giao diện; đây là tốc độ demo, không phải năng lực cực đại của engine.
DEMO_RATE = 300          # sự kiện/giây hiển thị trên dashboard; engine đạt khoảng 15k/s mỗi lõi
# Đặt file log bằng chứng trong thư mục log do cấu hình v2 quản lý.
EVIDENCE_PATH = Path(os.environ.get("FRAUD_EVIDENCE_FILE", LOG_DIR / ("evidence_demo.jsonl" if DEMO_MODE else "evidence_live.jsonl")))
PROTECTED_NUMBERS = set(PROTECTED_CLI)


class LiveEngine:
    # Chuẩn bị dữ liệu, mô hình, hàng đợi và các bộ đếm dùng chung cho phiên dashboard.
    def __init__(self, *, demo_mode=None, state_path=None, evidence_path=None,
                 detector_factory=Detector, start_workers=True):
        self.demo_mode = DEMO_MODE if demo_mode is None else demo_mode
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
        self.store = BackendState(None if self.demo_mode else (
            state_path or os.environ.get("FRAUD_STATE_DB", LOG_DIR / "live_state.sqlite3")))
        # The runtime acquires the exclusive owner before any live-state replay.
        self.evidence_path = evidence_path or EVIDENCE_PATH
        try:
            self.store.acquire_owner()
            self.log = DurableEvidenceLog(self.evidence_path, self.store)
        except BaseException:
            self.store.close()
            raise
        self.sim = TrafficSimulator(seed=777, n_personal=300, n_callcentres=2, window="test") if self.demo_mode else None
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
            self.runtime = InferenceRuntime(self.g, self.store, on_result=self._record_live_result,
                                            on_retro=self._record_retro, execution_lock=self.det_lock, start=False)
        except BaseException:
            self.log.close()
            self.store.close()
            raise
        if not self.demo_mode:
            security.configure_store(self.store)
        self.agent = OpsAgent(self, audit, state_store=self.store if not self.demo_mode else None)
        if start_workers:
            self.runtime.start()
            self.agent.start()
            if self.demo_mode:
                self._stream_thread = threading.Thread(target=self._run, name="demo-replay", daemon=True)
                self._stream_thread.start()

    async def submit_event(self, event, budget_ms=None):
        budget = budget_ms if budget_ms is not None else (
            SLA_TIER1_MS if event.get("event_type") == "CALL" else SLA_TIER2_MS)
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
            self.counts["FAIL_OPEN" if result.get("fail_open") and result["decision"] == "ALLOW" else result["decision"]] += 1
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
                ev["ts"] += self.offset                 # replay loops keep time moving forward
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
                fut.set_result(res[i:i + n])
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
        a = {k: r.get(k) for k in ("event_id", "event_type", "decision", "risk", "action_vi", "action_en", "reasons",
                                   "party", "counterparty", "zero_day", "fail_open", "degraded", "retro", "anomaly_score",
                                   "requires_review", "retro_context", "model_decision")}
        # Đánh dấu sự kiện tiêm thử, loại gian lận mô phỏng (để đối chiếu) và đặc trưng làm căn cứ.
        a["injected"] = ev.get("injected", False)
        a["truth"] = ev.get("fraud_type", "")
        a["features"] = r.get("evidence_features", {})
        a["time"] = time.strftime("%H:%M:%S")
        a["imei"], a["cell"] = ev.get("imei"), ev.get("cell_id")
        a["human_override"] = r.get("human_override")
        self.alerts.appendleft(a)
        if r["decision"] != "ALLOW" and not r.get("requires_review"):
            self.recent_hits.append({"wall": time.time(), "type": r["event_type"], "decision": r["decision"],
                                     "party": r.get("party"), "imei": ev.get("imei"), "cell": ev.get("cell_id"),
                                     "event_id": r["event_id"], "human_override": r.get("human_override"),
                                     "caller_age_days": (r.get("evidence_features") or {}).get("caller_age_days")})
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
        self._push_inbox(who, {
            "id": r["event_id"], "type": r["event_type"], "from": r.get("party"), "time": time.strftime("%H:%M:%S"),
            "decision": r["decision"], "action": r.get("action_vi"), "risk": r.get("risk"),
            "reasons": [x["vi"] for x in r.get("reasons", [])][:4], "text": ev.get("text", ""),
            "retro": bool(r.get("retro")), "degraded": bool(r.get("degraded")), "simulated": ev.get("app_kind", ""),
        })
        # Bảo vệ người thân: báo cho người giám hộ khi người được bảo vệ gặp lừa đảo.
        # Chỉ gửi loại sự kiện và kết quả, không gửi nội dung tin nhắn (giữ riêng tư cho người được bảo vệ).
        if r["decision"] != "ALLOW" and r["event_type"] in ("CALL", "SMS", "AUDIO"):
            what = {"CALL": "cuộc gọi", "SMS": "tin nhắn", "AUDIO": "cuộc gọi (giọng nói)"}[r["event_type"]]
            verdict = "đã bị nhà mạng chặn" if r["decision"] == "BLOCK" else "nghi lừa đảo"
            for g in self.guardians.get(who, ()):
                if g in self.inbox:
                    self._push_inbox(g, {
                        "id": f"FAM-{r['event_id']}", "type": "FAMILY", "from": who, "time": time.strftime("%H:%M:%S"),
                        "decision": r["decision"], "action": f"Người thân của bạn vừa nhận một {what} {verdict}",
                        "reasons": [f"Số gọi / gửi: {r.get('party')}", "Hãy gọi hỏi thăm người thân, nhắc không chuyển tiền, không đọc OTP"],
                        "text": "", "retro": False, "degraded": False, "simulated": ""})

    # notify gửi cảnh báo do chuyên viên phê duyệt tới các thuê bao (người dùng app nhận ngay trong hộp thư).
    def notify(self, recipients, message):
        n = 0
        with self.lock:
            for who in recipients:
                if who in self.inbox:
                    self._push_inbox(who, {
                        "id": f"WARN-{int(time.time() * 1000)}-{n}", "type": "WARN", "from": "Nhà mạng",
                        "time": time.strftime("%H:%M:%S"), "decision": "FLAG", "action": message, "reasons": [],
                        "text": "", "retro": True, "degraded": False, "simulated": ""})
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
        # Child process: the hash-chain re-check must not compete with the inference owner for the GIL.
        ok, n, bad, state = verify_in_subprocess(self.evidence_path, state)
        if ok is None:
            self.evidence_verify_errors = getattr(self, "evidence_verify_errors", 0) + 1
            return True, n, None          # could not check: never report tampering without evidence
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
            sim.simbox(t0, t1, 3, (600, 900), gateway_tac_p=1.0, tag="DEMO", active_h=(0.05, 0.05), dedicated_imei=False)
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
            for e in (mine[-1:] if spread else mine):
                e["callee" if e["event_type"] != "SMS" else "recipient"] = target
                e["app_kind"] = kind
        # Tạo Future rồi chuyển batch sang thread nền xử lý đồng bộ với dữ liệu đang phát.
        fut: Future = Future()
        try:
            self.inject_q.put_nowait((evs, fut))
        except queue.Full:
            raise HTTPException(503, "demo injection queue is full")
        # Chờ tối đa 10 giây; timeout sẽ báo lỗi thay vì giả vờ injection thành công.
        res = fut.result(timeout=10)
        # Chỉ trả các trường cần thiết để giao diện thống kê và hiển thị lý do.
        return [{"event_id": e["event_id"], "event_type": e["event_type"], "decision": r["decision"], "risk": r.get("risk"),
                 "reasons": r.get("reasons", []), "zero_day": r.get("zero_day", False),
                 "to_target": bool(target) and r.get("counterparty") == target, "party": r.get("party"),
                 "action": r.get("action_vi"), "text": e.get("text", "")} for e, r in zip(evs, res)]

    # Tạo bản chụp nhất quán các bộ đếm và số liệu mà API trạng thái trả về.
    def state(self, mask: bool = False):
        # Khóa các cấu trúc đang được thread stream cập nhật.
        with self.lock:
            alerts = list(self.alerts)[:40]
            # Vai trò người xem chỉ thấy số đã che, đủ để theo dõi cảnh báo nhưng không định danh được thuê bao.
            if mask:
                alerts = [dict(a, party=mask_number(a.get("party")), counterparty=mask_number(a.get("counterparty")),
                               imei=mask_imei(a.get("imei"))) for a in alerts]
            # Chuyển mẫu latency thành mảng để tính các phân vị; dùng 0 nếu chưa có mẫu.
            lat = np.array(self.lat) if self.lat else np.array([0.0])
            # Lấy trạng thái circuit breaker hiện tại từ lớp bảo vệ.
            cb = self.g.cb
            # Trả JSON-friendly values để API có thể mã hóa thành JSON.
            return {
                "model_version": self.runtime.model_version, "model_identity": self.runtime.model_identity,
                "model_manifest": getattr(self.g.det, "model_manifest", {"status": "legacy_unverified"}),
                "uptime_s": round(time.time() - self.started),
                "processed": self.processed, "decisions": dict(self.counts), "by_type": dict(self.by_type),
                "ingest_unscored": dict(self.ingest_counts), "call_decisions": dict(self.call_decisions),
                "eps": self.rate_hist[-1] if self.rate_hist else 0, "eps_history": list(self.rate_hist),
                "latency_ms": {"p50": round(float(np.percentile(lat, 50)), 3), "p99": round(float(np.percentile(lat, 99)), 3)},
                "breaker": {"state": cb.state, **cb.stats}, "chaos": {"delay_ms": self.g.chaos_delay_ms, "error": self.g.chaos_error},
                "resilience": {**self.g.stats, "rescore_pending": len(self.g.rescore_queue),
                               "rescore_capacity": self.g.rescore_queue.maxlen, "denylist_size": len(self.g.fallback.deny)},
                "evidence_written": self.log.written, "truth": dict(self.truth), "alerts": alerts,
                "demo_mode": self.demo_mode,
                "inference": {**self.runtime.stats, "queue_size": self.runtime.queue.qsize(), "queue_capacity": self.runtime.queue.maxsize},
            }


# Engine chỉ được tạo khi app bắt đầu chạy (lifespan), không phải lúc import (để test không nạp mô hình).
engine: LiveEngine | None = None


@contextlib.asynccontextmanager
# Nạp dữ liệu, khởi tạo detector và bắt đầu thread phát lại khi server khởi động.
async def lifespan(_app):
    global engine
    engine = await asyncio.to_thread(LiveEngine)
    portal.startup(lambda: engine)
    from gc_tuning import tune_gc_for_latency
    engine.gc_tuned = tune_gc_for_latency()   # same GC settings the in-process SLA benchmark always used
    try:
        yield
    finally:
        portal.shutdown()
        await asyncio.to_thread(engine.close)
        engine = None


# Tạo ứng dụng web FastAPI; các route bên dưới thuộc ứng dụng này.
app = FastAPI(title="Telecom Real-Time AI Fraud Detection (v2)", lifespan=lifespan)
app.include_router(portal.router)


@app.middleware("http")
async def _http_metrics(request: Request, call_next):
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        # The console and the subscriber app must never be framed by another site (clickjacking).
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response
    finally:
        if request.url.path != "/metrics":
            portal.observability.observe(request, status, started)


@app.get("/health/live")
def health_live():
    return {"status": "live"}


@app.get("/health/ready")
def health_ready():
    if engine is None:
        raise HTTPException(503, "inference owner unavailable")
    progress = engine.runtime.progress()
    if not progress["ready"]:
        raise HTTPException(503, {"message": "inference owner unavailable", "progress": progress})
    return {"status": "ready", "progress": progress, "mode": engine.network_mode, "demo": engine.demo_mode,
            "model_version": engine.runtime.model_version, "model_identity": engine.runtime.model_identity,
            "model_manifest": getattr(engine.g.det, "model_manifest", {"status": "legacy_unverified"}),
            # NORMAL is an opt-in durability trade-off (FRAUD_SQLITE_SYNCHRONOUS); operators must see it.
            "journal_durability": getattr(engine.runtime.state, "synchronous", "FULL")}


@app.get("/")
# Trang chủ công khai là app thuê bao; bảng vận hành nằm riêng ở /admin/.
def index():
    return RedirectResponse("/app/")


@app.get("/admin")
def admin_redirect():
    return RedirectResponse("/admin/")


@app.get("/admin/")
# Bảng điều khiển vận hành (giám sát luồng, bằng chứng, AI agent).
def admin_console():
    return FileResponse(STATIC_DIR / "admin" / "index.html")


@app.get("/admin/manage")
# Quản trị: tài khoản nhân viên, duyệt báo cáo số, chi phí, pipeline.
def admin_manage():
    return FileResponse(STATIC_DIR / "admin" / "manage.html")


# Nhật ký thao tác: mọi hành động đặc quyền được ghi kèm người thực hiện và chữ ký của chính người đó.
audit = AuditLog()


# demo_only chặn các endpoint chỉ dành cho trình diễn (tiêm tấn công, gây lỗi) khi chạy thật.
def demo_only():
    if not DEMO_MODE:
        raise HTTPException(403, "demo endpoints are disabled (set FRAUD_DEMO=1 to enable)")


# Đăng nhập app bằng số demo KHÔNG cần tài khoản: chỉ cho chạy cục bộ (run_demo.py). Bản công khai tắt
# (FRAUD_LEGACY_DEMO_LOGIN=0) để mọi người dùng đi qua tài khoản thật.
def legacy_demo_login():
    demo_only()
    if os.environ.get("FRAUD_LEGACY_DEMO_LOGIN", "1") != "1":
        raise HTTPException(403, "anonymous demo sign-in is disabled; create an account at /app/")


@app.get("/app")
# App thuê bao (PWA) nằm ở /app/; chuyển hướng để đường dẫn tương đối của manifest/service worker đúng.
def app_redirect():
    return RedirectResponse("/app/")


@app.post("/api/login")
# Đổi thông tin đăng nhập cá nhân lấy một phiên có hạn (8 giờ, hoặc 30 phút không thao tác). Ghi nhật ký.
async def api_login(request: Request, authorization: str = Header(default="")):
    # Hai cách: JSON {username, password} (nhân viên) hoặc Bearer <token cá nhân> (tự động hoá / bản cũ).
    out = None
    if "json" in request.headers.get("content-type", ""):
        try:
            body = portal.OperatorLogin(**(await request.json()))
        except Exception:
            raise HTTPException(422, "username and password required")
        out = portal.operator_password_login(body, request)
    else:
        out = security.login(security.bearer(authorization))
    if out is None:
        raise HTTPException(401, "invalid credential")
    sid, u, expires = out
    if u["name"] in security.suspended:
        security.logout(sid)
        raise HTTPException(403, "account suspended by an administrator")
    audit.record(u["name"], "login")
    return {"session": sid, "expires_at": expires, "idle_timeout_s": security.SESSION_IDLE_S, **u}


@app.post("/api/logout")
def api_logout(authorization: str = Header(default="")):
    s = security.logout(security.bearer(authorization))
    if s:
        audit.record(s["name"], "logout")
    return {"ok": True}


@app.get("/api/whoami")
# Cho dashboard biết người đang đăng nhập và vai trò để ẩn/hiện các nút tương ứng.
def api_whoami(u=Depends(require("viewer"))):
    return {**u, "demo_mode": DEMO_MODE}


@app.get("/api/state")
# Trả các bộ đếm và cảnh báo mới nhất. Số điện thoại và IMEI luôn bị che với MỌI vai trò (tối thiểu hóa dữ liệu):
# ai cần số đầy đủ phải mở từng số qua /api/reveal, ghi lý do và để lại dấu vết trong nhật ký.
def api_state(u=Depends(require("viewer"))):
    return engine.state(mask=True)


@app.post("/api/reveal")
# Mở số đầy đủ của MỘT cảnh báo: chỉ chuyên viên trở lên, bắt buộc ghi lý do, ghi nhật ký có chữ ký,
# và AI agent theo dõi số lần mở để phát hiện rò rỉ từ bên trong.
def api_reveal(event_id: str | None = None, reason: str | None = None, body: dict | None = Body(default=None),
               u=Depends(require("analyst"))):
    # Lý do (thường có thông tin cá nhân) nên đi trong body: query string bị ghi vào log proxy/truy cập.
    # Query vẫn được nhận để không làm hỏng client cũ.
    body = body if isinstance(body, dict) else {}
    event_id = str(body.get("event_id") or event_id or "")
    reason = str(body.get("reason") or reason or "")
    if not event_id:
        raise HTTPException(422, "event_id is required")
    if len(reason.strip()) < 5:
        raise HTTPException(400, "a reason (at least 5 characters) is required to reveal personal data")
    with engine.lock:
        a = next((x for x in engine.alerts if x["event_id"] == event_id), None)
    if a is None:
        raise HTTPException(404, "alert no longer in the live window")
    audit.record(u["name"], "reveal_pii", event_id=event_id, reason=reason.strip()[:200])
    engine.agent.note_reveal(u["name"])
    return {"event_id": event_id, "party": a.get("party"), "counterparty": a.get("counterparty"), "imei": a.get("imei"),
            "note": "Lần mở này đã được ghi vào nhật ký thao tác kèm lý do."}


@app.get("/api/agent")
# Đề xuất của AI agent: mọi nhân viên có tài khoản đều xem được (số đã che); chỉ admin mới quyết định.
def api_agent(u=Depends(require("viewer"))):
    return {"proposals": engine.agent.list(), "can_decide": u["role"] == "admin"}


@app.post("/api/agent/{pid}/decide")
# Con người quyết định: duyệt thì hành động mới chạy, từ chối thì không có gì thay đổi. Cả hai đều ghi nhật ký có chữ ký.
def api_agent_decide(pid: str, decision: str | None = None, note: str = "", body: dict | None = Body(default=None),
                     u=Depends(require("admin"))):
    body = body if isinstance(body, dict) else {}
    decision = str(body.get("decision") or decision or "")
    note = str(body.get("note") or note or "")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve | reject")
    try:
        return engine.agent.decide(pid, u["name"], decision == "approve", note.strip()[:300])
    except KeyError:
        raise HTTPException(404, "unknown proposal")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/metrics")
# Đọc các báo cáo kết quả đã lưu và trả về một gói metric thống nhất.
def api_metrics(u=Depends(require("viewer"))):
    # Đọc JSON nếu file tồn tại; nếu chưa sinh báo cáo thì trả null cho mục đó.
    def rd(p):
        return json.loads(p.read_text()) if p.exists() else None
    return {"benchmark": rd(DATA_DIR / "benchmark_results.json"), "loadtest": rd(DATA_DIR / "loadtest_results.json"),
            "training": rd(MODEL_DIR / "training_report.json"), "dataset": rd(DATA_DIR / "dataset_summary.json")}


@app.post("/api/inject")
# Nhận loại tấn công từ query string, sinh sự kiện rồi trả các kết quả chấm điểm.
def api_inject(type: str, u=Depends(require("admin")), _=Depends(demo_only)):
    audit.record(u["name"], "inject_attack", type=type)
    return {"attack": type, "results": engine.inject(type)}


@app.post("/api/chaos")
# Bật/tắt tình huống gây trễ hoặc lỗi để kiểm thử circuit breaker và fail-open (chỉ admin, chỉ chế độ demo).
def api_chaos(mode: str, u=Depends(require("admin")), _=Depends(demo_only)):
    if mode not in ("latency", "error", "off"):
        raise HTTPException(400, "mode must be latency | error | off")
    audit.record(u["name"], "fault_drill", mode=mode)
    # Lấy detector có bảo vệ dùng chung của dashboard.
    g = engine.g
    # Mỗi mode thay đổi độ trễ giả lập hoặc bật cờ lỗi của detector.
    if mode == "latency":
        g.chaos_delay_ms, g.chaos_error = 60.0, False
    elif mode == "error":
        g.chaos_delay_ms, g.chaos_error = 0.0, True
    elif mode == "off":
        g.chaos_delay_ms, g.chaos_error = 0.0, False
    else:
        # Báo lỗi 400 cho mode ngoài tập được hỗ trợ.
        raise HTTPException(400, "mode must be latency | error | off")
    # Xác nhận mode hiện tại cùng trạng thái breaker.
    return {"chaos": mode, "breaker": g.cb.state}


@app.get("/api/evidence/verify")
# Kiểm tra tính liên tục của chuỗi hash đã ghi trong phiên hiện tại.
def api_verify(u=Depends(require("analyst"))):
    audit.record(u["name"], "verify_evidence_chain")
    # Chờ một nhịp để logger flush các bản ghi đang đợi.
    time.sleep(0.2)
    # Kiểm trong tiến trình con (vòng 6): băm lại cả chuỗi không được tranh GIL với owner chấm điểm.
    if not engine.evidence_path.exists():
        return {"chain_valid": True, "records": 0, "first_bad_line": None, "last_hash": ""}
    ok, n, bad, state = verify_in_subprocess(engine.evidence_path, {})
    if ok is None:
        raise HTTPException(503, "Không chạy được bước kiểm chuỗi bằng chứng, thử lại sau")
    last = state.get("prev", "") if ok and n else ""
    return {"chain_valid": ok, "records": n, "first_bad_line": bad, "last_hash": last}


@app.get("/api/evidence/latest")
# Trả về tối đa n dòng log cuối cùng để client có thể xem nhanh các bằng chứng mới.
def api_latest(n: int = 5, u=Depends(require("analyst"))):
    # Không cho tải hàng loạt: tối đa 50 bản ghi mỗi lần, số điện thoại đã che.
    n = max(1, min(n, 50))
    audit.record(u["name"], "read_evidence", n=n)
    rows = [json.loads(x) for x in engine.evidence_path.read_text(encoding="utf-8").splitlines()[-n:]]
    for row in rows:
        rec = row["record"]
        rec["party"], rec["counterparty"] = mask_number(rec.get("party")), mask_number(rec.get("counterparty"))
    return JSONResponse(rows)


@app.get("/api/audit")
# Nhật ký thao tác: ai đã làm gì, kèm kết quả kiểm tra chuỗi hash và chữ ký của từng dòng (chỉ admin).
def api_audit(n: int = 20, anchor_records: int | None = Query(default=None, ge=1),
              anchor_hash: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
              u=Depends(require("admin"))):
    # verify.head nên được chép định kỳ ra hệ thống ngoài; gửi lại qua anchor_* để phát hiện xoá đuôi log.
    if (anchor_records is None) != (anchor_hash is None):
        raise HTTPException(422, "anchor_records and anchor_hash must be given together")
    anchor = None if anchor_records is None else {"records": anchor_records, "hash": anchor_hash}
    return {"verify": verify_audit(anchor=anchor), "entries": audit.tail(n)}


# ====================================================================== network link (đứng song song với nhà mạng)
# Thiết bị mạng của nhà mạng gửi BẢN SAO sự kiện (không đi qua hệ thống này), nhận lại khuyến nghị và TỰ quyết định
# có thực thi hay không. Không có trả lời trong ngân sách thì cuộc gọi cứ đi tiếp: hệ thống này sập không làm rớt cuộc gọi nào.
NETWORK_REQUIRED = REQUIRED
MAX_NETWORK_BODY = 65536


async def network_request(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_NETWORK_BODY:
            raise HTTPException(413, "event body is too large")
    body = bytes(body)
    why = security.verify_network(body, request.headers.get("x-timestamp", ""), request.headers.get("x-signature", ""))
    if why:
        raise HTTPException(401, f"network link refused: {why}")
    try:
        ev = validate_event(json.loads(body), "CDR" if request.url.path.endswith("/cdr") else "VERDICT")
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(400, str(exc))
    # Yêu cầu đã ký trong ±30 s đồng hồ thật, nên mốc sự kiện kiểu Unix không thể ở xa trong tương lai.
    # Một mốc như vậy sẽ đẩy watermark của IngestGuard và làm mọi sự kiện thật sau đó thành LATE (không chấm).
    # Mốc < 1e9 là đồng hồ giả lập (demo/kiểm thử bắt đầu từ 0) nên không so với đồng hồ thật.
    if ev["ts"] >= UNIX_TS_FLOOR and ev["ts"] > time.time() + NETWORK_MAX_FUTURE_S:
        raise HTTPException(400, f"ts is more than {NETWORK_MAX_FUTURE_S:.0f} s in the future")
    return ev


UNIX_TS_FLOOR = 1e9
# Must stay below IngestGuard.allowed_lateness_s (120 s): one peer clocked 130-300 s fast would
# otherwise lift the global floor above every correctly clocked trunk and make their events LATE.
NETWORK_MAX_FUTURE_S = 60.0


@app.post("/api/v1/verdict")
async def network_verdict(request: Request):
    ev = await network_request(request)
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        result = await engine.submit_event(ev)
    except EventConflict as exc:
        raise HTTPException(409, str(exc))
    action = result["decision"] if (engine.network_mode == "enforce" and result.get("ingest") == "ADMITTED"
                                      and result.get("actionable") and not result.get("duplicate")) else "NONE"
    # These fields are already JSON-native. Returning the response directly avoids
    # FastAPI recursively copying/encoding the model identity on the event loop.
    return JSONResponse({"event_id": ev["event_id"], "decision": result["decision"], "recommended_action": action,
            "mode": engine.network_mode, "advisory": True, "risk": result.get("risk"),
            "reason_codes": [x["code"] for x in result.get("reasons", [])],
            "fail_open": bool(result.get("fail_open")), "pending": bool(result.get("pending")),
            "observed_without_model": bool(result.get("observed_without_model")),
            "duplicate": bool(result.get("duplicate")), "ingest": result.get("ingest"),
            "model_version": result.get("model_version") or engine.runtime.model_version,
            "artifact_status": result.get("artifact_status", "legacy_unverified"),
            "model_identity": result.get("model_identity", engine.runtime.model_identity),
            "latency_ms": result.get("latency_ms", 0.0),
            "scored": bool(result.get("scored")), "scored_before_deadline": bool(result.get("scored_before_deadline")),
            "durable": bool(result.get("durable")), "admitted": bool(result.get("admitted")),
            "retryable": bool(result.get("retryable")), "delivery_status": result.get("delivery_status"),
            "delivery_expired": bool(result.get("delivery_expired")), "model_decision": result.get("model_decision"),
            "commit_ms": result.get("commit_ms", 0.0), "queue_ms": result.get("queue_ms", 0.0)})


@app.post("/api/v1/cdr")
async def network_cdr(request: Request):
    ev = await network_request(request)
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        result = await engine.runtime.submit("CDR", ev, budget_ms=SLA_TOTAL_NETWORK_MS)
    except EventConflict as exc:
        raise HTTPException(409, str(exc))
    if result.get("pending"):
        return JSONResponse(result, status_code=202)
    if not result.get("ok"):
        return JSONResponse(result, status_code=503)
    return result


SLA_TOTAL_NETWORK_MS = 200.0


async def submit_voice_event(event):
    if engine is None:
        raise HTTPException(503, "engine starting")
    return await engine.submit_event(event)


async def record_voice_observation(event, result):
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        return await engine.record_observation(event, result)
    except EventConflict as exc:
        raise HTTPException(409, str(exc))


async def lookup_voice_observation(event):
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        return await engine.runtime.lookup_observation(event)
    except EventConflict as exc:
        raise HTTPException(409, str(exc))


from voice_api import create_voice_router
app.include_router(create_voice_router(submit_event=submit_voice_event,
                                      mode=lambda: engine.network_mode if engine else "shadow",
                                      record_observation=record_voice_observation,
                                      lookup_observation=lookup_voice_observation))


@app.get("/api/network/mode")
def network_mode_get(u=Depends(require("viewer"))):
    return {"mode": engine.network_mode, "stats": dict(engine.network_stats)}


@app.post("/api/network/mode")
# Chỉ admin nhà mạng chuyển shadow <-> enforce; ghi nhật ký có chữ ký.
def network_mode_set(mode: str, u=Depends(require("admin"))):
    if mode not in ("shadow", "enforce"):
        raise HTTPException(400, "mode must be shadow | enforce")
    if mode == "enforce" and os.environ.get("FRAUD_REAL_TRAFFIC_VALIDATED") != "1":
        raise HTTPException(409, "enforce requires domain validation on labelled real traffic (FRAUD_REAL_TRAFFIC_VALIDATED=1)")
    audit.record(u["name"], "network_mode", mode=mode, previous=engine.network_mode)
    engine.network_mode = mode
    return {"mode": mode}


# ====================================================================== mobile app (subscribers)
# app_user đọc token phiên của app; phiên gắn với đúng một số thuê bao, chỉ xem được dữ liệu của số đó.
def app_user(authorization: str = Header(default="")):
    # Ưu tiên phiên tài khoản thật (email/Google, lưu trong CSDL); phiên demo cũ vẫn dùng được khi FRAUD_DEMO=1.
    account_msisdn = portal.subscriber_msisdn(authorization)
    if account_msisdn:
        return account_msisdn
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    session = engine.app_sessions.get(token) if engine and token else None
    now = time.time()
    if session and (now - session["created"] > security.SESSION_TTL_S or now - session["last"] > security.SESSION_IDLE_S):
        engine.app_sessions.pop(token, None)
        session = None
    msisdn = session["msisdn"] if session else None
    if session:
        session["last"] = now
    if not msisdn:
        raise HTTPException(401, "app session missing or expired")
    return msisdn


@app.get("/api/app/demo-numbers")
# Các số thuê bao demo để đăng nhập app (thực tế: đăng nhập bằng OTP gửi tới chính số đó).
def app_demo_numbers(_=Depends(legacy_demo_login)):
    return {"numbers": engine.demo_numbers}


@app.post("/api/app/session")
# Mở phiên app cho một số demo; trả token phiên.
def app_session(msisdn: str, _=Depends(legacy_demo_login)):
    if msisdn not in engine.demo_numbers:
        raise HTTPException(403, "only demo numbers can sign in without OTP")
    if portal.db().one("SELECT 1 AS x FROM subscribers WHERE msisdn=?", (msisdn,)):
        raise HTTPException(403, "this number belongs to a registered account")
    token = secrets.token_urlsafe(24)
    with engine.lock:
        engine.app_sessions[token] = {"msisdn": msisdn, "created": time.time(), "last": time.time()}
        engine.inbox[msisdn]                     # touch: from now on events to this number reach its inbox
    return {"token": token, "msisdn": msisdn}


@app.get("/api/app/inbox")
# Cuộc gọi / tin nhắn gửi tới chính số của người dùng, kèm quyết định và lý do dễ hiểu.
def app_inbox(msisdn=Depends(app_user)):
    with engine.lock:
        items = list(engine.inbox[msisdn])
    # Thống kê chỉ tính sự kiện tới chính số này (không tính cảnh báo về người thân / thông báo của nhà mạng).
    stats = collections.Counter(i["decision"] for i in items if i["type"] in ("CALL", "SMS", "AUDIO"))
    return {"msisdn": msisdn, "items": items, "stats": {"blocked": stats["BLOCK"], "warned": stats["FLAG"], "allowed": stats["ALLOW"]}}


@app.post("/api/app/simulate")
# Chế độ demo: giả lập một cuộc gọi / tin nhắn (lừa đảo hoặc bình thường) gửi tới số của người dùng.
def app_simulate(kind: str, msisdn=Depends(app_user), _=Depends(demo_only)):
    if kind not in ("LEGIT", "SIM_BOX", "CLI_SPOOF", "SMS_PHISH", "DEEPFAKE_VOICE", "WANGIRI", "VOICE_HARVEST"):
        raise HTTPException(400, "unknown kind")
    portal.simulate_allowed(msisdn)
    res = [r for r in engine.inject(kind, target=msisdn) if r["to_target"]]
    return {"kind": kind, "results": res}


# Kịch bản chạy thử một phút: cuộc gọi/tin nhắn bình thường xen lẫn các kiểu lừa đảo, gửi tới số của người dùng
# với khoảng nghỉ ngẫu nhiên như ngoài đời. Mỗi sự kiện vẫn đi qua đúng hệ thống AI (engine.inject), app nhận qua
# hộp thư như cuộc gọi thật, KHÔNG qua phản hồi của nút bấm.
APP_STREAM_KINDS = ["LEGIT", "SMS_PHISH", "LEGIT", "CLI_SPOOF", "LEGIT", "SIM_BOX", "WANGIRI", "LEGIT",
                    "DEEPFAKE_VOICE", "VOICE_HARVEST"]
_app_streams: dict[str, float] = {}
_app_streams_lock = threading.Lock()


def _run_app_stream(msisdn: str, kinds: list[str], gaps: list[float]) -> None:
    try:
        for kind, gap in zip(kinds, gaps):
            time.sleep(gap)
            try:
                engine.inject(kind, target=msisdn)
            except Exception:                    # một sự kiện lỗi (hàng đợi đầy) không dừng cả kịch bản
                continue
    finally:
        with _app_streams_lock:
            _app_streams.pop(msisdn, None)


@app.post("/api/app/simulate-stream")
# Chế độ demo: chạy thử khoảng một phút với dữ liệu giả lập; app tự hiện màn hình chặn / cảnh báo khi sự kiện tới.
def app_simulate_stream(msisdn=Depends(app_user), _=Depends(demo_only)):
    with _app_streams_lock:
        until = _app_streams.get(msisdn, 0)
        if until > time.time():
            raise HTTPException(409, f"đang chạy thử, còn khoảng {int(until - time.time())} giây")
        portal.simulate_allowed(msisdn)
        rng = np.random.default_rng()
        kinds = [str(k) for k in rng.permutation(APP_STREAM_KINDS)]
        gaps = [float(g) for g in rng.uniform(4.0, 8.0, size=len(kinds))]
        _app_streams[msisdn] = time.time() + sum(gaps) + 15
    threading.Thread(target=_run_app_stream, args=(msisdn, kinds, gaps), daemon=True, name="app-stream").start()
    return {"started": True, "events": len(kinds), "seconds": round(sum(gaps)),
            "kinds": collections.Counter(kinds)}


@app.get("/api/app/lookup")
# Tra cứu một số: chỉ trả số liệu tổng hợp về số đó, không trả ai đã nhận cuộc gọi của nó.
def app_lookup(number: str, msisdn=Depends(app_user)):
    accounts.limiter.hit("app_lookup", msisdn, 120, 3600)   # không cho dò hàng loạt uy tín / số báo cáo
    keys = _number_keys(number)
    with engine.lock:
        rep = collections.Counter()
        for k in keys:
            rep.update(engine.reputation.get(k, {}))
        n_reports = len(set().union(*(engine.reports.get(k, set()) for k in keys)))
    blocked, flagged = rep.get("BLOCK", 0), rep.get("FLAG", 0) + rep.get("RETRO", 0)
    if keys & PROTECTED_NUMBERS:
        # Số tổng đài chính thức: bản thân số không xấu, kẻ gian giả mạo hiển thị số này. Không bao giờ báo "số lừa đảo".
        verdict = (f"Đây là số tổng đài chính thức. Kẻ gian có thể giả mạo hiển thị số này; nhà mạng đã chặn {blocked} cuộc "
                   "giả mạo. Cuộc gọi thật từ số này đi qua đường truyền riêng của ngân hàng/cơ quan.")
        level = "info"
    elif blocked:
        verdict, level = "Nhà mạng đã chặn số này vì dấu hiệu lừa đảo", "danger"
    elif flagged:
        verdict, level = "Số này có dấu hiệu đáng ngờ, hãy cẩn thận", "warn"
    else:
        verdict, level = "Chưa ghi nhận dấu hiệu lừa đảo", "ok"
    # Báo cáo của người dùng chỉ để tham khảo: không tự động chặn (tránh bị báo cáo hàng loạt để bôi xấu số sạch).
    return {"number": min(keys, key=len), "verdict": verdict, "level": level, "blocked": blocked, "flagged": flagged,
            "user_reports": n_reports}


# _number_keys trả mọi cách viết của cùng một số (0xxx / 84xxx / nguyên dạng) để tra cứu không phụ thuộc kiểu nhập.
def _number_keys(number: str) -> set:
    n = number.strip().replace(" ", "").replace(".", "").replace("-", "").lstrip("+")
    keys = {n}
    if n.startswith("0"):
        keys.add("84" + n[1:])
    elif n.startswith("84") and len(n) >= 11:
        keys.add("0" + n[2:])
    return keys


_NUMBER_RE = re.compile(r"^[0-9A-Za-z]{3,15}$")


# Báo cáo / lời mời chỉ nhận số (hoặc tên thương hiệu SMS) hợp lệ: chuỗi tuỳ ý làm phình bộ nhớ và CSDL.
def _checked_number_keys(number: str) -> set:
    keys = _number_keys(number or "")
    if not all(_NUMBER_RE.match(k) for k in keys):
        raise HTTPException(422, "invalid phone number")
    return keys


@app.post("/api/app/report")
# Người dùng báo cáo số lừa đảo; mỗi thuê bao chỉ được tính một lần cho mỗi số.
def app_report(number: str, reason: str = "", msisdn=Depends(app_user)):
    accounts.limiter.hit("app_report", msisdn, 30, 3600)
    if len(reason) > 500:
        raise HTTPException(422, "reason is limited to 500 characters")
    keys = _checked_number_keys(number)
    if msisdn in keys:
        raise HTTPException(400, "cannot report your own number")
    number = max(keys, key=len)
    with engine.lock:
        engine.reports[number].add(msisdn)
        n = len(engine.reports[number])
    portal.persist_report(number, msisdn, reason)
    return {"number": number, "user_reports": n, "note": "Báo cáo được chuyển cho chuyên viên xem xét, không tự động chặn số."}


@app.get("/api/app/family")
# Bảo vệ người thân: danh sách người mình bảo vệ, người bảo vệ mình và lời mời đang chờ.
def app_family(msisdn=Depends(app_user)):
    with engine.lock:
        protecting = sorted(p for p, gs in engine.guardians.items() if msisdn in gs)
        return {"protecting": protecting, "guardians": sorted(engine.guardians.get(msisdn, ())),
                "invites_sent": sorted(p for p, inv in engine.invites.items() if msisdn in inv),
                "invites_received": sorted(engine.invites.get(msisdn, ()))}


@app.post("/api/app/family/invite")
# Mời một người thân (ví dụ bố mẹ) cho phép mình nhận cảnh báo khi họ gặp lừa đảo. Cần họ đồng ý trên app của họ.
def app_family_invite(number: str, msisdn=Depends(app_user)):
    accounts.limiter.hit("family_invite", msisdn, 10, 3600)
    keys = _checked_number_keys(number)
    target = next((k for k in keys if len(k) == 11 and k.startswith("84")), max(keys, key=len))
    if target == msisdn:
        raise HTTPException(400, "cannot protect your own number")
    with engine.lock:
        if msisdn in engine.guardians.get(target, ()):
            return {"status": "already_linked"}
        engine.invites[target].add(msisdn)
    portal.persist_invite(target, msisdn)
    return {"status": "invited", "number": target,
            "note": "Người thân cần mở app và đồng ý. Không có sự đồng ý thì bạn không nhận được thông tin nào của họ."}


@app.post("/api/app/family/respond")
# Người được mời đồng ý hoặc từ chối. Người được bảo vệ có thể gỡ người giám hộ bất kỳ lúc nào (accept=false).
def app_family_respond(guardian: str, accept: bool, msisdn=Depends(app_user)):
    with engine.lock:
        # Chỉ chấp nhận lời mời đang chờ: không thì bất kỳ chuỗi nào cũng thành "người giám hộ" (và một inbox mới).
        if accept and guardian not in engine.invites.get(msisdn, ()):
            raise HTTPException(404, "no pending invite from this number")
        engine.invites[msisdn].discard(guardian)
        if accept:
            engine.guardians[msisdn].add(guardian)
        else:
            engine.guardians[msisdn].discard(guardian)
        engine.inbox[guardian]                 # the guardian's app will receive alerts from now on
    portal.persist_family_response(msisdn, guardian, accept)
    return {"status": "linked" if accept else "removed"}


# Tệp tĩnh của app (index.html, manifest, service worker, icon) phục vụ tại /app/.
app.mount("/app", StaticFiles(directory=STATIC_DIR / "app", html=True), name="app")


