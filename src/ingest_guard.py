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

POLICY = "per_party_event_time_frontier"
ADMITTED = "ADMITTED"
DUPLICATE = "DUPLICATE"
LATE = "LATE"


def event_party(ev: dict) -> str:
    """Whose history an event extends: the calling party of a CALL/AUDIO, the sender of an SMS."""
    return str(ev.get("caller") or ev.get("sender") or "")


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
      ADMITTED  - new, not behind its own party's frontier, and within allowed_lateness_s of the
                  global frontier: score and update state.
      DUPLICATE - this key was already processed: drop it, state untouched.
      LATE      - behind its party's frontier, or more than allowed_lateness_s behind the global one:
                  record without scoring, mutation or enforcement.

    Policy "per_party_event_time_frontier" (owner decision D1, 17/11/2026): the frontier is kept per
    calling party (caller / sender). Before, one global frontier meant that two trunks delivering 1 ms
    out of order made the older call LATE and unscored. Each party's own history (its call deques,
    out24, radio track) still only moves forward; the few cross-party structures of FeatureStore
    tolerate the bounded disorder (<= allowed_lateness_s). filter() stably sorts within a batch.
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
        # party -> highest admitted ts; least recently updated first, pruned below the global floor
        self._party_max: "OrderedDict[str, float]" = OrderedDict()
        self._legacy_frontier = float("-inf")   # set only when restoring a v1 (global-frontier) checkpoint
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

    def _prune_parties(self) -> None:
        """Party frontiers below the global floor are implied by it and can be forgotten."""
        floor = self.max_ts - self.allowed_lateness_s
        parties = self._party_max
        while parties:
            party, ts = next(iter(parties.items()))
            if ts >= floor:
                break
            del parties[party]

    @property
    def scoring_floor(self) -> float:
        """No event older than this can still be admitted (used as the state-compaction horizon)."""
        return max(self.max_ts - self.allowed_lateness_s, self._legacy_frontier)

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

        party = event_party(ev)
        floor = self.max_ts - self.allowed_lateness_s
        late = ts < floor or ts < self._legacy_frontier or ts < self._party_max.get(party, float("-inf"))
        if ts > self.max_ts:
            self.max_ts = ts
        if not late:
            self._party_max[party] = max(ts, self._party_max.get(party, ts))
            self._party_max.move_to_end(party)
        self._prune_parties()
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
        return {"version": 2, "policy": POLICY, "seen": list(self._seen.items()),
                "party_max": list(self._party_max.items()),
                "legacy_frontier": self._legacy_frontier if math.isfinite(self._legacy_frontier) else None,
                "max_ts": self.max_ts if math.isfinite(self.max_ts) else None,
                "dedup_ttl_s": self.dedup_ttl_s, "allowed_lateness_s": self.allowed_lateness_s,
                "stats": dict(self.stats)}

    def load_state_dict(self, state: dict) -> None:
        legacy = state.get("version") == 1 and state.get("policy") == "strict_event_time_frontier"
        if not legacy and (state.get("version") != 2 or state.get("policy") != POLICY):
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
        for party, ts in ([] if legacy else state.get("party_max", [])):
            ts = float(ts)
            if not isinstance(party, str) or not math.isfinite(ts) or ts > restored.max_ts:
                raise ValueError("invalid ingestion party frontier")
            restored._party_max[party] = ts
        if legacy and math.isfinite(restored.max_ts):
            # A v1 checkpoint knew only the global frontier: nothing older than it may be admitted.
            restored._legacy_frontier = restored.max_ts
        elif state.get("legacy_frontier") is not None:
            restored._legacy_frontier = float(state["legacy_frontier"])
            if not math.isfinite(restored._legacy_frontier) or restored._legacy_frontier > restored.max_ts:
                raise ValueError("invalid legacy ingestion frontier")
        restored.stats = {k: int(state.get("stats", {}).get(k, 0)) for k in (ADMITTED, DUPLICATE, LATE)}
        if any(v < 0 for v in restored.stats.values()):
            raise ValueError("invalid ingestion counters")
        restored._prune()
        self.__dict__.update(restored.__dict__)


    # state xuất trạng thái nội bộ để dashboard hoặc debug kiểm tra nhanh.
    def state(self) -> dict:
        return {"policy": POLICY, "keys": len(self._seen), "parties": len(self._party_max), "max_ts": self.max_ts, "watermark": self.watermark,
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
