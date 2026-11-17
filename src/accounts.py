"""Tài khoản thật cho app thuê bao + tiện ích xác thực dùng chung (mật khẩu, Google, OTP, rate-limit).

Thuê bao đăng ký bằng email/mật khẩu hoặc Google, sau đó gắn một số điện thoại:
- Chế độ demo công khai: hệ thống cấp một số trong tập thuê bao GIẢ LẬP (mỗi tài khoản một số riêng),
  vì luồng lưu lượng là dữ liệu mô phỏng — người dùng thật không sở hữu số nào trong đó.
- Chế độ thật: xác minh quyền sở hữu số bằng OTP SMS (Twilio Verify hoặc nhà cung cấp tương đương).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException

from appdb import AppDB, now

logger = logging.getLogger("fraud.accounts")

SESSION_TTL_S = int(os.environ.get("FRAUD_APP_SESSION_TTL_S", str(30 * 24 * 3600)))
SESSION_IDLE_S = int(os.environ.get("FRAUD_APP_SESSION_IDLE_S", str(14 * 24 * 3600)))
OTP_TTL_S = 300
OTP_MAX_ATTEMPTS = 5
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


# --------------------------------------------------------------------------- mật khẩu (scrypt, stdlib)
def hash_password(password: str) -> str:
    if not 8 <= len(password) <= 128:
        raise ValueError("Mật khẩu cần 8–128 ký tự")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$14$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, log_n, r, p, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=2 ** int(log_n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = None


def burn_password_check(password: str) -> None:
    """Chạy một lần scrypt giả khi không có tài khoản, để thời gian phản hồi không lộ email/tên nào tồn tại."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("timing-equaliser-not-a-password")
    verify_password(password or "", _DUMMY_HASH)


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(422, "Email không hợp lệ")
    return email


def normalize_msisdn(number: str) -> str:
    """Chuẩn hoá số Việt Nam về dạng 84xxxxxxxxx."""
    n = re.sub(r"[\s.\-()]", "", number or "").lstrip("+")
    if n.startswith("0"):
        n = "84" + n[1:]
    if not re.fullmatch(r"84\d{9}", n):
        raise HTTPException(422, "Số điện thoại không hợp lệ (VD 0912345678)")
    return n


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- rate limit
class RateLimiter:
    """Cửa sổ trượt trong bộ nhớ (1 instance). Vượt ngưỡng → 429."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque] = {}

    def hit(self, scope: str, key: str, limit: int, window_s: int) -> None:
        if os.environ.get("FRAUD_RATE_LIMIT", "1") == "0" or not key:
            return
        t = time.monotonic()
        with self._lock:
            # Cùng chuẩn hoá với phần tra tài khoản (strip + lower): " admin" không được có ngăn đếm riêng.
            q = self._hits.setdefault((scope, key.strip().lower()), deque())
            while q and q[0] <= t - window_s:
                q.popleft()
            if len(q) >= limit:
                raise HTTPException(429, "Thao tác quá nhanh, vui lòng thử lại sau.",
                                    headers={"Retry-After": str(int(q[0] + window_s - t) + 1)})
            q.append(t)

    def reset(self, scope: str | None = None, key: str | None = None) -> None:
        with self._lock:
            if scope is None:
                self._hits.clear()
            else:
                self._hits.pop((scope, (key or "").strip().lower()), None)


limiter = RateLimiter()


# --------------------------------------------------------------------------- Google Sign-In
def google_client_ids() -> list[str]:
    return [x.strip() for x in os.environ.get("GOOGLE_CLIENT_ID", "").split(",") if x.strip()]


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str


@lru_cache(maxsize=1)
def _jwks():
    import jwt

    return jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", cache_keys=True, lifespan=3600, timeout=10)


def verify_google_token(credential: str) -> GoogleIdentity:
    """Kiểm chữ ký RS256 bằng JWKS của Google, aud = client ID của ta, iss, hạn dùng, email_verified."""
    import jwt

    audiences = google_client_ids()
    if not audiences:
        raise HTTPException(404, "Đăng nhập Google chưa được bật")
    if not credential or len(credential) > 4096:
        raise HTTPException(401, "Thông tin Google không hợp lệ")
    try:
        key = _jwks().get_signing_key_from_jwt(credential).key
        claims = jwt.decode(credential, key, algorithms=["RS256"], audience=audiences, leeway=30,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Thông tin Google không hợp lệ hoặc đã hết hạn") from exc
    return identity_from_claims(claims)


def identity_from_claims(claims: dict) -> GoogleIdentity:
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "Token không do Google phát hành")
    if claims.get("email_verified") not in (True, "true"):
        raise HTTPException(401, "Email Google chưa được xác minh")
    sub, email = claims.get("sub"), claims.get("email")
    if not isinstance(sub, str) or not isinstance(email, str) or "@" not in email:
        raise HTTPException(401, "Tài khoản Google thiếu email")
    return GoogleIdentity(sub=sub, email=email.strip().lower())


# --------------------------------------------------------------------------- OTP SMS
class OtpProvider:
    name = "none"
    cost_usd = 0.0

    def send(self, phone: str, code: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleOtp(OtpProvider):
    """Chỉ cho phát triển: ghi mã ra log máy chủ, không gửi SMS."""

    name = "console"

    def send(self, phone: str, code: str) -> None:
        logger.warning("OTP (console, không gửi SMS) cho %s: %s", phone[:5] + "****", code)


class TwilioSmsOtp(OtpProvider):
    name = "twilio"

    def __init__(self):
        self.sid = os.environ["TWILIO_ACCOUNT_SID"]
        self.token = os.environ["TWILIO_AUTH_TOKEN"]
        self.sender = os.environ["TWILIO_FROM"]
        self.cost_usd = float(os.environ.get("SMS_COST_USD", "0.08"))

    def send(self, phone: str, code: str) -> None:
        import httpx

        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
            auth=(self.sid, self.token), timeout=15,
            data={"To": "+" + phone, "From": self.sender,
                  "Body": f"Ma xac thuc La chan lua dao: {code}. Het han sau 5 phut. Khong chia se ma nay."},
        )
        if resp.status_code >= 300:
            raise HTTPException(502, "Không gửi được SMS, vui lòng thử lại")


def otp_provider() -> OtpProvider | None:
    kind = os.environ.get("FRAUD_OTP_PROVIDER", "").strip().lower()
    if kind == "twilio":
        return TwilioSmsOtp()
    if kind == "console":
        return ConsoleOtp()
    return None


# --------------------------------------------------------------------------- dịch vụ thuê bao
class SubscriberAccounts:
    def __init__(self, db: AppDB):
        self.db = db

    # ---- tạo / đăng nhập
    def register(self, email: str, password: str) -> int:
        email = normalize_email(email)
        try:
            pw = hash_password(password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM subscribers WHERE email=?", (email,)).fetchone():
                raise HTTPException(400, "Email đã được sử dụng")
            cur = c.execute("INSERT INTO subscribers(email, password_hash, created_at) VALUES (?,?,?)",
                            (email, pw, now()))
            return int(cur.lastrowid)

    def authenticate(self, email: str, password: str) -> int | None:
        row = self.db.one("SELECT id, password_hash FROM subscribers WHERE email=? AND disabled_at IS NULL",
                          ((email or "").strip().lower(),))
        if row is None or not row["password_hash"]:
            burn_password_check(password)
            return None
        if verify_password(password, row["password_hash"]):
            return int(row["id"])
        return None

    def google(self, ident: GoogleIdentity) -> tuple[int, bool]:
        with self.db.tx() as c:
            row = c.execute("SELECT id, disabled_at FROM subscribers WHERE google_sub=?", (ident.sub,)).fetchone()
            if row is None:
                row = c.execute("SELECT id, disabled_at, google_sub, password_hash FROM subscribers WHERE email=?",
                                (ident.email,)).fetchone()
                if row is not None:
                    if row["google_sub"] and row["google_sub"] != ident.sub:
                        raise HTTPException(403, "Email đã liên kết với tài khoản Google khác")
                    c.execute("UPDATE subscribers SET google_sub=? WHERE id=?", (ident.sub, row["id"]))
                    if row["password_hash"]:
                        # Đăng ký bằng email không xác minh quyền sở hữu hộp thư; Google thì có. Người đã
                        # đặt mật khẩu trước (có thể là kẻ chiếm chỗ) mất mật khẩu và mọi phiên đang mở.
                        c.execute("UPDATE subscribers SET password_hash=NULL WHERE id=?", (row["id"],))
                        c.execute("DELETE FROM subscriber_sessions WHERE subscriber_id=?", (row["id"],))
            if row is not None:
                if row["disabled_at"]:
                    raise HTTPException(403, "Tài khoản đã bị khoá")
                return int(row["id"]), False
            cur = c.execute("INSERT INTO subscribers(email, google_sub, created_at) VALUES (?,?,?)",
                            (ident.email, ident.sub, now()))
            return int(cur.lastrowid), True

    # ---- phiên
    def create_session(self, subscriber_id: int) -> str:
        token = secrets.token_urlsafe(32)
        t = now()
        with self.db.tx() as c:
            c.execute("INSERT INTO subscriber_sessions VALUES (?,?,?,?)", (_token_hash(token), subscriber_id, t, t))
            # Giữ tối đa 10 phiên/tài khoản (nhiều thiết bị), xoá phiên cũ nhất.
            c.execute("""DELETE FROM subscriber_sessions WHERE subscriber_id=? AND token_hash NOT IN (
                         SELECT token_hash FROM subscriber_sessions WHERE subscriber_id=?
                         ORDER BY last_seen DESC LIMIT 10)""", (subscriber_id, subscriber_id))
        return token

    def session(self, token: str) -> dict | None:
        if not token:
            return None
        h, t = _token_hash(token), now()
        row = self.db.one("""SELECT s.created_at, s.last_seen, u.id, u.email, u.msisdn, u.msisdn_kind, u.disabled_at,
                                    u.google_sub
                             FROM subscriber_sessions s JOIN subscribers u ON u.id = s.subscriber_id
                             WHERE s.token_hash=?""", (h,))
        if row is None:
            return None
        if row["disabled_at"] or t - row["created_at"] > SESSION_TTL_S or t - row["last_seen"] > SESSION_IDLE_S:
            self.revoke(token)
            return None
        if t - row["last_seen"] > 60:  # giảm ghi đĩa: cập nhật tối đa mỗi phút
            with self.db.tx() as c:
                c.execute("UPDATE subscriber_sessions SET last_seen=? WHERE token_hash=?", (t, h))
        return {"id": int(row["id"]), "email": row["email"], "msisdn": row["msisdn"],
                "msisdn_kind": row["msisdn_kind"], "has_google": bool(row["google_sub"])}

    def revoke(self, token: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM subscriber_sessions WHERE token_hash=?", (_token_hash(token),))

    # ---- gắn số
    def claim_demo_number(self, subscriber_id: int, pool: list[str]) -> str:
        """Cấp một số giả lập chưa ai dùng (ổn định: cùng tài khoản luôn giữ số của mình)."""
        with self.db.tx() as c:
            row = c.execute("SELECT msisdn FROM subscribers WHERE id=?", (subscriber_id,)).fetchone()
            if row and row["msisdn"]:
                return row["msisdn"]
            taken = {r["msisdn"] for r in c.execute("SELECT msisdn FROM subscribers WHERE msisdn IS NOT NULL")}
            free = [n for n in pool if n not in taken]
            if not free:
                raise HTTPException(503, "Đã hết số giả lập để cấp, vui lòng thử lại sau")
            number = free[secrets.randbelow(len(free))]
            c.execute("UPDATE subscribers SET msisdn=?, msisdn_kind='demo' WHERE id=?", (number, subscriber_id))
            return number

    def otp_start(self, subscriber_id: int, phone: str, provider: OtpProvider) -> None:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self.db.tx() as c:
            owner = c.execute("SELECT id FROM subscribers WHERE msisdn=? AND id<>?", (phone, subscriber_id)).fetchone()
            if owner:
                raise HTTPException(409, "Số này đã gắn với tài khoản khác")
            c.execute("INSERT OR REPLACE INTO otp_codes VALUES (?,?,?,?,0)",
                      (phone, subscriber_id, _token_hash(f"{phone}:{code}"), now() + OTP_TTL_S))
        provider.send(phone, code)

    def otp_verify(self, subscriber_id: int, phone: str, code: str) -> str:
        # Lỗi được ghi nhận rồi mới ném SAU khi COMMIT: ném HTTPException bên trong tx() sẽ ROLLBACK
        # và xoá luôn lần đếm sai, khiến mã 6 số bị dò không giới hạn.
        failure = None
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM otp_codes WHERE phone=?", (phone,)).fetchone()
            if row is None or row["subscriber_id"] != subscriber_id or row["expires_at"] < now():
                failure = HTTPException(400, "Mã OTP không hợp lệ hoặc đã hết hạn")
            elif row["attempts"] >= OTP_MAX_ATTEMPTS:
                c.execute("DELETE FROM otp_codes WHERE phone=?", (phone,))
                failure = HTTPException(429, "Nhập sai quá nhiều lần, hãy gửi lại mã")
            elif not hmac.compare_digest(row["code_hash"], _token_hash(f"{phone}:{(code or '').strip()}")):
                c.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE phone=?", (phone,))
                if row["attempts"] + 1 >= OTP_MAX_ATTEMPTS:
                    c.execute("DELETE FROM otp_codes WHERE phone=?", (phone,))
                failure = HTTPException(400, "Mã OTP không đúng")
            else:
                c.execute("DELETE FROM otp_codes WHERE phone=?", (phone,))
                c.execute("UPDATE subscribers SET msisdn=?, msisdn_kind='verified' WHERE id=?", (phone, subscriber_id))
        if failure is not None:
            raise failure
        return phone

    def count(self) -> int:
        return int(self.db.one("SELECT COUNT(*) AS n FROM subscribers")["n"])
