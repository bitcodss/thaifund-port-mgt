from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "user"
    date_of_birth: date | None = None


class UserUpdate(BaseModel):
    """Admin-side user mutation. Admins don't need a current-password gate."""
    email: EmailStr | None = None
    password: str | None = None
    role: str | None = None
    date_of_birth: date | None = None
    is_active: bool | None = None


class SelfUserUpdate(BaseModel):
    """Self-service updates via /users/me. Password change requires the
    current password to defend against session-token theft → account takeover."""
    password: str | None = None
    current_password: str | None = None
    date_of_birth: date | None = None


class UserOut(BaseModel):
    id: UUID
    email: str
    role: str
    date_of_birth: date | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
