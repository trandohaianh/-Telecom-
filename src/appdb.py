"""CSDL ứng dụng (SQLite): tài khoản thuê bao, phiên, báo cáo số, người thân, chi phí, pipeline.

Tách khỏi `backend_state` (journal suy luận một chủ sở hữu) để dữ liệu người dùng không phụ thuộc
vào vòng đời mô hình và tồn tại qua các lần khởi động lại, kể cả ở chế độ demo.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    password_hash TEXT,
    google_sub TEXT UNIQUE,
    msisdn TEXT UNIQUE,
    msisdn_kind TEXT,                 -- demo | verified
    created_at REAL NOT NULL,
    disabled_at REAL
);
CREATE TABLE IF NOT EXISTS subscriber_sessions (
    token_hash TEXT PRIMARY KEY,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_sub ON subscriber_sessions(subscriber_id);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS otp_codes (
    phone TEXT PRIMARY KEY,
    subscriber_id INTEGER NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS number_reports (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL,
    reporter TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | confirmed | dismissed
    reviewer TEXT,
    review_note TEXT,
    created_at REAL NOT NULL,
    reviewed_at REAL,
    UNIQUE(number, reporter)
);
CREATE INDEX IF NOT EXISTS ix_reports_status ON number_reports(status, created_at);
CREATE TABLE IF NOT EXISTS guardians (
    protected TEXT NOT NULL,
    guardian TEXT NOT NULL,
    PRIMARY KEY (protected, guardian)
);
CREATE TABLE IF NOT EXISTS invites (
    protected TEXT NOT NULL,
    guardian TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (protected, guardian)
);
CREATE TABLE IF NOT EXISTS subscriber_alerts (
    id INTEGER PRIMARY KEY,
    msisdn TEXT NOT NULL,
    created_at REAL NOT NULL,
    item_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alerts_msisdn ON subscriber_alerts(msisdn, id);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,          -- sms_otp | email | simulate | voice_analysis ...
    provider TEXT,
    units REAL NOT NULL DEFAULT 1,
    cost_usd REAL NOT NULL DEFAULT 0,
    subscriber_id INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_usage_time ON usage_events(created_at);
CREATE TABLE IF NOT EXISTS infra_costs (
    id INTEGER PRIMARY KEY,
    month TEXT NOT NULL,
    provider TEXT NOT NULL,
    item TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    note TEXT,
    created_by TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kpi_snapshots (
    id INTEGER PRIMARY KEY,
    taken_at REAL NOT NULL,
    day TEXT NOT NULL,
    processed INTEGER, blocked INTEGER, flagged INTEGER, allowed INTEGER,
    fail_open INTEGER, degraded INTEGER, retro INTEGER,
    latency_p50_ms REAL, latency_p99_ms REAL,
    cpu_seconds REAL, subscribers INTEGER, reports_pending INTEGER,
    drift_max_psi REAL, drift_status TEXT
);
CREATE INDEX IF NOT EXISTS ix_kpi_day ON kpi_snapshots(day);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    detail TEXT,
    trigger TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_job ON pipeline_runs(job, id);
"""


class AppDB:
    def __init__(self, path: str | Path | None):
        self.path = str(path) if path else ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """Giao dịch ghi: BEGIN IMMEDIATE ... COMMIT (rollback khi lỗi)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def backup_to(self, dest: str | Path) -> None:
        with self._lock, sqlite3.connect(str(dest)) as target:
            self._conn.backup(target)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def now() -> float:
    return time.time()
