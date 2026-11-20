import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import security
from test_voice_pipeline import tone, wav_bytes
from voice_api import VoiceService, create_voice_router
from voice_pipeline import analyze_wav

KEY = bytes.fromhex("f1" * 32)


def packet(event_id="W1", **extra):
    return dict(
        event_type="AUDIO",
        event_id=event_id,
        call_id="CALL-1",
        caller="84900000001",
        callee="84900000002",
        ts=10.0,
        wav_b64=base64.b64encode(wav_bytes(tone())).decode(),
        **extra,
    )


def send(client, data):
    body = json.dumps(data).encode()
    timestamp = security.network_timestamp()
    headers = {"x-timestamp": timestamp, "x-signature": security.sign_network(body, timestamp, KEY)}
    return client.post("/api/v1/audio", content=body, headers=headers)


def test_signed_wav_pipeline_never_calls_synthetic_detector_and_records_once(monkeypatch):
    monkeypatch.setattr(security, "network_key", lambda: KEY)
    recorded = []

    async def synthetic(_):
        raise AssertionError("real audio must not use synthetic feature inference")

    async def record(event, result):
        recorded.append((event, result))
        assert "wav_b64" not in event
        assert "transcript" not in result["observation"]
        return result

    app = FastAPI()
    app.include_router(create_voice_router(submit_event=synthetic, record_observation=record))
    with TestClient(app) as client:
        assert client.post("/api/v1/audio", json=packet()).status_code == 401
        response = send(client, packet())
        for _ in range(30):
            if response.status_code != 202:
                break
            time.sleep(0.02)
            response = send(client, packet())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["decision"] == "OBSERVE" and data["recommended_action"] == "NONE"
        assert data["observation"]["source"] == "real_pcm_wav"
        assert send(client, packet()).status_code == 200
        assert len(recorded) == 1
        changed = packet()
        changed["callee"] = "DIFFERENT"
        assert send(client, changed).status_code == 409
        invalid = packet(event_id="W2")
        invalid["ts"] = float("nan")
        assert send(client, invalid).status_code == 400


def test_slow_audio_returns_pending_and_bounded_queue_rejects_overload():
    async def scenario():
        calls = []

        def slow(payload, **kwargs):
            calls.append(1)
            time.sleep(0.08)
            return analyze_wav(payload, **kwargs)

        service = VoiceService(analyzer=slow, workers=1, max_jobs=1, response_ms=5)
        event = dict(
            event_id="SLOW",
            call_id="CALL",
            event_type="VOICE_OBSERVATION",
            ts=1,
            audio_sha256="a",
            caller="A",
            callee="B",
            caller_channel=None,
        )
        payload = wav_bytes(tone())
        start = time.perf_counter()
        result, status = await service.submit(event, payload, None)
        assert status == 202 and result["recommended_action"] == "NONE"
        assert time.perf_counter() - start < 0.07
        with pytest.raises(HTTPException) as error:
            await service.submit({**event, "event_id": "OTHER"}, payload, None)
        assert error.value.status_code == 429
        await asyncio.sleep(0.15)
        result, status = await service.submit(event, payload, None)
        assert status == 200 and len(calls) == 1
        await service.close()

    asyncio.run(scenario())


def test_shadow_masks_a_valid_real_audio_model_recommendation(monkeypatch):
    monkeypatch.setattr(security, "network_key", lambda: KEY)

    class Model:
        def predict(self, _):
            return {"decision": "FLAG", "score": 0.8, "reason": "fixture", "recommended_action": "FLAG"}

    app = FastAPI()
    app.include_router(create_voice_router(model=Model(), mode=lambda: "shadow"))
    with TestClient(app) as client:
        response = send(client, packet(event_id="SHADOW"))
        assert response.status_code == 200
        assert response.json()["decision"] == "FLAG"
        assert response.json()["recommended_action"] == "NONE"


def test_completed_job_uses_current_mode_on_every_retry(monkeypatch):
    monkeypatch.setattr(security, "network_key", lambda: KEY)
    current = {"mode": "enforce"}

    class Model:
        artifact_id = "fixture-artifact"

        def predict(self, _):
            return {"decision": "BLOCK", "score": 0.99, "reason": "fixture", "recommended_action": "BLOCK"}

    app = FastAPI()
    app.include_router(create_voice_router(model=Model(), mode=lambda: current["mode"]))
    with TestClient(app) as client:
        first = send(client, packet(event_id="MODE-SWITCH"))
        assert first.status_code == 200 and first.json()["recommended_action"] == "BLOCK"
        current["mode"] = "shadow"
        retried = send(client, packet(event_id="MODE-SWITCH"))
        assert retried.json()["mode"] == "shadow" and retried.json()["recommended_action"] == "NONE"


def test_durable_old_enforce_result_cannot_override_shadow_or_changed_model(monkeypatch):
    monkeypatch.setattr(security, "network_key", lambda: KEY)

    async def old_result(event, result):
        return {
            **result,
            "mode": "enforce",
            "decision": "BLOCK",
            "policy_action": "BLOCK",
            "recommended_action": "BLOCK",
            "model_id": "old-artifact",
            "duplicate": True,
        }

    class Model:
        artifact_id = "new-artifact"

        def predict(self, _):
            return {"decision": "FLAG", "score": 0.8, "reason": "fixture", "recommended_action": "FLAG"}

    for mode in ("shadow", "enforce"):
        app = FastAPI()
        app.include_router(create_voice_router(model=Model(), mode=lambda: mode, record_observation=old_result))  # noqa: B023
        with TestClient(app) as client:
            response = send(client, packet(event_id="DURABLE-" + mode))
            assert response.status_code == 200
            assert response.json()["mode"] == mode and response.json()["recommended_action"] == "NONE"
