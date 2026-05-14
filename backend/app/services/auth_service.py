import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import jwt, JWTError, ExpiredSignatureError

from app.config import settings

logger = logging.getLogger(__name__)


class TokenExpired(Exception):
    """Token has a valid signature but is past `exp`. Caller can distinguish
    from a hard-invalid token to give a clearer 'session expired' message."""


class TokenInvalid(Exception):
    """Token failed signature, structure, or claim validation."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: Any) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(subject), "exp": expire},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def decode_token(token: str) -> str | None:
    """Decode and return the `sub` claim. Returns None for any failure
    (back-compat); callers wanting to distinguish expired-vs-invalid should
    use decode_token_strict()."""
    try:
        return decode_token_strict(token)
    except (TokenExpired, TokenInvalid):
        return None


def decode_token_strict(token: str) -> str:
    """Like decode_token but raises TokenExpired or TokenInvalid to let the
    API layer return distinct error messages."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except ExpiredSignatureError as e:
        raise TokenExpired(str(e)) from e
    except JWTError as e:
        raise TokenInvalid(str(e)) from e
    sub = payload.get("sub")
    if not sub:
        raise TokenInvalid("missing sub claim")
    return sub
