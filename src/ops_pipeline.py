"""Pipeline vận hành chạy trong tiến trình API (dữ liệu demo nằm trong bộ nhớ engine).

Job:
- kpi_snapshot (mỗi giờ): đếm quyết định, fail-open, độ trễ, CPU, thuê bao, báo cáo chờ duyệt
  + PSI phân bố quyết định so với baseline huấn luyện (drift).
- backup (hằng ngày): CSDL app, users.json (khoá ký), nhật ký audit, state SQLite (chế độ live),
  evidence log (nén), giữ N bản.
- retention (hằng ngày): dọn OTP/phiên hết hạn, sự kiện chi phí và KPI quá hạn.
Mỗi lần chạy ghi vào pipeline_runs → metric tfs_pipeline_* → cảnh báo khi trễ.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from appdb import AppDB, now

logger = logging.getLogger("fraud.pipeline")
JOBS = ("kpi_snapshot", "backup", "retention")


def _decision_psi(current: dict, baseline: dict) -> float | None:
    """PSI trên 3 nhóm ALLOW/FLAG/BLOCK; None khi chưa đủ dữ liệu."""
    total = sum(current.get(k, 0) for k in ("ALLOW", "FLAG", "BLOCK"))
    if total < 500 or not baseline:
        return None
    eps = 1e-4
    psi = 0.0
    for k in ("ALLOW", "FLAG", "BLOCK"):
        a = max(current.get(k, 0) / total, eps)
        e = max(float(baseline.get(k, 0.0)), eps)
        psi += (a - e) * math.log(a / e)
    return round(psi, 4)


_LAST_CALL_DECISIONS: dict = {}


def _call_decision_window(st: dict) -> dict:
    """CALL decisions since the previous snapshot (D7).

    Before: cumulative counts since start-up for CALL+SMS+AUDIO were compared with CALL-only reference
    rates, so the PSI mixed domains and a recent shift was diluted by all earlier traffic.
    """
    global _LAST_CALL_DECISIONS
    totals = st.get("call_decisions")
    if totals is None:   # older engine without per-type counters: keep the previous behaviour
        d = st.get("decisions", {})
        return {"ALLOW": d.get("ALLOW", 0) + d.get("FAIL_OPEN", 0), "FLAG": d.get("FLAG", 0), "BLOCK": d.get("BLOCK", 0)}
    totals = {k: int(totals.get(k, 0)) for k in ("ALLOW", "FLAG", "BLOCK")}
    previous = _LAST_CALL_DECISIONS
    if any(totals[k] < previous.get(k, 0) for k in totals):
        previous = {}    # engine restarted: counters began again from zero
    window = {k: totals[k] - previous.get(k, 0) for k in totals}
    if sum(window.values()) >= 500:
        _LAST_CALL_DECISIONS = totals   # too few calls: keep accumulating into the next snapshot
    return window


def _block_rate_pvalue(window: dict, baseline: dict) -> float | None:
    """One-sided binomial tail P(X >= observed BLOCKs | reference rate).

    The 3-band PSI is dominated by ALLOW: with a 0.03 % reference BLOCK rate it only reaches 0.10 when
    BLOCK grows ~80x. The block rate is what subscribers feel, so it gets its own exact test (M3).
    """
    n = sum(window.get(k, 0) for k in ("ALLOW", "FLAG", "BLOCK"))
    p0 = float(baseline.get("BLOCK", 0.0) or 0.0)
    if n < 500 or not 0 < p0 < 1:
        return None
    from scipy.stats import binom
    return float(binom.sf(window.get("BLOCK", 0) - 1, n, p0))


def _baseline_rates() -> dict:
    from config import MODEL_DIR

    try:
        data = json.loads((Path(MODEL_DIR) / "drift_baseline.json").read_text(encoding="utf-8"))
        return data.get("decision_rates") or {}
    except Exception:
        return {}


def kpi_snapshot(db: AppDB, engine) -> tuple[str, dict]:
    from costs import process_cpu_seconds

    if engine is None:
        return "warn", {"reason": "engine not running"}
    st = engine.state(mask=True)
    d = st.get("decisions", {})
    window, baseline = _call_decision_window(st), _baseline_rates()
    psi = _decision_psi(window, baseline)
    block_p = _block_rate_pvalue(window, baseline)
    drift_status = None if psi is None else ("alert" if psi >= 0.25 or (block_p is not None and block_p < 1e-3)
                                             else "watch" if psi >= 0.1 or (block_p is not None and block_p < 1e-2)
                                             else "stable")
    res = st.get("resilience") or {}
    row = {
        "processed": st.get("processed", 0), "blocked": d.get("BLOCK", 0), "flagged": d.get("FLAG", 0),
        "allowed": d.get("ALLOW", 0), "fail_open": d.get("FAIL_OPEN", 0),
        "degraded": int(res.get("fallback_block", 0) + res.get("fallback_flag", 0)),
        "retro": int(res.get("rescored", 0)),
        "latency_p50_ms": (st.get("latency_ms") or {}).get("p50"), "latency_p99_ms": (st.get("latency_ms") or {}).get("p99"),
        "cpu_seconds": round(process_cpu_seconds(), 2),
        "subscribers": int(db.one("SELECT COUNT(*) n FROM subscribers")["n"]),
        "reports_pending": int(db.one("SELECT COUNT(*) n FROM number_reports WHERE status='pending'")["n"]),
        "drift_max_psi": psi, "drift_status": drift_status,
    }
    with db.tx() as c:
        c.execute(f"INSERT INTO kpi_snapshots(taken_at, day, {', '.join(row)}) VALUES (?, ?, {', '.join('?' * len(row))})",
                  (now(), datetime.now(timezone.utc).date().isoformat(), *row.values()))
    status = "warn" if drift_status == "alert" else "ok"
    return status, row


def backup(db: AppDB, engine) -> tuple[str, dict]:
    from config import LOG_DIR
    import security

    keep = int(os.environ.get("FRAUD_BACKUP_KEEP", "7"))
    root = Path(os.environ.get("FRAUD_BACKUP_DIR", str(LOG_DIR / "backups")))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = root / stamp
    target.mkdir(parents=True, exist_ok=True)
    made = []
    db.backup_to(target / "app.sqlite3")
    made.append("app.sqlite3")
    for src in (Path(security.USERS_FILE), Path(security.AUDIT_FILE)):
        if src.exists():
            shutil.copy2(src, target / src.name)
            made.append(src.name)
    if engine is not None:
        ev = Path(getattr(engine, "evidence_path", ""))
        if ev.is_file():
            with ev.open("rb") as fin, gzip.open(target / (ev.name + ".gz"), "wb") as fout:
                shutil.copyfileobj(fin, fout)
            made.append(ev.name + ".gz")
        state_path = getattr(getattr(engine, "store", None), "path", None)
        if state_path and str(state_path) != ":memory:" and Path(str(state_path)).is_file():
            import sqlite3

            with sqlite3.connect(str(state_path)) as s, sqlite3.connect(str(target / "live_state.sqlite3")) as d:
                s.backup(d)
            made.append("live_state.sqlite3")
    snapshots = sorted(p for p in root.iterdir() if p.is_dir())
    for stale in snapshots[:-keep]:
        shutil.rmtree(stale, ignore_errors=True)
    size = sum(f.stat().st_size for f in target.iterdir())
    return "ok", {"dir": str(target), "files": made, "bytes": size}


def retention(db: AppDB, engine) -> tuple[str, dict]:
    t = now()
    usage_days = int(os.environ.get("FRAUD_USAGE_RETENTION_DAYS", "400"))
    kpi_days = int(os.environ.get("FRAUD_KPI_RETENTION_DAYS", "180"))
    with db.tx() as c:
        otp = c.execute("DELETE FROM otp_codes WHERE expires_at < ?", (t,)).rowcount
        sessions = c.execute("DELETE FROM subscriber_sessions WHERE last_seen < ?", (t - 30 * 86400,)).rowcount
        usage = c.execute("DELETE FROM usage_events WHERE created_at < ?", (t - usage_days * 86400,)).rowcount
        kpi = c.execute("DELETE FROM kpi_snapshots WHERE taken_at < ?", (t - kpi_days * 86400,)).rowcount
        runs = c.execute("DELETE FROM pipeline_runs WHERE started_at < ?", (t - 90 * 86400,)).rowcount
        # Giữ 200 cảnh báo gần nhất mỗi số (hộp thư app hiển thị 50).
        alerts = c.execute("""DELETE FROM subscriber_alerts WHERE id IN (
                                SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY msisdn ORDER BY id DESC) rn
                                                FROM subscriber_alerts) WHERE rn > 200)""").rowcount
    return "ok", {"otp": otp, "sessions": sessions, "usage_events": usage, "kpi_snapshots": kpi, "runs": runs,
                  "alerts": alerts}


_FUNCS = {"kpi_snapshot": kpi_snapshot, "backup": backup, "retention": retention}


def run_job(db: AppDB, engine, job: str, trigger: str = "manual") -> dict:
    with db.tx() as c:
        run_id = c.execute("INSERT INTO pipeline_runs(job, started_at, status, trigger) VALUES (?,?, 'running', ?)",
                           (job, now(), trigger)).lastrowid
    started = time.perf_counter()
    try:
        status, detail = _FUNCS[job](db, engine)
    except Exception as exc:
        logger.exception("pipeline job %s failed", job)
        status, detail = "fail", {"error": type(exc).__name__, "message": str(exc)[:300]}
    detail["duration_s"] = round(time.perf_counter() - started, 3)
    with db.tx() as c:
        c.execute("UPDATE pipeline_runs SET finished_at=?, status=?, detail=? WHERE id=?",
                  (now(), status, json.dumps(detail, ensure_ascii=False, default=str), run_id))
    return {"job": job, "status": status, "detail": detail}


def parse_daily_at(value: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in value.strip().split(":"))
    except ValueError:
        raise ValueError(f"FRAUD_PIPELINE_DAILY_AT must be HH:MM, got {value!r}") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"FRAUD_PIPELINE_DAILY_AT must be HH:MM, got {value!r}")
    return h, m


class Scheduler:
    """kpi_snapshot mỗi giờ; backup + retention lúc FRAUD_PIPELINE_DAILY_AT (UTC). Chạy bù khi khởi động."""

    def __init__(self, db: AppDB, engine_getter):
        self.db, self.engine_getter = db, engine_getter
        # Validate at start-up: a malformed value used to raise inside the thread and silently stop
        # backups and retention for good.
        self.daily_at = parse_daily_at(os.environ.get("FRAUD_PIPELINE_DAILY_AT", "02:00"))
        self.kpi_every_s = int(os.environ.get("FRAUD_KPI_EVERY_S", "3600"))
        if self.kpi_every_s < 60:
            raise ValueError("FRAUD_KPI_EVERY_S must be >= 60")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _last_ok(self, job: str) -> float | None:
        r = self.db.one("SELECT MAX(finished_at) t FROM pipeline_runs WHERE job=? AND status IN ('ok','warn')", (job,))
        return r["t"] if r else None

    def _loop(self) -> None:
        hourly = self.kpi_every_s
        at_h, at_m = self.daily_at
        # Chờ engine phát dữ liệu một chút trước lần snapshot đầu.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            t = now()
            engine = self.engine_getter()
            last_kpi = self._last_ok("kpi_snapshot")
            if last_kpi is None or t - last_kpi >= hourly:
                run_job(self.db, engine, "kpi_snapshot", "schedule")
            utc = datetime.now(timezone.utc)
            due_today = utc.replace(hour=at_h, minute=at_m, second=0, microsecond=0).timestamp()
            for job in ("backup", "retention"):
                last = self._last_ok(job)
                if last is None or (t >= due_today and last < due_today) or t - last > 26 * 3600:
                    run_job(self.db, engine, job, "schedule")
            self._stop.wait(60)

    def start(self) -> None:
        if os.environ.get("FRAUD_PIPELINE", "1") == "0" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="ops-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
