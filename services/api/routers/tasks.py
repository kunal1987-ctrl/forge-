"""FastAPI tasks router handling creation, SSE event streaming, and approval.

Endpoints:
- POST /api/tasks: Creates task and launches background execution worker.
- GET /api/tasks/{id}/events: Server-Sent Events stream from Redis Pub/Sub.
- POST /api/tasks/{id}/approve: Approves task changes and transitions graph to complete.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from services.api.core.database import async_session_factory, get_db
from services.api.forge.orchestration.worker import run_task_and_publish
from services.api.models.repository import Repository
from services.api.models.task import Task, TaskEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


class TaskCreateRequest(BaseModel):
    """Payload to initiate an autonomous repair/coding task."""

    prompt: str = Field(..., description="Goal and instructions for the task", min_length=1)
    repo_id: Optional[str] = Field(None, description="Optional target repository UUID")


class TaskCreateResponse(BaseModel):
    """Response returned upon task initialization."""

    task_id: str
    status: str
    repo_id: str


@router.post("", response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    request: TaskCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> TaskCreateResponse:
    """Create a new task, persist it in PostgreSQL, and trigger the background agent.

    Args:
        request: Task creation parameters containing prompt and optional repo_id.
        background_tasks: FastAPI background tasks queue.
        db: Active AsyncSession database session.

    Returns:
        TaskCreateResponse with task_id and initial status.
    """
    # 1. Resolve or create a default repository if needed
    repo: Optional[Repository] = None
    if request.repo_id:
        try:
            repo_uuid = uuid.UUID(request.repo_id)
            res = await db.execute(select(Repository).where(Repository.id == repo_uuid))
            repo = res.scalar_one_or_none()
        except ValueError:
            pass

    if not repo:
        res = await db.execute(select(Repository).limit(1))
        repo = res.scalar_one_or_none()
        if not repo:
            repo = Repository(
                id=uuid.uuid4(),
                name="default-repo",
                local_path=os.getcwd(),
                default_branch="main",
            )
            db.add(repo)
            await db.flush()

    # 2. Persist Task
    task_id = uuid.uuid4()
    branch_name = f"forge/task-{task_id}"
    task = Task(
        id=task_id,
        repo_id=repo.id,
        prompt=request.prompt,
        status="pending",
        branch_name=branch_name,
        retry_count=0,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    # 3. Dispatch worker to background
    background_tasks.add_task(
        run_task_and_publish,
        str(task.id),
        request.prompt,
        str(repo.id),
    )

    logger.info("Created Task '%s' for repository '%s'", task.id, repo.id)
    return TaskCreateResponse(
        task_id=str(task.id),
        status="pending",
        repo_id=str(repo.id),
    )


@router.get("/{id}/events")
async def get_task_events(id: str, request: Request) -> EventSourceResponse:
    """Stream live task execution events via Server-Sent Events (SSE).

    Subscribes to Redis Pub/Sub channel 'task_events:{id}' and yields incoming
    messages to connected browser clients.

    Args:
        id: Task UUID string.
        request: FastAPI HTTP request instance to monitor client disconnects.

    Returns:
        EventSourceResponse streaming JSON event chunks.
    """
    async def event_generator() -> AsyncGenerator[Dict[str, str], None]:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        pubsub = redis_client.pubsub()
        channel = f"task_events:{id}"
        await pubsub.subscribe(channel)

        # First, replay any historical events recorded in the database
        try:
            async with async_session_factory() as session:
                task_uuid = uuid.UUID(id)
                stmt = select(TaskEvent).where(TaskEvent.task_id == task_uuid).order_by(TaskEvent.sequence)
                res = await session.execute(stmt)
                past_events = res.scalars().all()
                for past_evt in past_events:
                    yield {"data": json.dumps(past_evt.payload)}
        except Exception as db_err:
            logger.debug("Could not replay historical events: %s", db_err)

        try:
            while True:
                if await request.is_disconnected():
                    logger.info("Client disconnected from task SSE stream: %s", id)
                    break

                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    raw_data = message.get("data")
                    if raw_data:
                        yield {"data": raw_data}

                        # Check if completion event reached
                        try:
                            parsed = json.loads(raw_data)
                            if parsed.get("status") in ("completed", "failed"):
                                break
                        except Exception:
                            pass

                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.info("SSE connection cancelled for task %s", id)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await redis_client.aclose()

    return EventSourceResponse(event_generator())


@router.post("/{id}/approve")
async def approve_task(
    id: str,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Approve task changes from 'awaiting_approval' to complete and notify subscribers.

    Args:
        id: Task UUID string.
        db: Active AsyncSession database session.

    Returns:
        JSON response with updated task_id and status.
    """
    try:
        task_uuid = uuid.UUID(id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task UUID format")

    res = await db.execute(select(Task).where(Task.id == task_uuid))
    task = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{id}' not found")

    task.status = "completed"
    await db.commit()

    # Publish approval event to Redis Pub/Sub
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        completion_payload = {
            "step": 5,
            "message": "[Approved] Patch approved by engineer. Changes merged.",
            "status": "completed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await redis_client.publish(f"task_events:{id}", json.dumps(completion_payload))
    finally:
        await redis_client.aclose()

    logger.info("Task '%s' approved and marked completed.", id)
    return {"task_id": id, "status": "completed"}
