from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "user"
    date_of_birth: date | None = None


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    password: str | None = None
    role: str | None = None
    date_of_birth: date | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    id: UUID
    email: str
    role: str
    date_of_birth: date | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
