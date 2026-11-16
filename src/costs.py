"""Theo dõi chi phí: dịch vụ ngoài tính tiền theo lượt (SMS OTP...), chi phí server nhập tay,
và chi phí tính toán quy đổi trên mỗi 1.000 quyết định.

Hệ thống chấm điểm chạy mô hình tại chỗ (LightGBM / IsolationForest / LR) nên không có phí token.
Chi phí chính là server: lấy tổng chi phí hạ tầng tháng chia cho số quyết định dự kiến trong tháng,
kèm số giây CPU thực tế của tiến trình cho mỗi 1.000 quyết định (để biết khi nào phải nâng máy).
"""
from __future__ import annotations

import calendar
import os
import resource
import time
from datetime import datetime, timezone

from appdb import AppDB, now


def record_usage(db: AppDB, kind: str, *, provider: str | None = None, units: float = 1.0,
                 cost_usd: float = 0.0, subscriber_id: int | None = None, detail: str | None = None) -> None:
    try:
        with db.tx() as c:
            c.execute("INSERT INTO usage_events(created_at, kind, provider, units, cost_usd, subscriber_id, detail)"
                      " VALUES (?,?,?,?,?,?,?)", (now(), kind, provider, units, cost_usd, subscriber_id, detail))
    except Exception:  # chi phí là best-effort, không làm hỏng luồng chính
        pass
    try:
        from observability import USAGE_COST, USAGE_UNITS

        USAGE_UNITS.labels(kind=kind).inc(units)
        if cost_usd:
            USAGE_COST.labels(kind=kind).inc(cost_usd)
    except Exception:
        pass


def process_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def process_rss_mb() -> float:
    # ru_maxrss: KB trên Linux
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def summary(db: AppDB, *, processed: int, uptime_s: float, days: int = 30, cpu_baseline_s: float = 0.0) -> dict:
    since = now() - days * 86400
    by_kind = [dict(r) for r in db.query(
        "SELECT kind, provider, COUNT(*) AS events, SUM(units) AS units, SUM(cost_usd) AS cost_usd "
        "FROM usage_events WHERE created_at >= ? GROUP BY kind, provider ORDER BY cost_usd DESC", (since,))]
    by_day = [dict(r) for r in db.query(
        "SELECT date(created_at, 'unixepoch') AS day, SUM(cost_usd) AS cost_usd, COUNT(*) AS events "
        "FROM usage_events WHERE created_at >= ? GROUP BY day ORDER BY day", (since,))]
    usage_total = sum(r["cost_usd"] or 0 for r in by_kind)

    today = datetime.now(timezone.utc)
    month = today.strftime("%Y-%m")
    infra = [dict(r) for r in db.query("SELECT * FROM infra_costs WHERE month=? ORDER BY id", (month,))]
    infra_month = sum(r["amount_usd"] for r in infra)
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    # Trừ CPU lúc khởi động (nạp mô hình, warm-up) để chỉ tính phần dùng cho việc chấm điểm.
    cpu = max(0.0, process_cpu_seconds() - cpu_baseline_s)
    eps = processed / uptime_s if uptime_s > 0 else 0.0
    projected_month_decisions = eps * 86400 * days_in_month
    cost_per_1k = (infra_month / projected_month_decisions * 1000) if projected_month_decisions else None
    return {
        "period_days": days,
        "external_usage": {"total_usd": round(usage_total, 6), "by_kind": by_kind, "by_day": by_day},
        "infra": {"month": month, "total_usd": round(infra_month, 4), "items": infra},
        "compute": {
            "processed_decisions": processed,
            "uptime_s": round(uptime_s, 1),
            "decisions_per_s": round(eps, 2),
            "cpu_seconds": round(cpu, 2),
            "cpu_ms_per_1k_decisions": round(cpu / processed * 1000 * 1000, 2) if processed else None,
            "rss_mb_peak": round(process_rss_mb(), 1),
            "projected_month_decisions": int(projected_month_decisions),
            "infra_cost_per_1k_decisions_usd": round(cost_per_1k, 8) if cost_per_1k is not None else None,
        },
        "total_month_estimate_usd": round(infra_month + usage_total * (30 / max(days, 1)), 4),
        "note": "Mô hình chạy tại chỗ: không có phí token. Chi phí theo lượt chỉ phát sinh khi dùng dịch vụ ngoài (SMS OTP).",
        "generated_at": time.time(),
        "sms_cost_usd_setting": float(os.environ.get("SMS_COST_USD", "0.08")),
    }
