"""Atomic local state transactions retain SQLite WAL/FULL durability."""

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend_state import BackendState, EventConflict, TransactionAborted


def event(identity="setup-1"):
    return {"event_id": identity, "event_type": "CALL", "ts": 10.0, "caller": "84900000001"}


def test_reserve_result_and_checkpoint_commit_as_one_unit_after_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = BackendState(path)
    try:
        assert state.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert state.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        with state.transaction() as joined:
            assert joined is state
            seq, cached = state.reserve("VERDICT", event())
            assert cached is None
            state.finish(seq, {"decision": "ALLOW", "journal_seq": seq})
            state.set("checkpoint", {"last_seq": seq})
            state.save_session("token", {"name": "reviewer"})
        assert not state.db.in_transaction
    finally:
        state.close()
    reopened = BackendState(path)
    try:
        assert reopened.result(seq) == {"decision": "ALLOW", "journal_seq": seq}
        assert reopened.get("checkpoint") == {"last_seq": seq}
        assert reopened.session("token") == {"name": "reviewer"}
        assert reopened.reserve("VERDICT", event()) == (seq, reopened.result(seq))
        assert reopened.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt, SystemExit])
def test_exception_rolls_back_all_state_and_allows_next_transaction(tmp_path, failure):
    path = tmp_path / "state.sqlite3"
    state = BackendState(path)
    try:
        with pytest.raises(failure):
            with state.transaction():
                seq, _ = state.reserve("VERDICT", event())
                state.finish(seq, {"decision": "BLOCK"})
                state.set("checkpoint", {"last_seq": seq})
                state.save_session("token", {"name": "reviewer"})
                state.suspend("reviewer")
                raise failure("abort this unit")
        assert not state.db.in_transaction
        assert state.journal_after(0) == []
        assert state.get("checkpoint") is None
        assert state.session("token") is None
        assert state.suspended_users() == set()
        with state.transaction():
            state.set("next", "committed")
    finally:
        state.close()
    reopened = BackendState(path)
    try:
        assert reopened.journal_after(0) == []
        assert reopened.get("checkpoint") is None
        assert reopened.get("next") == "committed"
    finally:
        reopened.close()


def test_nested_scopes_join_outer_commit_and_mark_evidence(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    reader = sqlite3.connect(str(state.path), isolation_level=None)
    try:
        with state.transaction():
            seq, _ = state.reserve("VERDICT", event())
            with state.transaction():
                state.finish(seq, {"decision": "ALLOW"})
                with state.transaction():
                    state.mark_evidence([seq])
            # An inner exit must not commit the result, metadata or outbox flag.
            assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
            assert state.db.in_transaction
        row = reader.execute("SELECT result,evidence_exported FROM events").fetchone()
        assert row == ('{"decision":"ALLOW"}', 1)
    finally:
        reader.close()
        state.close()


def test_caught_inner_exception_marks_outer_rollback_only(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(TransactionAborted, match="nested transaction failed"):
            with state.transaction():
                state.set("before", True)
                try:
                    with state.transaction():
                        seq, _ = state.reserve("VERDICT", event())
                        state.finish(seq, {"decision": "BLOCK"})
                        raise ValueError("caller catches this")
                except ValueError:
                    pass
                state.set("after", True)
        assert not state.db.in_transaction
        assert state.get("before") is None and state.get("after") is None
        assert state.journal_after(0) == []
        with state.transaction():
            state.set("next", True)
        assert state.get("next") is True
    finally:
        state.close()


def test_nested_conflict_preserves_original_exception_and_rolls_back(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(EventConflict):
            with state.transaction():
                state.reserve("VERDICT", event())
                with state.transaction():
                    state.reserve("VERDICT", dict(event(), caller="84900000002"))
        assert state.journal_after(0) == []
        assert not state.db.in_transaction
    finally:
        state.close()


def test_mark_evidence_is_rolled_back_with_outer_failure(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        seq, _ = state.reserve("VERDICT", event())
        state.finish(seq, {"decision": "ALLOW"})
        with pytest.raises(ValueError):
            with state.transaction():
                state.mark_evidence([seq])
                raise ValueError("no acknowledgement committed")
        assert state.pending_evidence() == [(seq, {"decision": "ALLOW"})]
        state.mark_evidence([seq])
        assert state.pending_evidence() == []
    finally:
        state.close()


def test_same_connection_reader_waits_until_outer_commit(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    ready = threading.Event()
    completed = threading.Event()
    result = []
    worker = None
    try:
        with state.transaction():
            seq, _ = state.reserve("VERDICT", event())
            state.finish(seq, {"decision": "ALLOW"})

            def read_result():
                ready.set()
                result.append(state.result(seq))
                completed.set()

            worker = threading.Thread(target=read_result)
            worker.start()
            assert ready.wait(2)
            assert not completed.wait(0.05)
        assert completed.wait(2)
        assert result == [{"decision": "ALLOW"}]
    finally:
        if worker:
            worker.join(timeout=2)
        state.close()


def test_commit_failure_rolls_back_pending_work(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    # Deferred constraints make COMMIT fail, after all statements succeeded.
    try:
        state.db.execute("PRAGMA foreign_keys=ON")
        state.db.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (parent_id INTEGER REFERENCES parent(id)
                DEFERRABLE INITIALLY DEFERRED);
        """)
        with pytest.raises(sqlite3.IntegrityError):
            with state.transaction():
                state.set("pending", True)
                state.db.execute("INSERT INTO child VALUES (404)")
        assert not state.db.in_transaction
        assert state.get("pending") is None
        assert state.db.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        with state.transaction():
            state.set("committed", True)
        assert state.get("committed") is True
    finally:
        state.close()


def completed_events(state, count):
    sequences = []
    with state.transaction():
        for index in range(count):
            seq, _ = state.reserve("VERDICT", event(f"setup-{index}"))
            state.finish(seq, {"decision": "ALLOW"})
            sequences.append(seq)
    return sequences


def test_queue_evidence_ack_is_ram_only_and_skips_more_than_one_page(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        sequences = completed_events(state, 1005)
        sql = []
        state.db.set_trace_callback(sql.append)
        assert state.queue_evidence_ack(sequences[:1001]) == 1001
        assert state.queue_evidence_ack(sequences[:10]) == 1001
        assert sql == []
        assert [seq for seq, _ in state.pending_evidence(limit=3)] == sequences[1001:1004]
        assert [seq for seq, _ in state.pending_evidence(limit=1000)] == sequences[1001:]
        assert state.db.execute("SELECT SUM(evidence_exported) FROM events").fetchone()[0] == 0
    finally:
        state.close()


def test_evidence_ack_flush_joins_one_owner_commit_and_clears_after_commit(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    reader = sqlite3.connect(str(state.path), isolation_level=None)
    try:
        sequences = completed_events(state, 2)
        state.queue_evidence_ack(sequences)
        sql = []
        state.db.set_trace_callback(sql.append)
        with state.transaction():
            state.set("checkpoint", {"last_seq": sequences[-1]})
            assert state.flush_evidence_acks() == 2
            assert state.flush_evidence_acks() == 0
            assert state.queue_evidence_ack([]) == 2
            assert reader.execute("SELECT SUM(evidence_exported) FROM events").fetchone()[0] == 0
        assert state.queue_evidence_ack([]) == 0
        assert reader.execute("SELECT SUM(evidence_exported) FROM events").fetchone()[0] == 2
        assert sql.count("BEGIN IMMEDIATE") == sql.count("COMMIT") == 1
        sql.clear()
        assert state.flush_evidence_acks() == 0
        assert sql == []
    finally:
        reader.close()
        state.close()


@pytest.mark.parametrize("nested_caught", [False, True])
def test_evidence_ack_rollback_retains_ram_ack_for_retry(tmp_path, nested_caught):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        sequences = completed_events(state, 1)
        state.queue_evidence_ack(sequences)
        if nested_caught:
            with pytest.raises(TransactionAborted):
                with state.transaction():
                    try:
                        with state.transaction():
                            assert state.flush_evidence_acks() == 1
                            raise ValueError("caught")
                    except ValueError:
                        pass
        else:
            with pytest.raises(ValueError):
                with state.transaction():
                    assert state.flush_evidence_acks() == 1
                    raise ValueError("rollback")
        assert state.queue_evidence_ack([]) == 1
        assert state.db.execute("SELECT evidence_exported FROM events").fetchone()[0] == 0
        assert state.pending_evidence() == []
        assert state.flush_evidence_acks() == 1
        assert state.queue_evidence_ack([]) == 0
        assert state.db.execute("SELECT evidence_exported FROM events").fetchone()[0] == 1
    finally:
        state.close()


def test_evidence_ack_commit_failure_retains_ack_for_retry(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        sequences = completed_events(state, 1)
        state.queue_evidence_ack(sequences)
        state.db.execute("PRAGMA foreign_keys=ON")
        state.db.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (parent_id INTEGER REFERENCES parent(id)
                DEFERRABLE INITIALLY DEFERRED);
        """)
        with pytest.raises(sqlite3.IntegrityError):
            with state.transaction():
                assert state.flush_evidence_acks() == 1
                state.db.execute("INSERT INTO child VALUES (404)")
        assert state.queue_evidence_ack([]) == 1
        assert state.db.execute("SELECT evidence_exported FROM events").fetchone()[0] == 0
        assert state.flush_evidence_acks() == 1
        assert state.queue_evidence_ack([]) == 0
    finally:
        state.close()


def test_unflushed_ram_ack_is_recoverable_outbox_after_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = BackendState(path)
    sequences = completed_events(state, 1)
    state.queue_evidence_ack(sequences)
    assert state.pending_evidence() == []
    state.close()
    reopened = BackendState(path)
    try:
        assert reopened.pending_evidence() == [(sequences[0], {"decision": "ALLOW"})]
        reopened.queue_evidence_ack(sequences)
        assert reopened.flush_evidence_acks() == 1
    finally:
        reopened.close()
    reopened = BackendState(path)
    try:
        assert reopened.pending_evidence() == []
        assert reopened.queue_evidence_ack([]) == 0
    finally:
        reopened.close()


def test_evidence_poll_cost_tracks_pending_rows_after_large_exported_journal(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        sequences = completed_events(state, 2003)
        state.mark_evidence(sequences[:-3])
        progress = [0]

        def count_vm_steps():
            progress[0] += 1
            return 0

        state.db.set_progress_handler(count_vm_steps, 100)
        pending = state.pending_evidence(limit=3)
        state.db.set_progress_handler(None, 0)
        assert [seq for seq, _ in pending] == sequences[-3:]
        # A full scan over 2000 exported rows costs many thousands of VM steps.
        # The partial pending index should read only the three outstanding rows.
        assert progress[0] < 10
    finally:
        state.db.set_progress_handler(None, 0)
        state.close()


def test_completed_lookup_miss_and_pending_are_select_only_and_namespace_scoped(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        payload = event("observation-1")
        state.reserve("OBSERVATION", payload)
        before = (
            state.db.total_changes,
            state.journal_after(0),
            state.db.execute("SELECT key,value FROM metadata ORDER BY key").fetchall(),
        )
        statements = []
        state.db.set_trace_callback(statements.append)
        assert state.lookup_completed("OBSERVATION", event("missing")) is None
        assert state.lookup_completed("OBSERVATION", payload) is None
        assert state.lookup_completed("VERDICT", payload) is None
        state.db.set_trace_callback(None)
        assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        assert (
            state.db.total_changes,
            state.journal_after(0),
            state.db.execute("SELECT key,value FROM metadata ORDER BY key").fetchall(),
        ) == before
    finally:
        state.close()


def test_completed_lookup_uses_canonical_digest_returns_fresh_data_and_survives_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = BackendState(path)
    payload = {
        "event_id": "observation-1",
        "event_type": "VOICE_OBSERVATION",
        "ts": 100.0,
        "attributes": {"caller_channel": 0, "schema": "voice-acoustic-v1"},
    }
    result = {"decision": "OBSERVE", "observation": {"speech_ratio": 0.25}, "status": "COMPLETE"}
    try:
        with state.transaction():
            seq, _ = state.reserve("OBSERVATION", payload)
            state.finish(seq, result)
        reordered = {
            "attributes": {"schema": "voice-acoustic-v1", "caller_channel": 0},
            "ts": 100.0,
            "event_type": "VOICE_OBSERVATION",
            "event_id": "observation-1",
        }
        before = state.db.total_changes
        found = state.lookup_completed("OBSERVATION", reordered)
        assert found == result
        found["observation"]["speech_ratio"] = 999
        assert state.lookup_completed("OBSERVATION", payload) == result
        assert state.db.total_changes == before
    finally:
        state.close()
    reopened = BackendState(path)
    try:
        before = reopened.db.total_changes
        assert reopened.lookup_completed("OBSERVATION", payload) == result
        assert reopened.db.total_changes == before
    finally:
        reopened.close()


@pytest.mark.parametrize("completed", [False, True])
def test_completed_lookup_conflict_never_changes_pending_or_completed_ledger(tmp_path, completed):
    state = BackendState(tmp_path / "state.sqlite3")
    try:
        payload = event("observation-1")
        seq, _ = state.reserve("OBSERVATION", payload)
        if completed:
            state.finish(seq, {"decision": "OBSERVE"})
        before = (state.db.total_changes, state.journal_after(0))
        with pytest.raises(EventConflict):
            state.lookup_completed("OBSERVATION", dict(payload, caller="84900000099"))
        assert (state.db.total_changes, state.journal_after(0)) == before
    finally:
        state.close()


def test_completed_lookup_waits_for_outer_commit_before_returning_to_other_thread(tmp_path):
    state = BackendState(tmp_path / "state.sqlite3")
    started, finished = threading.Event(), threading.Event()
    found, failures = [], []
    worker = None
    try:
        payload = event("observation-1")
        with state.transaction():
            seq, _ = state.reserve("OBSERVATION", payload)
            state.finish(seq, {"decision": "OBSERVE"})

            def lookup():
                started.set()
                try:
                    found.append(state.lookup_completed("OBSERVATION", payload))
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    finished.set()

            worker = threading.Thread(target=lookup)
            worker.start()
            assert started.wait(2)
            assert not finished.wait(0.05)
        assert finished.wait(2)
        assert not failures and found == [{"decision": "OBSERVE"}]
    finally:
        if worker:
            worker.join(timeout=2)
        state.close()


def test_event_payload_is_select_only_namespace_scoped_and_returns_fresh_original(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = BackendState(path)
    payload = dict(event("shared-id"), call_id="session-1")
    try:
        state.reserve("VERDICT", payload)  # A setup need not have a completed result yet.
        state.reserve("CDR", dict(payload, duration_s=5.0))
        before = (state.db.total_changes, state.journal_after(0))
        statements = []
        state.db.set_trace_callback(statements.append)
        assert state.event_payload("VERDICT", "missing") is None
        assert state.event_payload("OBSERVATION", "shared-id") is None
        found = state.event_payload("VERDICT", "shared-id")
        assert found == payload
        found["caller"] = "modified-by-reader"
        assert state.event_payload("VERDICT", "shared-id") == payload
        assert state.event_payload("CDR", "shared-id") == dict(payload, duration_s=5.0)
        state.db.set_trace_callback(None)
        assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        assert (state.db.total_changes, state.journal_after(0)) == before
    finally:
        state.close()
    reopened = BackendState(path)
    try:
        before = reopened.db.total_changes
        assert reopened.event_payload("VERDICT", "shared-id") == payload
        assert reopened.db.total_changes == before
    finally:
        reopened.close()
