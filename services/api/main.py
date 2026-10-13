"""Main FastAPI application entry point for FORGE.

Initializes FastAPI, configures OpenTelemetry distributed tracing,
registers correlation ID & structured logging middleware, registers global
exception handlers, and mounts sub-routers.
"""

from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.forge.exceptions import register_exception_handlers
from services.api.forge.logging.logger import (
    CorrelationIdMiddleware,
    configure_structlog,
)

# Explicitly load .env from project root
env_file = Path(__file__).resolve().parents[2] / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file)
else:
    load_dotenv()

try:
    from services.api.core.telemetry import setup_telemetry
except ImportError:
    from core.telemetry import setup_telemetry  # type: ignore[no-redef]

from services.api.core.database import get_db  # noqa: E402
from services.api.routers.repositories import router as repositories_router  # noqa: E402
from services.api.routers.retrieval import router as retrieval_router  # noqa: E402
from services.api.routers.tasks import router as tasks_router  # noqa: E402
from services.api.forge.routers.health import router as health_router  # noqa: E402
from services.api.forge.routers.status import router as status_router  # noqa: E402
from services.api.forge.routers.rag import router as rag_router  # noqa: E402

app = FastAPI(
    title="FORGE Autonomous AI Platform API",
    version="0.1.0",
    description="Backend API for FORGE autonomous software engineering platform",
)

# Configure standardized JSON structlog logging pipeline
configure_structlog()

# Register global exception handlers for all custom exceptions
register_exception_handlers(app)

# Initialize OpenTelemetry distributed tracing and background TelemetryDispatcher
setup_telemetry(app)
from services.api.forge.telemetry.dispatcher import setup_dispatcher_lifecycle  # noqa: E402
setup_dispatcher_lifecycle(app)

# Configure CORS to allow Vite frontend dev server (port 5173) and any local origins
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add Correlation ID and Structured Logging Middleware
app.add_middleware(CorrelationIdMiddleware)

# Register routers
app.include_router(tasks_router)
app.include_router(repositories_router)
app.include_router(retrieval_router)
app.include_router(health_router)
app.include_router(status_router)
app.include_router(rag_router)


@app.get("/health/db", status_code=status.HTTP_200_OK, tags=["Health"])
async def db_health_check(db: AsyncSession = Depends(get_db)) -> Any:
    """Database connectivity health check."""
    try:
        result = await db.execute(text("SELECT 1"))
        scalar = result.scalar()
        return {
            "status": "healthy",
            "database": "connected",
            "result": scalar,
        }
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "database": "disconnected", "error": str(exc)},
        )
