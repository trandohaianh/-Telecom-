"""FastAPI service + live dashboard for the Telecom Fraud Detection MVP.

A background thread replays the independent test stream (after priming the feature store with the
24 h warm-up) through the guarded two-tier detector, writes evidence, and keeps live counters.
Attack injection and chaos (latency / failure) endpoints drive the demo.

Run:  python run_demo.py   ->  http://localhost:8080
"""

# Docstring trên mô tả thread replay dữ liệu, detector có bảo vệ và các API demo.
# Bật cú pháp annotation hiện đại mà không cần đánh giá một số kiểu ngay khi import module.
from __future__ import annotations

# collections cung cấp bộ đếm và hàng đợi có giới hạn cho trạng thái dashboard.
import asyncio
import collections

# contextlib tạo lifespan (khởi động / tắt) cho FastAPI.
import contextlib

# gc dùng để dọn rác và đóng băng các đối tượng dùng chung trước khi stream chạy.
# json đọc các file số liệu đã lưu và giải mã bản ghi bằng chứng.
import json
import os

# queue chuyển các sự kiện thử tấn công từ request API sang thread xử lý.
import re

# secrets tạo token phiên cho app thuê bao.
import secrets

# threading chạy luồng giả lập song song với máy chủ web.
import threading

# time đo thời gian, tạo mốc sự kiện và hiển thị thời gian trên cảnh báo.
import time

# Future cho phép request chờ thread nền xử lý xong sự kiện được tiêm.
# NumPy tính phân vị độ trễ từ mẫu được giữ trong bộ đệm.
import numpy as np

# FastAPI định nghĩa ứng dụng và lỗi HTTP; JSONResponse trả JSON trực tiếp.
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Phân quyền theo vai trò, che số điện thoại cho người xem và nhật ký thao tác có chữ ký.
import accounts
import http_hardening

# Tài khoản thuê bao, quản trị, chi phí, pipeline vận hành và /metrics.
import portal
import security

# Kiểm tra sự kiện mạng và xung đột nội dung trong journal.
from backend_runtime import REQUIRED, validate_event
from backend_state import EventConflict

# Nạp cấu hình đường dẫn của hệ thống.
from config import DATA_DIR, MODEL_DIR, STATIC_DIR

# PROTECTED_CLI là số tổng đài chính thức.
from data_generator import PROTECTED_CLI

# verify kiểm tra tính toàn vẹn của chuỗi bằng chứng (ghi qua DurableEvidenceLog).
from evidence import verify_in_subprocess

# LiveEngine và các thiết lập của nó nằm ở live_engine.py; tên được nhập lại ở đây cho route và test.
from live_engine import DEMO_RATE, EVIDENCE_PATH, LiveEngine, _demo_evidence_cap  # noqa: F401

# Chế độ demo, nhật ký thao tác, che số và phân quyền.
from security import DEMO_MODE, AuditLog, mask_number, require, verify_audit

PROTECTED_NUMBERS = set(PROTECTED_CLI)


# Engine chỉ được tạo khi app bắt đầu chạy (lifespan), không phải lúc import (để test không nạp mô hình).
engine: LiveEngine | None = None


@contextlib.asynccontextmanager
# Nạp dữ liệu, khởi tạo detector và bắt đầu thread phát lại khi server khởi động.
async def lifespan(_app):
    global engine
    engine = await asyncio.to_thread(lambda: LiveEngine(audit_log=audit))
    portal.startup(lambda: engine)
    from gc_tuning import tune_gc_for_latency

    engine.gc_tuned = tune_gc_for_latency()  # same GC settings the in-process SLA benchmark always used
    try:
        yield
    finally:
        portal.shutdown()
        await asyncio.to_thread(engine.close)
        engine = None


# Tạo ứng dụng web FastAPI; các route bên dưới thuộc ứng dụng này.
app = FastAPI(title="Telecom Real-Time AI Fraud Detection (v2)", lifespan=lifespan)
app.include_router(portal.router)


@app.middleware("http")
async def _http_metrics(request: Request, call_next):
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        # Clickjacking, MIME sniffing, referrer leaks; HSTS when served over https (http_hardening.py).
        http_hardening.apply_security_headers(response.headers, request.url.scheme, request.url.path)
        return response
    finally:
        if request.url.path != "/metrics":
            portal.observability.observe(request, status, started)


# Outermost user middleware: refuse oversized bodies before any route buffers them.
app.add_middleware(http_hardening.BodyLimitMiddleware)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Starlette's error middleware runs outside @app.middleware: without this a 500 had no security
    # headers. The body never contains the exception text (no stack traces or internals leak).
    response = JSONResponse({"detail": "Internal server error"}, status_code=500)
    http_hardening.apply_security_headers(response.headers, request.url.scheme)
    return response


@app.get("/health/live")
def health_live():
    return {"status": "live"}


@app.get("/health/ready")
def health_ready():
    if engine is None:
        raise HTTPException(503, "inference owner unavailable")
    progress = engine.runtime.progress()
    if not progress["ready"]:
        raise HTTPException(503, {"message": "inference owner unavailable", "progress": progress})
    return {
        "status": "ready",
        "progress": progress,
        "mode": engine.network_mode,
        "demo": engine.demo_mode,
        "model_version": engine.runtime.model_version,
        "model_identity": engine.runtime.model_identity,
        "model_manifest": getattr(engine.g.det, "model_manifest", {"status": "legacy_unverified"}),
        # NORMAL is an opt-in durability trade-off (FRAUD_SQLITE_SYNCHRONOUS); operators must see it.
        "journal_durability": getattr(engine.runtime.state, "synchronous", "FULL"),
    }


@app.get("/")
# Trang chủ công khai là app thuê bao; bảng vận hành nằm riêng ở /admin/.
def index():
    return RedirectResponse("/app/")


@app.get("/admin")
def admin_redirect():
    return RedirectResponse("/admin/")


@app.get("/admin/")
# Bảng điều khiển vận hành (giám sát luồng, bằng chứng, AI agent).
def admin_console():
    return FileResponse(STATIC_DIR / "admin" / "index.html")


@app.get("/admin/manage")
# Quản trị: tài khoản nhân viên, duyệt báo cáo số, chi phí, pipeline.
def admin_manage():
    return FileResponse(STATIC_DIR / "admin" / "manage.html")


# Nhật ký thao tác: mọi hành động đặc quyền được ghi kèm người thực hiện và chữ ký của chính người đó.
audit = AuditLog()


# demo_only chặn các endpoint chỉ dành cho trình diễn (tiêm tấn công, gây lỗi) khi chạy thật.
def demo_only():
    if not DEMO_MODE:
        raise HTTPException(403, "demo endpoints are disabled (set FRAUD_DEMO=1 to enable)")


# Đăng nhập app bằng số demo KHÔNG cần tài khoản: chỉ cho chạy cục bộ (run_demo.py). Bản công khai tắt
# (FRAUD_LEGACY_DEMO_LOGIN=0) để mọi người dùng đi qua tài khoản thật.
def legacy_demo_login():
    demo_only()
    if os.environ.get("FRAUD_LEGACY_DEMO_LOGIN", "1") != "1":
        raise HTTPException(403, "anonymous demo sign-in is disabled; create an account at /app/")


@app.get("/app")
# App thuê bao (PWA) nằm ở /app/; chuyển hướng để đường dẫn tương đối của manifest/service worker đúng.
def app_redirect():
    return RedirectResponse("/app/")


@app.post("/api/login")
# Đổi thông tin đăng nhập cá nhân lấy một phiên có hạn (8 giờ, hoặc 30 phút không thao tác). Ghi nhật ký.
async def api_login(request: Request, authorization: str = Header(default="")):
    # Hai cách: JSON {username, password} (nhân viên) hoặc Bearer <token cá nhân> (tự động hoá / bản cũ).
    out = None
    if "json" in request.headers.get("content-type", ""):
        try:
            body = portal.OperatorLogin(**(await request.json()))
        except Exception:
            raise HTTPException(422, "username and password required") from None
        out = portal.operator_password_login(body, request)
    else:
        out = security.login(security.bearer(authorization))
    if out is None:
        raise HTTPException(401, "invalid credential")
    sid, u, expires = out
    if u["name"] in security.suspended:
        security.logout(sid)
        raise HTTPException(403, "account suspended by an administrator")
    audit.record(u["name"], "login")
    return {"session": sid, "expires_at": expires, "idle_timeout_s": security.SESSION_IDLE_S, **u}


@app.post("/api/logout")
def api_logout(authorization: str = Header(default="")):
    s = security.logout(security.bearer(authorization))
    if s:
        audit.record(s["name"], "logout")
    return {"ok": True}


@app.get("/api/whoami")
# Cho dashboard biết người đang đăng nhập và vai trò để ẩn/hiện các nút tương ứng.
def api_whoami(u=Depends(require("viewer"))):
    return {**u, "demo_mode": DEMO_MODE}


@app.get("/api/state")
# Trả các bộ đếm và cảnh báo mới nhất. Số điện thoại và IMEI luôn bị che với MỌI vai trò (tối thiểu hóa dữ liệu):
# ai cần số đầy đủ phải mở từng số qua /api/reveal, ghi lý do và để lại dấu vết trong nhật ký.
def api_state(u=Depends(require("viewer"))):
    return engine.state(mask=True)


@app.post("/api/reveal")
# Mở số đầy đủ của MỘT cảnh báo: chỉ chuyên viên trở lên, bắt buộc ghi lý do, ghi nhật ký có chữ ký,
# và AI agent theo dõi số lần mở để phát hiện rò rỉ từ bên trong.
def api_reveal(
    event_id: str | None = None,
    reason: str | None = None,
    body: dict | None = Body(default=None),
    u=Depends(require("analyst")),
):
    # Lý do (thường có thông tin cá nhân) nên đi trong body: query string bị ghi vào log proxy/truy cập.
    # Query vẫn được nhận để không làm hỏng client cũ.
    body = body if isinstance(body, dict) else {}
    event_id = str(body.get("event_id") or event_id or "")
    reason = str(body.get("reason") or reason or "")
    if not event_id:
        raise HTTPException(422, "event_id is required")
    if len(reason.strip()) < 5:
        raise HTTPException(400, "a reason (at least 5 characters) is required to reveal personal data")
    with engine.lock:
        a = next((x for x in engine.alerts if x["event_id"] == event_id), None)
    if a is None:
        raise HTTPException(404, "alert no longer in the live window")
    audit.record(u["name"], "reveal_pii", event_id=event_id, reason=reason.strip()[:200])
    engine.agent.note_reveal(u["name"])
    return {
        "event_id": event_id,
        "party": a.get("party"),
        "counterparty": a.get("counterparty"),
        "imei": a.get("imei"),
        "note": "Lần mở này đã được ghi vào nhật ký thao tác kèm lý do.",
    }


@app.get("/api/agent")
# Đề xuất của AI agent: mọi nhân viên có tài khoản đều xem được (số đã che); chỉ admin mới quyết định.
def api_agent(u=Depends(require("viewer"))):
    return {"proposals": engine.agent.list(), "can_decide": u["role"] == "admin"}


@app.post("/api/agent/{pid}/decide")
# Con người quyết định: duyệt thì hành động mới chạy, từ chối thì không có gì thay đổi. Cả hai đều ghi nhật ký có chữ ký.
def api_agent_decide(
    pid: str,
    decision: str | None = None,
    note: str = "",
    body: dict | None = Body(default=None),
    u=Depends(require("admin")),
):
    body = body if isinstance(body, dict) else {}
    decision = str(body.get("decision") or decision or "")
    note = str(body.get("note") or note or "")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve | reject")
    try:
        return engine.agent.decide(pid, u["name"], decision == "approve", note.strip()[:300])
    except KeyError:
        raise HTTPException(404, "unknown proposal") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/metrics")
# Đọc các báo cáo kết quả đã lưu và trả về một gói metric thống nhất.
def api_metrics(u=Depends(require("viewer"))):
    # Đọc JSON nếu file tồn tại; nếu chưa sinh báo cáo thì trả null cho mục đó.
    def rd(p):
        return json.loads(p.read_text()) if p.exists() else None

    return {
        "benchmark": rd(DATA_DIR / "benchmark_results.json"),
        "loadtest": rd(DATA_DIR / "loadtest_results.json"),
        "training": rd(MODEL_DIR / "training_report.json"),
        "dataset": rd(DATA_DIR / "dataset_summary.json"),
    }


@app.post("/api/inject")
# Nhận loại tấn công từ query string, sinh sự kiện rồi trả các kết quả chấm điểm.
def api_inject(type: str, u=Depends(require("admin")), _=Depends(demo_only)):
    audit.record(u["name"], "inject_attack", type=type)
    return {"attack": type, "results": engine.inject(type)}


@app.post("/api/chaos")
# Bật/tắt tình huống gây trễ hoặc lỗi để kiểm thử circuit breaker và fail-open (chỉ admin, chỉ chế độ demo).
def api_chaos(mode: str, u=Depends(require("admin")), _=Depends(demo_only)):
    if mode not in ("latency", "error", "off"):
        raise HTTPException(400, "mode must be latency | error | off")
    audit.record(u["name"], "fault_drill", mode=mode)
    # Lấy detector có bảo vệ dùng chung của dashboard.
    g = engine.g
    # Mỗi mode thay đổi độ trễ giả lập hoặc bật cờ lỗi của detector.
    if mode == "latency":
        g.chaos_delay_ms, g.chaos_error = 60.0, False
    elif mode == "error":
        g.chaos_delay_ms, g.chaos_error = 0.0, True
    elif mode == "off":
        g.chaos_delay_ms, g.chaos_error = 0.0, False
    else:
        # Báo lỗi 400 cho mode ngoài tập được hỗ trợ.
        raise HTTPException(400, "mode must be latency | error | off")
    # Xác nhận mode hiện tại cùng trạng thái breaker.
    return {"chaos": mode, "breaker": g.cb.state}


@app.get("/api/evidence/verify")
# Kiểm tra tính liên tục của chuỗi hash đã ghi trong phiên hiện tại.
def api_verify(u=Depends(require("analyst"))):
    audit.record(u["name"], "verify_evidence_chain")
    # Chờ một nhịp để logger flush các bản ghi đang đợi.
    time.sleep(0.2)
    # Kiểm trong tiến trình con (vòng 6): băm lại cả chuỗi không được tranh GIL với owner chấm điểm.
    if not engine.evidence_path.exists():
        return {"chain_valid": True, "records": 0, "first_bad_line": None, "last_hash": ""}
    ok, n, bad, state = verify_in_subprocess(engine.evidence_path, {})
    if ok is None:
        raise HTTPException(503, "Không chạy được bước kiểm chuỗi bằng chứng, thử lại sau")
    last = state.get("prev", "") if ok and n else ""
    return {"chain_valid": ok, "records": n, "first_bad_line": bad, "last_hash": last}


def _tail_evidence(path, n, chunk=1 << 16):
    """Last n complete JSONL rows, reading backwards from the end (was: the whole file on every call,
    and a 500 when the writer was midway through the last line)."""
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        data = b""
        while end > 0 and data.count(b"\n") <= n:
            start = max(0, end - chunk)
            fh.seek(start)
            data = fh.read(end - start) + data
            end = start
    rows = []
    for line in data.split(b"\n")[::-1]:
        if len(rows) == n:
            break
        try:
            rows.append(json.loads(line))
        except ValueError:  # empty, torn (being written) or the partial first line of the chunk
            continue
    return rows[::-1]


@app.get("/api/evidence/latest")
# Trả về tối đa n dòng log cuối cùng để client có thể xem nhanh các bằng chứng mới.
def api_latest(n: int = 5, u=Depends(require("analyst"))):
    # Không cho tải hàng loạt: tối đa 50 bản ghi mỗi lần, số điện thoại đã che.
    n = max(1, min(n, 50))
    audit.record(u["name"], "read_evidence", n=n)
    rows = _tail_evidence(engine.evidence_path, n)
    for row in rows:
        rec = row["record"]
        rec["party"], rec["counterparty"] = mask_number(rec.get("party")), mask_number(rec.get("counterparty"))
    return JSONResponse(rows)


@app.get("/api/audit")
# Nhật ký thao tác: ai đã làm gì, kèm kết quả kiểm tra chuỗi hash và chữ ký của từng dòng (chỉ admin).
def api_audit(
    n: int = Query(default=20, ge=1, le=500),
    anchor_records: int | None = Query(default=None, ge=1),
    anchor_hash: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    u=Depends(require("admin")),
):
    # verify.head nên được chép định kỳ ra hệ thống ngoài; gửi lại qua anchor_* để phát hiện xoá đuôi log.
    if (anchor_records is None) != (anchor_hash is None):
        raise HTTPException(422, "anchor_records and anchor_hash must be given together")
    anchor = None if anchor_records is None else {"records": anchor_records, "hash": anchor_hash}
    return {"verify": verify_audit(anchor=anchor), "entries": audit.tail(n)}


# ====================================================================== network link (đứng song song với nhà mạng)
# Thiết bị mạng của nhà mạng gửi BẢN SAO sự kiện (không đi qua hệ thống này), nhận lại khuyến nghị và TỰ quyết định
# có thực thi hay không. Không có trả lời trong ngân sách thì cuộc gọi cứ đi tiếp: hệ thống này sập không làm rớt cuộc gọi nào.
NETWORK_REQUIRED = REQUIRED
MAX_NETWORK_BODY = 65536


async def network_request(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_NETWORK_BODY:
            raise HTTPException(413, "event body is too large")
    body = bytes(body)
    why = security.verify_network(body, request.headers.get("x-timestamp", ""), request.headers.get("x-signature", ""))
    if why:
        raise HTTPException(401, f"network link refused: {why}")
    try:
        ev = validate_event(json.loads(body), "CDR" if request.url.path.endswith("/cdr") else "VERDICT")
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(400, str(exc)) from exc
    # Yêu cầu đã ký trong ±30 s đồng hồ thật, nên mốc sự kiện kiểu Unix không thể ở xa trong tương lai.
    # Một mốc như vậy sẽ đẩy watermark của IngestGuard và làm mọi sự kiện thật sau đó thành LATE (không chấm).
    # Mốc < 1e9 là đồng hồ giả lập (demo/kiểm thử bắt đầu từ 0) nên không so với đồng hồ thật.
    if ev["ts"] >= UNIX_TS_FLOOR and ev["ts"] > time.time() + NETWORK_MAX_FUTURE_S:
        raise HTTPException(400, f"ts is more than {NETWORK_MAX_FUTURE_S:.0f} s in the future")
    return ev


UNIX_TS_FLOOR = 1e9
# Must stay below IngestGuard.allowed_lateness_s (120 s): one peer clocked 130-300 s fast would
# otherwise lift the global floor above every correctly clocked trunk and make their events LATE.
NETWORK_MAX_FUTURE_S = 60.0


@app.post("/api/v1/verdict")
async def network_verdict(request: Request):
    ev = await network_request(request)
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        result = await engine.submit_event(ev)
    except EventConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    action = (
        result["decision"]
        if (
            engine.network_mode == "enforce"
            and result.get("ingest") == "ADMITTED"
            and result.get("actionable")
            and not result.get("duplicate")
        )
        else "NONE"
    )
    # These fields are already JSON-native. Returning the response directly avoids
    # FastAPI recursively copying/encoding the model identity on the event loop.
    return JSONResponse(
        {
            "event_id": ev["event_id"],
            "decision": result["decision"],
            "recommended_action": action,
            "mode": engine.network_mode,
            "advisory": True,
            "risk": result.get("risk"),
            "reason_codes": [x["code"] for x in result.get("reasons", [])],
            "fail_open": bool(result.get("fail_open")),
            "pending": bool(result.get("pending")),
            "observed_without_model": bool(result.get("observed_without_model")),
            "duplicate": bool(result.get("duplicate")),
            "ingest": result.get("ingest"),
            "model_version": result.get("model_version") or engine.runtime.model_version,
            "artifact_status": result.get("artifact_status", "legacy_unverified"),
            "model_identity": result.get("model_identity", engine.runtime.model_identity),
            "latency_ms": result.get("latency_ms", 0.0),
            "scored": bool(result.get("scored")),
            "scored_before_deadline": bool(result.get("scored_before_deadline")),
            "durable": bool(result.get("durable")),
            "admitted": bool(result.get("admitted")),
            "retryable": bool(result.get("retryable")),
            "delivery_status": result.get("delivery_status"),
            "delivery_expired": bool(result.get("delivery_expired")),
            "model_decision": result.get("model_decision"),
            "commit_ms": result.get("commit_ms", 0.0),
            "queue_ms": result.get("queue_ms", 0.0),
        }
    )


@app.post("/api/v1/cdr")
async def network_cdr(request: Request):
    ev = await network_request(request)
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        result = await engine.runtime.submit("CDR", ev, budget_ms=SLA_TOTAL_NETWORK_MS)
    except EventConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if result.get("pending"):
        return JSONResponse(result, status_code=202)
    if not result.get("ok"):
        return JSONResponse(result, status_code=503)
    return result


SLA_TOTAL_NETWORK_MS = 200.0


async def submit_voice_event(event):
    if engine is None:
        raise HTTPException(503, "engine starting")
    return await engine.submit_event(event)


async def record_voice_observation(event, result):
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        return await engine.record_observation(event, result)
    except EventConflict as exc:
        raise HTTPException(409, str(exc)) from exc


async def lookup_voice_observation(event):
    if engine is None:
        raise HTTPException(503, "engine starting")
    try:
        return await engine.runtime.lookup_observation(event)
    except EventConflict as exc:
        raise HTTPException(409, str(exc)) from exc


from voice_api import create_voice_router  # noqa: E402

app.include_router(
    create_voice_router(
        submit_event=submit_voice_event,
        mode=lambda: engine.network_mode if engine else "shadow",
        record_observation=record_voice_observation,
        lookup_observation=lookup_voice_observation,
    )
)


@app.get("/api/network/mode")
def network_mode_get(u=Depends(require("viewer"))):
    return {"mode": engine.network_mode, "stats": dict(engine.network_stats)}


@app.post("/api/network/mode")
# Chỉ admin nhà mạng chuyển shadow <-> enforce; ghi nhật ký có chữ ký.
def network_mode_set(mode: str, u=Depends(require("admin"))):
    if mode not in ("shadow", "enforce"):
        raise HTTPException(400, "mode must be shadow | enforce")
    if mode == "enforce" and os.environ.get("FRAUD_REAL_TRAFFIC_VALIDATED") != "1":
        raise HTTPException(
            409, "enforce requires domain validation on labelled real traffic (FRAUD_REAL_TRAFFIC_VALIDATED=1)"
        )
    audit.record(u["name"], "network_mode", mode=mode, previous=engine.network_mode)
    engine.network_mode = mode
    return {"mode": mode}


# ====================================================================== mobile app (subscribers)
# app_user đọc token phiên của app; phiên gắn với đúng một số thuê bao, chỉ xem được dữ liệu của số đó.
def app_user(authorization: str = Header(default="")):
    # Ưu tiên phiên tài khoản thật (email/Google, lưu trong CSDL); phiên demo cũ vẫn dùng được khi FRAUD_DEMO=1.
    account_msisdn = portal.subscriber_msisdn(authorization)
    if account_msisdn:
        return account_msisdn
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    session = engine.app_sessions.get(token) if engine and token else None
    now = time.time()
    if session and (
        now - session["created"] > security.SESSION_TTL_S or now - session["last"] > security.SESSION_IDLE_S
    ):
        engine.app_sessions.pop(token, None)
        session = None
    msisdn = session["msisdn"] if session else None
    if session:
        session["last"] = now
    if not msisdn:
        raise HTTPException(401, "app session missing or expired")
    return msisdn


@app.get("/api/app/demo-numbers")
# Các số thuê bao demo để đăng nhập app (thực tế: đăng nhập bằng OTP gửi tới chính số đó).
def app_demo_numbers(_=Depends(legacy_demo_login)):
    return {"numbers": engine.demo_numbers}


@app.post("/api/app/session")
# Mở phiên app cho một số demo; trả token phiên.
def app_session(msisdn: str, _=Depends(legacy_demo_login)):
    if msisdn not in engine.demo_numbers:
        raise HTTPException(403, "only demo numbers can sign in without OTP")
    if portal.db().one("SELECT 1 AS x FROM subscribers WHERE msisdn=?", (msisdn,)):
        raise HTTPException(403, "this number belongs to a registered account")
    token = secrets.token_urlsafe(24)
    with engine.lock:
        engine.app_sessions[token] = {"msisdn": msisdn, "created": time.time(), "last": time.time()}
        engine.inbox[msisdn]  # touch: from now on events to this number reach its inbox
    return {"token": token, "msisdn": msisdn}


@app.get("/api/app/inbox")
# Cuộc gọi / tin nhắn gửi tới chính số của người dùng, kèm quyết định và lý do dễ hiểu.
def app_inbox(msisdn=Depends(app_user)):
    with engine.lock:
        items = list(engine.inbox[msisdn])
    # Thống kê chỉ tính sự kiện tới chính số này (không tính cảnh báo về người thân / thông báo của nhà mạng).
    stats = collections.Counter(i["decision"] for i in items if i["type"] in ("CALL", "SMS", "AUDIO"))
    return {
        "msisdn": msisdn,
        "items": items,
        "stats": {"blocked": stats["BLOCK"], "warned": stats["FLAG"], "allowed": stats["ALLOW"]},
    }


@app.post("/api/app/simulate")
# Chế độ demo: giả lập một cuộc gọi / tin nhắn (lừa đảo hoặc bình thường) gửi tới số của người dùng.
def app_simulate(kind: str, msisdn=Depends(app_user), _=Depends(demo_only)):
    if kind not in ("LEGIT", "SIM_BOX", "CLI_SPOOF", "SMS_PHISH", "DEEPFAKE_VOICE", "WANGIRI", "VOICE_HARVEST"):
        raise HTTPException(400, "unknown kind")
    portal.simulate_allowed(msisdn)
    res = [r for r in engine.inject(kind, target=msisdn) if r["to_target"]]
    return {"kind": kind, "results": res}


# Kịch bản chạy thử một phút: cuộc gọi/tin nhắn bình thường xen lẫn các kiểu lừa đảo, gửi tới số của người dùng
# với khoảng nghỉ ngẫu nhiên như ngoài đời. Mỗi sự kiện vẫn đi qua đúng hệ thống AI (engine.inject), app nhận qua
# hộp thư như cuộc gọi thật, KHÔNG qua phản hồi của nút bấm.
APP_STREAM_KINDS = [
    "LEGIT",
    "SMS_PHISH",
    "LEGIT",
    "CLI_SPOOF",
    "LEGIT",
    "SIM_BOX",
    "WANGIRI",
    "LEGIT",
    "DEEPFAKE_VOICE",
    "VOICE_HARVEST",
]
_app_streams: dict[str, float] = {}
_app_streams_lock = threading.Lock()


def _run_app_stream(msisdn: str, kinds: list[str], gaps: list[float]) -> None:
    try:
        for kind, gap in zip(kinds, gaps):
            time.sleep(gap)
            try:
                engine.inject(kind, target=msisdn)
            except Exception:  # một sự kiện lỗi (hàng đợi đầy) không dừng cả kịch bản
                continue
    finally:
        with _app_streams_lock:
            _app_streams.pop(msisdn, None)


@app.post("/api/app/simulate-stream")
# Chế độ demo: chạy thử khoảng một phút với dữ liệu giả lập; app tự hiện màn hình chặn / cảnh báo khi sự kiện tới.
def app_simulate_stream(msisdn=Depends(app_user), _=Depends(demo_only)):
    with _app_streams_lock:
        until = _app_streams.get(msisdn, 0)
        if until > time.time():
            raise HTTPException(409, f"đang chạy thử, còn khoảng {int(until - time.time())} giây")
        portal.simulate_allowed(msisdn)
        rng = np.random.default_rng()
        kinds = [str(k) for k in rng.permutation(APP_STREAM_KINDS)]
        gaps = [float(g) for g in rng.uniform(4.0, 8.0, size=len(kinds))]
        _app_streams[msisdn] = time.time() + sum(gaps) + 15
    threading.Thread(target=_run_app_stream, args=(msisdn, kinds, gaps), daemon=True, name="app-stream").start()
    return {"started": True, "events": len(kinds), "seconds": round(sum(gaps)), "kinds": collections.Counter(kinds)}


@app.get("/api/app/lookup")
# Tra cứu một số: chỉ trả số liệu tổng hợp về số đó, không trả ai đã nhận cuộc gọi của nó.
def app_lookup(number: str, msisdn=Depends(app_user)):
    accounts.limiter.hit("app_lookup", msisdn, 120, 3600)  # không cho dò hàng loạt uy tín / số báo cáo
    keys = _number_keys(number)
    with engine.lock:
        rep = collections.Counter()
        for k in keys:
            rep.update(engine.reputation.get(k, {}))
        n_reports = len(set().union(*(engine.reports.get(k, set()) for k in keys)))
    blocked, flagged = rep.get("BLOCK", 0), rep.get("FLAG", 0) + rep.get("RETRO", 0)
    if keys & PROTECTED_NUMBERS:
        # Số tổng đài chính thức: bản thân số không xấu, kẻ gian giả mạo hiển thị số này. Không bao giờ báo "số lừa đảo".
        verdict = (
            f"Đây là số tổng đài chính thức. Kẻ gian có thể giả mạo hiển thị số này; nhà mạng đã chặn {blocked} cuộc "
            "giả mạo. Cuộc gọi thật từ số này đi qua đường truyền riêng của ngân hàng/cơ quan."
        )
        level = "info"
    elif blocked:
        verdict, level = "Nhà mạng đã chặn số này vì dấu hiệu lừa đảo", "danger"
    elif flagged:
        verdict, level = "Số này có dấu hiệu đáng ngờ, hãy cẩn thận", "warn"
    else:
        verdict, level = "Chưa ghi nhận dấu hiệu lừa đảo", "ok"
    # Báo cáo của người dùng chỉ để tham khảo: không tự động chặn (tránh bị báo cáo hàng loạt để bôi xấu số sạch).
    return {
        "number": min(keys, key=len),
        "verdict": verdict,
        "level": level,
        "blocked": blocked,
        "flagged": flagged,
        "user_reports": n_reports,
    }


# _number_keys trả mọi cách viết của cùng một số (0xxx / 84xxx / nguyên dạng) để tra cứu không phụ thuộc kiểu nhập.
def _number_keys(number: str) -> set:
    n = number.strip().replace(" ", "").replace(".", "").replace("-", "").lstrip("+")
    keys = {n}
    if n.startswith("0"):
        keys.add("84" + n[1:])
    elif n.startswith("84") and len(n) >= 11:
        keys.add("0" + n[2:])
    return keys


_NUMBER_RE = re.compile(r"^[0-9A-Za-z]{3,15}$")


# Báo cáo / lời mời chỉ nhận số (hoặc tên thương hiệu SMS) hợp lệ: chuỗi tuỳ ý làm phình bộ nhớ và CSDL.
def _checked_number_keys(number: str) -> set:
    keys = _number_keys(number or "")
    if not all(_NUMBER_RE.match(k) for k in keys):
        raise HTTPException(422, "invalid phone number")
    return keys


@app.post("/api/app/report")
# Người dùng báo cáo số lừa đảo; mỗi thuê bao chỉ được tính một lần cho mỗi số.
def app_report(number: str, reason: str = "", msisdn=Depends(app_user)):
    accounts.limiter.hit("app_report", msisdn, 30, 3600)
    if len(reason) > 500:
        raise HTTPException(422, "reason is limited to 500 characters")
    keys = _checked_number_keys(number)
    if msisdn in keys:
        raise HTTPException(400, "cannot report your own number")
    number = max(keys, key=len)
    with engine.lock:
        engine.reports[number].add(msisdn)
        n = len(engine.reports[number])
    portal.persist_report(number, msisdn, reason)
    return {
        "number": number,
        "user_reports": n,
        "note": "Báo cáo được chuyển cho chuyên viên xem xét, không tự động chặn số.",
    }


@app.get("/api/app/family")
# Bảo vệ người thân: danh sách người mình bảo vệ, người bảo vệ mình và lời mời đang chờ.
def app_family(msisdn=Depends(app_user)):
    with engine.lock:
        protecting = sorted(p for p, gs in engine.guardians.items() if msisdn in gs)
        return {
            "protecting": protecting,
            "guardians": sorted(engine.guardians.get(msisdn, ())),
            "invites_sent": sorted(p for p, inv in engine.invites.items() if msisdn in inv),
            "invites_received": sorted(engine.invites.get(msisdn, ())),
        }


@app.post("/api/app/family/invite")
# Mời một người thân (ví dụ bố mẹ) cho phép mình nhận cảnh báo khi họ gặp lừa đảo. Cần họ đồng ý trên app của họ.
def app_family_invite(number: str, msisdn=Depends(app_user)):
    accounts.limiter.hit("family_invite", msisdn, 10, 3600)
    keys = _checked_number_keys(number)
    target = next((k for k in keys if len(k) == 11 and k.startswith("84")), max(keys, key=len))
    if target == msisdn:
        raise HTTPException(400, "cannot protect your own number")
    with engine.lock:
        if msisdn in engine.guardians.get(target, ()):
            return {"status": "already_linked"}
        engine.invites[target].add(msisdn)
    portal.persist_invite(target, msisdn)
    return {
        "status": "invited",
        "number": target,
        "note": "Người thân cần mở app và đồng ý. Không có sự đồng ý thì bạn không nhận được thông tin nào của họ.",
    }


@app.post("/api/app/family/respond")
# Người được mời đồng ý hoặc từ chối. Người được bảo vệ có thể gỡ người giám hộ bất kỳ lúc nào (accept=false).
def app_family_respond(guardian: str, accept: bool, msisdn=Depends(app_user)):
    accounts.limiter.hit("family_respond", msisdn, 30, 3600)
    if not _NUMBER_RE.match(guardian or ""):
        raise HTTPException(404, "no pending invite from this number")  # same answer as an unknown number
    with engine.lock:
        # Chỉ chấp nhận lời mời đang chờ: không thì bất kỳ chuỗi nào cũng thành "người giám hộ" (và một inbox mới).
        invited = guardian in engine.invites.get(msisdn, ())
        if accept and not invited:
            raise HTTPException(404, "no pending invite from this number")
        if not accept and not invited and guardian not in engine.guardians.get(msisdn, ()):
            # Declining / removing something that does not exist must not create state (was: an inbox per string).
            raise HTTPException(404, "no invite or guardian link for this number")
        engine.invites[msisdn].discard(guardian)
        if accept:
            engine.guardians[msisdn].add(guardian)
            engine.inbox[guardian]  # the guardian's app will receive alerts from now on
        else:
            engine.guardians[msisdn].discard(guardian)
    portal.persist_family_response(msisdn, guardian, accept)
    return {"status": "linked" if accept else "removed"}


# Tệp tĩnh của app (index.html, manifest, service worker, icon) phục vụ tại /app/.
app.mount("/app", StaticFiles(directory=STATIC_DIR / "app", html=True), name="app")
# Scripts of /admin/ and /admin/manage (moved out of the HTML so the CSP can refuse inline scripts).
app.mount("/admin/assets", StaticFiles(directory=STATIC_DIR / "admin" / "assets"), name="admin-assets")
