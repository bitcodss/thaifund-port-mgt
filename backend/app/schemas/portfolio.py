from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class PortfolioCreate(BaseModel):
    name: str


class PortfolioUpdate(BaseModel):
    name: str


class PortfolioOut(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}
