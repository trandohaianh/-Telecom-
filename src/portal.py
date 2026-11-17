"""API tài khoản thuê bao, quản trị (tài khoản vận hành, duyệt báo cáo số, chi phí, pipeline) và /metrics.

Giao diện:
- /app/            app thuê bao (đăng ký / đăng nhập email hoặc Google, gắn số, cảnh báo của mình)
- /admin/          bảng điều khiển vận hành (giám sát luồng, bằng chứng, AI agent)
- /admin/manage    quản trị: tài khoản nhân viên, duyệt báo cáo số, chi phí, pipeline
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import accounts
import costs
import observability
import ops_pipeline
import security
from appdb import AppDB, now
from security import require

router = APIRouter()
_db: AppDB | None = None
_db_lock = threading.Lock()
_engine_getter = lambda: None  # noqa: E731  (server gắn hàm trả engine hiện tại)
scheduler: ops_pipeline.Scheduler | None = None
CPU_AT_START = 0.0


def db() -> AppDB:
    global _db
    with _db_lock:
        if _db is None:
            from config import LOG_DIR

            _db = AppDB(os.environ.get("FRAUD_APP_DB", str(LOG_DIR / "app.sqlite3")))
        return _db


def subscribers() -> accounts.SubscriberAccounts:
    return accounts.SubscriberAccounts(db())


def engine():
    return _engine_getter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _auth_event(actor: str, event: str, result: str) -> None:
    observability.AUTH_EVENTS.labels(actor=actor, event=event, result=result).inc()


# =========================================================================== khởi động / tắt
def startup(engine_getter) -> None:
    """Gắn engine, nạp dữ liệu người dùng bền vững vào bộ nhớ engine, bootstrap admin, chạy scheduler."""
    global _engine_getter, scheduler, CPU_AT_START
    _engine_getter = engine_getter
    CPU_AT_START = costs.process_cpu_seconds()
    observability.register(engine_getter, lambda: _db)
    name = security.bootstrap_admin_password()
    if name:
        print(f"  Đã đặt mật khẩu ban đầu cho tài khoản vận hành '{name}' (từ FRAUD_ADMIN_PASSWORD).")
    hydrate_engine(engine_getter())
    scheduler = ops_pipeline.Scheduler(db(), engine_getter)
    scheduler.start()


def shutdown() -> None:
    if scheduler is not None:
        scheduler.stop()


class AlertSink:
    """Lưu hộp thư của thuê bao xuống CSDL ở luồng riêng (không chặn luồng chấm điểm)."""

    def __init__(self):
        import queue

        self.q: "queue.Queue" = queue.Queue(maxsize=10_000)
        self.dropped = 0
        threading.Thread(target=self._run, name="alert-sink", daemon=True).start()

    def __call__(self, msisdn, item) -> None:
        try:
            self.q.put_nowait((msisdn, item, now()))
        except Exception:
            self.dropped += 1

    def _run(self) -> None:
        import json
        import queue

        while True:
            batch = [self.q.get()]
            try:
                while len(batch) < 200:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            try:
                with db().tx() as c:
                    c.executemany("INSERT INTO subscriber_alerts(msisdn, created_at, item_json) VALUES (?,?,?)",
                                  [(m, t, json.dumps(i, ensure_ascii=False, default=str)) for m, i, t in batch])
            except Exception:
                self.dropped += len(batch)

    def flush(self, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while not self.q.empty() and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)


alert_sink: AlertSink | None = None


def hydrate_engine(eng) -> None:
    global alert_sink
    if eng is None:
        return
    import json

    d = db()
    history = {}
    for r in d.query("""SELECT msisdn, item_json FROM (
                          SELECT msisdn, item_json, id, ROW_NUMBER() OVER (PARTITION BY msisdn ORDER BY id DESC) rn
                          FROM subscriber_alerts) WHERE rn <= 50 ORDER BY id"""):
        history.setdefault(r["msisdn"], []).append(json.loads(r["item_json"]))
    with eng.lock:
        for msisdn, items in history.items():
            box = eng.inbox[msisdn]
            for item in items:          # cũ → mới; appendleft để mục mới nhất đứng đầu như lúc chạy
                box.appendleft(item)
        for r in d.query("SELECT msisdn FROM subscribers WHERE msisdn IS NOT NULL AND disabled_at IS NULL"):
            eng.inbox[r["msisdn"]]  # chạm hộp thư: cảnh báo tới số này được giữ lại kể cả khi app đang đóng
        for r in d.query("SELECT protected, guardian FROM guardians"):
            eng.guardians[r["protected"]].add(r["guardian"])
        for r in d.query("SELECT protected, guardian FROM invites"):
            eng.invites[r["protected"]].add(r["guardian"])
        for r in d.query("SELECT number, reporter FROM number_reports"):
            eng.reports[r["number"]].add(r["reporter"])
        confirmed = [r["number"] for r in d.query("SELECT DISTINCT number FROM number_reports WHERE status='confirmed'")]
    if confirmed:
        with eng.det_lock:
            eng.g.fallback.block_manual(confirmed, "REPORT-CONFIRMED")
    if alert_sink is None:
        alert_sink = AlertSink()
    eng.inbox_hook = alert_sink


# =========================================================================== phụ thuộc xác thực thuê bao
def subscriber_session(authorization: str = Header(default="")) -> dict:
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    s = subscribers().session(token)
    if s is None:
        raise HTTPException(401, "Phiên đăng nhập đã hết hạn")
    return s


def subscriber_msisdn(authorization: str = Header(default="")) -> str | None:
    """Số của tài khoản đăng nhập bằng email/Google; None nếu token không phải phiên tài khoản."""
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    s = subscribers().session(token) if token else None
    if s is None:
        return None
    if not s["msisdn"]:
        raise HTTPException(409, "Tài khoản chưa gắn số điện thoại")
    eng = engine()
    if eng is not None:
        with eng.lock:
            eng.inbox[s["msisdn"]]
    return s["msisdn"]


# =========================================================================== cấu hình đăng nhập
@router.get("/api/auth/config")
def auth_config():
    eng = engine()
    demo = bool(getattr(eng, "demo_mode", security.DEMO_MODE))
    ids = accounts.google_client_ids()
    return {"google_client_id": ids[0] if ids else None, "demo_mode": demo,
            "phone_verification": "demo_pool" if demo else ("otp" if accounts.otp_provider() else "unavailable")}


# =========================================================================== thuê bao
class Credentials(BaseModel):
    email: str = Field(..., max_length=320)
    password: str = Field(..., min_length=1, max_length=128)


class GoogleCredential(BaseModel):
    credential: str = Field(..., min_length=20, max_length=4096)


def _session_response(sub_id: int, created: bool = False) -> dict:
    token = subscribers().create_session(sub_id)
    me = subscribers().session(token)
    return {"token": token, "is_new_user": created, "me": me}


@router.post("/api/app/auth/register")
def app_register(body: Credentials, request: Request):
    accounts.limiter.hit("app_register", _ip(request), 10, 3600)
    sub_id = subscribers().register(body.email, body.password)
    _auth_event("subscriber", "register", "ok")
    return _session_response(sub_id, created=True)


@router.post("/api/app/auth/login")
def app_login(body: Credentials, request: Request):
    accounts.limiter.hit("app_login_ip", _ip(request), 30, 900)
    accounts.limiter.hit("app_login_email", body.email, 10, 900)
    sub_id = subscribers().authenticate(body.email, body.password)
    if sub_id is None:
        _auth_event("subscriber", "login", "failed")
        raise HTTPException(401, "Email hoặc mật khẩu không đúng")
    accounts.limiter.reset("app_login_email", body.email)
    _auth_event("subscriber", "login", "ok")
    return _session_response(sub_id)


@router.post("/api/app/auth/google")
def app_google(body: GoogleCredential, request: Request):
    accounts.limiter.hit("google_ip", _ip(request), 30, 900)
    ident = accounts.verify_google_token(body.credential)
    sub_id, created = subscribers().google(ident)
    _auth_event("subscriber", "google", "created" if created else "ok")
    return _session_response(sub_id, created=created)


@router.get("/api/app/auth/me")
def app_me(me: dict = Depends(subscriber_session)):
    return me


@router.post("/api/app/auth/logout")
def app_logout(authorization: str = Header(default="")):
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if token:
        subscribers().revoke(token)
    return {"ok": True}


@router.post("/api/app/phone/claim-demo")
def claim_demo_number(me: dict = Depends(subscriber_session)):
    eng = engine()
    if eng is None or not eng.demo_mode or eng.sim is None:
        raise HTTPException(409, "Chỉ chế độ demo mới cấp số giả lập; hãy xác minh số bằng OTP")
    # Ba số của đăng nhập demo kiểu cũ ai cũng mở được: không bao giờ cấp chúng cho tài khoản thật.
    public = set(getattr(eng, "demo_numbers", ()) or ())
    pool = [s.msisdn for s in eng.sim.subs if s.msisdn not in public]
    number = subscribers().claim_demo_number(me["id"], pool)
    with eng.lock:
        eng.inbox[number]
    return {"msisdn": number, "kind": "demo"}


class PhoneIn(BaseModel):
    phone: str = Field(..., max_length=20)


class OtpIn(PhoneIn):
    code: str = Field(..., min_length=4, max_length=8)


@router.post("/api/app/phone/otp/start")
def otp_start(body: PhoneIn, request: Request, me: dict = Depends(subscriber_session)):
    provider = accounts.otp_provider()
    if provider is None:
        raise HTTPException(503, "Máy chủ chưa cấu hình gửi SMS OTP")
    phone = accounts.normalize_msisdn(body.phone)
    accounts.limiter.hit("otp_phone", phone, 3, 3600)
    accounts.limiter.hit("otp_ip", _ip(request), 10, 3600)
    subscribers().otp_start(me["id"], phone, provider)
    costs.record_usage(db(), "sms_otp", provider=provider.name, cost_usd=provider.cost_usd, subscriber_id=me["id"])
    return {"sent": True, "expires_in_s": accounts.OTP_TTL_S}


@router.post("/api/app/phone/otp/verify")
def otp_verify(body: OtpIn, me: dict = Depends(subscriber_session)):
    phone = subscribers().otp_verify(me["id"], accounts.normalize_msisdn(body.phone), body.code)
    if me.get("msisdn") != phone:
        forget_number_history(phone)
    eng = engine()
    if eng is not None:
        with eng.lock:
            eng.inbox[phone]
    return {"msisdn": phone, "kind": "verified"}


def forget_number_history(msisdn: str) -> None:
    """Số vừa có chủ mới: chủ mới không được thấy cảnh báo (kèm nội dung SMS) của chủ cũ,
    và người giám hộ chủ cũ đã duyệt không được tiếp tục nhận cảnh báo của chủ mới."""
    with db().tx() as c:
        c.execute("DELETE FROM subscriber_alerts WHERE msisdn=?", (msisdn,))
        c.execute("DELETE FROM guardians WHERE protected=? OR guardian=?", (msisdn, msisdn))
        c.execute("DELETE FROM invites WHERE protected=? OR guardian=?", (msisdn, msisdn))
    eng = engine()
    if eng is None:
        return
    with eng.lock:
        if msisdn in eng.inbox:
            eng.inbox[msisdn].clear()
        eng.guardians.pop(msisdn, None)
        eng.invites.pop(msisdn, None)
        for mapping in (eng.guardians, eng.invites):
            for linked in mapping.values():
                linked.discard(msisdn)


# ---- ghi xuống CSDL các thao tác của app (server.py gọi sau khi cập nhật bộ nhớ engine)
def persist_report(number: str, reporter: str, reason: str) -> None:
    with db().tx() as c:
        c.execute("INSERT OR IGNORE INTO number_reports(number, reporter, reason, created_at) VALUES (?,?,?,?)",
                  (number, reporter, (reason or "")[:500], now()))


def persist_invite(protected: str, guardian: str) -> None:
    with db().tx() as c:
        c.execute("INSERT OR IGNORE INTO invites VALUES (?,?,?)", (protected, guardian, now()))


def persist_family_response(protected: str, guardian: str, accept: bool) -> None:
    with db().tx() as c:
        c.execute("DELETE FROM invites WHERE protected=? AND guardian=?", (protected, guardian))
        if accept:
            c.execute("INSERT OR IGNORE INTO guardians VALUES (?,?)", (protected, guardian))
        else:
            c.execute("DELETE FROM guardians WHERE protected=? AND guardian=?", (protected, guardian))


def simulate_allowed(msisdn: str) -> None:
    """Demo công khai: giới hạn mỗi số 10 lần giả lập/phút để không ai làm nghẽn hàng đợi tiêm."""
    accounts.limiter.hit("simulate", msisdn, 10, 60)
    costs.record_usage(db(), "simulate", provider="local", cost_usd=0.0)


# =========================================================================== vận hành: đăng nhập
class OperatorLogin(BaseModel):
    username: str = Field(..., max_length=40)
    password: str = Field(..., max_length=128)


def operator_password_login(body: OperatorLogin, request: Request):
    accounts.limiter.hit("op_login_ip", _ip(request), 30, 900)
    accounts.limiter.hit("op_login_user", body.username, 8, 900)
    out = security.login_password(body.username, body.password)
    if out is None:
        _auth_event("operator", "login", "failed")
        return None
    accounts.limiter.reset("op_login_user", body.username)
    _auth_event("operator", "login", "ok")
    return out


@router.post("/api/login/google")
def operator_google(body: GoogleCredential, request: Request):
    accounts.limiter.hit("op_google_ip", _ip(request), 30, 900)
    ident = accounts.verify_google_token(body.credential)
    out = security.login_google_operator(ident.email)
    if out is None:
        _auth_event("operator", "google", "no_account")
        raise HTTPException(403, "Email Google này chưa được cấp tài khoản vận hành")
    sid, u, expires = out
    if u["name"] in security.suspended:
        security.logout(sid)
        raise HTTPException(403, "account suspended by an administrator")
    from server import audit

    audit.record(u["name"], "login", method="google")
    _auth_event("operator", "google", "ok")
    return {"session": sid, "expires_at": expires, "idle_timeout_s": security.SESSION_IDLE_S, **u}


# =========================================================================== quản trị tài khoản vận hành
class OperatorIn(BaseModel):
    name: str = Field(..., max_length=40)
    role: str
    email: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=128)
    disabled: bool | None = None


@router.get("/api/admin/operators")
def operators(u=Depends(require("admin"))):
    return security.list_operators()


@router.post("/api/admin/operators")
def save_operator(body: OperatorIn, u=Depends(require("admin"))):
    if (body.name or "").strip().lower() == u["name"] and (body.disabled or body.role != "admin"):
        raise HTTPException(400, "Không tự khoá hoặc tự hạ quyền chính mình")
    if body.password is not None and len(body.password) < 10:
        raise HTTPException(422, "Mật khẩu nhân viên tối thiểu 10 ký tự")
    try:
        out = security.upsert_operator(body.name, body.role, email=body.email, password=body.password,
                                       disabled=body.disabled)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    from server import audit

    audit.record(u["name"], "operator_upsert", target=body.name, role=body.role,
                 disabled=bool(body.disabled), password_changed=bool(body.password))
    return out


# =========================================================================== duyệt báo cáo số
def _report_key(number: str) -> str:
    import hashlib

    return hashlib.sha256(f"report:{number}".encode()).hexdigest()[:16]


@router.get("/api/admin/reports")
def list_reports(status: str = Query("pending", pattern="^(pending|confirmed|dismissed)$"),
                 limit: int = Query(100, ge=1, le=500), u=Depends(require("analyst"))):
    """Số bị thuê bao báo cáo, gộp theo số. Số luôn hiển thị dạng che (tối thiểu hoá dữ liệu),
    kèm số lần mô hình đã chặn/gắn cờ số đó để chuyên viên đối chiếu."""
    rows = db().query(
        """SELECT number, COUNT(*) AS reporters, MIN(created_at) AS first_at, MAX(created_at) AS last_at,
                  GROUP_CONCAT(NULLIF(reason, ''), ' | ') AS reasons, MAX(reviewer) AS reviewer,
                  MAX(review_note) AS review_note
           FROM number_reports WHERE status = ? GROUP BY number ORDER BY reporters DESC, last_at DESC LIMIT ?""",
        (status, limit))
    eng = engine()
    out = []
    for r in rows:
        item = {k: r[k] for k in ("reporters", "first_at", "last_at", "reasons", "reviewer", "review_note")}
        item.update(status=status, key=_report_key(r["number"]), number_masked=security.mask_number(r["number"]))
        if eng is not None:
            with eng.lock:
                rep = eng.reputation.get(r["number"], {})
            item["model_blocks"], item["model_flags"] = int(rep.get("BLOCK", 0)), int(rep.get("FLAG", 0))
        out.append(item)
    return out


class ReviewIn(BaseModel):
    decision: str = Field(..., pattern="^(confirm|dismiss)$")
    note: str = Field(default="", max_length=500)


@router.post("/api/admin/reports/{key}/review")
def review_report(key: str, body: ReviewIn, u=Depends(require("analyst"))):
    pending = [r["number"] for r in db().query("SELECT DISTINCT number FROM number_reports WHERE status='pending'")]
    number = next((n for n in pending if _report_key(n) == key), None)
    if number is None:
        raise HTTPException(404, "Không có báo cáo chờ duyệt cho số này")
    status = "confirmed" if body.decision == "confirm" else "dismissed"
    with db().tx() as c:
        n = c.execute("UPDATE number_reports SET status=?, reviewer=?, review_note=?, reviewed_at=? "
                      "WHERE number=? AND status='pending'", (status, u["name"], body.note, now(), number)).rowcount
    eng = engine()
    if status == "confirmed" and eng is not None:
        # Chuyên viên xác nhận → luật dự phòng chặn số (con người quyết định, mô hình không tự học từ báo cáo).
        with eng.det_lock:
            eng.g.fallback.block_manual([number], f"REPORT-{u['name']}")
    from server import audit

    audit.record(u["name"], "report_review", number=security.mask_number(number), decision=status, reports=n,
                 note=body.note)
    return {"key": key, "number_masked": security.mask_number(number), "status": status, "reports": n}


# =========================================================================== chi phí
class InfraIn(BaseModel):
    month: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    provider: str = Field(..., min_length=1, max_length=60)
    item: str = Field(..., min_length=1, max_length=120)
    amount_usd: float = Field(..., ge=0, le=100000)
    note: str | None = Field(default=None, max_length=500)


@router.get("/api/admin/costs")
def cost_summary(days: int = Query(30, ge=1, le=366), u=Depends(require("admin"))):
    eng = engine()
    processed = int(getattr(eng, "processed", 0) or 0)
    uptime = time.time() - getattr(eng, "started", time.time())
    return costs.summary(db(), processed=processed, uptime_s=uptime, days=days, cpu_baseline_s=CPU_AT_START)


@router.post("/api/admin/costs/infra")
def add_infra(body: InfraIn, u=Depends(require("admin"))):
    with db().tx() as c:
        rid = c.execute("INSERT INTO infra_costs(month, provider, item, amount_usd, note, created_by, created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (body.month, body.provider, body.item, body.amount_usd, body.note,
                                                   u["name"], now())).lastrowid
    from server import audit

    audit.record(u["name"], "infra_cost_add", target=str(rid), provider=body.provider, item=body.item,
                 amount_usd=body.amount_usd, month=body.month)
    return {"id": rid, **body.model_dump()}


@router.delete("/api/admin/costs/infra/{cost_id}")
def delete_infra(cost_id: int, u=Depends(require("admin"))):
    with db().tx() as c:
        if not c.execute("DELETE FROM infra_costs WHERE id=?", (cost_id,)).rowcount:
            raise HTTPException(404, "Không tìm thấy")
    from server import audit

    audit.record(u["name"], "infra_cost_delete", target=str(cost_id))
    return {"ok": True}


# =========================================================================== pipeline
@router.get("/api/admin/pipeline")
def pipeline_status(u=Depends(require("analyst"))):
    import json

    last = {}
    for job in ops_pipeline.JOBS:
        r = db().one("SELECT * FROM pipeline_runs WHERE job=? ORDER BY id DESC LIMIT 1", (job,))
        last[job] = None if r is None else {**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None}
    kpi = [dict(r) for r in db().query("SELECT * FROM kpi_snapshots ORDER BY id DESC LIMIT 48")]
    return {"last_runs": last, "kpi": kpi[::-1]}


@router.post("/api/admin/pipeline/run")
def pipeline_run(job: str = Query(...), u=Depends(require("admin"))):
    if job not in ops_pipeline.JOBS:
        raise HTTPException(422, f"job phải thuộc {list(ops_pipeline.JOBS)}")
    return ops_pipeline.run_job(db(), engine(), job, trigger=f"manual:{u['name']}")


# =========================================================================== metrics
@router.get("/metrics", include_in_schema=False)
def metrics(authorization: str = Header(default="")):
    eng = engine()
    if not observability.metrics_authorized(authorization, bool(getattr(eng, "demo_mode", False))):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    body, ctype = observability.render()
    return Response(content=body, media_type=ctype)
