"""Prometheus metrics cho Telecom Fraud Shield.

Bộ đếm tách bạch đúng khuyến nghị audit: số quyết định đã chấm, fail-open, pending, quá tải,
từ chối nạp, khuyến nghị gửi nhà mạng theo chế độ shadow/enforce — không gộp fail-open vào ALLOW.
Nhãn ít giá trị (không có số điện thoại / user) để phù hợp giới hạn 10k series của Grafana Cloud free.
"""

from __future__ import annotations

import hmac
import os
import time
from typing import Callable

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily

HTTP_REQUESTS = Counter("tfs_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "tfs_http_request_duration_seconds",
    "HTTP latency by route template",
    ["route"],
    buckets=(0.005, 0.01, 0.02, 0.03, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
AUTH_EVENTS = Counter("tfs_auth_events_total", "Authentication events", ["actor", "event", "result"])
USAGE_UNITS = Counter("tfs_usage_units_total", "Billable external usage units", ["kind"])
USAGE_COST = Counter("tfs_usage_cost_usd_total", "Billable external usage cost (USD)", ["kind"])


class EngineCollector:
    """Đọc trạng thái engine/appdb mỗi lần scrape (60s) — không thêm chi phí vào đường chấm điểm."""

    def __init__(self, engine_getter: Callable, appdb_getter: Callable):
        self._engine = engine_getter
        self._db = appdb_getter

    def collect(self):
        engine = self._engine()
        g = lambda name, doc, labels=(): GaugeMetricFamily(name, doc, labels=list(labels))  # noqa: E731
        decisions = g("tfs_decisions", "Quyết định theo loại kết quả (luỹ kế từ khi khởi động)", ["decision"])
        by_type = g("tfs_events", "Sự kiện đã chấm theo loại", ["event_type"])
        processed = g("tfs_processed_events", "Tổng sự kiện đã chấm")
        resilience = g("tfs_resilience_events", "Sự kiện dự phòng/chịu lỗi", ["kind"])
        inference = g("tfs_inference", "Bộ đếm runtime suy luận", ["kind"])
        queue = g("tfs_inference_queue", "Hàng đợi suy luận", ["kind"])
        breaker = g("tfs_breaker_open", "Circuit breaker đang mở (1) hay đóng (0)")
        mode = g("tfs_network_enforce", "1 = enforce, 0 = shadow")
        network = g("tfs_network_verdicts", "Khuyến nghị gửi nhà mạng theo kết quả", ["decision"])
        latency = g("tfs_decision_latency_ms", "Độ trễ quyết định (cửa sổ gần nhất)", ["quantile"])
        uptime = g("tfs_uptime_seconds", "Thời gian chạy")
        unscored = g("tfs_ingest_unscored", "Sự kiện bị guard loại (LATE/DUPLICATE): trả ALLOW, không chấm", ["status"])
        if engine is not None:
            try:
                st = engine.state(mask=True)
                for k, v in st.get("decisions", {}).items():
                    decisions.add_metric([k], float(v))
                for k, v in st.get("by_type", {}).items():
                    by_type.add_metric([k], float(v))
                processed.add_metric([], float(st.get("processed", 0)))
                for k, v in (st.get("resilience") or {}).items():
                    if isinstance(v, (int, float)) and k not in ("rescore_capacity",):
                        resilience.add_metric([k], float(v))
                for k in (
                    "overloaded",
                    "admission_rejected",
                    "duplicates",
                    "http_timeouts",
                    "worker_errors",
                    "committed_events",
                    "max_commit_ms",
                ):
                    inference.add_metric([k], float((st.get("inference") or {}).get(k, 0) or 0))
                inf = st.get("inference") or {}
                queue.add_metric(["size"], float(inf.get("queue_size", 0) or 0))
                queue.add_metric(["capacity"], float(inf.get("queue_capacity", 0) or 0))
                breaker.add_metric([], 1.0 if (st.get("breaker") or {}).get("state") == "OPEN" else 0.0)
                mode.add_metric([], 1.0 if getattr(engine, "network_mode", "shadow") == "enforce" else 0.0)
                for k, v in getattr(engine, "network_stats", {}).items():
                    network.add_metric([k], float(v))
                for q, v in (st.get("latency_ms") or {}).items():
                    latency.add_metric([q], float(v))
                uptime.add_metric([], float(st.get("uptime_s", 0)))
                for k, v in (st.get("ingest_unscored") or {}).items():
                    unscored.add_metric([k], float(v))
            except Exception:
                pass
        yield from (
            decisions,
            by_type,
            processed,
            resilience,
            inference,
            queue,
            breaker,
            mode,
            network,
            latency,
            uptime,
            unscored,
        )

        subs = g("tfs_subscribers", "Tài khoản thuê bao")
        reports = g("tfs_number_reports", "Báo cáo số theo trạng thái", ["status"])
        last_ok = g("tfs_pipeline_last_success_timestamp_seconds", "Lần chạy thành công gần nhất", ["task"])
        last_status = g("tfs_pipeline_last_status", "1=ok 0.5=warn 0=fail", ["task"])
        drift = g("tfs_drift_decision_psi", "PSI phân bố quyết định so với baseline huấn luyện")
        db = self._db()
        if db is not None:
            try:
                subs.add_metric([], float(db.one("SELECT COUNT(*) n FROM subscribers")["n"]))
                for r in db.query("SELECT status, COUNT(*) n FROM number_reports GROUP BY status"):
                    reports.add_metric([r["status"]], float(r["n"]))
                for r in db.query("""SELECT job, MAX(CASE WHEN status IN ('ok','warn') THEN finished_at END) ok_at,
                                     (SELECT status FROM pipeline_runs p2 WHERE p2.job = p.job AND p2.status <> 'running'
                                      ORDER BY id DESC LIMIT 1) last
                                     FROM pipeline_runs p GROUP BY job"""):
                    if r["ok_at"]:
                        last_ok.add_metric([r["job"]], float(r["ok_at"]))
                    if r["last"]:
                        last_status.add_metric([r["job"]], {"ok": 1.0, "warn": 0.5}.get(r["last"], 0.0))
                k = db.one(
                    "SELECT drift_max_psi FROM kpi_snapshots WHERE drift_max_psi IS NOT NULL ORDER BY id DESC LIMIT 1"
                )
                if k:
                    drift.add_metric([], float(k["drift_max_psi"]))
            except Exception:
                pass
        yield from (subs, reports, last_ok, last_status, drift)


_registered = False


def register(engine_getter: Callable, appdb_getter: Callable) -> None:
    global _registered
    if not _registered:
        REGISTRY.register(EngineCollector(engine_getter, appdb_getter))
        _registered = True


def metrics_authorized(authorization: str, demo_mode: bool) -> bool:
    """METRICS_TOKEN đặt → bắt buộc Bearer/Basic; chưa đặt → chỉ mở khi chạy demo cục bộ."""
    expected = os.environ.get("METRICS_TOKEN", "").strip()
    if not expected:
        # Đúng như docstring: không có token thì chỉ mở cho demo cục bộ. Trước đây tham số demo_mode bị bỏ qua,
        # nên run_live.py (chế độ thật, không đặt FRAUD_PUBLIC) để /metrics mở cho bất kỳ ai.
        return bool(demo_mode) and os.environ.get("FRAUD_PUBLIC", "0") != "1"
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() == "bearer":
        return hmac.compare_digest(value.strip().encode("utf-8"), str(expected).encode("utf-8"))
    if scheme.lower() == "basic":
        import base64

        try:
            _, _, pw = base64.b64decode(value.strip(), validate=True).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(pw.encode("utf-8"), str(expected).encode("utf-8"))
    return False


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def route_template(request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    raw = request.url.path
    # Tệp tĩnh: gộp theo tiền tố, không để mỗi file một series.
    for prefix in ("/app/", "/admin/"):
        if raw.startswith(prefix):
            return prefix + "*"
    return "unmatched"


_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def observe(request, status: int, started: float) -> None:
    route = route_template(request)
    # Any token is a valid HTTP method on the wire: an unbounded label set would grow /metrics
    # (and memory) forever, one counter + histogram series per invented method.
    method = request.method if request.method in _METHODS else "OTHER"
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_LATENCY.labels(route=route).observe(time.perf_counter() - started)
