"""Issue short-lived local demo JWTs; deliberately disabled outside demo mode."""
import os
import time

import jwt

from .config import JWT_AUDIENCE, JWT_ISSUER, jwt_secret


def main() -> None:
    if os.getenv("EDU_DEMO_MODE", "0") != "1":
        raise SystemExit("Refusing to issue demo tokens unless EDU_DEMO_MODE=1")
    now = int(time.time())
    for role in ("instructor", "student", "admin"):
        claims = {"sub": f"demo-{role}", "role": role, "iss": JWT_ISSUER,
                  "iat": now, "exp": now + 3600, "demo": True}
        if JWT_AUDIENCE:
            claims["aud"] = JWT_AUDIENCE
        if role == "student":
            claims["course_ids"] = ["demo-course"]
        if role == "instructor":
            claims["teaching_course_ids"] = ["demo-course"]
        print(f"{role}: {jwt.encode(claims, jwt_secret(), algorithm='HS256')}")


if __name__ == "__main__":
    main()
