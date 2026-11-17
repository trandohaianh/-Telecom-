"""Bounded inference admission, durable identities and single-owner model state.

The operator still enforces its own signalling timer. A timed-out task may finish later and
produce a retrospective alert, but its HTTP recommendation is always fail-open.
"""
from __future__ import annotations

import asyncio
import collections
import copy
import math
import hashlib
import json
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass

from backend_state import BackendState, EventConflict
from backend_model_identity import loaded_model_identity
from config import MODEL_VERSION
from ingest_guard import ADMITTED, IngestGuard
from resilience import fail_open_result, RescoreStateRestoreError

REQUIRED = {
    "CALL": ("event_id", "ts", "caller", "callee", "cell_id", "imei", "origin_trunk", "network"),
    "SMS": ("event_id", "ts", "sender", "recipient", "sms_route", "cell_id", "text"),
    "AUDIO": ("event_id", "ts", "caller", "callee", "codec", "vocoder_artifact", "pitch_jitter",
              "spectral_flatness", "speaker_drift"),
}
NUMERIC = {"ts", "duration_s", "end_ts", "caller_age_days", "vocoder_artifact", "pitch_jitter",
           "spectral_flatness", "speaker_drift", "caller_speech_ratio", "callee_speech_s"}
TEXT_FIELDS = {"event_id", "call_id", "caller", "callee", "sender", "recipient", "cell_id", "imei",
               "origin_trunk", "network", "sms_route", "codec", "text", "transcript", "event_type"}
OPTIONAL = NUMERIC | TEXT_FIELDS
# Upper sanity bound for event time in Unix SECONDS (~year 5138). A millisecond timestamp (1.7e12) or
# 1e308 would otherwise become the IngestGuard frontier and mark every later event LATE (unscored ALLOW),
# a state that is checkpointed and survives restarts.
MAX_EVENT_TS = 1e11


def validate_event(ev, kind="VERDICT"):
    if kind not in ("VERDICT", "CDR"):
        raise ValueError("operation must be VERDICT | CDR")
    if not isinstance(ev, dict) or not isinstance(ev.get("event_type"), str) or ev.get("event_type") not in REQUIRED:
        raise ValueError("event_type must be CALL | SMS | AUDIO")
    required = ("event_id", "ts", "caller", "duration_s") if kind == "CDR" else REQUIRED[ev["event_type"]]
    missing = [name for name in required if name not in ev]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    unknown = set(ev) - OPTIONAL
    if unknown:
        raise ValueError("unknown event fields: " + ", ".join(sorted(unknown)))
    ev = dict(ev)
    for name, value in ev.items():
        if name in NUMERIC:
            try:
                finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{name} must be a finite number")
            if name != "caller_age_days" and value < 0:
                raise ValueError(f"{name} must be nonnegative")
        elif name in TEXT_FIELDS:
            limit = 10000 if name in ("text", "transcript") else 200
            if not isinstance(value, str) or len(value) > limit or (not value and name not in ("text", "transcript", "imei")):
                raise ValueError(f"invalid {name}")
    for name in ("vocoder_artifact", "spectral_flatness", "speaker_drift", "caller_speech_ratio"):
        if name in ev and not 0 <= ev[name] <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    if kind == "CDR" and (ev["event_type"] != "CALL" or "duration_s" not in ev):
        raise ValueError("a CDR is a CALL with duration_s")
    if kind == "CDR":
        completion = float(ev.get("end_ts", float(ev["ts"]) + float(ev["duration_s"])))
        if not math.isfinite(completion) or completion < ev["ts"]:
            raise ValueError("CDR completion must be finite and not precede setup")
    if kind == "VERDICT" and ev["event_type"] == "CALL" and "duration_s" in ev:
        raise ValueError("setup verdicts must not include duration_s; send a separate CDR")
    for name in ("ts", "end_ts"):
        if name in ev and ev[name] > MAX_EVENT_TS:
            raise ValueError(f"{name} must be Unix seconds (got a value above {MAX_EVENT_TS:.0e})")
    ev.setdefault("caller_age_days", -1)
    return ev


async def await_precise_deadline(future, deadline):
    """Honor one perf_counter deadline even when an event-loop timer wakes early."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            result = await asyncio.wait_for(asyncio.shield(future), remaining)
            if time.perf_counter() >= deadline:
                raise asyncio.TimeoutError
            return result
        except asyncio.TimeoutError:
            if time.perf_counter() >= deadline:
                raise
            # Some Windows loop clocks/timers have coarser resolution than perf_counter.
            # Retrying shields the same job and uses only the remaining original budget.
            if future.done():
                return future.result()
            await asyncio.sleep(0)


@dataclass
class Work:
    seq: int
    kind: str
    event: dict
    deadline: float
    submitted: float
    future: Future
    deadline_unix: float = 0.0


class InferenceRuntime:
    def __init__(self, guarded, state=None, on_result=None, on_retro=None, execution_lock=None,
                 capacity=128, checkpoint_every=100_000, checkpoint_interval_s=30.0, start=True, admission_capacity=None,
                 batch_size=16, batch_wait_ms=0.0, cache_capacity=4096, owner_stall_after_s=5.0):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("inference capacity must be a positive integer")
        admission_capacity = capacity + 1 if admission_capacity is None else admission_capacity
        if type(admission_capacity) is not int or admission_capacity < 1:
            raise ValueError("admission capacity must be a positive integer")
        self.g = guarded
        self.state = state or BackendState()
        self.on_result = on_result or (lambda ev, result: None)
        self.on_retro = on_retro or (lambda results: None)
        self.execution_lock = execution_lock or threading.RLock()
        self.guard = IngestGuard()
        self.queue = queue.Queue(maxsize=capacity)
        self.admission_lock = threading.Lock()
        self._admission_slots = threading.BoundedSemaphore(admission_capacity)
        self._admission_count_lock = threading.Lock()
        self._admission_futures = set()
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if isinstance(batch_wait_ms, bool) or not math.isfinite(batch_wait_ms) or not 0 <= batch_wait_ms <= 5:
            raise ValueError("batch_wait_ms must be finite and between 0 and 5")
        if type(cache_capacity) is not int or cache_capacity < 1:
            raise ValueError("cache_capacity must be a positive integer")
        if isinstance(owner_stall_after_s, bool) or not math.isfinite(owner_stall_after_s) or owner_stall_after_s <= 0:
            raise ValueError("owner_stall_after_s must be finite and positive")
        self.owner_stall_after_s = owner_stall_after_s
        self._owner_busy_since = 0.0
        self._last_owner_progress = time.perf_counter()
        self._last_maintenance = time.perf_counter()
        self.busy_maintenance_interval_s = 1.0
        self.batch_size, self.batch_wait_ms = batch_size, batch_wait_ms
        self.cache_capacity = cache_capacity
        self._completed = collections.OrderedDict()
        self._store_id = self.state.get("store_id", "local")
        self.checkpoint_every = checkpoint_every
        self.checkpoint_interval_s = checkpoint_interval_s
        self.last_seq = 0
        self._since_checkpoint = 0
        self._checkpoint_at = time.perf_counter()
        self._closing = False
        self._fatal_error = None
        self._thread = None
        self.stats = collections.Counter(admission_capacity=admission_capacity)
        self._published_retro = 0
        self.model_identity = loaded_model_identity(self.g.det)
        self.model_version = self.model_identity.get("model_version") or MODEL_VERSION
        self.state.acquire_owner()
        self._restore()
        self.g.fallback.on_change = lambda policy: self.state.set("human_policy", policy)
        if start:
            self.start()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="inference-owner", daemon=True)
            self._thread.start()

    def _snapshot(self):
        det = self.g.det
        return {"seq": self.last_seq, "model_identity": self.model_identity, "feature_store": det.fs.state_dict(),
                "harvest_hits": copy.deepcopy(getattr(det, "harvest_hits", {})),
                "ingest": self.guard.state_dict(),
                "fallback_deny": dict(self.g.fallback.deny),
                "rescore": list(self.g.rescore_queue),
                "retro": list(self.g.retro), "retro_alerts": self.g.stats["retro_alerts"],
                "published_retro": self._published_retro}

    def _compact_state(self):
        """Drop window entries no future scoring can read, before each periodic checkpoint.

        Serving never called FeatureStore.prune_state, so per-callee deques, IMEI/cell links and pending
        CDR heaps of quiet parties grew for the life of the process and were serialised into every
        checkpoint. Scored events always have ts >= the guard's scoring floor (older ones are LATE), and
        rescoring replays fail-open events at their own ts, so the safe horizon is the smaller of the
        two. Metadata (first_seen, age, completed-call history) is kept: no TTL is applied here.
        """
        # Per-party frontiers (D1) admit events up to allowed_lateness_s behind the global maximum.
        horizon = getattr(self.guard, "scoring_floor", self.guard.max_ts)
        for event in self.g.rescore_queue:
            ts = event.get("ts") if isinstance(event, dict) else None
            if isinstance(ts, (int, float)) and math.isfinite(ts):
                horizon = min(horizon, float(ts))
        if math.isfinite(horizon):
            self.g.det.fs.prune_state(horizon)
            self.stats["state_compactions"] += 1

    def checkpoint(self):
        with self.execution_lock:
            self.state.set("checkpoint", self._snapshot())
            self._since_checkpoint = 0
            self._checkpoint_at = time.perf_counter()

    def _restore(self):
        snapshot = self.state.get("checkpoint")
        previous_identity = self.state.get("model_identity")
        if previous_identity is None:
            if snapshot is not None or self.state.has_completed_inference():
                raise RuntimeError("state has unversioned model history; use a fresh state database or an explicit migration")
            self.state.set("model_identity", self.model_identity)
        elif previous_identity != self.model_identity:
            raise RuntimeError("loaded model bundle/feature contract differs from durable state; explicit migration is required")
        if snapshot is not None and snapshot.get("model_identity") != self.model_identity:
            raise RuntimeError("checkpoint model identity mismatch; refusing to replay under another bundle")
        if snapshot:
            self.g.det.fs.load_state_dict(snapshot["feature_store"])
            self.g.det.harvest_hits = snapshot.get("harvest_hits", {})
            self.guard.load_state_dict(snapshot["ingest"])
            self.g.fallback.deny.update(snapshot.get("fallback_deny", {}))
            self.g.rescore_queue.extend(snapshot.get("rescore", []))
            self.g.retro.extend(snapshot.get("retro", []))
            self.g.stats["retro_alerts"] = snapshot.get("retro_alerts", len(self.g.retro))
            self._published_retro = snapshot.get("published_retro", self.g.stats["retro_alerts"])
            self.last_seq = snapshot["seq"]
        policy = self.state.get("human_policy", {})
        self.g.fallback.manual = dict(policy.get("manual", {}))
        self.g.fallback.allow = set(policy.get("allow", []))
        restored_refs = {r.get("journal_ref") for r in self.g.retro if r.get("journal_ref")}
        def restore_notifications(raw):
            ref = raw.get("journal_ref")
            if ref in restored_refs:
                return
            intents = raw.get("late_model_results", [])
            if not intents and raw.get("scored") and not raw.get("fail_open") and raw.get("decision") in ("BLOCK", "FLAG"):
                # A process crash cannot prove delivery of a committed response. Recover
                # an investigation intent, never a new network action or confirmed notice.
                intents = [dict(raw, decision="FLAG", model_decision=raw.get("decision"), retro=True,
                                requires_review=True, retro_context="commit_delivery_unconfirmed",
                                action_vi="Review committed evidence", action_en="Review committed evidence")]
            for intent in intents:
                limit = self.g.retro.maxlen
                if limit and self.g.stats["retro_alerts"] - self._published_retro >= limit:
                    self._publish_retro()
                item = dict(intent, journal_ref=ref or intent.get("journal_ref"))
                self.g.retro.append(item)
                self.g.stats["retro_alerts"] += 1
            if intents and ref:
                restored_refs.add(ref)
        if snapshot:
            for raw in snapshot.get("unconfirmed_delivery", []):
                restore_notifications(raw)
        # Rebuild changes since the checkpoint in journal order. Historical decisions are not
        # enforced again. The durable result ledger is the authority for previously returned RPCs.
        for seq, kind, ev, result in self.state.journal_after(self.last_seq):
            if kind not in ("VERDICT", "CDR"):
                # Observation/outbox reservations have no feature-store side effects. A crash
                # before their result commit must never feed them to the synthetic detector.
                self.last_seq = max(self.last_seq, seq)
                continue
            if result is None:
                work = Work(seq, kind, ev, time.perf_counter() - 1, time.perf_counter(), Future())
                self._execute(work)
                # Per-operation conflicts are delivered through the future, not raised
                # by _execute. Never checkpoint past an unresolved legacy journal row.
                if not work.future.done():
                    raise RuntimeError(
                        f"legacy pending journal sequence {seq} ({kind}) did not resolve; "
                        "explicit recovery or migration is required")
                try:
                    work.future.result()
                except Exception as exc:
                    raise RuntimeError(
                        f"legacy pending journal sequence {seq} ({kind}) replay failed; "
                        "explicit recovery or migration is required") from exc
            else:
                if kind == "CDR" and result.get("ok"):
                    self.g.det.fs.commit_cdr(ev)
                elif kind == "VERDICT" and result.get("ingest") not in ("OVERLOAD", "CLOSED"):
                    status = self.guard.admit(ev)
                    if status == ADMITTED:
                        if result.get("observed_without_model"):
                            self._observe_without_model(ev, {})
                            self.g.rescore_queue.append(ev)
                        else:
                            scored = self.g.det.score_batch([ev])[0]
                            if "learned_deny_updates" not in result and not result.get("late_model_results"):
                                self.g.apply_policy(ev, scored)  # legacy journals
                        for transition in [result] + result.get("late_model_results", []):
                            self.g.fallback.deny.update(transition.get("learned_deny_updates", {}))
                            for key in transition.get("learned_deny_evicted", []):
                                self.g.fallback.deny.pop(key, None)
                        restore_notifications(result)
                self.last_seq = max(self.last_seq, seq)
        for number in self.g.fallback.allow:
            self.g.fallback.deny.pop(number, None)
        self._publish_retro()
        if snapshot or self.last_seq:
            self.checkpoint()

    def _fail_open(self, ev, why, ingest="PENDING"):
        result = fail_open_result(ev, why)
        result.update(scored=False, scored_before_deadline=False, actionable=False, durable=False,
                      degraded=True, pending=ingest == "PENDING", ingest=ingest,
                      model_version=self.model_version, model_identity=self.model_identity,
                      artifact_status=getattr(self.g.det, "model_manifest", {}).get("status", "legacy_unverified"))
        return result

    def _finish(self, work, result):
        # Called inside the owner's transaction. Publication is deliberately post-COMMIT.
        result = dict(result)
        result.setdefault("model_version", self.model_version)
        result.setdefault("model_identity", self.model_identity)
        result.setdefault("artifact_status", getattr(self.g.det, "model_manifest", {}).get("status", "legacy_unverified"))
        result["journal_ref"] = f"{self._store_id}:{work.seq}"
        result["model_decision"] = result.get("model_decision", result.get("decision"))
        result.setdefault("learned_deny_updates", {})
        result.setdefault("learned_deny_evicted", [])
        result["delivery_status"] = "model_result_only"
        result["delivery_deadline_unix"] = work.deadline_unix
        result["network_action_taken"] = False
        result["score_elapsed_ms"] = round((time.perf_counter() - work.submitted) * 1000, 3)
        result["latency_phase"] = "before_commit"
        result["latency_ms"] = result["score_elapsed_ms"]
        self.state.finish(work.seq, result)
        return result

    def _delivery(self, work, raw, *, duplicate=False, commit_ms=0.0):
        elapsed = (time.perf_counter() - work.submitted) * 1000
        expired = time.perf_counter() >= work.deadline
        result = dict(raw)
        if work.kind == "VERDICT" and expired:
            result = self._fail_open(work.event, "durable result missed the response deadline",
                                     raw.get("ingest", "ADMITTED"))
            result.update(scored=bool(raw.get("scored")), model_seen=bool(raw.get("model_seen")),
                          model_decision=raw.get("model_decision", raw.get("decision")),
                          journal_ref=raw.get("journal_ref"), pending=False, delivery_expired=True)
        result.update(durable=True, admitted=True, duplicate=duplicate,
                      latency_ms=round(elapsed, 3), commit_ms=round(commit_ms, 3),
                      latency_phase="durable_delivery", delivery_status="duplicate" if duplicate else
                      ("expired" if expired else "completed"))
        result["scored_before_deadline"] = bool(not expired and not duplicate and result.get("scored")
                                                and not result.get("fail_open") and not result.get("pending"))
        result["actionable"] = bool(not expired and not duplicate and result.get("ingest") == ADMITTED
                                    and not result.get("pending"))
        return result

    def _enqueue(self, kind, ev, deadline, submitted):
        # This path is memory-only, so the event loop can enqueue without an executor hop.
        fingerprint = hashlib.sha256(json.dumps(ev, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        with self.admission_lock:
            if self._closing:
                return self._admission_rejected(kind, ev, closing=True)
            key = (kind, ev["event_id"])
            cached = self._completed.get(key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise EventConflict("event_id already belongs to different content for this operation")
                self._completed.move_to_end(key)
                self.stats["duplicates"] += 1
                work = Work(cached[1], kind, ev, deadline, submitted, Future())
                return self._delivery(work, cached[2], duplicate=True)
            work = Work(0, kind, ev, deadline, submitted, Future())
            work.fingerprint = fingerprint
            work.deadline_unix = time.time() + max(0.0, deadline - time.perf_counter())
            try:
                self.queue.put_nowait(work)
            except queue.Full:
                self.stats["overloaded"] += 1
                if kind == "CDR":
                    self.stats["cdr_admission_rejected"] += 1
                    return {"ok": False, "pending": False, "retryable": True, "durable": False,
                            "admitted": False, "event_id": ev["event_id"], "ingest": "OVERLOAD"}
                result = self._fail_open(ev, "inference queue full", "OVERLOAD")
                result.update(admitted=False, retryable=True)
                return result
            return work

    def _admission_finished(self, future):
        # A caller timeout/cancellation does not release an outstanding owner job.
        with self._admission_count_lock:
            self._admission_futures.discard(future)
            self.stats["admission_inflight"] -= 1
        self._admission_slots.release()

    def _admission_rejected(self, kind, event, closing=False):
        self.stats["admission_rejected"] += 1
        status = "CLOSED" if closing else ("OVERLOAD" if self.queue.full() else "ADMISSION_OVERLOAD")
        if kind == "CDR":
            return {"ok": False, "pending": False, "retryable": True, "durable": False,
                    "admitted": False, "event_id": event["event_id"], "ingest": status}
        result = self._fail_open(event, "admission service closing" if closing else "admission capacity full", status)
        result.update(admitted=False, retryable=True)
        return result

    async def submit(self, kind, event, budget_ms=30.0):
        ev = validate_event(event, kind)
        if isinstance(budget_ms, bool) or not math.isfinite(budget_ms) or budget_ms <= 0:
            raise ValueError("budget_ms must be finite and positive")
        submitted = time.perf_counter()
        deadline = submitted + budget_ms / 1000.0
        if self._closing:
            return self._admission_rejected(kind, ev, closing=True)
        if not self._admission_slots.acquire(blocking=False):
            return self._admission_rejected(kind, ev)
        with self._admission_count_lock:
            self.stats["admission_inflight"] += 1
            self.stats["admission_peak"] = max(self.stats["admission_peak"], self.stats["admission_inflight"])
        try:
            work = self._enqueue(kind, ev, deadline, submitted)
        except BaseException:
            self._admission_finished(None)
            raise
        if isinstance(work, dict):
            self._admission_finished(None)
            return work
        with self._admission_count_lock:
            self._admission_futures.add(work.future)
        work.future.add_done_callback(self._admission_finished)
        try:
            if work.future.done():
                if time.perf_counter() >= deadline:
                    raise asyncio.TimeoutError
                return work.future.result()
            wrapped = asyncio.wrap_future(work.future)
            wrapped.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
            return await await_precise_deadline(wrapped, deadline)
        except asyncio.TimeoutError:
            self.stats["http_timeouts"] += 1
            if work.future.done() and not work.future.cancelled():
                raw = work.future.result()
                return self._delivery(work, raw, duplicate=bool(raw.get("duplicate")),
                                      commit_ms=raw.get("commit_ms", 0))
            if kind == "CDR":
                return {"ok": False, "pending": True, "retryable": True, "durable": False,
                        "event_id": ev["event_id"]}
            result = self._fail_open(ev, "HTTP inference deadline exceeded")
            result.update(latency_ms=round((time.perf_counter() - submitted) * 1000, 3),
                          retryable=True, admitted=False)
            return result

    def _observe_without_model(self, event, result):
        # Detector.score_batch is transactional: on a model error no partial feature writes
        # remain. Keep counting admitted traffic even while the model/breaker is unavailable.
        fs = self.g.det.fs
        method = {"CALL": fs.call_features, "SMS": fs.sms_features, "AUDIO": fs.audio_features}[event["event_type"]]
        features = method(event)
        result["observed_without_model"] = True
        if getattr(self.g.det, "keep_features", False):
            result["evidence_features"] = features

    def _execute(self, work):
        self._execute_batch([work])

    def _execute_batch(self, works):
        completed, failures, pending = [], [], []
        pending_ids = set()
        checkpointed = False
        new_count = 0
        with self.execution_lock:
            with self.state.transaction():
                if hasattr(self.state, "flush_evidence_acks"):
                    self.state.flush_evidence_acks()
                def flush_scores():
                    if not pending:
                        return
                    items = list(pending)
                    pending.clear()
                    pending_ids.clear()
                    started = time.perf_counter()
                    retro_before = self.g.stats["retro_alerts"]
                    results = self.g.score_batch([work.event for work in items])
                    retro_count = self.g.stats["retro_alerts"] - retro_before
                    new_retro = list(self.g.retro)[-retro_count:] if retro_count else []
                    if len(results) != len(items):
                        raise RuntimeError("detector result count does not match the owner batch")
                    for work, result in zip(items, results):
                        result = dict(result, ingest=ADMITTED,
                                      queue_ms=round((started - work.submitted) * 1000, 3))
                        if not result.get("model_seen", True):
                            self._observe_without_model(work.event, result)
                        result["scored"] = bool(result.get("model_seen", not result.get("fail_open")))
                        late = [r for r in new_retro if r.get("event_id") == work.event["event_id"]]
                        for retro in late:
                            retro["journal_ref"] = f"{self._store_id}:{work.seq}"
                        if late:
                            result["late_model_results"] = copy.deepcopy(late)
                        completed.append((work, self._finish(work, result), False))
                for work in works:
                    if (work.kind, work.event['event_id']) in pending_ids:
                        flush_scores()
                    try:
                        # A committed operation remains authoritative across cache eviction
                        # and restart, including contradictory legacy cross-operation pairs.
                        # Check its digest without inserting before validating new links.
                        own_result = self.state.lookup_completed(work.kind, work.event)
                        if own_result is None:
                            related_kind = "VERDICT" if work.kind == "CDR" else "CDR"
                            related = self.state.event_payload(related_kind, work.event["event_id"])
                            if related is not None:
                                fields = ("caller", "ts", "call_id", "callee")
                                mismatch = [name for name in fields if name in related and
                                            name in work.event and related[name] != work.event[name]]
                                if related.get("event_type") != "CALL" or work.event.get("event_type") != "CALL" or mismatch:
                                    message = ("CDR does not match its linked CALL setup" if work.kind == "CDR"
                                               else "CALL setup does not match its linked CDR")
                                    raise EventConflict(message)
                        seq, previous = self.state.reserve(work.kind, work.event)
                    except EventConflict as exc:
                        failures.append((work, exc))
                        continue
                    work.seq = seq
                    if previous is not None:
                        flush_scores()
                        self.stats["duplicates"] += 1
                        completed.append((work, previous, True))
                        continue
                    new_count += 1
                    if work.kind == "CDR":
                        flush_scores()
                        self.g.det.fs.commit_cdr(work.event)
                        raw = {"ok": True, "event_id": work.event["event_id"], "scored": False,
                               "queue_ms": round((time.perf_counter() - work.submitted) * 1000, 3)}
                        completed.append((work, self._finish(work, raw), False))
                    else:
                        status = self.guard.admit(work.event)
                        if status == ADMITTED:
                            pending.append(work)
                            pending_ids.add((work.kind, work.event['event_id']))
                        else:
                            raw = self._fail_open(work.event, "event suppressed by ingestion guard", status)
                            raw.update(fail_open=False, degraded=False, pending=False)
                            completed.append((work, self._finish(work, raw), False))
                flush_scores()
                next_seq = max([self.last_seq] + [work.seq for work, _, _ in completed])
                if new_count and (self._since_checkpoint + new_count >= self.checkpoint_every or
                                  time.perf_counter() - self._checkpoint_at >= self.checkpoint_interval_s):
                    self._compact_state()
                    snapshot = self._snapshot()
                    snapshot["seq"] = next_seq
                    snapshot["unconfirmed_delivery"] = [raw for _, raw, duplicate in completed
                        if not duplicate and raw.get("scored") and not raw.get("fail_open")
                        and raw.get("decision") in ("BLOCK", "FLAG")]
                    self.state.set("checkpoint", snapshot)
                    checkpointed = True
                before_commit = time.perf_counter()
            commit_ms = (time.perf_counter() - before_commit) * 1000
            self.last_seq = next_seq
            self._since_checkpoint += new_count
            if checkpointed:
                self._since_checkpoint = 0
                self._checkpoint_at = time.perf_counter()
            self.stats["owner_commits"] += 1
            self.stats["committed_events"] += new_count
            self.stats["last_commit_ms"] = round(commit_ms, 3)
            self.stats["max_commit_ms"] = max(self.stats["max_commit_ms"], round(commit_ms, 3))
            self.stats["max_owner_batch"] = max(self.stats["max_owner_batch"], len(works))
        # No database or model lock is held when a future or callback is published.
        delivered = []
        for work, raw, duplicate in completed:
            result = self._delivery(work, raw, duplicate=duplicate, commit_ms=commit_ms)
            with self.admission_lock:
                self._completed[(work.kind, work.event["event_id"])] = (
                    getattr(work, "fingerprint", "") or hashlib.sha256(json.dumps(work.event, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest(), work.seq, copy.deepcopy(raw))
                self._completed.move_to_end((work.kind, work.event["event_id"]))
                while len(self._completed) > self.cache_capacity:
                    self._completed.popitem(last=False)
            if not work.future.done():
                work.future.set_result(result)
            if not duplicate:
                delivered.append((work, raw, result))
        for work, exc in failures:
            if not work.future.done():
                work.future.set_exception(exc)
        for work, raw, result in delivered:
            self.on_result(work.event, result)
            if result.get("delivery_expired") and raw.get("scored") and not raw.get("fail_open"):
                with self.execution_lock:
                    self.g._keep_retro([work.event], [raw])
        self._publish_retro()

    def _publish_retro(self):
        new = self.g.stats["retro_alerts"] - self._published_retro
        if new > 0:
            if new > len(self.g.retro):
                raise RuntimeError("unpublished retrospective intents exceed retained history; recover from journal")
            self.on_retro(list(self.g.retro)[-new:])
            self._published_retro = self.g.stats["retro_alerts"]

    async def lookup_observation(self, event):
        """Read durable audio history without recomputing or reinterpreting old features."""
        def lookup():
            try:
                return self.state.lookup_completed("OBSERVATION", event)
            except EventConflict:
                stored = self.state.event_payload("OBSERVATION", event["event_id"])
                if (stored is None or stored.get("schema_version") != "voice-acoustic-v1"
                        or event.get("schema_version") != "voice-acoustic-v2"):
                    raise
                # Schema is server-derived metadata, not a changed upstream WAV.
                # Canonical lookup still checks every original business field and hash.
                legacy = dict(event, schema_version="voice-acoustic-v1")
                previous = self.state.lookup_completed("OBSERVATION", legacy)
                if previous is None:
                    raise EventConflict("legacy audio observation is incomplete; explicit recovery is required")
                return previous
        previous = await asyncio.to_thread(lookup)
        return dict(previous, duplicate=True) if previous is not None else None

    async def record_observation(self, event, result):
        """Persist real-audio results with one FULL commit and no proxy model call."""
        def record():
            with self.state.transaction():
                seq, previous = self.state.reserve("OBSERVATION", event)
                if previous is None:
                    observed = dict(result, record_type="audio_observation",
                                    journal_ref=f"{self._store_id}:{seq}")
                    self.state.finish(seq, observed)
            if previous is not None:
                return dict(previous, duplicate=True)
            self.on_result(event, observed)
            return observed
        return await asyncio.to_thread(record)

    def _stop_after_owner_error(self, exc, works=()):
        """Stop admissions and resolve volatile work without checkpointing suspect state."""
        with self.admission_lock:
            self._fatal_error = exc
            self._closing = True
            self.stats["worker_errors"] += 1
            pending = list(works)
            while True:
                try:
                    abandoned = self.queue.get_nowait()
                except queue.Empty:
                    break
                pending.append(abandoned)
                self.queue.task_done()
        for work in pending:
            if work.future.done():
                continue
            if work.kind == "CDR":
                result = {"ok": False, "pending": False, "durable": False,
                          "event_id": work.event["event_id"], "ingest": "RECOVERY_REQUIRED"}
            else:
                result = self._fail_open(work.event, "inference owner requires recovery", "RECOVERY_REQUIRED")
            result.update(admitted=False, retryable=True)
            work.future.set_result(result)

    def _loop(self):
        while True:
            try:
                first = self.queue.get(timeout=0.1)
            except queue.Empty:
                if self._closing:
                    break
                if not self._maintenance(32):
                    break
                continue
            works = [first]
            if self.batch_wait_ms:
                # Optional coalescing is bounded by the earliest original caller deadline.
                until = min(first.deadline, time.perf_counter() + self.batch_wait_ms / 1000)
                while len(works) < self.batch_size:
                    remaining = until - time.perf_counter()
                    if remaining <= 0:
                        break
                    try:
                        works.append(self.queue.get(timeout=remaining))
                    except queue.Empty:
                        break
            while len(works) < self.batch_size:
                try:
                    works.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._owner_busy_since = time.perf_counter()
                self._execute_batch(works)
            except Exception as exc:
                # A failed COMMIT invalidates every speculative RAM mutation in this batch.
                # Stop without checkpointing; the next owner replays only committed records.
                self._stop_after_owner_error(exc, works)
                break
            finally:
                self._owner_busy_since = 0.0
                self._last_owner_progress = time.perf_counter()
                for work in works:
                    self.queue.task_done()
            # Under steady traffic the queue is never empty for 100 ms, so idle-only draining
            # starved the rescore queue until it silently dropped its oldest fail-open events.
            busy_for = time.perf_counter() - self._last_maintenance
            if busy_for >= self.busy_maintenance_interval_s and (
                    getattr(self.g, "rescore_queue", None) or getattr(self.g, "retro", None)):
                if not self._maintenance(8):
                    break
        if self._fatal_error is None:
            self.checkpoint()

    def _maintenance(self, max_rescore):
        """Publish retrospective intents and drain a bounded rescore batch. False = owner stopped."""
        try:
            self._owner_busy_since = time.perf_counter()
            # Publish pending intents first: a failing callback must apply
            # backpressure before another rescore can overwrite the bounded deque.
            self._publish_retro()
            with self.execution_lock:
                limit = self.g.retro.maxlen or 32
                self.g.drain_rescore(max_n=min(max_rescore, limit))
            self._publish_retro()
        except Exception as exc:
            self.stats["maintenance_errors"] += 1
            if isinstance(exc, RescoreStateRestoreError):
                self._stop_after_owner_error(exc)
                return False
        finally:
            self._owner_busy_since = 0.0
            self._last_owner_progress = self._last_maintenance = time.perf_counter()
        return True

    def progress(self):
        # Readiness must not take the writer/model locks when a native scorer stalls.
        now = time.perf_counter()
        busy = max(0.0, now - self._owner_busy_since) if self._owner_busy_since else 0.0
        stalled = busy >= self.owner_stall_after_s
        return {"ready": not self._closing and self._fatal_error is None and not stalled,
                "stalled": stalled, "owner_busy_ms": round(busy * 1000, 3),
                "seconds_since_progress": round(now - self._last_owner_progress, 3)}

    def close(self):
        with self.admission_lock:
            self._closing = True
        if self._thread is not None:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("inference worker is still draining; committed journal retained for recovery")
        elif self._fatal_error is None:
            # Unstarted volatile jobs were never accepted durably and must remain retryable.
            while not self.queue.empty():
                work = self.queue.get_nowait()
                if not work.future.done():
                    work.future.set_result(self._admission_rejected(work.kind, work.event, closing=True))
                self.queue.task_done()
            self.checkpoint()
