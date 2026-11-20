"""Network HMAC key caching keeps file reads out of the HTTP verification path."""

import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import security

KEY = bytes(range(32))
ROTATED_KEY = bytes(reversed(range(32)))


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "network.key"
    path.write_text(KEY.hex(), encoding="utf-8")
    monkeypatch.setattr(security, "NETWORK_KEY_FILE", path)
    monkeypatch.setattr(security, "_network_key_cache", None)
    monkeypatch.setattr(security, "_seen_sigs", OrderedDict())
    return path


def test_repeated_signed_requests_use_no_filesystem_or_lock_after_warmup(key_file, monkeypatch):
    assert security.network_key() == KEY

    def unexpected_io(*args, **kwargs):
        raise AssertionError("warm HMAC verification touched the filesystem")

    class UnexpectedLock:
        def __enter__(self):
            raise AssertionError("warm HMAC verification took the cold-load lock")

        def __exit__(self, *args):
            return False

    with monkeypatch.context() as warm:
        warm.setattr(Path, "read_text", unexpected_io)
        warm.setattr(Path, "exists", unexpected_io)
        warm.setattr(Path, "stat", unexpected_io)
        warm.setattr(security, "_network_key_lock", UnexpectedLock())
        for index in range(100):
            body, timestamp = f"request-{index}".encode(), "1000"
            signature = security.sign_network(body, timestamp)
            assert security.verify_network(body, timestamp, signature, clock=lambda: 1000) is None
        assert security.verify_network(body, timestamp, signature, clock=lambda: 1000) == "replayed request"


def test_concurrent_cold_load_reads_existing_file_once(key_file, monkeypatch):
    original_read = Path.read_text
    reached, release = threading.Event(), threading.Event()
    barrier = threading.Barrier(8)
    reads = []

    def gated_read(path, *args, **kwargs):
        if path == key_file:
            reads.append(path)
            reached.set()
            assert release.wait(5)
        return original_read(path, *args, **kwargs)

    def load():
        barrier.wait(timeout=5)
        return security.network_key()

    monkeypatch.setattr(Path, "read_text", gated_read)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(load) for _ in range(8)]
        try:
            assert reached.wait(5)
        finally:
            release.set()
        assert [future.result(timeout=5) for future in futures] == [KEY] * 8
    assert reads == [key_file]


def test_configured_path_change_does_not_reuse_previous_key(key_file, tmp_path, monkeypatch):
    assert security.network_key() == KEY
    second = tmp_path / "second.key"
    second.write_text(ROTATED_KEY.hex(), encoding="utf-8")
    monkeypatch.setattr(security, "NETWORK_KEY_FILE", second)
    assert security.network_key() == ROTATED_KEY
    monkeypatch.setattr(security, "NETWORK_KEY_FILE", key_file)
    assert security.network_key() == KEY


@pytest.mark.parametrize("encoded", ["zz" * 32, "ab" * 31, "ab" * 33, "", "ab " * 32])
def test_invalid_reload_raises_and_keeps_last_good_key(key_file, encoded):
    assert security.network_key() == KEY
    key_file.write_text(encoded, encoding="utf-8")
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        security.reload_network_key()
    assert security.network_key() == KEY
    body, timestamp = b"valid-after-failed-reload", "1000"
    signature = security.sign_network(body, timestamp, KEY)
    assert security.verify_network(body, timestamp, signature, clock=lambda: 1000) is None


def test_missing_reload_keeps_cache_without_recreating_file(key_file):
    assert security.network_key() == KEY
    key_file.unlink()
    with pytest.raises(FileNotFoundError):
        security.reload_network_key()
    assert security.network_key() == KEY
    assert not key_file.exists()


def test_explicit_rotation_accepts_new_signatures_and_rejects_old_key(key_file):
    assert security.network_key() == KEY
    key_file.write_text(ROTATED_KEY.hex(), encoding="utf-8")
    assert security.network_key() == KEY  # An edit alone is not an implicit rotation.
    assert security.reload_network_key() == ROTATED_KEY
    assert security.network_key() == ROTATED_KEY
    timestamp = "1000"
    new_body, old_body = b"new-key-request", b"old-key-request"
    assert (
        security.verify_network(
            new_body, timestamp, security.sign_network(new_body, timestamp, ROTATED_KEY), clock=lambda: 1000
        )
        is None
    )
    assert (
        security.verify_network(
            old_body, timestamp, security.sign_network(old_body, timestamp, KEY), clock=lambda: 1000
        )
        == "bad signature"
    )


def test_first_load_initializes_one_valid_random_key(tmp_path, monkeypatch):
    path = tmp_path / "private" / "network.key"
    monkeypatch.setattr(security, "NETWORK_KEY_FILE", path)
    monkeypatch.setattr(security, "_network_key_cache", None)
    key = security.network_key()
    assert len(key) == 32
    assert bytes.fromhex(path.read_text(encoding="utf-8")) == key
    assert security.network_key() == key
