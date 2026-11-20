"""Signed retries remain distinct on coarse wall clocks."""

import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import security


def test_retry_authentication_with_identical_wall_clock(monkeypatch):
    fixed_ns = time.time_ns()
    monkeypatch.setattr(security.time, "time_ns", lambda: fixed_ns)
    monkeypatch.setattr(security, "_last_network_timestamp_ns", 0)
    monkeypatch.setattr(security, "_seen_sigs", OrderedDict())
    body, key = b'{"event_id":"same-retry"}', bytes.fromhex("7c" * 32)
    stamps = [security.network_timestamp(), security.network_timestamp()]
    assert stamps[0] != stamps[1]
    for timestamp in stamps:
        signature = security.sign_network(body, timestamp, key)
        assert security.verify_network(body, timestamp, signature, key) is None
        assert security.verify_network(body, timestamp, signature, key) == "replayed request"


def test_concurrent_signed_retries_have_unique_timestamps(monkeypatch):
    fixed_ns = time.time_ns()
    monkeypatch.setattr(security.time, "time_ns", lambda: fixed_ns)
    monkeypatch.setattr(security, "_last_network_timestamp_ns", 0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        timestamps = list(pool.map(lambda _: security.network_timestamp(), range(800)))
    assert len(set(timestamps)) == 800
    assert all(abs(float(timestamp) - fixed_ns / 1e9) < 0.001 for timestamp in timestamps)
