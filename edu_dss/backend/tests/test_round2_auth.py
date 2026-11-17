"""Round-2 audit regressions: course-scoped AutoML, demo token claim, worker secret."""
import asyncio
import os
import time
from pathlib import Path

import jwt
import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 32)
os.environ.setdefault("REDIS_PASSWORD", "test-redis")

ROOT = Path(__file__).resolve().parents[1]


def _token(**claims):
    from app import config
    base = {"sub": "u1", "role": "instructor", "iss": config.JWT_ISSUER, "exp": int(time.time()) + 600}
    return jwt.encode({**base, **claims}, config.jwt_secret(), algorithm="HS256")


def _user(token):
    from fastapi.security import HTTPAuthorizationCredentials
    from app import security
    return asyncio.run(security.current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)))


def test_demo_tokens_stop_working_outside_demo_mode(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("EDU_DEMO_MODE", "0")
    with pytest.raises(HTTPException):
        _user(_token(demo=True))
    monkeypatch.setenv("EDU_DEMO_MODE", "1")
    assert _user(_token(demo=True))["sub"] == "u1"
    assert _user(_token())["sub"] == "u1"


def test_automl_requires_a_taught_course():
    from pydantic import ValidationError
    from fastapi import HTTPException
    from app import main
    with pytest.raises(ValidationError):
        main.TrainingRequest(dataset_ref="d", target="y")              # course_id is now required
    body = main.TrainingRequest(course_id="CS101", dataset_ref="d", target="y")
    user = {"sub": "u1", "role": main.Role.INSTRUCTOR, "course_ids": frozenset(),
            "teaching_course_ids": frozenset({"MA201"})}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.start_training(body, user))
    assert exc.value.status_code == 404


def test_worker_does_not_receive_the_jwt_secret():
    compose = (ROOT.parent / "docker-compose.yml").read_text(encoding="utf-8")
    worker = compose[compose.index("  celery-worker:"):compose.index("\nsecrets:")]
    assert "jwt_secret" not in worker and "JWT_SECRET" not in worker


def test_body_limit_fits_the_schema_in_vietnamese():
    from app import config
    assert config.MAX_BODY_BYTES >= 250_000 * 4
    nginx = (ROOT.parent / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 1100k" in nginx and "$proxy_add_x_forwarded_for" not in nginx


def test_short_jwt_secret_is_refused(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "_jwt_secret", None)
    monkeypatch.setenv("JWT_SECRET", "short")
    monkeypatch.delenv("JWT_SECRET_FILE", raising=False)
    with pytest.raises(RuntimeError, match="32"):
        config.jwt_secret()
    monkeypatch.setenv("EDU_ALLOW_WEAK_JWT_SECRET", "1")
    assert config.jwt_secret() == "short"
    monkeypatch.setattr(config, "_jwt_secret", None)
