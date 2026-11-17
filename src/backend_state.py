"""Durable state for one local inference owner; SQLite is not a distributed feature store."""
from __future__ import annotations

from contextlib import contextmanager

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path


class EventConflict(ValueError):
    """An event identity was reused for different content."""


class TransactionAborted(RuntimeError):
    """A joined transaction failed and the whole unit of work was rolled back."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class BackendState:
    # Background WAL checkpointing (round 6). With SQLite's automatic checkpoint, the COMMIT that pushes
    # the WAL past the threshold copies the WAL into the database file and fsyncs it on the inference
    # owner thread: about 20 ms on the measured disk, every ~136 verdicts at 50 EPS, which produced the
    # periodic deadline fail-opens of the serving SLA run. Raising the threshold (tried 8 192 pages on
    # 2026-10-25) only makes the stall rarer and longer. Here the writer connection never checkpoints;
    # a separate connection runs wal_checkpoint(PASSIVE), which never blocks the writer. Every COMMIT
    # is still fsynced with synchronous=FULL, so durability is unchanged.
    BACKGROUND_CHECKPOINT_INTERVAL_S = 0.25

    def __init__(self, path=None, *, wal_autocheckpoint_pages=None, background_checkpoint=None):
        explicit_threshold = (wal_autocheckpoint_pages is not None
                              or "FRAUD_SQLITE_WAL_AUTOCHECKPOINT_PAGES" in os.environ)
        if background_checkpoint is None:
            background_checkpoint = (path is not None and not explicit_threshold
                                     and os.environ.get("FRAUD_SQLITE_BACKGROUND_CHECKPOINT", "1") != "0")
        if wal_autocheckpoint_pages is None:
            try:
                wal_autocheckpoint_pages = int(os.environ.get("FRAUD_SQLITE_WAL_AUTOCHECKPOINT_PAGES", "1000"))
            except ValueError:
                raise ValueError("WAL autocheckpoint pages must be an integer in [1, 65536]") from None
        if type(wal_autocheckpoint_pages) is not int or not 1 <= wal_autocheckpoint_pages <= 65536:
            raise ValueError("WAL autocheckpoint pages must be an integer in [1, 65536]")
        # Durability of each verdict COMMIT. FULL (default): fsync on every commit, nothing acknowledged is
        # lost even on power loss. NORMAL (opt-in, owner decision): WAL is fsynced only at checkpoints, so
        # an OS crash / power loss can drop the verdicts committed since the last checkpoint (< ~1 s with
        # the background checkpointer); a process crash loses nothing. Measured 2026-11-17 on a WSL virtual
        # disk at 100 EPS: commit p50 4.6 -> 0.17 ms, p99 11-16 -> 1-4 ms, fail-opens 56-216 -> 0-34.
        synchronous = os.environ.get("FRAUD_SQLITE_SYNCHRONOUS", "FULL").strip().upper()
        if synchronous not in ("FULL", "NORMAL"):
            raise ValueError("FRAUD_SQLITE_SYNCHRONOUS must be FULL or NORMAL")
        self.synchronous = synchronous
        raw_interval = os.environ.get("FRAUD_SQLITE_CHECKPOINT_INTERVAL_S")
        try:
            interval = self.BACKGROUND_CHECKPOINT_INTERVAL_S if raw_interval is None else float(raw_interval)
        except ValueError:
            interval = math.nan
        if not math.isfinite(interval) or not 0.05 <= interval <= 10.0:
            raise ValueError("FRAUD_SQLITE_CHECKPOINT_INTERVAL_S must be within [0.05, 10] seconds")
        self.checkpoint_interval_s = interval
        self.path = Path(path).resolve() if path is not None else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path) if self.path else ":memory:",
                                  timeout=5.0, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self._transaction_rollback_only = False
        self._pending_evidence_acks = set()
        self._transaction_evidence_acks = set()
        self._owner_file = None
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA synchronous={self.synchronous}")
        # Allow measured checkpoint tuning without changing FULL durability.
        # Preserve SQLite's usual 1000-page default. Readers can pin older
        # frames, so this threshold is not a hard WAL size cap.
        self.background_checkpoint = bool(background_checkpoint and self.path is not None)
        self.db.execute(f"PRAGMA wal_autocheckpoint={0 if self.background_checkpoint else wal_autocheckpoint_pages}")
        self.wal_autocheckpoint_pages = self.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        self.checkpoint_stats = {"passes": 0, "frames_checkpointed": 0, "busy": 0, "errors": 0, "max_wal_frames": 0}
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread = None
        if self.background_checkpoint:
            self._checkpoint_thread = threading.Thread(target=self._checkpoint_loop, name="sqlite-wal-checkpoint",
                                                       daemon=True)
            self._checkpoint_thread.start()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, event_id TEXT NOT NULL,
                digest TEXT NOT NULL, payload TEXT NOT NULL, result TEXT, received REAL NOT NULL,
                evidence_exported INTEGER NOT NULL DEFAULT 0,
                UNIQUE(kind, event_id));
            CREATE INDEX IF NOT EXISTS events_pending_evidence ON events(seq)
                WHERE result IS NOT NULL AND evidence_exported=0;
            CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suspended (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS checkpoint_delta (seq INTEGER PRIMARY KEY, value TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        """Commit a durable unit of work while excluding other connection users.

        Nested scopes join the outer transaction. Any escaping inner exception
        makes the entire transaction rollback-only, even if the caller catches
        that exception. Publish results or call external callbacks only after the
        outer context exits successfully.
        """
        with self.lock:
            outermost = self._transaction_depth == 0
            if outermost:
                self.db.execute("BEGIN IMMEDIATE")
                self._transaction_rollback_only = False
                self._transaction_evidence_acks.clear()
            self._transaction_depth += 1
            failed = False
            try:
                yield self
            except BaseException:
                failed = True
                self._transaction_rollback_only = True
                raise
            finally:
                self._transaction_depth -= 1
                if outermost:
                    rollback_only = self._transaction_rollback_only
                    self._transaction_rollback_only = False
                    committed_acks = self._transaction_evidence_acks
                    self._transaction_evidence_acks = set()
                    if rollback_only:
                        if self.db.in_transaction:
                            self.db.execute("ROLLBACK")
                        if not failed:
                            raise TransactionAborted("a nested transaction failed; all changes were rolled back")
                    else:
                        try:
                            self.db.execute("COMMIT")
                        except BaseException:
                            if self.db.in_transaction:
                                self.db.execute("ROLLBACK")
                            raise
                        self._pending_evidence_acks.difference_update(committed_acks)

    def acquire_owner(self):
        """Fail startup rather than let two model processes maintain divergent state."""
        if self.path is None or self._owner_file is not None:
            return
        fh = self.path.with_suffix(self.path.suffix + ".owner").open("a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            fh.close()
            raise RuntimeError("live state already has an inference owner; run one worker per state database") from exc
        self._owner_file = fh

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # D14: incremental checkpoints. A base snapshot lives in metadata["checkpoint"]; every later
    # periodic checkpoint appends only what changed. Recovery = base + deltas in order + journal.
    def add_checkpoint_delta(self, seq, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO checkpoint_delta(seq, value) VALUES (?,?)", (int(seq), _json(value)))

    def checkpoint_deltas(self):
        with self.lock:
            rows = self.db.execute("SELECT seq, value FROM checkpoint_delta ORDER BY seq").fetchall()
        return [(seq, json.loads(value)) for seq, value in rows]

    def clear_checkpoint_deltas(self):
        with self.lock:
            self.db.execute("DELETE FROM checkpoint_delta")

    def set(self, key, value):
        encoded = _json(value)
        with self.lock:
            # Unchanged agent/policy snapshots must not add WAL frames and fsyncs.
            self.db.execute("INSERT INTO metadata(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                            "WHERE metadata.value<>excluded.value", (key, encoded))

    def reserve(self, kind, ev):
        payload = _json(ev)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT seq,digest,result FROM events WHERE kind=? AND event_id=?",
                                  (kind, str(ev["event_id"]))).fetchone()
            if row is None:
                cursor = self.db.execute("INSERT OR IGNORE INTO events(kind,event_id,digest,payload,received) VALUES (?,?,?,?,?)",
                                         (kind, str(ev["event_id"]), digest, payload, time.time()))
                if cursor.rowcount:
                    return cursor.lastrowid, None
                row = self.db.execute("SELECT seq,digest,result FROM events WHERE kind=? AND event_id=?",
                                      (kind, str(ev["event_id"]))).fetchone()
            if row[1] != digest:
                raise EventConflict("event_id already belongs to different content for this operation")
        return row[0], json.loads(row[2]) if row[2] else None

    def event_payload(self, kind, event_id):
        """Read one journal identity without scanning, reserving or modifying it."""
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM events WHERE kind=? AND event_id=?",
                (kind, str(event_id))).fetchone()
        return json.loads(row[0]) if row is not None else None

    def lookup_completed(self, kind, event):
        """Read a completed identity without reserving or modifying journal state.

        The transaction lock excludes another thread's uncommitted owner work.
        A known identity with different canonical content conflicts even while
        its result is still pending.
        """
        digest = hashlib.sha256(_json(event).encode("utf-8")).hexdigest()
        with self.lock:
            row = self.db.execute(
                "SELECT digest,result FROM events WHERE kind=? AND event_id=?",
                (kind, str(event["event_id"]))).fetchone()
            if row is None:
                return None
            if row[0] != digest:
                raise EventConflict("event_id already belongs to different content for this operation")
            return json.loads(row[1]) if row[1] is not None else None

    def result(self, seq):
        with self.lock:
            row = self.db.execute("SELECT result FROM events WHERE seq=?", (seq,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def finish(self, seq, result):
        with self.lock:
            self.db.execute("UPDATE events SET result=? WHERE seq=? AND result IS NULL", (_json(result), seq))

    def has_completed_inference(self):
        with self.lock:
            return self.db.execute("SELECT 1 FROM events WHERE kind='VERDICT' AND result IS NOT NULL LIMIT 1").fetchone() is not None

    def journal_after(self, seq):
        with self.lock:
            rows = self.db.execute("SELECT seq,kind,payload,result FROM events WHERE seq>? ORDER BY seq", (seq,)).fetchall()
        return [(n, kind, json.loads(ev), json.loads(r) if r else None) for n, kind, ev, r in rows]

    def acknowledged_evidence_after(self, seq=0, limit=1000):
        """Read bounded pages of durable export acknowledgements for startup checks."""
        if type(limit) is not int or limit < 1:
            raise ValueError("acknowledged evidence limit must be a positive integer")
        with self.lock:
            rows = self.db.execute(
                "SELECT seq,result FROM events WHERE seq>? AND result IS NOT NULL "
                "AND evidence_exported=1 ORDER BY seq LIMIT ?", (seq, limit)).fetchall()
        return [(number, json.loads(result)) for number, result in rows]

    def pending_evidence(self, limit=200):
        if type(limit) is not int or limit < 0:
            raise ValueError("evidence limit must be a nonnegative integer")
        if limit == 0:
            return []
        with self.lock:
            pending, after = [], 0
            page_size = min(max(limit, 200), 1000)
            while len(pending) < limit:
                rows = self.db.execute(
                    "SELECT seq,result FROM events WHERE seq>? AND result IS NOT NULL "
                    "AND evidence_exported=0 ORDER BY seq LIMIT ?", (after, page_size)).fetchall()
                if not rows:
                    break
                for seq, result in rows:
                    if seq not in self._pending_evidence_acks:
                        pending.append((seq, result))
                        if len(pending) == limit:
                            break
                after = rows[-1][0]
                if len(rows) < page_size:
                    break
        # Evidence serialization can be large. Never hold the inference writer's
        # connection lock while decoding a background export snapshot.
        return [(seq, json.loads(result)) for seq, result in pending]

    def queue_evidence_ack(self, sequences):
        """Remember JSONL-fsynced records without starting a SQLite transaction.

        The single evidence writer flushes at 1000 queued acknowledgements,
        after at most one idle second, and on close. A crash before that flush
        safely replays the durable outbox against the existing JSONL references.
        """
        with self.lock:
            self._pending_evidence_acks.update(sequences)
            return len(self._pending_evidence_acks)

    def flush_evidence_acks(self):
        """Join an owner transaction, clearing RAM acknowledgements after commit.

        Standalone calls make one FULL transaction. Rollback or COMMIT failure
        retains every queued acknowledgement so the next flush can retry.
        """
        with self.lock:
            sequences = self._pending_evidence_acks - self._transaction_evidence_acks
            if not sequences:
                return 0
            with self.transaction():
                self.db.executemany("UPDATE events SET evidence_exported=1 WHERE seq=?",
                                    [(seq,) for seq in sequences])
                self._transaction_evidence_acks.update(sequences)
            return len(sequences)

    def mark_evidence(self, sequences):
        with self.transaction():
            self.db.executemany("UPDATE events SET evidence_exported=1 WHERE seq=?", [(n,) for n in sequences])

    def save_session(self, token, payload):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (digest, _json(payload)))

    def session(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT payload FROM sessions WHERE token_hash=?", (digest,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete_session(self, token):
        payload = self.session(token)
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        return payload

    def delete_sessions_for(self, name):
        """Revoke every durable operator session of one account (disable, password change)."""
        with self.lock:
            for token_hash, payload in self.db.execute("SELECT token_hash,payload FROM sessions").fetchall():
                if json.loads(payload).get("name") == name:
                    self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def suspend(self, name):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO suspended VALUES (?)", (name,))
            for token_hash, payload in self.db.execute("SELECT token_hash,payload FROM sessions").fetchall():
                if json.loads(payload)["name"] == name:
                    self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def suspended_users(self):
        with self.lock:
            return {row[0] for row in self.db.execute("SELECT name FROM suspended")}

    def _checkpoint_loop(self):
        conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA wal_autocheckpoint=0")
            while not self._checkpoint_stop.wait(self.checkpoint_interval_s):
                self.checkpoint_wal(conn)
            self.checkpoint_wal(conn)
        finally:
            conn.close()

    def checkpoint_wal(self, conn=None):
        """One PASSIVE checkpoint pass: copies committed frames, never waits for or blocks the writer."""
        try:
            busy, frames, done = (conn or self.db).execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except sqlite3.Error:
            self.checkpoint_stats["errors"] += 1
            return None
        self.checkpoint_stats["passes"] += 1
        self.checkpoint_stats["busy"] += int(bool(busy))
        self.checkpoint_stats["frames_checkpointed"] += max(0, done)
        self.checkpoint_stats["max_wal_frames"] = max(self.checkpoint_stats["max_wal_frames"], frames)
        return busy, frames, done

    def close(self):
        if self._checkpoint_thread is not None:
            self._checkpoint_stop.set()
            self._checkpoint_thread.join(timeout=10)
            self._checkpoint_thread = None
        with self.lock:
            self.db.close()
        if self._owner_file:
            self._owner_file.close()
            self._owner_file = None
