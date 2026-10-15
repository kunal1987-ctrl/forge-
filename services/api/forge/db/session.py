from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
import logging
import os
from typing import AsyncGenerator

from services.api.models import Base

logger = logging.getLogger("forge.db.session")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://forge_user:password@postgres:5432/forge_production",
)

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield database session from AsyncSessionLocal."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database: create pgvector extension and auto-create all tables."""
    logger.info("Initializing database and verifying pgvector extension...")
    async with engine.begin() as conn:
        # Ensure pgvector extension exists
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            logger.info("pgvector extension verified/created successfully.")
        except Exception:
            try:
                await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector;")
            except Exception as ext_err:
                logger.warning(
                    "Could not execute CREATE EXTENSION vector (might already exist or unsupported in dialect): %s",
                    ext_err,
                )

        # Ensure all tables are created automatically on boot
        try:
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database pgvector tables verified/created successfully.")
        except Exception as tbl_err:
            logger.warning("Could not run Base.metadata.create_all: %s", tbl_err)

        # Migrate code_chunks schema safely for AST symbol viewer
        try:
            await conn.execute(text("ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS line_number INTEGER;"))
            await conn.execute(text("ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS end_line INTEGER;"))
        except Exception:
            pass
