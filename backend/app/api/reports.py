"""Monthly fund reports (Phase 3): list, read, and (admin) build."""
import uuid
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.database import AsyncSessionLocal, get_db
from app.models.market import MonthlyReport
from app.models.user import User, UserRole
from app.services import peer_metrics, report_builder
from app.services.clock import today_ict

router = APIRouter(prefix="/reports", tags=["reports"])


def _target(user: User, user_id: uuid.UUID | None) -> uuid.UUID:
    if user_id and user_id != user.id and user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user_id or user.id


@router.get("")
async def list_reports(user_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    rows = (await db.execute(select(MonthlyReport).where(MonthlyReport.user_id == _target(user, user_id))
                             .order_by(MonthlyReport.month.desc()))).scalars().all()
    return [{"month": str(r.month), "status": r.status, "updated_at": r.updated_at,
             "errors": len(r.errors or [])} for r in rows]


@router.get("/{month}")
async def get_report(month: date, user_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    row = (await db.execute(select(MonthlyReport).where(
        MonthlyReport.user_id == _target(user, user_id),
        MonthlyReport.month == peer_metrics.month_end(month)))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No report for this month")
    return {"month": str(row.month), "status": row.status, "html": row.html, "errors": row.errors or [],
            "updated_at": row.updated_at}


class BuildBody(BaseModel):
    month: date | None = None          # default: last complete month
    user_id: uuid.UUID | None = None   # default: every user holding funds


async def _build(month: date, user_ids: list | None) -> None:
    async with AsyncSessionLocal() as db:
        await report_builder.build_reports(db, month, user_ids)


@router.post("/build", status_code=202)
async def build(body: BuildBody, tasks: BackgroundTasks, _=Depends(require_admin)):
    """Build reports in the background (LLM: ~1 call per fund + 1 overview per user, with retries)."""
    month = peer_metrics.month_end(body.month) if body.month else peer_metrics.last_complete_month(today_ict())
    tasks.add_task(_build, month, [body.user_id] if body.user_id else None)
    return {"month": str(month), "started": True}
