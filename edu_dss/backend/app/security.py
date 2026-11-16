"""JWT role authorization. User provisioning and signing belong to the IdP."""
from datetime import datetime, timezone
from enum import StrEnum

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import JWT_ISSUER, JWT_SECRET


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
        claims = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"],
                            issuer=JWT_ISSUER, options={"require": ["sub", "exp", "iss", "role"]})
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
