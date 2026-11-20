import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backend_state import BackendState


def test_repeated_metadata_write_does_not_add_database_changes(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        state.set("agent_state", {"open": {}, "proposals": []})
        before = state.db.total_changes
        state.set("agent_state", {"proposals": [], "open": {}})
        assert state.db.total_changes == before
        state.set("agent_state", {"open": {"new": "P1"}, "proposals": []})
        assert state.db.total_changes == before + 1
        assert state.get("agent_state")["open"] == {"new": "P1"}
    finally:
        state.close()


def test_slow_evidence_decoding_does_not_block_a_durable_writer(tmp_path, monkeypatch):
    state = BackendState(tmp_path / "state.sqlite3")
    decoding, release = threading.Event(), threading.Event()
    original = json.loads
    try:
        seq, _ = state.reserve("VERDICT", {"event_id": "FIRST"})
        state.finish(seq, {"event_id": "FIRST", "slow_evidence_decode": True})

        def decode(value, *args, **kwargs):
            if "slow_evidence_decode" in value:
                decoding.set()
                assert release.wait(3), "background decode held the writer lock"
            return original(value, *args, **kwargs)

        monkeypatch.setattr(json, "loads", decode)
        with ThreadPoolExecutor(max_workers=2) as pool:
            export = pool.submit(state.pending_evidence)
            assert decoding.wait(2)
            write = pool.submit(state.set, "concurrent", {"committed": True})
            try:
                write.result(timeout=1)
                assert state.get("concurrent") == {"committed": True}
            finally:
                release.set()
            assert export.result(timeout=2)[0][0] == seq
    finally:
        release.set()
        state.close()
