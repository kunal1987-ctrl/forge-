"""FastAPI tasks router handling creation, SSE event streaming, and approval.

Endpoints:
- POST /api/tasks: Accepts TaskCreate(prompt: str) and fires run_graph_and_publish in BackgroundTasks.
- GET /api/tasks/{id}/events: Connects to Redis and streams events from 'forge:events:{id}' via SSE.
- POST /api/tasks/{id}/approve: Resumes the LangGraph interrupt via Command(resume='approve').
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import sys
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from langgraph.types import Command
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from services.api.core.database import async_session_factory, get_db, DATABASE_URL
from services.api.forge.orchestration.graph import get_compiled_graph
from services.api.forge.orchestration.nodes import cleanup_node
from services.api.forge.orchestration.worker import RUNNING_TASKS, run_graph_and_publish
from services.api.forge.telemetry.tracer import init_trace_context
from services.api.models.repository import Repository
from services.api.models.task import Task, TaskEvent

# Ensure Windows selector event loop on win32 for psycopg on older Python versions
if sys.platform == "win32" and sys.version_info < (3, 14):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
CLEAN_DATABASE_URL = os.getenv("DATABASE_URL", DATABASE_URL)


class TaskCreate(BaseModel):
    """Payload to initiate an autonomous repair or development task."""

    prompt: str = Field(..., description="Goal and instructions for the task", min_length=1)
    repo_id: Optional[str] = Field(None, description="Optional target repository UUID")
    mode: str = Field(
        "fast", description="LLM execution mode: 'fast' (gpt-4o-mini) or 'deep' (deepseek-r1)"
    )


class TaskCreateResponse(BaseModel):
    """Response returned upon task creation."""

    task_id: str
    status: str = "pending"
    repo_id: Optional[str] = None
    mode: str = "fast"
    trace_token: Optional[str] = None


class TaskApproveRequest(BaseModel):
    """Optional payload for reviewer decision."""

    decision: str = "approve"
    feedback: Optional[str] = None


@router.post("", response_model=TaskCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_task(
    task_in: TaskCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> TaskCreateResponse:
    """Create a new autonomous task and trigger run_graph_and_publish in the background.

    Args:
        task_in: TaskCreate payload containing prompt and optional repo_id.
        background_tasks: FastAPI background tasks queue.
        db: Active AsyncSession database session.

    Returns:
        TaskCreateResponse with the generated task_id and status.
    """
    task_id = str(uuid.uuid4())
    repo_uuid: Optional[uuid.UUID] = None

    # Persist Task record in PostgreSQL if available
    try:
        if task_in.repo_id:
            try:
                repo_uuid = uuid.UUID(task_in.repo_id)
            except ValueError:
                pass

        if not repo_uuid:
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
            repo_uuid = repo.id

        task = Task(
            id=uuid.UUID(task_id),
            repo_id=repo_uuid,
            prompt=task_in.prompt,
            status="pending",
            branch_name=f"forge/task-{task_id}",
            retry_count=0,
        )
        db.add(task)
        await db.commit()
    except Exception as db_err:
        logger.debug("Database task persistence skipped or failed gracefully: %s", db_err)

    # Extract or generate correlation trace token
    trace_token = (
        request.headers.get("x-trace-id")
        or request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or getattr(request.state, "correlation_id", None)
        or init_trace_context()
    )

    effective_repo_id = str(repo_uuid) if repo_uuid else (task_in.repo_id or "default-repo")
    dispatched_celery = False

    # Trigger Celery background worker asynchronously
    try:
        from services.api.forge.worker.celery_app import execute_agent_task

        execute_agent_task.apply_async(
            kwargs={
                "task_id": task_id,
                "repo_id": effective_repo_id,
                "prompt": task_in.prompt,
                "mode": task_in.mode,
                "correlation_id": trace_token,
            }
        )
        dispatched_celery = True
        logger.info(
            "Dispatched execute_agent_task via Celery background worker for task_id='%s' (trace_token='%s')",
            task_id,
            trace_token,
        )
    except Exception as celery_err:
        logger.warning(
            "Celery background worker dispatch unavailable or failed (%s); falling back to direct background task runner",
            celery_err,
        )

    if not dispatched_celery:
        coro_or_result = run_graph_and_publish(
            task_id=task_id,
            prompt=task_in.prompt,
            db_uri=CLEAN_DATABASE_URL,
            redis_uri=REDIS_URL,
            mode=task_in.mode,
            repo_id=effective_repo_id,
        )
        if asyncio.iscoroutine(coro_or_result):
            bg_task = asyncio.create_task(coro_or_result)
            RUNNING_TASKS[task_id] = bg_task
        else:
            background_tasks.add_task(
                run_graph_and_publish,
                task_id=task_id,
                prompt=task_in.prompt,
                db_uri=CLEAN_DATABASE_URL,
                redis_uri=REDIS_URL,
                mode=task_in.mode,
                repo_id=effective_repo_id,
            )
        logger.info(
            "Dispatched run_graph_and_publish for task_id='%s' (mode='%s')", task_id, task_in.mode
        )

    return TaskCreateResponse(
        task_id=task_id,
        status="pending",
        repo_id=str(repo_uuid) if repo_uuid else None,
        mode=task_in.mode,
        trace_token=trace_token,
    )


@router.get("/{id}/events")
async def get_task_events(id: str, request: Request) -> EventSourceResponse:
    """Stream live task execution events via Server-Sent Events (SSE).

    Connects to Redis, subscribes to 'forge:events:{id}', and yields messages.

    Args:
        id: Task string identifier (thread_id).
        request: FastAPI HTTP request instance to monitor client disconnects.

    Returns:
        EventSourceResponse streaming JSON event chunks.
    """

    async def event_generator() -> AsyncGenerator[Dict[str, str], None]:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        pubsub = redis_client.pubsub()
        channel = f"forge:events:{id}"
        await pubsub.subscribe(channel)

        # First replay any historical events saved in PostgreSQL
        try:
            async with async_session_factory() as session:
                task_uuid = uuid.UUID(id)
                stmt = (
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_uuid)
                    .order_by(TaskEvent.sequence)
                )
                res = await session.execute(stmt)
                past_events = res.scalars().all()
                for past_evt in past_events:
                    yield {"data": json.dumps(past_evt.payload, default=str)}
        except Exception as replay_err:
            logger.debug("Event replay skipped: %s", replay_err)

        try:
            while True:
                if await request.is_disconnected():
                    logger.info("Client disconnected from SSE stream for task %s", id)
                    break

                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    raw_data = message.get("data")
                    if raw_data:
                        yield {"data": raw_data}

                        # Check if terminal status reached
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
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.aclose()
                except Exception:
                    pass
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass

    return EventSourceResponse(event_generator())


@router.post("/{id}/approve")
async def approve_task(
    id: str,
    payload: Optional[TaskApproveRequest] = None,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Resume a paused LangGraph execution after human reviewer approval.

    Connects to the graph and invokes Command(resume="approve") with the thread_id.

    Args:
        id: Task string identifier (thread_id).
        payload: Optional TaskApproveRequest containing reviewer decision.
        db: Active AsyncSession database session.

    Returns:
        JSON response with updated task_id and status.
    """
    config = {"configurable": {"thread_id": id}}

    # 1. Connect to compiled graph
    graph = await get_compiled_graph(CLEAN_DATABASE_URL)

    # 2. Call graph.ainvoke(Command(resume=...))
    decision_val = payload.decision if payload else "approve"
    feedback_val = payload.feedback if payload else ""
    try:
        resume_command: Any = Command(resume={"decision": decision_val, "feedback": feedback_val})
        result = await graph.ainvoke(resume_command, config=config)
        logger.info("Graph execution resumed for task %s: %s", id, result)
    except Exception as exc:
        logger.warning("Error resuming graph for task %s: %s", id, exc)

    # 3. Update task status in PostgreSQL if recorded
    try:
        task_uuid = uuid.UUID(id)
        await db.execute(update(Task).where(Task.id == task_uuid).values(status="completed"))
        await db.commit()
    except Exception as db_err:
        logger.debug("Could not update Task in DB on approval: %s", db_err)

    # 4. Publish completion event to Redis Pub/Sub channel
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            completion_payload = {
                "node": "reviewer_node",
                "data": {"status": "completed"},
                "step": 5,
                "status": "completed",
                "message": "[Approved] Patch approved by reviewer. Merging changes.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await redis_client.publish(f"forge:events:{id}", json.dumps(completion_payload))
        finally:
            await redis_client.aclose()
    except Exception as redis_err:
        logger.debug("Redis publish skipped or non-critical: %s", redis_err)

    logger.info("Task '%s' approved and completed.", id)
    return {"task_id": id, "status": "completed"}


class TaskRejectRequest(BaseModel):
    """Payload containing human reviewer feedback for required changes."""

    feedback: str = Field(..., description="Human reviewer feedback for changes required")


@router.post("/{id}/reject")
async def reject_task(
    id: str,
    payload: TaskRejectRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Resume a paused LangGraph execution with human rejection feedback.

    Args:
        id: Task identifier string.
        payload: TaskRejectRequest containing feedback comments.
        db: Active AsyncSession database session.

    Returns:
        JSON response with status 'implementing'.
    """
    config = {"configurable": {"thread_id": id}}
    graph = await get_compiled_graph(CLEAN_DATABASE_URL)
    resume_command: Any = Command(resume={"decision": "reject", "feedback": payload.feedback})
    try:
        result = await graph.ainvoke(resume_command, config=config)
        logger.info("Graph execution resumed with rejection for task %s: %s", id, result)
    except Exception as exc:
        logger.warning("Error resuming graph with rejection for task %s: %s", id, exc)

    try:
        task_uuid = uuid.UUID(id)
        await db.execute(update(Task).where(Task.id == task_uuid).values(status="implementing"))
        await db.commit()
    except Exception as db_err:
        logger.debug("Could not update Task in DB on rejection: %s", db_err)

    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            reject_payload = {
                "node": "reviewer_node",
                "data": {"status": "implementing"},
                "step": 3,
                "status": "implementing",
                "message": f"[Rejected] Changes requested: {payload.feedback}. Re-implementing patch.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await redis_client.publish(f"forge:events:{id}", json.dumps(reject_payload))
        finally:
            await redis_client.aclose()
    except Exception as redis_err:
        logger.debug("Redis publish skipped or non-critical: %s", redis_err)

    logger.info("Task '%s' revision requested: %s", id, payload.feedback)
    return {"task_id": id, "status": "implementing"}


@router.post("/{id}/cancel")
async def cancel_task(
    id: str,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Cancel task execution, abort the LangGraph loop, and cleanup worktree immediately.

    Args:
        id: Task identifier string.
        db: Active AsyncSession database session.

    Returns:
        JSON response with status 'cleaned_up'.
    """
    logger.info("Received cancel request for task '%s'", id)

    # 1. Immediately abort running asyncio worker if active
    running_task = RUNNING_TASKS.get(id)
    if running_task and not running_task.done():
        logger.info("Cancelling running asyncio worker for task %s", id)
        running_task.cancel()
    RUNNING_TASKS.pop(id, None)

    # 2. Update task status in database immediately
    try:
        task_uuid = uuid.UUID(id)
        await db.execute(update(Task).where(Task.id == task_uuid).values(status="cleaned_up"))
        await db.commit()
    except Exception as db_err:
        logger.debug("Database status update skipped: %s", db_err)

    # 3. Broadcast cancellation event to Redis channel immediately
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            cancel_payload = {
                "node": "cleanup_node",
                "data": {"status": "cleaned_up"},
                "step": 0,
                "status": "cleaned_up",
                "message": "[Cancelled] Task cancelled by user. Worktree cleaned up.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await redis_client.publish(f"forge:events:{id}", json.dumps(cancel_payload))
        finally:
            await redis_client.aclose()
    except Exception as redis_err:
        logger.debug("Redis publish skipped: %s", redis_err)

    # 4. Schedule worktree and temporary branch cleanup asynchronously in background
    async def _async_cleanup() -> None:
        try:
            await cleanup_node(
                {
                    "task_id": id,
                    "worktree_path": "",
                    "status": "cleaned_up",
                    "repo_id": "",
                    "commit_sha": "main",
                    "prompt": "",
                    "mode": "fast",
                    "messages": [],
                    "context_retrieved": [],
                    "plan": {},
                    "generated_diff": "",
                    "test_results": {},
                    "retry_count": 0,
                }
            )
        except Exception as cleanup_err:
            logger.warning("Error in background cleanup_node for task %s: %s", id, cleanup_err)

    asyncio.create_task(_async_cleanup())

    logger.info("Task '%s' cancelled immediately.", id)
    return {"task_id": id, "status": "cleaned_up"}
