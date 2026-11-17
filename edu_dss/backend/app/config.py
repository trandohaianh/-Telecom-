"""Fail-closed production configuration."""
import os
from pathlib import Path
from urllib.parse import quote


def secret(name: str) -> str:
    path = os.getenv(name + "_FILE")
    value = Path(path).read_text(encoding="utf-8").strip() if path else os.getenv(name, "")
    if not value:
        raise RuntimeError(f"{name} or {name}_FILE is required")
    return value


_jwt_secret = None


def jwt_secret() -> str:
    """Needed only by the API and the demo-token CLI. The Celery worker never verifies tokens,
    so it no longer receives the signing secret (a compromised worker could mint admin tokens)."""
    global _jwt_secret
    if _jwt_secret is None:
        value = secret("JWT_SECRET")
        # HS256 with a short secret can be brute-forced offline from any captured token. 32 characters
        # is the floor (bootstrap-local.ps1 makes 64); EDU_ALLOW_WEAK_JWT_SECRET=1 is an explicit override.
        if len(value) < 32 and os.getenv("EDU_ALLOW_WEAK_JWT_SECRET", "0") != "1":
            raise RuntimeError("JWT_SECRET must be at least 32 characters")
        _jwt_secret = value
    return _jwt_secret


JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "").strip() or None
JWT_ISSUER = os.getenv("JWT_ISSUER", "edu-dss")
REDIS_PASSWORD = secret("REDIS_PASSWORD")
REDIS_AUTH = ":" + quote(REDIS_PASSWORD, safe="") + "@"
REDIS_URL = os.getenv("REDIS_URL", f"redis://{REDIS_AUTH}redis-cache:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", f"redis://{REDIS_AUTH}redis-cache:6379/1")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", f"redis://{REDIS_AUTH}redis-cache:6379/2")
# CmsRequest allows 250,000 characters; Vietnamese UTF-8 needs up to 3-4 bytes per character.
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "1100000"))
if MAX_BODY_BYTES <= 0:
    raise RuntimeError("MAX_BODY_BYTES must be positive")
