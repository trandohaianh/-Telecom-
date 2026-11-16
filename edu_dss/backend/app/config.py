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


JWT_SECRET = secret("JWT_SECRET")
JWT_ISSUER = os.getenv("JWT_ISSUER", "edu-dss")
REDIS_PASSWORD = secret("REDIS_PASSWORD")
REDIS_AUTH = ":" + quote(REDIS_PASSWORD, safe="") + "@"
REDIS_URL = os.getenv("REDIS_URL", f"redis://{REDIS_AUTH}redis-cache:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", f"redis://{REDIS_AUTH}redis-cache:6379/1")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", f"redis://{REDIS_AUTH}redis-cache:6379/2")
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "300000"))
if MAX_BODY_BYTES <= 0:
    raise RuntimeError("MAX_BODY_BYTES must be positive")
