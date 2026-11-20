import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backend_state import BackendState


@pytest.mark.parametrize("pages", [1, 1000, 8192])
def test_checkpoint_threshold_keeps_full_durability_and_recovery(tmp_path, pages):
    path = tmp_path / "state.sqlite3"
    state = BackendState(path, wal_autocheckpoint_pages=pages)
    try:
        assert state.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert state.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert state.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == pages
        with state.transaction():
            seq, _ = state.reserve("VERDICT", {"event_id": "DURABLE"})
            state.finish(seq, {"decision": "FLAG"})
    finally:
        state.close()
    reopened = BackendState(path)
    try:
        assert reopened.result(seq) == {"decision": "FLAG"}
        assert reopened.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        reopened.close()


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "65537", "true"])
def test_invalid_threshold_fails_before_creating_storage(tmp_path, monkeypatch, value):
    monkeypatch.setenv("FRAUD_SQLITE_WAL_AUTOCHECKPOINT_PAGES", value)
    path = tmp_path / "uncreated" / "state.sqlite3"
    with pytest.raises(ValueError, match="WAL autocheckpoint"):
        BackendState(path)
    assert not path.parent.exists()
