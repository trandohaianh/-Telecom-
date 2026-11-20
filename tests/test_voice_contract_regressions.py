"""Regression contracts for real-WAV delivery, cancellation and numeric safety."""
import asyncio
import io
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import security
from test_voice_api import KEY, packet, send
from test_voice_pipeline import tone, wav_bytes
from voice_api import VoiceService, _metadata, create_voice_router
from voice_model import DOMAIN, RealVoiceModel, feature_vector
from voice_pipeline import REAL_FEATURES, SCHEMA_VERSION, analyze_wav


class BlockModel:
    artifact_id = "voice-fixture"

    def predict(self, _):
        return {"decision": "BLOCK", "score": .99, "recommended_action": "BLOCK"}


def event(event_id="VOICE-REGRESSION"):
    return {"event_id": event_id, "call_id": "CALL", "ts": 1,
            "caller": "A", "callee": "B", "event_type": "VOICE_OBSERVATION",
            "audio_sha256": "fixture", "caller_channel": None,
            "schema_version": SCHEMA_VERSION}


@pytest.mark.parametrize("timestamp", [10 ** 400, -(10 ** 400), -1, float("nan"), float("inf"), True])
def test_bad_timestamp_returns_400_instead_of_server_error(timestamp):
    with pytest.raises(HTTPException) as exc:
        _metadata({**event(), "ts": timestamp, "wav_b64": "eA=="})
    assert exc.value.status_code == 400


def test_signed_huge_timestamp_is_a_client_error(monkeypatch):
    monkeypatch.setattr(security, "network_key", lambda: KEY)
    app = FastAPI()
    app.include_router(create_voice_router())
    with TestClient(app) as client:
        data = packet()
        data["ts"] = 10 ** 400
        assert send(client, data).status_code == 400


def test_metadata_tracks_the_current_acoustic_schema():
    metadata, _, _ = _metadata({**event(), "event_type": "AUDIO", "wav_b64": "eA=="})
    assert metadata["schema_version"] == SCHEMA_VERSION == "voice-acoustic-v2"


def test_slow_predict_does_not_block_loop_and_late_poll_never_arms_action():
    async def scenario():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        class SlowModel(BlockModel):
            def predict(self, observation):
                entered.set()
                release.wait(2)
                finished.set()
                return super().predict(observation)
        service = VoiceService(analyzer=lambda *a, **k: {}, model=SlowModel(),
                               mode=lambda: "enforce", workers=1, max_jobs=1, response_ms=10)
        try:
            result, status = await service.submit(event(), b"x", None)
            assert status == 202 and result["recommended_action"] == "NONE"
            assert await asyncio.to_thread(entered.wait, 1)
            # The response and event loop progress while inference is still running.
            assert not finished.is_set()
            release.set()
            await service.jobs[event()["event_id"]][1]
            result, status = await service.submit(event(), b"x", None)
            assert status == 200 and result["decision"] == "BLOCK"
            assert result["duplicate"] and result["recommended_action"] == "NONE"
            assert not result["actionable"]
        finally:
            release.set()
            await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("initial_mode", ["shadow", "enforce"])
def test_completed_retry_never_rearms_a_previous_prediction(initial_mode):
    async def scenario():
        current = {"mode": initial_mode}
        service = VoiceService(analyzer=lambda *a, **k: {}, model=BlockModel(),
                               mode=lambda: current["mode"])
        try:
            first, status = await service.submit(event(), b"x", None)
            assert status == 200
            assert first["recommended_action"] == ("BLOCK" if initial_mode == "enforce" else "NONE")
            current["mode"] = "enforce"
            repeated, status = await service.submit(event(), b"x", None)
            assert status == 200 and repeated["decision"] == "BLOCK"
            assert repeated["duplicate"] and repeated["recommended_action"] == "NONE"
        finally:
            await service.close()
    asyncio.run(scenario())


def test_mode_flip_during_inference_cannot_arm_a_shadow_observation():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        current = {"mode": "shadow"}
        def analyze(*a, **k):
            entered.set()
            release.wait(2)
            return {}
        service = VoiceService(analyzer=analyze, model=BlockModel(),
                               mode=lambda: current["mode"], response_ms=1000)
        submit = asyncio.create_task(service.submit(event(), b"x", None))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            current["mode"] = "enforce"
            release.set()
            result, status = await submit
            assert status == 200 and not result["duplicate"]
            assert result["original_mode"] == "shadow" and result["mode"] == "enforce"
            assert result["recommended_action"] == "NONE"
        finally:
            release.set()
            await service.close()
    asyncio.run(scenario())


def test_same_model_durable_replay_is_advisory_and_persistence_stays_private():
    async def scenario():
        async def record(_, result):
            assert "transcript" not in result["observation"]
            assert "segments" not in result["observation"]
            assert result["recommended_action"] == "NONE"
            return {**result, "duplicate": True, "recommended_action": "BLOCK"}
        service = VoiceService(analyzer=lambda *a, **k: {"transcript": "private", "segments": []},
                               model=BlockModel(), mode=lambda: "enforce", record_observation=record)
        try:
            result, status = await service.submit(event(), b"x", None)
            assert status == 200 and result["duplicate"]
            assert result["decision"] == "BLOCK" and result["recommended_action"] == "NONE"
        finally:
            await service.close()
    asyncio.run(scenario())


def test_transient_record_failure_can_retry_without_changing_identity_or_rearming():
    async def scenario():
        records = []
        async def record(_, result):
            records.append(result)
            if len(records) == 1:
                raise OSError("temporary persistence failure")
            return result
        service = VoiceService(analyzer=lambda *a, **k: {}, model=BlockModel(),
                               mode=lambda: "enforce", record_observation=record)
        try:
            with pytest.raises(OSError):
                await service.submit(event(), b"x", None)
            with pytest.raises(HTTPException) as conflict:
                await service.submit({**event(), "callee": "CHANGED"}, b"x", None)
            assert conflict.value.status_code == 409
            result, status = await service.submit(event(), b"x", None)
            assert status == 200 and len(records) == 2
            assert result["duplicate"] and result["recommended_action"] == "NONE"
        finally:
            await service.close()
    asyncio.run(scenario())


def test_cancelled_async_job_keeps_capacity_until_native_work_finishes():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def analyze(*a, **k):
            entered.set()
            release.wait(2)
            return {}
        service = VoiceService(analyzer=analyze, model=BlockModel(), workers=1,
                               max_jobs=1, response_ms=10)
        try:
            _, status = await service.submit(event(), b"x", None)
            assert status == 202 and await asyncio.to_thread(entered.wait, 1)
            task = service.jobs[event()["event_id"]][1]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(HTTPException) as busy:
                await service.submit(event("SECOND"), b"x", None)
            assert busy.value.status_code == 429
        finally:
            release.set()
            await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("width", [1, 2, 3, 4])
@pytest.mark.parametrize("rate", [8000, 16000, 48000])
def test_source_pcm_saturation_is_bit_depth_aware_and_survives_resampling(width, rate):
    # Half the source samples are exactly the positive extreme code, half zero.
    codes = np.tile(np.r_[np.full(rate // 400, (1 << (8 * width - 1)) - 1),
                          np.zeros(rate // 400)], 200).astype(np.int64)
    if width == 1:
        raw = (codes + 128).astype(np.uint8).tobytes()
    elif width == 3:
        raw = np.column_stack([codes & 255, (codes >> 8) & 255,
                               (codes >> 16) & 255]).astype(np.uint8).tobytes()
    else:
        raw = codes.astype("<i2" if width == 2 else "<i4").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(raw)
    observation = analyze_wav(out.getvalue())
    assert observation["features"]["clipping_ratio"] == .5
    assert "clipping" in observation["quality_warnings"]
    assert observation["clipping_measurement"] == "source_pcm_extreme_code_fraction"


class Estimator:
    classes_ = np.array([0, 1])

    def __init__(self, probabilities):
        self.probabilities = probabilities

    def predict_proba(self, _):
        return self.probabilities


def bundle(estimator):
    return {"domain": DOMAIN, "schema_version": SCHEMA_VERSION, "features": REAL_FEATURES,
            "estimator": estimator, "policies": {
                "block": {"eligible": True, "threshold": .9},
                "flag": {"eligible": True, "threshold": .5}}}


@pytest.mark.parametrize("probabilities", [np.array([[np.nan, np.nan]]),
    np.array([[np.inf, -np.inf]]), np.array([[-.1, 1.1]]), np.array([[.1, .1]]),
    np.array([.1, .9]), np.array([["bad", "value"]]), np.array([[.1j, .9]])])
def test_invalid_estimator_output_abstains_with_json_safe_score(probabilities):
    model = RealVoiceModel(bundle(Estimator(probabilities)))
    result = model.predict(analyze_wav(wav_bytes(tone())))
    assert result == {"decision": "OBSERVE", "score": None,
                      "reason": "invalid_model_output", "recommended_action": "NONE"}


def test_probability_column_is_selected_by_label_instead_of_position():
    estimator = Estimator(np.array([[.95, .05]]))
    estimator.classes_ = np.array([1, 0])
    model = RealVoiceModel(bundle(estimator))
    result = model.predict(analyze_wav(wav_bytes(tone())))
    assert result["decision"] == "BLOCK" and result["score"] == .95


@pytest.mark.parametrize("value", [10 ** 400, 1j, np.complex128(1j), np.bool_(True)])
def test_invalid_numeric_features_raise_clear_validation_error(value):
    observation = analyze_wav(wav_bytes(tone()))
    observation["features"]["rms_dbfs"] = value
    with pytest.raises(ValueError, match="invalid real-audio feature"):
        feature_vector(observation)


def test_old_clipping_schema_and_invalid_policy_are_rejected_before_serving():
    old = bundle(Estimator(np.array([[.01, .99]])))
    old["schema_version"] = "voice-acoustic-v1"
    with pytest.raises(ValueError, match="schema"):
        RealVoiceModel(old)
    invalid = bundle(Estimator(np.array([[.01, .99]])))
    invalid["policies"]["block"]["threshold"] = float("nan")
    with pytest.raises(ValueError, match="threshold"):
        RealVoiceModel(invalid)



def durable_result():
    return {"event_id": event()["event_id"], "call_id": "CALL", "ts": 1,
            "status": "COMPLETE", "decision": "BLOCK", "score": .99,
            "policy_action": "BLOCK", "recommended_action": "BLOCK", "advisory": True,
            "original_mode": "enforce", "mode": "enforce", "model_id": "voice-fixture",
            "observation": {"source": "real_pcm_wav", "schema_version": SCHEMA_VERSION,
                            "features": {"saved": 1}}, "journal_ref": "existing:7"}


def test_durable_lookup_survives_restart_with_unavailable_extractor_and_model():
    async def scenario():
        lookups = []
        def broken(*a, **k):
            raise AssertionError("completed durable observation must not be recomputed")
        class BrokenModel(BlockModel):
            predict = broken
        async def lookup(value):
            lookups.append(value["event_id"])
            return durable_result()
        service = VoiceService(lookup_observation=lookup, analyzer=broken, model=BrokenModel(),
                               mode=lambda: "enforce")
        try:
            first, status = await service.submit(event(), b"x", None)
            assert status == 200 and first["journal_ref"] == "existing:7"
            assert first["observation"]["features"] == {"saved": 1}
            assert first["decision"] == "BLOCK" and first["duplicate"]
            assert first["recommended_action"] == "NONE" and not first["actionable"]
            second, status = await service.submit(event(), b"x", None)
            assert status == 200 and second["recommended_action"] == "NONE"
            assert lookups == [event()["event_id"]]
        finally:
            await service.close()
    asyncio.run(scenario())


def test_lookup_conflict_is_409_before_any_audio_work_and_router_wires_callback(monkeypatch):
    monkeypatch.setattr(security, "network_key", lambda: KEY)
    calls = []
    async def lookup(value):
        calls.append(value["event_id"])
        raise HTTPException(409, "event_id belongs to another observation")
    def broken(*a, **k):
        raise AssertionError("conflicting event must not run extraction")
    app = FastAPI()
    app.include_router(create_voice_router(lookup_observation=lookup, analyzer=broken))
    with TestClient(app) as client:
        response = send(client, packet())
        assert response.status_code == 409 and len(calls) == 1


def test_lookup_miss_computes_and_records_exactly_once():
    async def scenario():
        calls = []
        async def lookup(_):
            calls.append("lookup")
            return None
        def analyzer(*a, **k):
            calls.append("analyze")
            return {}
        async def record(_, result):
            calls.append("record")
            return result
        service = VoiceService(lookup_observation=lookup, analyzer=analyzer,
                               record_observation=record, model=BlockModel(), mode=lambda: "enforce")
        try:
            result, status = await service.submit(event(), b"x", None)
            assert status == 200 and result["recommended_action"] == "BLOCK"
            await service.submit(event(), b"x", None)
            assert calls == ["lookup", "analyze", "record"]
        finally:
            await service.close()
    asyncio.run(scenario())


def test_slow_lookup_uses_bounded_jobs_and_same_id_poll_reuses_the_job():
    async def scenario():
        release = asyncio.Event()
        calls = []
        async def lookup(value):
            calls.append(value["event_id"])
            await release.wait()
            return durable_result()
        service = VoiceService(lookup_observation=lookup, workers=1, max_jobs=1, response_ms=5)
        try:
            assert (await service.submit(event(), b"x", None))[1] == 202
            assert (await service.submit(event(), b"x", None))[1] == 202
            with pytest.raises(HTTPException) as busy:
                await service.submit(event("ANOTHER"), b"x", None)
            assert busy.value.status_code == 429
            assert calls == [event()["event_id"]]
            release.set()
            await service.jobs[event()["event_id"]][1]
            result, status = await service.submit(event(), b"x", None)
            assert status == 200 and result["duplicate"]
            assert result["recommended_action"] == "NONE" and len(calls) == 1
        finally:
            release.set()
            await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_job", [False, True])
def test_shutdown_deadline_reports_unfinished_worker_without_cancelling_or_releasing(cancel_job):
    async def scenario():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def analyzer(*a, **k):
            entered.set()
            release.wait(2)
            finished.set()
            return {}
        service = VoiceService(analyzer=analyzer, workers=1, max_jobs=1,
                               response_ms=5, close_timeout_s=.02)
        try:
            assert (await service.submit(event(), b"x", None))[1] == 202
            assert await asyncio.to_thread(entered.wait, 1)
            task = service.jobs[event()["event_id"]][1]
            if cancel_job:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            with pytest.raises(RuntimeError, match="unfinished work.*not cancelled"):
                await asyncio.wait_for(service.close(), .5)
            assert not finished.is_set()
            assert task.cancelled() if cancel_job else not task.done()
            assert not service.slots.acquire(blocking=False)
            with pytest.raises(HTTPException) as stopping:
                await service.submit(event("SECOND"), b"x", None)
            assert stopping.value.status_code == 503
        finally:
            release.set()
            await service.close(timeout_s=1)
        assert finished.is_set() and not service._active_work
    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_shutdown_configuration_requires_a_positive_finite_timeout(timeout):
    with pytest.raises(ValueError, match="configuration"):
        VoiceService(close_timeout_s=timeout)


@pytest.mark.parametrize("now", [.02, .03])
def test_voice_delivery_deadline_uses_precise_clock_even_if_wall_clock_is_coarse(monkeypatch, now):
    from types import SimpleNamespace

    import voice_api
    model = BlockModel()
    service = VoiceService(model=model, mode=lambda: "enforce")
    # Keep this clock substitute local to the voice module; the asyncio loop is unaffected.
    monkeypatch.setattr(voice_api, "time", SimpleNamespace(
        monotonic=lambda: 0., perf_counter=lambda: now))
    stored = {"decision": "BLOCK", "policy_action": "BLOCK", "original_mode": "enforce",
              "model_id": model.artifact_id}
    try:
        result = service._render(stored, duplicate=False, deadline=.02)
        assert result["delivery_expired"] and result["recommended_action"] == "NONE"
        assert not result["actionable"]
    finally:
        asyncio.run(service.close())
