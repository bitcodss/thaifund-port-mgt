from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, field_validator


# bcrypt silently truncates inputs longer than 72 bytes — meaning two different
# long passwords sharing their first 72 bytes hash to the same value. Reject
# at the boundary instead.
_BCRYPT_MAX_BYTES = 72


def _check_password_bytes(v: str | None) -> str | None:
    if v is None:
        return v
    if len(v.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise ValueError(
            f"password too long ({len(v.encode('utf-8'))} bytes); "
            f"bcrypt accepts at most {_BCRYPT_MAX_BYTES} UTF-8 bytes"
        )
    return v


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "user"
    date_of_birth: date | None = None

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)  # type: ignore[return-value]


class UserUpdate(BaseModel):
    """Admin-side user mutation. Admins don't need a current-password gate."""
    email: EmailStr | None = None
    password: str | None = None
    role: str | None = None
    date_of_birth: date | None = None
    is_active: bool | None = None

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str | None) -> str | None:
        return _check_password_bytes(v)


class SelfUserUpdate(BaseModel):
    """Self-service updates via /users/me. Password change requires the
    current password to defend against session-token theft → account takeover."""
    password: str | None = None
    current_password: str | None = None
    date_of_birth: date | None = None

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str | None) -> str | None:
        return _check_password_bytes(v)


class UserOut(BaseModel):
    id: UUID
    email: str
    role: str
    date_of_birth: date | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
