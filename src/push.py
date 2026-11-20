"""Web Push (VAPID) to subscriber PWAs — owner decision 2026-11-17 ("Web Push + SMS HTTP").

Before this, a subscriber was only alerted while the app tab was open. A standard Web Push message
reaches the installed PWA (Chrome/Edge/Firefox desktop + Android, Safari 16.4+ on a home-screen app)
without a Firebase account. Limits: the Android WebView shell cannot receive Web Push; an iOS browser
tab cannot either (only an installed home-screen app).

Privacy: the payload carries NO phone number or SMS text — only the verdict level and an action line.
The full alert stays behind the authenticated /api/app/inbox.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("fraud.push")

ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = Path(os.environ.get("FRAUD_VAPID_PRIVATE_KEY_FILE") or ROOT / "secrets" / "vapid_private.pem")
SUBJECT = os.environ.get("FRAUD_VAPID_SUBJECT") or "mailto:security@example.invalid"
MIN_INTERVAL_S = 10.0  # at most one push per subscription per 10 s (bursts collapse)

try:  # optional dependency: the API works without it, push is just disabled
    from py_vapid import Vapid01
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - exercised when the package is absent
    Vapid01 = None
    webpush = None
    WebPushException = Exception


def available() -> bool:
    return webpush is not None and os.environ.get("FRAUD_PUSH", "1") != "0"


def _vapid():
    if not available():
        return None
    try:
        return _load_or_create_key()
    except OSError:
        # e.g. the image's code directory is read-only for the service user; push is then disabled (503),
        # never a 500. Containers set FRAUD_VAPID_PRIVATE_KEY_FILE to the persistent state volume.
        logger.warning("web push disabled: cannot read or create %s", KEY_FILE, exc_info=True)
        return None


def _load_or_create_key():
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        v = Vapid01()
        v.generate_keys()
        v.save_key(str(KEY_FILE))
        try:
            os.chmod(KEY_FILE, 0o600)
        except OSError:
            pass
    return Vapid01.from_file(str(KEY_FILE))


def public_key() -> str | None:
    """applicationServerKey for PushManager.subscribe (uncompressed P-256 point, base64url)."""
    v = _vapid()
    if v is None:
        return None
    from cryptography.hazmat.primitives import serialization

    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def validate_subscription(sub: dict) -> dict:
    """Accept only a well-formed W3C PushSubscription with an https endpoint (no SSRF to internal hosts)."""
    if not isinstance(sub, dict):
        raise ValueError("subscription must be an object")
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        raise ValueError("invalid endpoint")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or host in ("localhost",)
        or host.endswith((".local", ".internal"))
        or host.replace(".", "").isdigit()
        or ":" in host
    ):
        raise ValueError("endpoint must be a public https push service")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    for value, lo, hi in ((p256dh, 80, 100), (auth, 16, 32)):
        if not isinstance(value, str) or not lo <= len(value) <= hi:
            raise ValueError("invalid subscription keys")
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}


def payload_for(item: dict) -> dict:
    level = item.get("decision") or item.get("level") or "FLAG"
    title = {
        "BLOCK": "Đã chặn cuộc gọi/tin nhắn lừa đảo",
        "FLAG": "Cảnh báo: có dấu hiệu lừa đảo",
        "FAMILY": "Người thân của bạn vừa gặp lừa đảo",
    }.get(item.get("type") or level, "Cảnh báo lừa đảo")
    body = str(item.get("action") or "Mở app để xem chi tiết.")[:120]
    return {"title": title, "body": body, "url": "/app/", "tag": "fraud-alert"}


class PushSender:
    """Background sender: never blocks the inference owner or the event loop."""

    def __init__(self, db_getter, send=None):
        self.db = db_getter
        self.q: queue.Queue = queue.Queue(maxsize=10_000)
        self._last: dict[str, float] = {}
        self._send = send or self._webpush
        self.stats = {"sent": 0, "expired": 0, "failed": 0, "dropped": 0, "throttled": 0}
        threading.Thread(target=self._run, name="web-push", daemon=True).start()

    def notify(self, msisdn: str, item: dict) -> None:
        try:
            self.q.put_nowait((msisdn, payload_for(item)))
        except queue.Full:
            self.stats["dropped"] += 1

    def _webpush(self, sub: dict, data: dict) -> None:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(data, ensure_ascii=False),
            vapid_private_key=str(KEY_FILE),
            vapid_claims={"sub": SUBJECT},
            ttl=3600,
            timeout=10,
        )

    def deliver(self, msisdn: str, data: dict) -> None:
        rows = self.db().query(
            "SELECT p.id, p.endpoint, p.p256dh, p.auth FROM push_subscriptions p "
            "JOIN subscribers s ON s.id = p.subscriber_id WHERE s.msisdn=? AND s.disabled_at IS NULL",
            (msisdn,),
        )
        now = time.time()
        for r in rows:
            if now - self._last.get(r["endpoint"], 0.0) < MIN_INTERVAL_S:
                self.stats["throttled"] += 1
                continue
            self._last[r["endpoint"]] = now
            try:
                self._send(dict(r), data)
                self.stats["sent"] += 1
            except WebPushException as exc:  # 404/410: the browser dropped the subscription
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    with self.db().tx() as c:
                        c.execute("DELETE FROM push_subscriptions WHERE id=?", (r["id"],))
                    self.stats["expired"] += 1
                else:
                    self.stats["failed"] += 1
            except Exception:
                self.stats["failed"] += 1
                logger.warning("web push delivery failed", exc_info=True)

    def _run(self):
        while True:
            msisdn, data = self.q.get()
            try:
                self.deliver(msisdn, data)
            except Exception:
                logger.warning("web push worker error", exc_info=True)
