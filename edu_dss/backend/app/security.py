"""JWT role authorization. User provisioning and signing belong to the IdP."""
from datetime import datetime, timezone
import os
from enum import StrEnum

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import JWT_AUDIENCE, JWT_ISSUER, jwt_secret


class Role(StrEnum):
    INSTRUCTOR = "instructor"
    STUDENT = "student"
    ADMIN = "admin"


bearer = HTTPBearer(auto_error=False)


def _string_claim_set(claims, name):
    value = claims.get(name, [])
    if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings")
    return frozenset(value)


async def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if credentials is None:
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        required = ["sub", "exp", "iss", "role"] + (["aud"] if JWT_AUDIENCE else [])
        claims = jwt.decode(credentials.credentials, jwt_secret(), algorithms=["HS256"],
                            issuer=JWT_ISSUER, audience=JWT_AUDIENCE,
                            options={"require": required, "verify_aud": JWT_AUDIENCE is not None})
        if claims.get("demo") and os.getenv("EDU_DEMO_MODE", "0").strip() != "1":
            # Demo tokens are signed with the real secret; they must stop working once demo mode is off.
            raise ValueError("demo token outside demo mode")
        role = Role(claims["role"])
        if "iat" in claims and claims["iat"] > int(datetime.now(timezone.utc).timestamp()) + 30:
            raise ValueError("iat in the future")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    try:
        course_ids = _string_claim_set(claims, "course_ids")
        teaching_course_ids = _string_claim_set(claims, "teaching_course_ids")
    except ValueError:
        raise HTTPException(401, "Invalid course authorization claims",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"sub": claims["sub"], "role": role, "scopes": claims.get("scope", []),
            "course_ids": course_ids, "teaching_course_ids": teaching_course_ids}


def require_roles(*roles: Role):
    async def dependency(user=Depends(current_user)):
        if user["role"] not in roles:
            raise HTTPException(403, "Insufficient role")
        return user
    return dependency
