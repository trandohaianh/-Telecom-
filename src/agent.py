"""Operations AI agent: watches the live system, explains what it sees and PROPOSES actions.

Human-in-the-loop by construction: the agent has no code path that changes the network. Its only
output is a proposal (what it saw, why it matters, what it recommends, what the action would do).
An action runs only inside decide(), which the API calls when an authorised human approves it.
Both sides are signed in the audit trail: the proposal by the agent's key, the decision by the
human's own key, so afterwards anyone can prove who proposed and who decided.

Detectors are deterministic rules over the live state (so every proposal is reproducible and
explainable); an LLM can be added later to word the summaries, fed only masked aggregates.
Numbers and IMEIs in proposals are always masked; the full values stay server-side.
"""
from __future__ import annotations

import collections
import contextlib
import uuid
import threading
import time

from security import mask_imei, mask_number

PROPOSAL_TTL_S = 30 * 60
WINDOW_S = 10 * 60          # how far back the agent looks for related events
INSIDER_REVEALS = 8         # full-number reveals per user per window before the agent raises it


class OpsAgent:
    def __init__(self, engine, audit, interval_s: float = 3.0, clock=time.time, state_store=None):
        self.e, self.audit, self.interval_s, self.clock = engine, audit, interval_s, clock
        self.proposals: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self._open: dict[str, str] = {}        # dedup key -> id of the pending proposal
        self._done_keys: set[str] = set()      # keys already decided (do not nag again)
        self.state_store = state_store
        self._persist_depth = 0
        self._stop = threading.Event()
        self._thread = None
        self.lock = threading.RLock()
        self._degraded_since = None
        self._retro_seen = 0
        self._last_chain_check = 0.0
        self.reveals: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        if state_store is not None:
            saved = state_store.get("agent_state", {})
            self.proposals.update((p["id"], p) for p in saved.get("proposals", []))
            self._open.update(saved.get("open", {}))
            self._done_keys.update(saved.get("done_keys", []))

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="ops-agent", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _persist(self):
        if self.state_store is not None and self._persist_depth == 0:
            self.state_store.set("agent_state", {"proposals": list(self.proposals.values()),
                                                "open": self._open, "done_keys": sorted(self._done_keys)})

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            try:
                self.scan()
            except Exception as exc:  # noqa: BLE001 - the watcher must never take the service down
                print("agent scan error:", exc)

    def scan(self):
        with self.lock:
            # A scan may create hundreds of proposals. Persist their complete
            # snapshot once, not once per proposal (quadratic JSON/WAL traffic).
            # Human decisions outside this locked scan still persist immediately.
            self._persist_depth += 1
            try:
                self._watch_breaker()
                self._watch_groups()
                self._watch_false_positives()
                self._watch_reports()
                self._watch_retro()
                self._watch_insider()
                self._watch_evidence_chain()
                self._expire()
            finally:
                self._persist_depth -= 1
                # Preserve successful proposals even if a later watcher failed.
                self._persist()

    # ------------------------------------------------------------------ proposals
    def propose(self, key, kind, severity, title, why, action, subjects=(), params=None):
        if key in self._open or key in self._done_keys:
            return None
        pid = "P" + uuid.uuid4().hex[:16]
        p = {"id": pid, "kind": kind, "severity": severity, "title": title, "why": list(why),
             "action": action, "subjects": list(subjects), "created": self.clock(), "status": "pending",
             "decided_by": None, "decided_at": None, "note": "", "result": None, "_params": params or {}}
        p["_key"] = key
        self.audit.record("ai_agent", "agent_proposal", id=pid, kind=kind, title=title)
        # A scan's finally block persists successful work even after failures.
        # Do not publish an unaudited proposal if writing its audit record failed.
        self.proposals[pid] = p
        self._open[key] = pid
        self._persist()
        return p

    def decide(self, pid: str, actor: str, approve: bool, note: str = "") -> dict:
        """The ONLY place an action runs, and only for an approval by a human."""
        with self.lock:
            p = self.proposals.get(pid)
            if p is None:
                raise KeyError(pid)
            if p["status"] != "pending":
                raise ValueError(f"proposal already {p['status']}")
            execution_lock = getattr(self.e, "det_lock", None)
            with execution_lock if execution_lock is not None else contextlib.nullcontext():
                result = self._execute(p) if approve else "Không thực hiện (bị từ chối)."
            p.update(status="approved" if approve else "rejected", decided_by=actor, decided_at=self.clock(), note=note)
            self._open.pop(p["_key"], None)
            self._done_keys.add(p["_key"])
            p["result"] = result
            self.audit.record(actor, "agent_decision", id=pid, kind=p["kind"],
                              decision=p["status"], note=note, result=p["result"])
            self._persist()
            return self.public(p)

    @staticmethod
    def public(p) -> dict:
        return {k: v for k, v in p.items() if not k.startswith("_")}

    def list(self, limit: int = 30) -> list:
        with self.lock:
            return [self.public(p) for p in reversed(self.proposals.values())][:limit]

    def note_reveal(self, user: str):
        with self.lock:
            self.reveals[user].append(self.clock())

    def _expire(self):
        now = self.clock()
        for p in self.proposals.values():
            if p["status"] == "pending" and now - p["created"] > PROPOSAL_TTL_S:
                p["status"] = "expired"
                self._open.pop(p["_key"], None)

    def _obsolete(self, key, why):
        pid = self._open.pop(key, None)
        if pid:
            self.proposals[pid].update(status="obsolete", result=why)

    # ------------------------------------------------------------------ actions (run only from decide)
    def _execute(self, p) -> str:
        kind, prm, e = p["action"]["kind"], p["_params"], self.e
        if kind == "block_group":
            keys = list(prm.get("numbers", [])) + list(prm.get("imeis", []))
            e.g.fallback.block_manual(keys, p["id"])
            return f"Đã chặn {len(prm.get('numbers', []))} số và {len(prm.get('imeis', []))} thiết bị theo nhóm."
        if kind == "unblock":
            e.g.fallback.allow_manual(prm["number"])
            return "Đã gỡ chặn và đưa số vào danh sách tin cậy (mô hình chỉ còn cảnh báo, không chặn)."
        if kind == "notify":
            n = e.notify(prm["recipients"], prm["message"])
            return f"Đã gửi cảnh báo tới {len(prm['recipients'])} thuê bao ({n} đang dùng app nhận ngay)."
        if kind == "restore":
            e.g.chaos_delay_ms, e.g.chaos_error = 0.0, False
            return "Đã khôi phục mô hình. Hàng đợi chấm lại sẽ được xử lý tự động."
        if kind == "suspend_user":
            import security
            security.suspend(prm["user"])
            return f"Đã tạm khóa tài khoản {prm['user']}."
        return "Đã ghi nhận."

    # ------------------------------------------------------------------ detectors
    def _recent_hits(self):
        cutoff = self.clock() - WINDOW_S
        with self.e.lock:
            return [h for h in self.e.recent_hits if h["wall"] >= cutoff]

    def _watch_breaker(self):
        g = self.e.g
        if g.cb.state == g.cb.CLOSED and not g.chaos_error and not g.chaos_delay_ms:
            self._degraded_since = None
            self._obsolete("breaker", "Mô hình đã tự hồi phục trước khi có người xử lý.")
            return
        now = self.clock()
        self._degraded_since = self._degraded_since or now
        if now - self._degraded_since < 5:
            return
        s = g.stats
        self.propose("breaker", "restore", "high", "Mô hình không phản hồi kịp: hệ thống đang chạy chế độ dự phòng",
                     [f"Circuit breaker: {g.cb.state}, kéo dài {now - self._degraded_since:.0f} giây",
                      f"Luật dự phòng đã chặn {s['fallback_block']} sự kiện; phần còn lại đang được cho qua",
                      f"{len(g.rescore_queue)} sự kiện chờ chấm lại sau khi hồi phục"],
                     {"kind": "restore", "label": "Khôi phục mô hình / quay về phiên bản ổn định trước"})

    def _watch_groups(self):
        """One proposal per connected group of numbers and devices (number -- IMEI edges), not per device:
        a farm that rotates SIMs across several gateway modules is one group, proposed once."""
        hits = [h for h in self._recent_hits() if h["type"] == "CALL" and h["decision"] in ("BLOCK", "FLAG")]
        manual = self.e.g.fallback.manual
        parent: dict[str, str] = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for h in hits:
            imei, party = h.get("imei"), h.get("party")
            if imei and imei not in ("-1", "") and party and party not in manual and imei not in manual:
                parent[find("N" + party)] = find("I" + imei)
        groups = collections.defaultdict(set)
        for node in list(parent):
            groups[find(node)].add(node)
        for members in groups.values():
            nums = sorted(m[1:] for m in members if m[0] == "N")
            imeis = sorted(m[1:] for m in members if m[0] == "I")
            if len(nums) < 2:
                continue
            self.propose("group:" + ",".join(imeis), "block_group", "high",
                         f"Nghi trang trại SIM box: {len(nums)} số trên {len(imeis)} thiết bị liên quan",
                         [f"{len(nums)} số bị chặn / gắn cờ trong {WINDOW_S // 60} phút qua dùng chung "
                          f"{'một thiết bị' if len(imeis) == 1 else f'{len(imeis)} thiết bị'} ({', '.join(mask_imei(i) for i in imeis[:3])})",
                          "Nhiều SIM xoay vòng trên cùng thiết bị là dấu hiệu GSM gateway (SIM box)",
                          "Chặn cả nhóm: kẻ gian thay SIM mới trên các thiết bị này cũng bị chặn ngay"],
                         {"kind": "block_group", "label": f"Chặn cả nhóm ({len(nums)} số + {len(imeis)} thiết bị)"},
                         subjects=[mask_number(n) for n in nums] + [mask_imei(i) for i in imeis],
                         params={"numbers": nums, "imeis": imeis})

    def _watch_false_positives(self):
        protected = self.e.g.fallback.protected
        allow = self.e.g.fallback.allow
        for h in self._recent_hits():
            age = h.get("caller_age_days") or 0
            if h["decision"] == "BLOCK" and age >= 365 and h["party"] not in protected and h["party"] not in allow \
                    and not h.get("human_override"):
                self.propose(f"fp:{h['party']}", "unblock", "medium", "Có thể chặn nhầm: thuê bao lâu năm bị chặn",
                             [f"Số {mask_number(h['party'])} đã hoạt động {int(age)} ngày nhưng vừa bị chặn",
                              "Thuê bao lâu năm hiếm khi là SIM lừa đảo; cần chuyên viên xem lại",
                              "Từ chối = giữ nguyên chặn"],
                             {"kind": "unblock", "label": "Gỡ chặn, đưa vào danh sách tin cậy"},
                             subjects=[mask_number(h["party"])], params={"number": h["party"]})

    def _watch_reports(self):
        with self.e.lock:
            reports = {n: len(v) for n, v in self.e.reports.items()}
            rep = {n: dict(self.e.reputation.get(n, {})) for n in reports}
        manual = self.e.g.fallback.manual
        for n, k in reports.items():
            if k >= 2 and not rep[n].get("BLOCK") and n not in manual and n not in self.e.g.fallback.protected:
                self.propose(f"rep:{n}", "block_group", "medium", f"{k} người dùng báo cáo một số mà hệ thống chưa chặn",
                             [f"Số {mask_number(n)} bị {k} thuê bao khác nhau báo cáo lừa đảo",
                              "Mô hình chưa thấy dấu hiệu đủ mạnh để chặn",
                              "Báo cáo của người dùng không bao giờ tự động chặn số (tránh bị lợi dụng bôi xấu số sạch)"],
                             {"kind": "block_group", "label": "Chặn số này"},
                             subjects=[mask_number(n)], params={"numbers": [n], "imeis": []})

    def _watch_retro(self):
        g = self.e.g
        new = g.stats["retro_alerts"] - self._retro_seen
        if new <= 0:
            return
        items = list(g.retro)[-new:]
        self._retro_seen = g.stats["retro_alerts"]
        review = [r for r in items if r.get("requires_review")]
        if review:
            self.propose(f"retro-review:{self._retro_seen}", "investigate", "medium",
                         "Retrospective results require a human review",
                         [f"{len(review)} events were rescored against current history, not their original event-time state",
                          "These scores do not create automatic deny-list entries or confirmed-fraud notifications"],
                         {"kind": "investigate", "label": "Review retrospective evidence"},
                         subjects=[mask_number(r.get("party")) for r in review[:8]])
        items = [r for r in items if not r.get("requires_review")]
        recips = sorted({r.get("counterparty") for r in items if r.get("counterparty")})
        if not recips:
            return
        self.propose(f"retro:{self._retro_seen}", "notify", "medium",
                     f"{len(items)} sự kiện lọt qua lúc sự cố nay được xác định là lừa đảo",
                     [f"Các cuộc gọi / tin nhắn này đã tới {len(recips)} thuê bao trong lúc mô hình không phản hồi",
                      "Không thể chặn lại (đã qua), nhưng có thể cảnh báo người nhận ngay"],
                     {"kind": "notify", "label": f"Gửi cảnh báo tới {len(recips)} người nhận"},
                     subjects=[mask_number(r) for r in recips[:8]],
                     params={"recipients": recips,
                             "message": "Bạn vừa nhận một cuộc gọi / tin nhắn mà nhà mạng xác định là lừa đảo. "
                                        "Không làm theo yêu cầu, không chuyển tiền, không đọc OTP."})

    def _watch_insider(self):
        cutoff = self.clock() - WINDOW_S
        for user, q in self.reveals.items():
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) > INSIDER_REVEALS:
                self.propose(f"insider:{user}:{int(q[0])}", "suspend_user", "high",
                             "Tài khoản xem số điện thoại đầy đủ nhiều bất thường",
                             [f"Tài khoản {user} đã mở {len(q)} số đầy đủ trong {WINDOW_S // 60} phút",
                              f"Ngưỡng bình thường: tối đa {INSIDER_REVEALS}",
                              "Có thể là rò rỉ dữ liệu từ bên trong; mọi lần mở đều có trong nhật ký kèm lý do"],
                             {"kind": "suspend_user", "label": f"Tạm khóa tài khoản {user}"},
                             subjects=[user], params={"user": user})

    def _watch_evidence_chain(self):
        now = self.clock()
        if now - self._last_chain_check < 120 or not hasattr(self.e, "verify_evidence"):
            return
        self._last_chain_check = now
        ok, n, bad = self.e.verify_evidence()
        if not ok:
            self.propose(f"chain:{bad}", "investigate", "high", "Nhật ký bằng chứng bị sửa đổi",
                         [f"Chuỗi hash gãy ở dòng {bad} (đã kiểm {n} bản ghi hợp lệ trước đó)",
                          "Có người đã sửa hoặc xóa bằng chứng sau khi ghi; cần điều tra"],
                         {"kind": "investigate", "label": "Ghi nhận và mở điều tra"})


