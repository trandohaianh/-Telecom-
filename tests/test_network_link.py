"""Operator integration link: signed, replay-proof, advisory only. Run: python -m pytest -q tests"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import security  # noqa: E402

KEY = bytes.fromhex("ab" * 32)
CALL = {"event_type": "CALL", "event_id": "N1", "ts": 0.0, "caller": "84961234567", "callee": "84351234567",
        "cell_id": "HN-0001", "imei": "35391110000001", "origin_trunk": "LOCAL", "network": "4G"}


def signed(body: bytes, ts=None, key=KEY):
    ts = str(ts) if ts is not None else security.network_timestamp()
    return {"x-timestamp": ts, "x-signature": security.sign_network(body, ts, key), "content-type": "application/json"}


def test_valid_signature_is_accepted_once():
    body = json.dumps(CALL).encode()
    h = signed(body)
    assert security.verify_network(body, h["x-timestamp"], h["x-signature"], KEY) is None
    assert security.verify_network(body, h["x-timestamp"], h["x-signature"], KEY) == "replayed request"


def test_tampered_body_stale_time_and_wrong_key_are_refused():
    body = json.dumps(CALL).encode()
    h = signed(body)
    assert security.verify_network(body.replace(b"LOCAL", b"BANK_TRUNK"), h["x-timestamp"], h["x-signature"], KEY) == "bad signature"
    old = signed(body, ts=time.time() - 120)
    assert security.verify_network(body, old["x-timestamp"], old["x-signature"], KEY) == "stale timestamp"
    other = signed(body, key=bytes.fromhex("cd" * 32))
    assert security.verify_network(body, other["x-timestamp"], other["x-signature"], KEY) == "bad signature"


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr(security, "network_key", lambda: KEY)
    return TestClient(server.app)


def test_endpoint_refuses_unsigned_or_invalid_requests(client):
    body = json.dumps(CALL).encode()
    assert client.post("/api/v1/verdict", content=body).status_code == 401
    bad = json.dumps({**CALL, "event_type": "FAX"}).encode()
    assert client.post("/api/v1/verdict", content=bad, headers=signed(bad)).status_code == 400
    partial = json.dumps({k: v for k, v in CALL.items() if k != "imei"}).encode()
    r = client.post("/api/v1/verdict", content=partial, headers=signed(partial))
    assert r.status_code == 400 and "imei" in r.json()["detail"]
