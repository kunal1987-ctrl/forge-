"""FastAPI router for engine operational state and status probes.

Endpoints:
- GET /api/engine/status: Lightweight endpoint returning server connection, repository ingestion, and workflow execution states.
"""

from __future__ import annotations

import logging
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.core.database import get_db
from services.api.forge.orchestration.worker import RUNNING_TASKS
from services.api.models.code import CodeChunk
from services.api.models.repository import Repository
from services.api.models.task import Task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engine", tags=["Engine Status"])


class EngineStatusResponse(BaseModel):
    """Normalized engine operational status schema."""

    is_server_connected: bool = Field(
        True, description="True if the backend API service is reachable"
    )
    repo_connected: bool = Field(
        False, description="True if a valid repository has been ingested or loaded"
    )
    task_active: bool = Field(
        False, description="True if an autonomous task workflow is currently executing"
    )
    active_tasks_count: int = Field(
        0, description="Number of currently executing background tasks"
    )
    repo_id: Optional[str] = Field(
        None, description="Active or verified repository UUID if connected"
    )


@router.get(
    "/status",
    response_model=EngineStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get real-time operational engine status",
)
async def get_engine_status(
    repo_id: Optional[str] = Query(
        None, description="Optional target repository UUID or identifier to check"
    ),
    task_id: Optional[str] = Query(
        None, description="Optional task ID to verify active execution"
    ),
    db: AsyncSession = Depends(get_db),
) -> EngineStatusResponse:
    """Retrieve true operational state for server, repository, and active tasks.

    Returns:
        EngineStatusResponse with is_server_connected, repo_connected, and task_active.
    """
    repo_connected = False
    task_active = False
    active_tasks_count = 0
    resolved_repo_id: Optional[str] = None

    # 1. Determine active workflow execution state
    # A) Check in-memory running tasks
    active_memory_tasks = [t for t in RUNNING_TASKS.values() if not t.done()]
    if active_memory_tasks:
        task_active = True
        active_tasks_count = len(active_memory_tasks)

    # B) Check specific task ID if supplied
    if task_id and not task_active:
        if task_id in RUNNING_TASKS and not RUNNING_TASKS[task_id].done():
            task_active = True
            active_tasks_count = 1

    # C) Check database for active execution states
    try:
        active_statuses = (
            "pending",
            "running",
            "planning",
            "investigating",
            "implementing",
            "verifying",
            "awaiting_approval",
        )
        task_stmt = select(func.count(Task.id)).where(Task.status.in_(active_statuses))
        task_res = await db.execute(task_stmt)
        db_active_count = task_res.scalar() or 0
        if db_active_count > 0:
            task_active = True
            active_tasks_count = max(active_tasks_count, db_active_count)
    except Exception as db_task_err:
        logger.debug("Database active tasks probe skipped or failed gracefully: %s", db_task_err)

    # 2. Determine repository connection / ingestion state
    try:
        # If a specific repo_id is provided
        if repo_id and repo_id.strip().lower() not in ("default-repo", "none", "null", ""):
            try:
                repo_uuid = uuid.UUID(repo_id.strip())
                repo_stmt = select(Repository).where(Repository.id == repo_uuid)
                repo_res = await db.execute(repo_stmt)
                repo_obj = repo_res.scalar_one_or_none()
                if repo_obj:
                    repo_connected = True
                    resolved_repo_id = str(repo_obj.id)
            except ValueError:
                # Non-UUID string repo identifier check by name
                repo_stmt = select(Repository).where(Repository.name == repo_id.strip())
                repo_res = await db.execute(repo_stmt)
                repo_obj = repo_res.scalar_one_or_none()
                if repo_obj:
                    repo_connected = True
                    resolved_repo_id = str(repo_obj.id)

        # If repo_id was not provided or not found, check if ANY ingested repository exists
        if not repo_connected:
            # Check for non-default repositories
            repo_stmt = select(Repository).where(Repository.name != "default-repo").limit(1)
            repo_res = await db.execute(repo_stmt)
            ingested_repo = repo_res.scalar_one_or_none()
            if ingested_repo:
                repo_connected = True
                resolved_repo_id = str(ingested_repo.id)
            else:
                # Check if any indexed code chunks exist in database
                chunk_stmt = select(func.count(CodeChunk.id))
                chunk_res = await db.execute(chunk_stmt)
                chunk_count = chunk_res.scalar() or 0
                if chunk_count > 0:
                    repo_connected = True
    except Exception as db_repo_err:
        logger.debug("Database repository connection probe failed gracefully: %s", db_repo_err)

    return EngineStatusResponse(
        is_server_connected=True,
        repo_connected=repo_connected,
        task_active=task_active,
        active_tasks_count=active_tasks_count,
        repo_id=resolved_repo_id or repo_id,
    )
