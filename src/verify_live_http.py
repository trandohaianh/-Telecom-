"""Exercise the real HTTP stack in a fresh temporary live/shadow instance, then restart it."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from evidence import verify
from http_loadtest import parse_rates, run_http, validate_output_destination, validate_run_parameters
from precise_event_loop import clock_description, run_async
from security import network_timestamp, sign_network

ROOT = Path(__file__).resolve().parents[1]


def signed_post(client, url, path, data, key):
    body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    timestamp = network_timestamp()
    return client.post(
        url + path,
        content=body,
        headers={
            "content-type": "application/json",
            "x-timestamp": timestamp,
            "x-signature": sign_network(body, timestamp, key),
        },
    )


def await_result(client, url, path, data, key):
    for _ in range(100):
        response = signed_post(client, url, path, data, key)
        if response.status_code != 202 and not response.json().get("pending"):
            return response
        time.sleep(0.03)
    raise RuntimeError("isolated request never completed")


def start_server(directory, model_dir=None):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(
        os.environ,
        FRAUD_DEMO="0",
        FRAUD_REAL_TRAFFIC_VALIDATED="0",
        FRAUD_REQUIRE_MANIFEST="0",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(ROOT / "src"),
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMBA_NUM_THREADS="1",
        FRAUD_STATE_DB=str(directory / "state.sqlite3"),
        FRAUD_EVIDENCE_FILE=str(directory / "evidence.jsonl"),
        FRAUD_AUDIT_FILE=str(directory / "audit.jsonl"),
        FRAUD_USERS_FILE=str(directory / "users.json"),
        FRAUD_NETWORK_KEY=str(directory / "network.key"),
    )
    env["FRAUD_MODEL_DIR"] = str(model_dir or ROOT / "models")
    env["FRAUD_DATA_DIR"] = str(model_dir.parent / "data" if model_dir else ROOT / "data")
    env["FRAUD_REQUIRE_MANIFEST"] = "1" if model_dir else "0"
    env.pop("FRAUD_REAL_VOICE_MODEL", None)
    env.pop("FRAUD_ASR_MODEL", None)
    code = (
        "import sys,threading,uvicorn,security,faulthandler;"
        "faulthandler.enable();"
        "security.load_users();security.network_key();import server;"
        f"s=uvicorn.Server(uvicorn.Config('server:app',host='127.0.0.1',port={port},log_level='warning',loop='precise_event_loop:uvicorn_loop_factory'));"
        "threading.Thread(target=lambda:(sys.stdin.readline(),setattr(s,'should_exit',True)),daemon=True).start();"
        "s.run()"
    )
    log = (directory / "server.log").open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            deadline = time.monotonic() + 90
            last_probe = "no response"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("isolated server exited; inspect temporary server.log")
                try:
                    response = client.get(url + "/health/ready")
                    last_probe = f"HTTP {response.status_code}: {response.text[:120]}"
                    if response.status_code == 200:
                        return process, log, url
                except httpx.HTTPError as exc:
                    last_probe = type(exc).__name__
                time.sleep(0.1)
        raise RuntimeError(f"isolated server did not become ready: {last_probe}; log={directory / 'server.log'}")
    except Exception:
        stop_server(process, log)
        raise


def stop_server(process, log):
    shutdown_error = None
    try:
        if process.poll() is None:
            try:
                process.stdin.write("stop\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                # A child can exit between poll and flush. Report its exit code,
                # rather than masking a native crash with a broken stdin pipe.
                shutdown_error = error
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("isolated server could not drain within 30 seconds") from shutdown_error
        if process.returncode != 0:
            raise RuntimeError(
                f"isolated server exited with code {process.returncode}; inspect server.log"
            ) from shutdown_error
    finally:
        if process.stdin:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        log.close()


def verify_stack(*, rates=(100, 500), seconds=2.0, model_dir=None, concurrency=32):
    rates = tuple(rates)
    if not rates:
        raise ValueError("at least one finite positive rate is required")
    for rate in rates:
        validate_run_parameters(rate=rate, seconds=seconds, concurrency=concurrency)
    directory = Path(tempfile.mkdtemp(prefix="telecom_live_http_"))
    process, log, url = start_server(directory, model_dir)
    smoke = {}
    try:
        key = bytes.fromhex((directory / "network.key").read_text(encoding="utf-8").strip())
        with httpx.Client(timeout=3, trust_env=False) as client:
            event = {
                "event_id": "SMOKE-CALL",
                "call_id": "SMOKE-SESSION",
                "event_type": "CALL",
                "ts": time.time(),
                "caller": "84900000001",
                "callee": "84900000002",
                "cell_id": "HN-0001",
                "imei": "353911100000001",
                "origin_trunk": "LOCAL",
                "network": "4G",
                "caller_age_days": 500,
            }
            first = await_result(client, url, "/api/v1/verdict", event, key)
            assert first.status_code == 200, first.text
            assert first.json()["mode"] == "shadow" and first.json()["recommended_action"] == "NONE"
            duplicate = await_result(client, url, "/api/v1/verdict", event, key)
            assert duplicate.json()["duplicate"]
            conflict = signed_post(client, url, "/api/v1/verdict", {**event, "callee": "OTHER"}, key)
            assert conflict.status_code == 409, conflict.text
            assert client.post(url + "/api/v1/verdict", json=event).status_code == 401
            cdr = {
                "event_type": "CALL",
                "event_id": event["event_id"],
                "call_id": event["call_id"],
                "ts": event["ts"],
                "caller": event["caller"],
                "duration_s": 1.0,
            }
            assert await_result(client, url, "/api/v1/cdr", cdr, key).json()["ok"]
            assert await_result(client, url, "/api/v1/cdr", cdr, key).json()["duplicate"]
            samples = (0.15 * np.sin(2 * np.pi * 200 * np.arange(16000) / 16000) * 32767).astype("<i2")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(samples.tobytes())
            voice = {
                "event_type": "AUDIO",
                "event_id": "SMOKE-VOICE",
                "call_id": event["call_id"],
                "caller": event["caller"],
                "callee": event["callee"],
                "ts": event["ts"] + 1,
                "wav_b64": base64.b64encode(buffer.getvalue()).decode(),
            }
            audio_result = await_result(client, url, "/api/v1/audio", voice, key)
            assert audio_result.status_code == 200, audio_result.text
            assert audio_result.json()["decision"] == "OBSERVE"
            assert audio_result.json()["recommended_action"] == "NONE"
            users = json.loads((directory / "users.json").read_text(encoding="utf-8"))
            login = client.post(
                url + "/api/login", headers={"Authorization": "Bearer " + users["viettel_admin"]["token"]}
            )
            headers = {"Authorization": "Bearer " + login.json()["session"]}
            enforce = client.post(url + "/api/network/mode?mode=enforce", headers=headers)
            assert enforce.status_code == 409
            smoke = {
                "signed_setup": True,
                "duplicate_no_second_update": True,
                "conflicting_identity_409": True,
                "unsigned_401": True,
                "cdr_retry_idempotent": True,
                "real_wav_observation": True,
                "unvalidated_enforce_refused": True,
            }
        reports = [run_async(run_http(url, key, rate=rate, seconds=seconds, concurrency=concurrency)) for rate in rates]
    finally:
        stop_server(process, log)
    valid, evidence_records, bad = verify(directory / "evidence.jsonl")
    assert valid, f"evidence chain broken at {bad}"
    process, log, url = start_server(directory, model_dir)
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            restored = await_result(client, url, "/api/v1/verdict", event, key)
            assert restored.json()["duplicate"] and restored.json()["mode"] == "shadow", restored.text
            restored_audio = await_result(client, url, "/api/v1/audio", voice, key)
            assert restored_audio.json()["duplicate"], restored_audio.text
    finally:
        stop_server(process, log)
    connection = sqlite3.connect(directory / "state.sqlite3")
    counts = dict(connection.execute("SELECT kind,count(*) FROM events GROUP BY kind"))
    assert (
        connection.execute("SELECT count(*) FROM events WHERE kind='VERDICT' AND event_id='SMOKE-CALL'").fetchone()[0]
        == 1
    )
    assert (
        connection.execute("SELECT count(*) FROM events WHERE kind='CDR' AND event_id='SMOKE-CALL'").fetchone()[0] == 1
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM events WHERE kind='OBSERVATION' AND event_id='SMOKE-VOICE'"
        ).fetchone()[0]
        == 1
    )
    connection.close()
    smoke["restart_setup_and_voice_idempotent"] = True
    return {
        "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "isolation": "fresh local temporary credentials/state; user logs and secrets unchanged",
        "artifacts_dir": str(directory),
        "smoke": smoke,
        "runs": reports,
        "evidence_chain_valid": valid,
        "evidence_records_before_restart": evidence_records,
        "journal_counts": counts,
        "model_scope": "synthetic model bundle; runtime/transport verification, not real-world accuracy evidence",
        "model_version": first.json().get("model_version"),
        "event_loop": clock_description(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", default="100,500")
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_output_destination(args.out)
        rates = parse_rates(args.rates)
        for rate in rates:
            validate_run_parameters(rate=rate, seconds=args.seconds, concurrency=args.concurrency)
    except ValueError as error:
        parser.error(str(error))
    result = verify_stack(
        rates=rates,
        seconds=args.seconds,
        concurrency=args.concurrency,
        model_dir=args.model_dir.resolve() if args.model_dir else None,
    )
    content = json.dumps(result, indent=2)
    if args.out:
        with args.out.open("x", encoding="utf-8") as destination:
            destination.write(content + "\n")
    print(content)


if __name__ == "__main__":
    main()
