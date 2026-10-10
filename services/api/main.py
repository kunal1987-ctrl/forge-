"""Main FastAPI application entry point for FORGE.

Initializes FastAPI, registers CORS middleware for Vite frontend integration,
and mounts sub-routers.
"""

from fastapi import Depends, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.core.database import get_db
from services.api.routers.tasks import router as tasks_router
from services.api.routers.repositories import router as repositories_router

app = FastAPI(
    title="FORGE Autonomous AI Platform API",
    version="0.1.0",
    description="Backend API for FORGE autonomous software engineering platform",
)

# Configure CORS to allow Vite frontend dev server (port 5173)
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

# Register routers
app.include_router(tasks_router)
app.include_router(repositories_router)



@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
async def health_check():
    """Health check endpoint confirming API service status."""
    return {
        "status": "healthy",
        "service": "forge-api",
        "version": "0.1.0",
    }


@app.get("/health/db", status_code=status.HTTP_200_OK, tags=["Health"])
async def db_health_check(db: AsyncSession = Depends(get_db)):
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
