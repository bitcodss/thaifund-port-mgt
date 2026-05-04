"""
Bootstrap: create the first admin user if no users exist.
Run once at startup after migrations.
"""
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings
from app.models.user import User, UserRole
from app.services.auth_service import hash_password


async def bootstrap() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(select(User).limit(1))
        if result.scalar_one_or_none() is not None:
            return  # users exist — nothing to do

        admin = User(
            id=uuid.uuid4(),
            email=settings.FIRST_ADMIN_EMAIL,
            password_hash=hash_password(settings.FIRST_ADMIN_PASSWORD),
            role=UserRole.ADMIN.value,
        )
        db.add(admin)
        await db.commit()
        print(f"[init_db] Created admin user: {settings.FIRST_ADMIN_EMAIL}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(bootstrap())
