from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
                             options={"require": ["sub", "exp"]})
        subject = payload["sub"]
        # User IDs are positive SQL INTEGERs. Malformed signed claims must be
        # unauthorized, not ValueError/overflow failures in the database layer.
        if (not isinstance(subject, str) or not subject.isascii() or not subject.isdecimal()
                or len(subject) > 10 or not 0 < int(subject) <= 2147483647):
            return None
        return subject
    except jwt.InvalidTokenError:
        return None
