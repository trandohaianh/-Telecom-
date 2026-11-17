"""Circuit breaker + fail-open wrapper around the detector.

Telecom signalling cannot wait for a slow model: if the verdict is not ready inside the tier's
budget, the switch must continue the call (fail-open) rather than drop legitimate traffic.
In production the network element enforces the hard timer. While the model is unavailable a
small set of model-free fallback rules still runs (so overloading the detector is not a free pass),
then traffic fails open. Late verdicts are kept, events the model never saw are re-scored after
recovery, and hits become post-hoc ("retro") alerts.
"""
from __future__ import annotations

import collections
import copy
import heapq
import itertools
import threading
import time

import reason_codes as rc
from config import CB_COOLDOWN_S, CB_FAILURE_THRESHOLD


class RescoreStateRestoreError(RuntimeError):
    """Live feature state could not be restored; the owner must stop and replay."""
    requires_recovery = True


# CircuitBreaker mở breaker khi lỗi lặp lại, chuyển sang fail-open để không chặn sai lưu lượng.
class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    # __init__ lưu trạng thái breaker, số lỗi liên tiếp và thời gian cooldown để half-open thử lại.
    def __init__(self, failure_threshold=CB_FAILURE_THRESHOLD, cooldown_s=CB_COOLDOWN_S, clock=time.monotonic):
        self.failure_threshold, self.cooldown_s, self.clock = failure_threshold, cooldown_s, clock
        self.state, self.failures, self.opened_at = self.CLOSED, 0, 0.0
        self.stats = {"opened": 0, "fail_open_events": 0, "timeouts": 0, "errors": 0}
        self._lock = threading.Lock()

    # allow_request cho phép request đi tiếp khi breaker đóng hoặc vừa thử lại sau cooldown.
    def allow_request(self) -> bool:
        with self._lock:
            if self.state == self.OPEN and self.clock() - self.opened_at >= self.cooldown_s:
                self.state = self.HALF_OPEN          # let one trial batch through
            return self.state != self.OPEN

    # record_success reset bộ đếm lỗi và đóng breaker sau khi phục hồi thành công.
    def record_success(self):
        with self._lock:
            self.failures = 0
            self.state = self.CLOSED

    # record_failure tăng số lỗi / timeout và mở breaker khi vượt ngưỡng.
    def record_failure(self, kind="error"):
        with self._lock:
            self.stats["timeouts" if kind == "timeout" else "errors"] += 1
            self.failures += 1
            if self.state == self.HALF_OPEN or self.failures >= self.failure_threshold:
                if self.state != self.OPEN:
                    self.stats["opened"] += 1
                self.state, self.opened_at = self.OPEN, self.clock()


# fail_open_result tạo payload fail-open chuẩn hóa để caller / log biết event đã được cho qua.
def fail_open_result(ev, why):
    code, vi, en = rc.FAIL_OPEN
    return {"event_id": ev["event_id"], "event_type": ev["event_type"], "ts": ev["ts"], "decision": "ALLOW",
            "fail_open": True, "risk": None, "action_vi": "Cho qua (fail-open)", "action_en": "Allowed (fail-open)",
            "reasons": [{"code": code, "vi": f"{vi} [{why}]", "en": f"{en} [{why}]", "feature": "resilience"}],
            "tier": 1 if ev["event_type"] == "CALL" else 2, "party": ev.get("caller") or ev.get("sender"),
            "counterparty": ev.get("callee") or ev.get("recipient")}


# FallbackRules: lớp phòng vệ tối thiểu khi mô hình không trả lời kịp, chỉ gồm tra cứu bảng (vài micro giây).
class FallbackRules:
    """Model-free checks used while the detector is unavailable.

    Without it, fail-open turns any overload into a free pass for every fraudster (an attacker only
    has to slow the detector down). The rules are deliberately few and near-certain, so the
    no-wrong-block goal still holds: a group the model already blocked, a protected hotline that
    does not come from its owner's trunk, and a brand sender name outside the brandname gateway.
    """

    def __init__(self, ttl_s: float = 24 * 3600, max_entries: int = 200_000):
        from data_generator import BRAND_SENDERS, PROTECTED_CLI
        self.protected, self.brands = set(PROTECTED_CLI), set(BRAND_SENDERS)
        self.ttl_s, self.max_entries = ttl_s, max_entries
        self.deny: dict[str, float] = {}     # number or IMEI -> event time it was blocked by the model
        # Human decisions (approved agent proposals). They override the model, in both directions.
        self.manual: dict[str, str] = {}     # number or IMEI -> proposal id that approved the group block
        self.allow: set[str] = set()         # numbers an analyst confirmed as legitimate
        self.on_change = None               # durable human-policy callback, installed by live runtime

    def _save_policy(self, manual, allow):
        if self.on_change is not None:
            self.on_change({"manual": manual, "allow": sorted(allow)})

    def block_manual(self, keys, ref: str):
        manual, allow = dict(self.manual), set(self.allow)
        for k in keys:
            if k and k not in self.protected and k not in self.brands:
                manual[str(k)] = ref
                allow.discard(str(k))
        self._save_policy(manual, allow)
        self.manual, self.allow = manual, allow

    def allow_manual(self, number: str):
        manual, allow = dict(self.manual), set(self.allow)
        allow.add(str(number))
        manual.pop(str(number), None)
        self._save_policy(manual, allow)
        self.manual, self.allow = manual, allow
        self.deny.pop(str(number), None)

    # learn ghi nhớ số và IMEI của mọi quyết định BLOCK do mô hình đưa ra (không học từ chính luật dự phòng).
    def learn(self, ev, result):
        # Persist per-event effects, including explicit no-ops, so recovery never has to
        # reinterpret historical decisions under a newer human/commercial policy.
        updates, evicted = {}, []
        result["learned_deny_updates"] = updates
        result["learned_deny_evicted"] = evicted
        if result.get("decision") != "BLOCK" or result.get("fail_open"):
            return
        if (ev.get("caller") or ev.get("sender")) in self.allow:
            return
        for key in (ev.get("caller") or ev.get("sender"), ev.get("imei")):
            if key in self.protected or key in self.brands:
                continue
            if key and key != "-1":
                key = str(key)
                before = self.deny.get(key, float("-inf"))
                after = max(before, ev["ts"])
                if after != before:
                    self.deny[key] = after
                    updates[key] = after
        if len(self.deny) > self.max_entries:
            # nsmallest: O(n log k) cho k mục cần bỏ (thường 1), thay vì sắp xếp lại 200k mục mỗi lần BLOCK.
            for key, _ in heapq.nsmallest(len(self.deny) - self.max_entries, self.deny.items(), key=lambda kv: kv[1]):
                del self.deny[key]
                evicted.append(key)

    def _denied(self, key, ts):
        t = self.deny.get(str(key)) if key else None
        return t is not None and 0 <= ts - t <= self.ttl_s

    # check trả về (decision, reason) hoặc None nếu không luật nào khớp.
    def check(self, ev):
        et, ts = ev.get("event_type"), ev.get("ts", 0.0)
        party = ev.get("caller") or ev.get("sender")
        if str(party) in self.manual or (ev.get("imei") and str(ev.get("imei")) in self.manual):
            return "BLOCK", rc.GROUP_BLOCK
        if party in self.allow:
            return None
        if self._denied(party, ts) or self._denied(ev.get("imei"), ts):
            return "BLOCK", rc.FALLBACK_DENYLIST
        if et == "CALL" and party in self.protected and ev.get("origin_trunk", "BANK_TRUNK") != "BANK_TRUNK":
            return "BLOCK", rc.FALLBACK_SPOOF
        if et == "SMS" and party in self.brands and ev.get("sms_route", "BRAND_GW") != "BRAND_GW":
            return "FLAG", rc.FALLBACK_BRAND
        return None


# GuardedDetector bọc detector bằng ngân sách latency và breaker; khi lỗi thì chạy luật dự phòng rồi mới fail-open.
class GuardedDetector:
    """Wraps Detector.score_batch with a latency budget, a circuit breaker, fallback rules and re-scoring."""

    # __init__ lưu detector được bảo vệ, ngân sách, luật dự phòng và hàng đợi chấm lại có giới hạn.
    def __init__(self, detector, budget_ms: float, breaker: CircuitBreaker | None = None,
                 fallback: FallbackRules | None = None, rescore_max: int = 50_000,
                 registry=None, content_budget_ms: float | None = None):
        self.det, self.budget_ms = detector, budget_ms
        # Batch không có CALL (chỉ SMS/AUDIO) có ngân sách tầng 2 (170 ms), không phải 30 ms của tầng 1.
        self.content_budget_ms = content_budget_ms
        self.cb = breaker or CircuitBreaker()
        self.fallback = fallback if fallback is not None else FallbackRules()
        # Danh bạ số thương mại đã đăng ký gọi ra hàng loạt. Chỉ miễn các tín hiệu
        # thuộc về việc gọi ra hàng loạt, không bao giờ miễn tín hiệu gian lận.
        from commercial_registry import CommercialRegistry
        self.registry = registry if registry is not None else CommercialRegistry()
        self.chaos_delay_ms = 0.0     # fault injection for demos / tests
        self.chaos_error = False
        # Events the model never saw (breaker open / error) wait here to be re-scored after recovery.
        # Bounded: when full the oldest is dropped and counted, never silently lost.
        self.rescore_queue: collections.deque = collections.deque(maxlen=rescore_max)
        # Verdicts that arrived after the budget (timeout) or from re-scoring, for post-hoc action.
        self.retro: collections.deque = collections.deque(maxlen=5_000)
        self.stats = {"fallback_block": 0, "fallback_flag": 0, "rescore_dropped": 0, "rescored": 0,
                      "retro_alerts": 0, "late_verdicts": 0}

    def _degraded(self, events, why, queue=True):
        """Fallback rules first; whatever they do not catch is allowed (fail-open)."""
        out = []
        for e in events:
            hit = self.fallback.check(e)
            r = fail_open_result(e, why)
            r["degraded"] = True
            r["model_seen"] = not queue
            if hit:
                decision, (code, vi, en) = hit
                r["decision"] = decision
                r["action_vi"], r["action_en"] = ("Chặn (luật dự phòng)", "Blocked (fallback)") if decision == "BLOCK" else ("Cảnh báo (luật dự phòng)", "Flagged (fallback)")
                r["reasons"].insert(0, {"code": code, "vi": vi, "en": en, "feature": "fallback"})
                self.stats["fallback_block" if decision == "BLOCK" else "fallback_flag"] += 1
            out.append(r)
        self.cb.stats["fail_open_events"] += len(events)
        if queue:
            for e in events:
                if len(self.rescore_queue) == self.rescore_queue.maxlen:
                    self.stats["rescore_dropped"] += 1
                self.rescore_queue.append(e)
        return out

    # score_batch chạy chấm điểm trong budget; nếu chậm hoặc lỗi thì chạy luật dự phòng và xếp hàng chấm lại.
    def score_batch(self, events):
        if not self.cb.allow_request():
            return self._degraded(events, "circuit OPEN")
        t = time.perf_counter()
        try:
            if self.chaos_error:
                raise RuntimeError("injected model failure")
            res = self.det.score_batch(events)
            if self.chaos_delay_ms:
                time.sleep(self.chaos_delay_ms / 1000.0)
        except Exception as exc:  # noqa: BLE001 - any detector failure must fail open
            self.cb.record_failure("error")
            return self._degraded(events, f"error: {type(exc).__name__}")
        elapsed = (time.perf_counter() - t) * 1000.0
        budget = self.budget_ms
        if self.content_budget_ms is not None and not any(e.get("event_type") == "CALL" for e in events):
            budget = max(budget, self.content_budget_ms)
        if elapsed > budget:
            self.cb.record_failure("timeout")
            # The model did score these events (its state is already updated), only too late to act
            # before the phone rang: keep the late verdicts instead of scoring the events twice.
            # Decide these events first (the network already continued them), only then learn from
            # the late verdicts, so a late verdict never acts on its own event.
            out = self._degraded(events, f"timeout {elapsed:.1f} ms > {budget:.0f} ms", queue=False)
            self.stats["late_verdicts"] += len(res)
            self._keep_retro(events, res)
            return out
        self.cb.record_success()
        for e, r in zip(events, res):
            r["model_seen"] = True
            self.apply_policy(e, r)
        return res

    def apply_policy(self, event, result, learn=True):
        """Use identical policies before normal, late and retrospective decisions are learned."""
        if result.get("policy_applied"):
            return result
        self.registry.apply(event, result)
        self._apply_human_decisions(event, result)
        if learn:
            self.fallback.learn(event, result)
        result["policy_applied"] = True
        return result

    def _apply_human_decisions(self, ev, r):
        """Approved group blocks and analyst-confirmed numbers override the model's verdict."""
        fb, party, imei = self.fallback, str(ev.get("caller") or ev.get("sender") or ""), str(ev.get("imei") or "")
        if party in fb.manual or (imei and imei in fb.manual):
            code, vi, en = rc.GROUP_BLOCK
            r["decision"], r["human_override"] = "BLOCK", "group_block"
            r["reasons"] = [{"code": code, "vi": vi, "en": en, "feature": "human"}] + r.get("reasons", [])
        elif party in fb.allow and r.get("decision") == "BLOCK":
            code, vi, en = rc.HUMAN_ALLOW
            r["decision"], r["human_override"] = "FLAG", "allow"
            r["reasons"] = [{"code": code, "vi": vi, "en": en, "feature": "human"}] + r.get("reasons", [])
        else:
            return
        from detector import ACTIONS
        r["action_vi"], r["action_en"] = ACTIONS.get((r.get("event_type"), r["decision"]), (r.get("action_vi"), r.get("action_en")))

    def _keep_retro(self, events, results, requires_review=False):
        new = []
        for e, r in zip(events, results):
            self.apply_policy(e, r, learn=not requires_review)
            if r.get("decision") != "ALLOW":
                code, vi, en = rc.RETRO
                rr = dict(r, retro=True, reasons=[{"code": code, "vi": vi, "en": en, "feature": "resilience"}] + r.get("reasons", []))
                if requires_review:
                    rr["model_decision"] = rr["decision"]
                    rr["decision"] = "FLAG"
                    rr["action_vi"] = "Cần chuyên viên xem lại kết quả chấm lại sau sự cố"
                    rr["action_en"] = "Analyst review required; no network action was taken"
                rr["requires_review"] = requires_review
                rr["retro_context"] = "current_snapshot" if requires_review else "original_event"
                self.retro.append(rr)
                new.append(rr)
                self.stats["retro_alerts"] += 1
        return new

    # drain_rescore chấm lại tối đa max_n sự kiện đang chờ, chỉ khi breaker đóng; gọi ngoài đường quyết định.
    def drain_rescore(self, max_n: int = 256, budget_ms: float = 20.0):
        """Prepare retrospective results without consuming queued work on failure.

        The owner serializes this method with live inference. Restore failures
        require owner recovery; policy failures retain the batch and roll back
        partially prepared retrospective alerts and counters.
        """
        if not self.rescore_queue or self.chaos_error or self.chaos_delay_ms:
            return []
        if not self.cb.allow_request():
            return []
        batch = list(itertools.islice(self.rescore_queue, min(max_n, len(self.rescore_queue))))
        if not batch:
            return []
        # Snapshot before consuming any queued work. A snapshot failure is retryable.
        fs = getattr(self.det, "fs", None)
        before = fs.state_dict() if fs is not None else None
        harvest = copy.deepcopy(getattr(self.det, "harvest_hits", None))
        model_error = None
        # Đo riêng thời gian chấm điểm: sao chép/khôi phục state là bảo trì, không phải lỗi mô hình,
        # nên không được đếm thành timeout và mở breaker cho cả lưu lượng thật.
        t = time.perf_counter()
        scoring_ms = 0.0
        try:
            res = self.det.score_batch(batch)
            scoring_ms = (time.perf_counter() - t) * 1000.0
            if len(res) != len(batch):
                raise RuntimeError("rescore result count does not match queued events")
        except Exception as exc:
            model_error = exc
        finally:
            restore_errors = []
            try:
                if before is not None:
                    fs.load_state_dict(before)
            except Exception as exc:
                restore_errors.append(exc)
            try:
                if harvest is not None:
                    self.det.harvest_hits = harvest
            except Exception as exc:
                restore_errors.append(exc)
            if restore_errors:
                raise RescoreStateRestoreError(
                    "rescore could not restore live state; inference owner requires recovery"
                ) from restore_errors[0]
        if model_error is not None:
            self.cb.record_failure("error")
            return []
        retro_before, stats_before = list(self.retro), dict(self.stats)
        try:
            new = self._keep_retro(batch, res, requires_review=True)
        except Exception:
            self.retro.clear()
            self.retro.extend(retro_before)
            self.stats.clear()
            self.stats.update(stats_before)
            self.cb.record_failure("error")
            raise
        for _ in batch:
            self.rescore_queue.popleft()
        self.stats["rescored"] += len(batch)
        if scoring_ms > budget_ms:
            self.cb.record_failure("timeout")
        else:
            self.cb.record_success()
        return new
