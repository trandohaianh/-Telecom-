"""Ingestion guard: deduplication, event-time watermark and idempotent action execution.

Why this exists
---------------
The offline pipeline reads every event exactly once, in timestamp order, from a file. A real
message queue gives neither guarantee:

* a consumer that retries delivers the *same* event twice, and the feature store would count one
  call as two (calls_1m doubles, the risk score rises, an ordinary subscriber can be blocked);
* events arrive out of order, so a window computed on event time must know when it is allowed to
  consider itself complete;
* a block command that is replayed must not cut a second, unrelated call.

This module sits in front of the detector and makes those three failures impossible to reach the
feature store. It is deliberately independent of the models: it only looks at event identity and
event time.

State is bounded: the dedup index is pruned by event time, never by wall-clock, so replaying a
historical stream behaves exactly like live traffic.
"""
from __future__ import annotations

import math
from collections import OrderedDict

# Longer than the maximum retry/redelivery delay of the upstream system. 15 minutes of events at
# 25k eps is ~22M keys, which is why a deployment keeps this index in Redis rather than in-process.
# DEFAULT_DEDUP_TTL_S là TTL de-dup để giữ key trùng đủ lâu cho retry/redelivery.
DEFAULT_DEDUP_TTL_S = 900.0
# Grace for an upstream reorder buffer; this causal store rejects timestamp regressions.
# DEFAULT_ALLOWED_LATENESS_S là độ trễ cho phép của event time trước khi coi là quá muộn.
DEFAULT_ALLOWED_LATENESS_S = 120.0

ADMITTED = "ADMITTED"
DUPLICATE = "DUPLICATE"
LATE = "LATE"


# event_key tạo định danh duy nhất cho event để phát hiện duplicate qua event_id hoặc tuple người gọi / đối tác / ts.
def event_key(ev: dict) -> str:
    """Identity of an event.

    The operator's own event_id when it is stable; otherwise a derived key, so the guard still
    works on feeds that do not carry one.
    """
    eid = ev.get("event_id")
    if eid:
        return f'{ev.get("event_type", "EVENT")}|{eid}'
    party = ev.get("caller") or ev.get("sender") or ""
    other = ev.get("callee") or ev.get("recipient") or ""
    return f'{ev.get("event_type")}|{party}|{other}|{float(ev["ts"]):.17g}'


# IngestGuard quyết định event có được cập nhật feature store hay không theo dedup + watermark.
class IngestGuard:
    """Decides, for every incoming event, whether it may update the feature store.

    Three outcomes:
      ADMITTED  - new and at/after the accepted event-time frontier: score and update state.
      DUPLICATE - this key was already processed: drop it, state untouched.
      LATE      - behind the accepted frontier: record without scoring, mutation or enforcement.

    filter() stably sorts within a batch. Across requests there is no reorder buffer, including
    inside allowed_lateness_s. The watermark is diagnostic eligibility for an upstream buffer,
    never permission to insert past events into already-pruned causal deques.
    """

    # __init__ lưu key đã thấy và max_ts để tính watermark cho stream live và replay lịch sử.
    def __init__(self, dedup_ttl_s: float = DEFAULT_DEDUP_TTL_S,
                 allowed_lateness_s: float = DEFAULT_ALLOWED_LATENESS_S):
        self.dedup_ttl_s = float(dedup_ttl_s)
        self.allowed_lateness_s = float(allowed_lateness_s)
        if not math.isfinite(self.dedup_ttl_s) or self.dedup_ttl_s <= 0:
            raise ValueError("dedup_ttl_s must be positive and finite")
        if not math.isfinite(self.allowed_lateness_s) or self.allowed_lateness_s < 0:
            raise ValueError("allowed_lateness_s must be non-negative and finite")
        self._seen: "OrderedDict[str, float]" = OrderedDict()   # key -> event ts, insertion ordered
        self.max_ts = float("-inf")                             # highest event time seen
        self.stats = {ADMITTED: 0, DUPLICATE: 0, LATE: 0}

    # ------------------------------------------------------------------ watermark
    # watermark định nghĩa mọi event cũ hơn ngưỡng này đã đóng cửa và không nên thay đổi state.
    @property
    def watermark(self) -> float:
        """Everything older than this is considered complete."""
        return self.max_ts - self.allowed_lateness_s

    # _prune xóa key quá cũ theo event time, không dựa trên wall-clock, để replay lịch sử giống live.
    def _prune(self) -> None:
        """Drop keys older than the TTL. Event time, not wall-clock."""
        cutoff = self.max_ts - self.dedup_ttl_s
        seen = self._seen
        while seen:
            k, ts = next(iter(seen.items()))
            if ts >= cutoff:
                break
            del seen[k]

    # ------------------------------------------------------------------ main entry
    # admit phân loại event là ADMITTED / DUPLICATE / LATE dựa trên key và watermark.
    def admit(self, ev: dict) -> str:
        ts = float(ev["ts"])
        if not math.isfinite(ts):
            raise ValueError("event timestamp must be finite")
        key = event_key(ev)

        if key in self._seen:
            self.stats[DUPLICATE] += 1
            return DUPLICATE

        late = ts < self.max_ts
        if ts > self.max_ts:
            self.max_ts = ts
        # Insertion frontiers are monotonic, including rejected events, so TTL pruning is safe.
        self._seen[key] = self.max_ts
        self._prune()

        status = LATE if late else ADMITTED
        self.stats[status] += 1
        return status

    # filter tách batch thành event cần score và bản lặp bị drop ngay để giữ state chuẩn xác.
    def filter(self, events):
        """Return chronological ADMITTED inputs and statuses aligned to original occurrences.

        Duplicates and late inputs never enter feature state. Equal timestamps retain input order.
        """
        order = sorted(range(len(events)), key=lambda i: float(events[i]["ts"]))
        if any(not math.isfinite(float(events[i]["ts"])) for i in order):
            raise ValueError("event timestamp must be finite")
        to_score, statuses = [], [None] * len(events)
        for i in order:
            st = self.admit(events[i])
            statuses[i] = st
            if st == ADMITTED:
                to_score.append(events[i])
        return to_score, statuses

    def state_dict(self) -> dict:
        """Durable guard snapshot; event types namespace the stable upstream identity."""
        return {"version": 1, "policy": "strict_event_time_frontier", "seen": list(self._seen.items()),
                "max_ts": self.max_ts if math.isfinite(self.max_ts) else None,
                "dedup_ttl_s": self.dedup_ttl_s, "allowed_lateness_s": self.allowed_lateness_s,
                "stats": dict(self.stats)}

    def load_state_dict(self, state: dict) -> None:
        if state.get("version") != 1 or state.get("policy") != "strict_event_time_frontier":
            raise ValueError("unsupported ingestion guard checkpoint policy/version")
        restored = IngestGuard(state["dedup_ttl_s"], state["allowed_lateness_s"])
        max_ts = state.get("max_ts")
        restored.max_ts = float("-inf") if max_ts is None else float(max_ts)
        if max_ts is not None and not math.isfinite(restored.max_ts):
            raise ValueError("invalid ingestion frontier")
        previous = float("-inf")
        for key, ts in state.get("seen", []):
            ts = float(ts)
            if not isinstance(key, str) or not math.isfinite(ts) or ts < previous or ts > restored.max_ts:
                raise ValueError("invalid ingestion dedup checkpoint")
            restored._seen[key] = ts
            previous = ts
        restored.stats = {k: int(state.get("stats", {}).get(k, 0)) for k in (ADMITTED, DUPLICATE, LATE)}
        if any(v < 0 for v in restored.stats.values()):
            raise ValueError("invalid ingestion counters")
        restored._prune()
        self.__dict__.update(restored.__dict__)


    # state xuất trạng thái nội bộ để dashboard hoặc debug kiểm tra nhanh.
    def state(self) -> dict:
        return {"policy": "strict_event_time_frontier", "keys": len(self._seen), "max_ts": self.max_ts, "watermark": self.watermark,
                "dedup_ttl_s": self.dedup_ttl_s, "allowed_lateness_s": self.allowed_lateness_s,
                **{k.lower(): v for k, v in self.stats.items()}}


# ActionLedger đảm bảo action chặn/flag chỉ thực thi đúng một lần cho cùng một quyết định.
class ActionLedger:
    """Makes the *execution* of a decision idempotent.

    Deduplicating the input is not enough: the component that actually drops a call can itself be
    asked twice (a retried RPC, a re-consumed decision topic). Every decision carries a key, and a
    second attempt with the same key is a no-op rather than a second disconnect.
    """

    # __init__ lưu lịch sử action đã chạy trong TTL để xoá key cũ khỏi bộ nhớ.
    def __init__(self, ttl_s: float = DEFAULT_DEDUP_TTL_S):
        self.ttl_s = float(ttl_s)
        self._done: "OrderedDict[str, float]" = OrderedDict()
        self.max_ts = float("-inf")
        self.executed = 0
        self.suppressed = 0

    # decision_key tạo khóa idempotent từ event_id và loại quyết định để tránh action trùng.
    @staticmethod
    def decision_key(result: dict) -> str:
        return f'{result["event_id"]}|{result["decision"]}'

    # execute nếu key chưa chạy thì thực hiện action, nếu lặp lại thì bỏ qua để tránh trùng lệnh.
    def execute(self, result: dict, action=None) -> bool:
        """Run ``action`` at most once per decision key. Returns True if it ran."""
        key = self.decision_key(result)
        ts = float(result.get("ts", 0.0))
        self.max_ts = max(self.max_ts, ts)
        if key in self._done:
            self.suppressed += 1
            return False
        self._done[key] = ts
        cutoff = self.max_ts - self.ttl_s
        while self._done:
            k, t = next(iter(self._done.items()))
            if t >= cutoff:
                break
            del self._done[k]
        if action is not None:
            action(result)
        self.executed += 1
        return True
