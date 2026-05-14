import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.portfolio import Portfolio
from app.models.user import User
from app.schemas.user import SelfUserUpdate, UserCreate, UserOut, UserUpdate
from app.api.deps import require_admin, get_current_user
from app.services.auth_service import hash_password, verify_password
from app.services.portfolio_service import invalidate_portfolio

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: SelfUserUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Self-service update: date_of_birth and/or password.

    Password changes require `current_password` to match — defends against
    session-token theft turning into an account lockout. Admin user mutation
    happens via /users/{id} and doesn't have this gate.
    """
    if body.password is not None:
        if not body.current_password or not verify_password(body.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        user.password_hash = hash_password(body.password)
    dob_changed = body.date_of_birth is not None and body.date_of_birth != user.date_of_birth
    if body.date_of_birth is not None:
        user.date_of_birth = body.date_of_birth
    await db.commit()
    await db.refresh(user)

    # DOB feeds RMF age-55 eligibility. A change must invalidate the analytics
    # cache for every portfolio this user owns — otherwise stale eligibility
    # data is served for up to 5 minutes.
    if dob_changed:
        portfolios = await db.execute(select(Portfolio.id).where(Portfolio.user_id == user.id))
        for (pid,) in portfolios.all():
            invalidate_portfolio(pid)
    return user


@router.get("", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    result = await db.execute(select(User))
    return result.scalars().all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        id=uuid.uuid4(),
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        date_of_birth=body.date_of_birth,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.email is not None:
        user.email = body.email
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    if body.role is not None:
        user.role = body.role
    if body.date_of_birth is not None:
        user.date_of_birth = body.date_of_birth
    if body.is_active is not None:
        user.is_active = body.is_active
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    await db.commit()
