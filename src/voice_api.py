"""Signed real-WAV observations, bounded work and an independent real-audio model domain."""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import security
from voice_model import RealVoiceModel
from voice_pipeline import AudioValidationError, MAX_WAV_BYTES, SCHEMA_VERSION, LocalWhisperASR, analyze_wav

MAX_BODY_BYTES = 4 * ((MAX_WAV_BYTES + 2) // 3) + 65536


def _metadata(data):
    required = ("event_id", "call_id", "caller", "callee", "ts", "wav_b64")
    missing = [name for name in required if name not in data]
    if missing:
        raise HTTPException(400, "missing fields: " + ", ".join(missing))
    for name in ("event_id", "call_id", "caller", "callee"):
        if not isinstance(data[name], str) or not data[name].strip() or len(data[name]) > 128:
            raise HTTPException(400, name + " must be a non-empty string of at most 128 characters")
    try:
        valid_ts = type(data["ts"]) in (int, float) and math.isfinite(float(data["ts"])) and data["ts"] >= 0
    except (OverflowError, TypeError, ValueError):
        valid_ts = False
    if not valid_ts:
        raise HTTPException(400, "ts must be finite and non-negative")
    if data.get("event_type", "AUDIO") != "AUDIO":
        raise HTTPException(400, "event_type must be AUDIO")
    if not isinstance(data["wav_b64"], str):
        raise HTTPException(400, "wav_b64 must be base64 text")
    channel = data.get("caller_channel")
    if channel is not None and (type(channel) is not int or channel not in (0, 1)):
        raise HTTPException(400, "caller_channel must be 0 or 1")
    try:
        payload = base64.b64decode(data["wav_b64"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(400, "invalid base64 WAV") from exc
    if len(payload) > MAX_WAV_BYTES:
        raise HTTPException(413, "WAV exceeds size limit")
    event = {name: data[name] for name in ("event_id", "call_id", "caller", "callee", "ts")}
    event.update(event_type="VOICE_OBSERVATION", audio_sha256=hashlib.sha256(payload).hexdigest(),
                 caller_channel=channel, schema_version=SCHEMA_VERSION)
    return event, payload, channel


class VoiceService:
    def __init__(self, *, record_observation=None, lookup_observation=None,
                 mode=lambda: "shadow", analyzer=analyze_wav, model=None, asr=None,
                 workers=2, max_jobs=4, response_ms=170, close_timeout_s=10.0):
        self.record, self.lookup = record_observation, lookup_observation
        self.mode, self.analyzer, self.model, self.asr = mode, analyzer, model, asr
        self.response_ms = float(response_ms)
        self.close_timeout_s = float(close_timeout_s)
        if (workers < 1 or max_jobs < workers or not 1 <= self.response_ms <= 1000
                or not math.isfinite(self.close_timeout_s) or self.close_timeout_s <= 0):
            raise ValueError("invalid voice worker/deadline configuration")
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="voice-extract")
        self.slots = threading.BoundedSemaphore(max_jobs)
        self.jobs = {}
        self._active_work = set()
        self.closed = False

    async def _execute(self, event, payload, channel, original_mode):
        current_work = None
        def track(work):
            self._active_work.add(work)
            def finished(done):
                self._active_work.discard(done)
                if not done.cancelled():
                    done.exception()
            work.add_done_callback(finished)
            return work
        try:
            # Durable replay is checked inside the same bounded admitted job, before
            # expensive extraction or a changed/unavailable model can fail a retry.
            if self.lookup:
                current_work = track(asyncio.ensure_future(self.lookup(event)))
                previous = await asyncio.shield(current_work)
                current_work = None
                if previous is not None:
                    return dict(previous, duplicate=True)
            model, asr = self.model, self.asr
            model_id = getattr(model, "artifact_id", "inprocess") if model else None
            def analyze_and_predict():
                observation = self.analyzer(payload, caller_channel=channel, asr=asr)
                verdict = model.predict(observation) if model else {
                    "decision": "OBSERVE", "score": None, "reason": "real_audio_model_not_configured",
                    "recommended_action": "NONE",
                }
                return observation, verdict

            loop = asyncio.get_running_loop()
            current_work = track(loop.run_in_executor(self.executor, analyze_and_predict))
            # A cancelled async waiter cannot stop an already-running native worker.
            observation, verdict = await asyncio.shield(current_work)
            current_work = None
            result = {
                "event_id": event["event_id"], "call_id": event["call_id"],
                "event_type": "VOICE_OBSERVATION", "ts": event["ts"], "advisory": True,
                "status": "COMPLETE", "observation": observation, **verdict,
                "policy_action": verdict.get("recommended_action", "NONE"),
                "model_id": model_id, "mode": original_mode, "original_mode": original_mode,
                "model_version": "real-voice-v1" if model else "observation-only",
                # Durable measurements are advisory raw results. Only a fresh,
                # timely delivery below may recommend an action to its caller.
                "recommended_action": "NONE", "delivery_status": "model_result_only",
            }
            if self.record:
                # Persist measurements/hash and decisions only. Raw WAV and transcript are not retained.
                persisted = {**result, "observation": {
                    k: v for k, v in observation.items() if k not in ("transcript", "segments")
                }}
                current_work = track(asyncio.ensure_future(self.record(event, persisted)))
                recorded = await asyncio.shield(current_work)
                current_work = None
                if recorded:
                    result.update({k: v for k, v in recorded.items() if k != "observation"})
            return result
        finally:
            if current_work is not None and not current_work.done():
                current_work.add_done_callback(lambda _: self.slots.release())
            else:
                self.slots.release()

    def _render(self, stored, *, duplicate, deadline):
        result = dict(stored)
        result["mode"] = self.mode()
        duplicate = bool(duplicate or result.get("duplicate"))
        expired = time.perf_counter() >= deadline
        current_model_id = getattr(self.model, "artifact_id", "inprocess") if self.model else None
        same_model = self.model is not None and result.get("model_id") == current_model_id
        original_mode = result.get("original_mode", result.get("mode"))
        fresh = not duplicate and not expired and not result.get("fail_open")
        action = result.get("policy_action", "NONE") if (
            fresh and same_model and original_mode == "enforce" and result["mode"] == "enforce"
        ) else "NONE"
        result["recommended_action"] = action if action in ("ALLOW", "FLAG", "BLOCK") else "NONE"
        result.update(duplicate=duplicate, delivery_expired=expired,
                      actionable=result["recommended_action"] != "NONE",
                      delivery_status="duplicate" if duplicate else "expired" if expired else "completed")
        return result

    async def submit(self, event, payload, channel):
        if self.closed:
            raise HTTPException(503, "voice service stopping")
        fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, allow_nan=False).encode()).hexdigest()
        key = event["event_id"]
        entry = self.jobs.get(key)
        duplicate = entry is not None
        if entry and entry[0] != fingerprint:
            raise HTTPException(409, "event_id already belongs to a different audio payload")
        retry_failed = bool(entry and entry[1].done() and (
            entry[1].cancelled() or entry[1].exception() is not None))
        if entry and not retry_failed:
            task = entry[1]
        else:
            for expired, value in list(self.jobs.items()):
                if value[1].done() and (time.perf_counter() - value[2] > 600 or len(self.jobs) >= 1024):
                    del self.jobs[expired]
            if not self.slots.acquire(blocking=False):
                raise HTTPException(429, "voice queue full; retry the same event_id later",
                                    headers={"Retry-After": "1"})
            created = entry[2] if entry else time.perf_counter()
            original_mode = entry[3] if entry else self.mode()
            task = asyncio.create_task(self._execute(event, payload, channel, original_mode))
            # Retrieve background exceptions even if a client never retries a pending job.
            task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
            entry = (fingerprint, task, created, original_mode)
            self.jobs[key] = entry
        try:
            result = await asyncio.wait_for(asyncio.shield(task), self.response_ms / 1000)
            return self._render(result, duplicate=duplicate,
                                deadline=entry[2] + self.response_ms / 1000), 200
        except asyncio.TimeoutError:
            return {"event_id": key, "call_id": event["call_id"], "status": "PROCESSING",
                    "advisory": True, "recommended_action": "NONE", "retry_after_ms": 250,
                    "duplicate": duplicate, "actionable": False}, 202
        except AudioValidationError as exc:
            raise HTTPException(400, str(exc)) from exc

    async def close(self, *, timeout_s=None):
        self.closed = True
        timeout = self.close_timeout_s if timeout_s is None else float(timeout_s)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("voice close timeout must be finite and positive")
        deadline = time.perf_counter() + timeout
        # Include native futures orphaned by cancellation of their async job.
        work = {entry[1] for entry in self.jobs.values()} | self._active_work
        if work:
            _, pending = await asyncio.wait(work, timeout=timeout)
            if pending:
                raise RuntimeError(
                    "voice shutdown deadline exceeded: unfinished work remains; "
                    "native workers were not cancelled and may still be running")
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise RuntimeError("voice shutdown deadline exceeded before executor shutdown")
        try:
            await asyncio.wait_for(asyncio.to_thread(self.executor.shutdown, wait=True), remaining)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "voice shutdown deadline exceeded: executor threads may still be running") from exc


def create_voice_router(*, submit_event=None, mode=lambda: "shadow", record_observation=None,
                        lookup_observation=None, analyzer=analyze_wav, model=None, asr=None,
                        response_ms=None, close_timeout_s=None):
    # submit_event is reserved for compatibility; raw-WAV features never enter synthetic AUDIO inference.
    # Model loading is explicit configuration, and the two domains have different schemas/artifacts.
    if model is None and os.environ.get("FRAUD_REAL_VOICE_MODEL"):
        model = RealVoiceModel.load(Path(os.environ["FRAUD_REAL_VOICE_MODEL"]))
    if asr is None and os.environ.get("FRAUD_ASR_MODEL"):
        asr = LocalWhisperASR(os.environ["FRAUD_ASR_MODEL"])
    service = VoiceService(
        record_observation=record_observation, lookup_observation=lookup_observation,
        mode=mode, analyzer=analyzer, model=model, asr=asr,
        response_ms=float(os.environ.get("FRAUD_AUDIO_RESPONSE_MS", "170")) if response_ms is None else response_ms,
        close_timeout_s=float(os.environ.get("FRAUD_AUDIO_SHUTDOWN_TIMEOUT_S", "10"))
                        if close_timeout_s is None else close_timeout_s)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await service.close()

    router = APIRouter(lifespan=lifespan)

    # HMAC cần cả body nên không kiểm được trước khi đọc. Giới hạn số upload đang đọc đồng thời và
    # loại sớm yêu cầu rõ ràng không hợp lệ (Content-Length quá lớn, mốc thời gian hết hạn) để một loạt
    # request chưa xác thực không giữ hàng trăm MB bộ nhớ (~16,8 MB mỗi request, nhân 3-4 bản sao).
    uploads = asyncio.Semaphore(int(os.environ.get("FRAUD_AUDIO_MAX_UPLOADS", "8")))

    @router.post("/api/v1/audio")
    async def receive_audio(request: Request):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_BODY_BYTES:
                    raise HTTPException(413, "audio request too large")
            except ValueError as exc:
                raise HTTPException(400, "invalid Content-Length") from exc
        stamp = request.headers.get("x-timestamp", "")
        try:
            fresh = math.isfinite(float(stamp)) and abs(time.time() - float(stamp)) <= security.NETWORK_MAX_SKEW_S
        except ValueError:
            fresh = False
        if not fresh:
            raise HTTPException(401, "network link refused: stale timestamp")
        if uploads.locked():
            raise HTTPException(503, "too many audio uploads in progress", headers={"Retry-After": "1"})
        async with uploads:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY_BYTES:
                    raise HTTPException(413, "audio request too large")
            body = bytes(raw)
            del raw
        why = security.verify_network(body, stamp, request.headers.get("x-signature", ""))
        if why:
            raise HTTPException(401, "network link refused: " + why)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "body must be JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be a JSON object")
        event, payload, channel = _metadata(data)
        result, status = await service.submit(event, payload, channel)
        return JSONResponse(result, status_code=status, headers={"Retry-After": "1"} if status == 202 else None)

    return router
