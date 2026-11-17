"""Access control + signed audit trail for the operator API.

Roles (highest first):
  admin    - operator (e.g. Viettel) administrator: fault drills, attack injection (demo mode only), audit
  analyst  - fraud analyst: evidence log, full phone numbers
  viewer   - read-only dashboard, phone numbers masked
Subscribers of the mobile app get a separate session bound to their own number (see server.py).

Every privileged action is appended to an audit log that is hash-chained (editing or deleting a
line breaks the chain) AND signed with HMAC-SHA256 under the acting user's own key, so a valid line
proves *who* did it and nobody can write a line in someone else's name without that key.
Limitation: the keys live on this server. In production they belong in an HSM or on each user's
device (asymmetric signatures), and the chain head is anchored periodically in the operator's system.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import Header, HTTPException

from config import ROOT

ROLES = {"viewer": 1, "analyst": 2, "admin": 3}
USERS_FILE = Path(os.environ.get("FRAUD_USERS_FILE", ROOT / "secrets" / "users.json"))
AUDIT_FILE = Path(os.environ.get("FRAUD_AUDIT_FILE", ROOT / "logs" / "audit.jsonl"))
GENESIS = "0" * 64
# Demo endpoints (attack injection, fault drills) exist only when this is on; run_demo.py turns it on.
DEMO_MODE = os.environ.get("FRAUD_DEMO", "0") == "1"


# ------------------------------------------------------------------ users
# Tài khoản mặc định. "viewer" là mọi nhân viên tập đoàn có tài khoản: xem phân tích, số đã che.
# "ai_agent" không đăng nhập được (vai trò ngoài ROLES): chỉ có khóa để ký các đề xuất của nó vào nhật ký.
DEFAULT_ACCOUNTS = (("viettel_admin", "admin"), ("analyst_1", "analyst"), ("viewer_1", "viewer"),
                    ("nhanvien_01", "viewer"), ("ai_agent", "agent"))
ROLE_LABEL_VI = {"admin": "Quản trị (nhà mạng)", "analyst": "Chuyên viên", "viewer": "Nhân viên", "agent": "AI agent"}


def _new_account(role):
    return {"role": role, "token": secrets.token_urlsafe(24), "sign_key": secrets.token_hex(32)}


def load_users(path: Path = None) -> dict:
    """Users keyed by name. Created with random tokens on first start (the file is the only copy).
    Default accounts missing from an older file are added (existing tokens are never changed)."""
    path = Path(path or USERS_FILE)
    db = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    missing = {n: _new_account(r) for n, r in DEFAULT_ACCOUNTS if n not in db}
    if missing:
        db.update(missing)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(db, indent=2), encoding="utf-8")
    return db


_users_cache: dict | None = None


def users() -> dict:
    global _users_cache
    if _users_cache is None:
        _users_cache = load_users()
    return _users_cache


# Tài khoản bị tạm khóa (do admin phê duyệt đề xuất của AI agent, ví dụ xem số đầy đủ bất thường).
suspended: set = set()


def suspend(name: str):
    """Lock the account and end every session it has open right now."""
    if _session_store is not None:
        _session_store.suspend(name)
    else:
        # Demo mode has no durable session store; owner decision 2026-11-17: an insider lockout must still
        # survive a restart, so record it as the account's `disabled` flag (admins unlock it in /admin/manage).
        with _users_write_lock:
            db = users()
            account = db.get(name)
            if account is not None and account.get("role") in ROLES:
                account["disabled"] = True
                _save_users(db)
    suspended.add(name)
    with _sessions_lock:
        for k in [k for k, s in SESSIONS.items() if s["name"] == name]:
            del SESSIONS[k]


def _same_secret(expected, given) -> bool:
    """Timing-safe compare that never raises: compare_digest(str, str) raises TypeError on non-ASCII
    input, which turned a crafted Authorization header into a 500."""
    try:
        return hmac.compare_digest(str(expected).encode("utf-8"), str(given).encode("utf-8"))
    except (TypeError, ValueError):
        return False


def user_for_token(token: str):
    """Personal credential -> user. Only accepted by /api/login, never directly by other endpoints."""
    for name, u in users().items():
        if token and u["role"] in ROLES and not u.get("disabled") and _same_secret(u["token"], token):
            return {"name": name, "role": u["role"], "role_label": ROLE_LABEL_VI.get(u["role"], u["role"])}
    return None


# ------------------------------------------------------------------ sessions
# Đăng nhập bằng thông tin cá nhân -> phiên có hạn. Lộ một phiên chỉ lộ tối đa 8 giờ (30 phút nếu bỏ máy),
# khác với token cố định dùng mãi mãi. Triển khai thật thay bước đăng nhập bằng SSO của tập đoàn.
SESSION_TTL_S = 8 * 3600
SESSION_IDLE_S = 30 * 60
SESSIONS: dict = {}
_sessions_lock = threading.Lock()
_session_store = None


def configure_store(store=None):
    """Attach a local durable session/revocation store at live startup; tests remain in memory."""
    global _session_store
    _session_store = store
    if store is not None:
        suspended.clear()
        suspended.update(store.suspended_users())



def _open_session(u: dict, clock=time.time):
    sid, now = secrets.token_urlsafe(32), clock()
    with _sessions_lock:
        for k in [k for k, s in SESSIONS.items() if now - s["created"] > SESSION_TTL_S or now - s["last"] > SESSION_IDLE_S]:
            del SESSIONS[k]                      # drop expired sessions as we go
        payload = {**u, "created": now, "last": now}
        if _session_store is not None:
            _session_store.save_session(sid, payload)
        else:
            SESSIONS[sid] = payload
    return sid, u, now + SESSION_TTL_S


def login(credential: str, clock=time.time):
    """Đăng nhập bằng token cá nhân (API/tự động hoá, tương thích bản cũ)."""
    u = user_for_token(credential)
    return None if u is None else _open_session(u, clock)


# ------------------------------------------------------------------ tài khoản vận hành: mật khẩu + Google
# users.json vẫn là nơi giữ khoá ký nhật ký của từng người; thêm password_hash / email / disabled.
_users_write_lock = threading.Lock()
_OPERATOR_NAME = __import__("re").compile(r"^[a-z0-9_.-]{3,40}$")


def _public(name: str, u: dict) -> dict:
    return {"name": name, "role": u["role"], "role_label": ROLE_LABEL_VI.get(u["role"], u["role"])}


def _save_users(db: dict) -> None:
    path = Path(USERS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def login_password(username: str, password: str, clock=time.time):
    from accounts import burn_password_check, verify_password

    name = (username or "").strip().lower()
    u = users().get(name)
    if not u or not u.get("password_hash"):
        burn_password_check(password)
        return None
    if not u or u.get("role") not in ROLES or u.get("disabled") or not verify_password(password, u.get("password_hash")):
        return None
    return _open_session(_public(name, u), clock)


def login_google_operator(email: str, clock=time.time):
    """Google SSO cho nhân viên: email Google (đã xác minh) phải khớp email gắn với tài khoản vận hành."""
    email = (email or "").strip().lower()
    for name, u in users().items():
        if u.get("role") in ROLES and not u.get("disabled") and (u.get("email") or "").lower() == email:
            return _open_session(_public(name, u), clock)
    return None


def list_operators() -> list[dict]:
    return [{"name": n, "role": u["role"], "role_label": ROLE_LABEL_VI.get(u["role"], u["role"]),
             "email": u.get("email"), "has_password": bool(u.get("password_hash")),
             "disabled": bool(u.get("disabled")), "suspended": n in suspended}
            for n, u in sorted(users().items()) if u.get("role") in ROLES]


def upsert_operator(name: str, role: str, *, email: str | None = None, password: str | None = None,
                    disabled: bool | None = None) -> dict:
    from accounts import hash_password

    name = (name or "").strip().lower()
    if not _OPERATOR_NAME.match(name):
        raise ValueError("Tên đăng nhập 3–40 ký tự: a-z, 0-9, _ . -")
    if role not in ROLES:
        raise ValueError("Vai trò không hợp lệ")
    with _users_write_lock:
        db = users()
        u = db.get(name) or _new_account(role)
        if u.get("role") not in ROLES and name in db:
            raise ValueError("Không sửa được tài khoản hệ thống")
        u["role"] = role
        if email is not None:
            u["email"] = email.strip().lower() or None
        if password:
            u["password_hash"] = hash_password(password)
        if disabled is not None:
            u["disabled"] = bool(disabled)
        db[name] = u
        _save_users(db)
    if disabled or password:
        # Khoá tài khoản hoặc đổi mật khẩu kết thúc mọi phiên đang mở, kể cả trong kho phiên bền:
        # trước đây kho bền giữ phiên lại, dùng tiếp được nếu tài khoản được mở khoá trong TTL.
        with _sessions_lock:
            for k in [k for k, s in SESSIONS.items() if s["name"] == name]:
                del SESSIONS[k]
            if _session_store is not None and hasattr(_session_store, "delete_sessions_for"):
                _session_store.delete_sessions_for(name)
    return next(o for o in list_operators() if o["name"] == name)


def bootstrap_admin_password() -> str | None:
    """Lần đầu triển khai: đặt mật khẩu cho admin mặc định từ FRAUD_ADMIN_PASSWORD (nếu chưa có).

    Trả về tên tài khoản được đặt mật khẩu (để log), không bao giờ trả/ghi mật khẩu.
    """
    password = os.environ.get("FRAUD_ADMIN_PASSWORD", "")
    name = os.environ.get("FRAUD_ADMIN_USER", "viettel_admin").strip().lower()
    if not password:
        return None
    u = users().get(name)
    if u and u.get("password_hash"):
        return None
    upsert_operator(name, "admin", email=os.environ.get("FRAUD_ADMIN_EMAIL") or None, password=password)
    return name


def logout(sid: str):
    with _sessions_lock:
        return _session_store.delete_session(sid) if _session_store is not None else SESSIONS.pop(sid, None)


def session_user(sid: str, clock=time.time):
    now = clock()
    with _sessions_lock:
        s = _session_store.session(sid) if _session_store is not None else SESSIONS.get(sid)
        if s is None:
            return None
        if now - s["created"] > SESSION_TTL_S or now - s["last"] > SESSION_IDLE_S:
            if _session_store is not None:
                _session_store.delete_session(sid)
            else:
                del SESSIONS[sid]
            return None
        s["last"] = now
        if _session_store is not None:
            _session_store.save_session(sid, s)
        return {k: s[k] for k in ("name", "role", "role_label")}


def bearer(authorization: str) -> str:
    return authorization[7:] if authorization.lower().startswith("bearer ") else ""


# require(role) trả về dependency FastAPI: đọc header Authorization: Bearer <phiên> và kiểm tra cấp quyền.
def require(min_role: str):
    def dep(authorization: str = Header(default="")):
        u = session_user(bearer(authorization))
        if u is None:
            raise HTTPException(401, "session missing or expired: sign in again at /api/login")
        account = users().get(u["name"], {})
        if u["name"] in suspended or account.get("disabled"):
            raise HTTPException(403, "account suspended by an administrator")
        # Vai trò lấy từ tài khoản hiện tại, không phải bản sao lúc mở phiên: hạ quyền có hiệu lực ngay.
        if account.get("role") in ROLES and account["role"] != u["role"]:
            u = {**u, "role": account["role"], "role_label": ROLE_LABEL_VI.get(account["role"], account["role"])}
        if ROLES[u["role"]] < ROLES[min_role]:
            raise HTTPException(403, f"role '{u['role']}' cannot do this (needs '{min_role}')")
        return u
    return dep


def mask_number(n):
    """84901234567 -> 849****4567: enough for a viewer to follow an alert, not to identify a subscriber.

    Brand sender names and short service codes (113, 1900...) are public, not subscribers: left as is."""
    s = str(n or "")
    normalized = s.strip().translate(str.maketrans("", "", " +-.()"))
    if len(normalized) >= 7 and normalized.isdigit():
        return ("+" if s.strip().startswith("+") else "") + normalized[:3] + "****" + normalized[-4:]
    return s


def mask_imei(n):
    """IMEI identifies a device (and so a person): keep only the model prefix (TAC, 8 digits) visible."""
    s = str(n or "")
    normalized = s.strip().translate(str.maketrans("", "", " -."))
    return s if len(normalized) < 10 or not normalized.isdigit() else normalized[:8] + "*" * (len(normalized) - 8)


# ------------------------------------------------------------------ network link (operator -> this system)
# Thiết bị mạng của nhà mạng ký mỗi yêu cầu bằng HMAC với khóa chung; có mốc thời gian để chống gửi lại.
NETWORK_KEY_FILE = Path(os.environ.get("FRAUD_NETWORK_KEY", ROOT / "secrets" / "network_link.key"))
NETWORK_MAX_SKEW_S = 30
_seen_sigs: OrderedDict = OrderedDict()
_seen_lock = threading.Lock()
_network_key_lock = threading.Lock()
_network_key_cache: tuple[Path, bytes] | None = None


def _network_key_path() -> Path:
    # Lexical absolute identity only: resolve()/stat() would add filesystem IO.
    path = Path(NETWORK_KEY_FILE)
    return path if path.is_absolute() else path.absolute()


def _read_network_key(path: Path, *, create: bool) -> bytes:
    try:
        encoded = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        if not create:
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Another process may have initialized the file while we acquired the lock.
            with path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        encoded = path.read_text(encoding="utf-8").strip()
    try:
        key = bytes.fromhex(encoded)
    except ValueError as exc:
        raise ValueError("network key must contain exactly 32 bytes encoded as 64 hexadecimal characters") from exc
    if len(encoded) != 64 or len(key) != 32:
        raise ValueError("network key must contain exactly 32 bytes encoded as 64 hexadecimal characters")
    return key


def network_key() -> bytes:
    """Return the process-cached key without file IO or locks after startup.

    File edits take effect only after reload_network_key() or a process restart.
    A configured path change selects a new cold load, never the previous path's key.
    """
    global _network_key_cache
    path = _network_key_path()
    cached = _network_key_cache
    if cached is not None and cached[0] == path:
        return cached[1]
    with _network_key_lock:
        cached = _network_key_cache
        if cached is None or cached[0] != path:
            key = _read_network_key(path, create=True)
            _network_key_cache = (path, key)
        return _network_key_cache[1]


def reload_network_key() -> bytes:
    """Explicitly rotate from the configured file; invalid reloads keep the old cache.

    Write a valid 64-character hex key, then call this function or restart the process.
    No endpoint exposes this operation, and failed reloads never generate a replacement.
    """
    global _network_key_cache
    with _network_key_lock:
        path = _network_key_path()
        key = _read_network_key(path, create=False)
        _network_key_cache = (path, key)
        return key



_network_timestamp_lock = threading.Lock()
_last_network_timestamp_ns = 0


def network_timestamp() -> str:
    """Fresh signed-request timestamp, even with a coarse Windows wall clock.

    Keep integer nanoseconds through formatting: a float conversion would erase
    uniqueness for rapid retries of the same body. The server still checks skew.
    """
    global _last_network_timestamp_ns
    with _network_timestamp_lock:
        current = max(time.time_ns(), _last_network_timestamp_ns + 1)
        _last_network_timestamp_ns = current
    seconds, fraction = divmod(current, 1_000_000_000)
    return f"{seconds}.{fraction:09d}"

def sign_network(body: bytes, ts: str, key: bytes = None) -> str:
    return hmac.new(key or network_key(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_network(body: bytes, ts: str, sig: str, key: bytes = None, clock=time.time) -> str | None:
    """None if valid, else the reason it was refused (bad signature, stale timestamp, replay)."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return "missing timestamp"
    now = clock()
    if not __import__("math").isfinite(t) or abs(now - t) > NETWORK_MAX_SKEW_S:
        return "stale timestamp"
    if not sig or not _same_secret(sign_network(body, ts, key), sig):
        return "bad signature"
    with _seen_lock:
        while _seen_sigs:
            first = next(iter(_seen_sigs))
            if now - _seen_sigs[first] <= 2 * NETWORK_MAX_SKEW_S:
                break
            _seen_sigs.popitem(last=False)
        if sig in _seen_sigs:
            return "replayed request"
        _seen_sigs[sig] = now
    return None


# ------------------------------------------------------------------ audit trail
def _digest(prev: str, payload: str) -> str:
    return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()


def _sign(key_hex: str, h: str) -> str:
    return hmac.new(bytes.fromhex(key_hex), h.encode("utf-8"), hashlib.sha256).hexdigest()


class AuditLog:
    """Append-only, hash-chained, per-user signed log of privileged actions."""

    def __init__(self, path: Path = None, user_db: dict | None = None):
        self.path = Path(path or AUDIT_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.user_db = user_db
        self.prev = GENESIS
        self.torn_lines = 0
        if self.path.exists():
            raw = self.path.read_text(encoding="utf-8", errors="replace")
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    self.prev = json.loads(line)["hash"]
                except (ValueError, TypeError, KeyError):
                    # A crash mid-write leaves a partial line. Keep it (verify_audit reports it as
                    # malformed: evidence is never silently rewritten) but do not refuse to start.
                    self.torn_lines += 1
            if raw and not raw.endswith("\n"):
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write("\n")   # the next record starts on its own line
        self._lock = threading.Lock()

    def record(self, actor: str, action: str, **params) -> dict:
        db = self.user_db if self.user_db is not None else users()
        entry = {"ts": round(time.time(), 3), "actor": actor, "role": db[actor]["role"], "action": action, "params": params}
        payload = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            h = _digest(self.prev, payload)
            row = {"prev": self.prev, "hash": h, "sig": _sign(db[actor]["sign_key"], h), "entry": entry}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self.prev = h
        return row

    def tail(self, n: int = 20) -> list:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()[-n:] if x.strip()]


def verify_audit(path: Path = None, user_db: dict | None = None, anchor: dict | None = None) -> dict:
    """Checks the chain and every signature. Reports the first broken line and why.

    The chain alone cannot see lines deleted from the END of the file. ``head`` (records + last
    hash) is returned so it can be copied to a system outside this server; passing that copy back
    as ``anchor`` detects truncation or rewriting of everything up to it.
    """
    path, db = Path(path or AUDIT_FILE), (user_db if user_db is not None else users())
    result = _verify_audit_chain(path, db)
    if anchor is not None and result["valid"]:
        want_n, want_hash = int(anchor.get("records", 0)), str(anchor.get("hash", ""))
        if want_n > result["records"] or (want_n and result["hashes"][want_n - 1] != want_hash):
            result.update(valid=False, problem="log truncated or rewritten before the external anchor",
                          first_bad_line=min(want_n, result["records"] + 1))
    result.pop("hashes", None)
    return result


def _verify_audit_chain(path: Path, db: dict) -> dict:
    prev, n, hashes = GENESIS, 0, []

    def out(**kw):
        return {"head": {"records": n, "hash": prev}, "hashes": hashes, **kw}
    if not path.exists():
        return out(valid=True, records=0, first_bad_line=None, problem=None)
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            payload = json.dumps(row["entry"], ensure_ascii=False, sort_keys=True)
            if not isinstance(row["entry"], dict) or not all(isinstance(row[k], str) for k in ("prev", "hash", "sig")):
                raise TypeError("audit record fields have the wrong type")
        except (ValueError, TypeError, KeyError, AttributeError):
            # Dòng hỏng là dấu hiệu bị sửa, không phải lỗi 500 của máy chủ.
            return out(valid=False, records=n, first_bad_line=i, problem="malformed record (not valid audit JSON)")
        if row["prev"] != prev:
            return out(valid=False, records=n, first_bad_line=i, problem="chain broken (line removed or reordered)")
        if _digest(prev, payload) != row["hash"]:
            return out(valid=False, records=n, first_bad_line=i, problem="content edited after it was written")
        actor = row["entry"].get("actor")
        if actor not in db or not hmac.compare_digest(_sign(db[actor]["sign_key"], row["hash"]), row["sig"]):
            return out(valid=False, records=n, first_bad_line=i,
                       problem=f"signature does not match '{actor}' (written without that user's key)")
        prev, n = row["hash"], n + 1
        hashes.append(prev)
    return out(valid=True, records=n, first_bad_line=None, problem=None)


