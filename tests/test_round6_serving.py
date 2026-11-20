"""Vòng 6: các nguồn làm owner thread dừng trong lượt đo SLA HTTP. Mỗi test thất bại trên mã cũ.

1. Checkpoint WAL tự động chạy trong COMMIT của owner (≈ 20 ms mỗi ~136 phán quyết ở 50 EPS)
   -> chuyển sang thread nền với kết nối riêng, wal_checkpoint(PASSIVE).
2. Kiểm chuỗi bằng chứng chạy trong tiến trình API, tranh GIL với owner (54 ms với ~300 bản ghi)
   -> chạy trong tiến trình con.
3. Server HTTP không đóng băng heap khởi động cho GC như load test trong tiến trình -> dùng chung gc_tuning.
"""

import gc
import json
from pathlib import Path

import pytest

from backend_state import BackendState

ROOT = Path(__file__).resolve().parents[1]


def test_writer_connection_never_checkpoints_and_background_thread_does(tmp_path, monkeypatch):
    monkeypatch.delenv("FRAUD_SQLITE_WAL_AUTOCHECKPOINT_PAGES", raising=False)
    monkeypatch.delenv("FRAUD_SQLITE_BACKGROUND_CHECKPOINT", raising=False)
    state = BackendState(tmp_path / "s.sqlite3")
    try:
        assert state.background_checkpoint and state.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
        assert state.db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL durability unchanged
        for i in range(50):
            with state.transaction():
                seq, _ = state.reserve("VERDICT", {"event_id": f"E{i}", "pad": "x" * 2000})
                state.finish(seq, {"decision": "FLAG", "pad": "y" * 2000})
        state.checkpoint_wal()
        assert state.checkpoint_stats["frames_checkpointed"] > 0
    finally:
        state.close()
    assert not state._checkpoint_thread
    reopened = BackendState(tmp_path / "s.sqlite3")
    try:
        assert reopened.result(seq) == {"decision": "FLAG", "pad": "y" * 2000}
    finally:
        reopened.close()


def test_explicit_threshold_and_memory_state_keep_inline_checkpoint(tmp_path):
    explicit = BackendState(tmp_path / "a.sqlite3", wal_autocheckpoint_pages=1000)
    memory = BackendState()
    try:
        assert not explicit.background_checkpoint and not memory.background_checkpoint
        assert explicit.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
    finally:
        explicit.close()
        memory.close()


def _chain(path, n):
    from evidence import EvidenceLog

    log = EvidenceLog(path)
    for i in range(n):
        log.submit({"event_id": f"E{i}", "decision": "FLAG"})
    log.close()


def test_evidence_chain_is_verified_in_a_child_process(tmp_path):
    from evidence import verify_in_subprocess

    path = tmp_path / "evidence.jsonl"
    _chain(path, 20)
    ok, n, bad, state = verify_in_subprocess(path, {})
    assert ok is True and n == 20 and bad is None and state["n"] == 20
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[4])
    row["record"]["decision"] = "ALLOW"  # tamper with an old line
    lines[4] = json.dumps(row, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, n, bad, _ = verify_in_subprocess(path, {})
    assert ok is False and bad == 5
    ok, *_ = verify_in_subprocess(tmp_path / "missing.jsonl", {})
    assert ok is None  # could not run: never reported as tampering


def test_server_verifies_evidence_outside_its_own_interpreter():
    src = (ROOT / "src" / "live_engine.py").read_text(encoding="utf-8")  # LiveEngine moved out of server.py
    body = src.split("def verify_evidence(self):", 1)[1].split("\n    def ", 1)[0]
    assert "verify_in_subprocess" in body and "verify(self.evidence_path)" not in body


def test_server_applies_the_same_gc_settings_as_the_in_process_benchmark(monkeypatch):
    import gc_tuning

    old = gc.get_threshold()
    try:
        monkeypatch.setenv("FRAUD_GC_TUNING", "0")
        assert gc_tuning.tune_gc_for_latency() is False
        monkeypatch.delenv("FRAUD_GC_TUNING")
        assert gc_tuning.tune_gc_for_latency() is True and gc.get_threshold() == gc_tuning.THRESHOLDS
    finally:
        gc.unfreeze()
        gc.set_threshold(*old)
    src = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
    assert "tune_gc_for_latency()" in src.split("async def lifespan", 1)[1].split("app = FastAPI", 1)[0]


def test_analyst_verify_endpoint_also_runs_out_of_process():
    src = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
    body = src.split("def api_verify(", 1)[1].split("\n@app.", 1)[0]
    assert "verify_in_subprocess" in body and "read_text" not in body


def test_journal_durability_defaults_to_full_and_normal_is_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("FRAUD_SQLITE_SYNCHRONOUS", raising=False)
    full = BackendState(tmp_path / "f.sqlite3")
    try:
        assert full.synchronous == "FULL" and full.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        full.close()
    monkeypatch.setenv("FRAUD_SQLITE_SYNCHRONOUS", "normal")
    normal = BackendState(tmp_path / "n.sqlite3")
    try:
        assert normal.synchronous == "NORMAL" and normal.db.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        normal.close()
    monkeypatch.setenv("FRAUD_SQLITE_SYNCHRONOUS", "OFF")
    with pytest.raises(ValueError, match="FULL or NORMAL"):
        BackendState(tmp_path / "x.sqlite3")
    src = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
    assert '"journal_durability"' in src.split("def health_ready", 1)[1].split("@app.", 1)[0]


def test_evidence_coalescing_and_checkpoint_interval_are_bounded_settings(monkeypatch):
    from backend_evidence import _env_seconds

    monkeypatch.delenv("X_S", raising=False)
    assert _env_seconds("X_S", 0.02, 0.0, 5.0) == 0.02
    monkeypatch.setenv("X_S", "0.2")
    assert _env_seconds("X_S", 0.02, 0.0, 5.0) == 0.2
    for bad in ("nan", "-1", "9", "abc"):
        monkeypatch.setenv("X_S", bad)
        with pytest.raises(ValueError):
            _env_seconds("X_S", 0.02, 0.0, 5.0)


def test_invalid_checkpoint_interval_fails_at_start_not_in_the_thread(tmp_path, monkeypatch):
    for bad in ("0", "nan", "abc", "60"):
        monkeypatch.setenv("FRAUD_SQLITE_CHECKPOINT_INTERVAL_S", bad)
        with pytest.raises(ValueError, match="CHECKPOINT_INTERVAL"):
            BackendState(tmp_path / "c.sqlite3")
