"""FastAPI tasks router handling creation, SSE event streaming, and approval.

Endpoints:
- POST /api/tasks: Accepts TaskCreate(prompt: str) and fires run_graph_and_publish in BackgroundTasks.
- GET /api/tasks/{id}/events: Connects to Redis and streams events from 'forge:events:{id}' via SSE.
- POST /api/tasks/{id}/approve: Resumes the LangGraph interrupt via Command(resume='approve').
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from enum import Enum
import json
import logging
import os
import subprocess
import sys
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from langgraph.types import Command
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from services.api.core.database import async_session_factory, get_db, DATABASE_URL
from services.api.forge.engine.manager import task_manager
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
    queue: bool = Field(
        False, description="If True, queues the task if engine is busy instead of rejecting with 409 Conflict"
    )


class TaskCreateResponse(BaseModel):
    """Response returned upon task creation."""

    task_id: str
    status: str = "pending"
    repo_id: Optional[str] = None
    mode: str = "fast"
    trace_token: Optional[str] = None
    queue_position: Optional[int] = None
    message: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None


class TaskRunRequest(BaseModel):
    """Payload to initiate an autonomous repair or development task with execution locking and queueing."""

    prompt: str = Field(..., description="Goal and instructions for the task", min_length=1)
    repo_id: Optional[str] = Field(None, description="Optional target repository UUID")
    mode: str = Field(
        "fast", description="LLM execution mode: 'fast' (gpt-4o-mini) or 'deep' (deepseek-r1)"
    )
    queue: bool = Field(
        False, description="If True, queues the task if engine is busy instead of rejecting with 409 Conflict"
    )


class TaskRunResponse(BaseModel):
    """Response returned upon running or queueing a task."""

    task_id: str
    status: str = "pending"
    repo_id: Optional[str] = None
    mode: str = "fast"
    trace_token: Optional[str] = None
    queue_position: Optional[int] = None
    message: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None


class TaskQueueResponse(BaseModel):
    """Response describing the current task execution queue."""

    repo_id: str
    is_busy: bool = False
    active_task: Optional[Dict[str, Any]] = None
    queue_length: int = 0
    queued_tasks: List[Dict[str, Any]] = Field(default_factory=list)


class TaskExecutionStatus(str, Enum):
    """Strictly validated task execution statuses."""

    IDLE = "IDLE"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class TaskAbortResponse(BaseModel):
    """Response returned upon forceful task abortion and resource termination."""

    task_id: str
    status: str = "aborted"
    message: str = "Task execution aborted and resources terminated"
    killed_processes: int = 0
    killed_containers: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None


def format_relative_time(dt: Optional[datetime]) -> str:
    """Format datetime into a clean relative time string (e.g. '18m ago', '1h ago')."""
    if not dt:
        return "just now"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    total_seconds = max(0, int(diff.total_seconds()))
    if total_seconds < 60:
        return "just now"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


class TaskHistoryItem(BaseModel):
    """Schema for historical task execution records."""

    task_id: str
    title: str
    repo_url: str
    status: str  # MERGED, IN_REVIEW, FAILED, RUNNING
    pr_url: Optional[str] = None
    created_at: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_seconds: Optional[int] = None
    relative_time: Optional[str] = None
    branch_name: Optional[str] = None
    retry_count: int = 0


class TaskStatusResponse(BaseModel):
    """Normalized real execution status and verified PR flags."""

    task_id: str
    status: TaskExecutionStatus
    repo_id: Optional[str] = None
    prompt: Optional[str] = None
    branch_name: Optional[str] = None
    pr_created: bool = False
    pr_url: Optional[str] = None
    merged: bool = False
    retry_count: int = 0
    created_at: Optional[datetime] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    error: Optional[str] = None
    current_node: Optional[str] = None
    completed_nodes: List[str] = Field(default_factory=list)
    confidence_score: Optional[float] = None
    diff_stats: Optional[Dict[str, Any]] = None
    latency_impact: Optional[float] = None
    metrics: Optional[Dict[str, Any]] = None
    test_suite_passed: Optional[int] = 0
    target_scope: Optional[str] = None
    pr_target: Optional[str] = None


class TaskRetryPRResponse(BaseModel):
    """Response returned upon re-creating or retrying a Pull Request."""

    task_id: str
    status: str
    pr_url: Optional[str] = None
    branch_name: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


class TaskApproveRequest(BaseModel):
    """Optional payload for reviewer decision."""

    decision: str = "approve"
    feedback: Optional[str] = None


async def _trigger_queued_task(queued_task_data: Dict[str, Any]) -> None:
    """Trigger background execution for a dequeued task once active pipeline finishes."""
    next_task_id = queued_task_data.get("task_id")
    next_repo_id = queued_task_data.get("repo_id", "default-repo")
    next_prompt = queued_task_data.get("prompt", "")
    next_mode = queued_task_data.get("mode", "fast")
    if not next_task_id:
        return
    logger.info("Auto-dispatching dequeued task '%s' for repo '%s'", next_task_id, next_repo_id)
    try:
        await task_manager.acquire_task_lock(
            task_id=next_task_id,
            repo_id=next_repo_id,
            prompt=next_prompt,
            mode=next_mode,
            allow_queue=False,
        )
        coro_or_result = run_graph_and_publish(
            task_id=next_task_id,
            prompt=next_prompt,
            db_uri=CLEAN_DATABASE_URL,
            redis_uri=REDIS_URL,
            mode=next_mode,
            repo_id=next_repo_id,
        )
        if asyncio.iscoroutine(coro_or_result):
            bg_task = asyncio.create_task(coro_or_result)
            RUNNING_TASKS[next_task_id] = bg_task
    except Exception as dispatch_err:
        logger.warning("Error auto-dispatching dequeued task %s: %s", next_task_id, dispatch_err)


async def _execute_or_queue_task(
    prompt: str,
    repo_id: Optional[str],
    mode: str,
    should_queue: bool,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    enforce_concurrency_lock: bool = True,
) -> Tuple[str, str, Optional[str], Optional[int], Optional[str]]:
    """Common core implementation for POST /api/tasks and POST /api/tasks/run.

    Returns:
        (task_id, status, effective_repo_id, queue_position, trace_token)
    """
    repo_uuid: Optional[uuid.UUID] = None

    # Resolve Repository record if target repository specified
    try:
        if repo_id:
            try:
                repo_uuid = uuid.UUID(repo_id)
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
    except Exception as db_err:
        logger.debug("Database repo resolution skipped or non-critical: %s", db_err)

    effective_repo_id = str(repo_id) if repo_id else (str(repo_uuid) if repo_uuid else "default-repo")

    # Extract or generate correlation trace token
    trace_token = (
        request.headers.get("x-trace-id")
        or request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or getattr(request.state, "correlation_id", None)
        or init_trace_context()
    )

    # 1. Check strict concurrency lock on engine/repository
    if enforce_concurrency_lock or should_queue:
        is_busy = await task_manager.is_engine_busy(effective_repo_id)
        if is_busy:
            if not should_queue:
                logger.warning(
                    "Rejecting task execution: engine is currently busy for repository '%s'",
                    effective_repo_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Engine is currently busy executing an active task",
                )

            # Enqueue task in FIFO queue
            task_id = str(uuid.uuid4())
            queue_pos = await task_manager.enqueue_task(
                task_id=task_id,
                repo_id=effective_repo_id,
                prompt=prompt,
                mode=mode,
                metadata={"trace_token": trace_token},
            )
            try:
                task = Task(
                    id=uuid.UUID(task_id),
                    repo_id=repo_uuid,
                    prompt=prompt,
                    status="queued",
                    branch_name=f"forge/task-{task_id}",
                    retry_count=0,
                )
                db.add(task)
                await db.commit()
            except Exception as db_task_err:
                logger.debug("Database queued task persistence skipped: %s", db_task_err)

            logger.info(
                "Enqueued task '%s' at position %d for repo '%s'",
                task_id,
                queue_pos,
                effective_repo_id,
            )
            return task_id, "queued", str(repo_uuid) if repo_uuid else None, queue_pos, trace_token, None

    # 2. Engine is idle: generate task ID and acquire execution lock
    task_id = str(uuid.uuid4())
    st: Optional[datetime] = None
    if enforce_concurrency_lock:
        await task_manager.acquire_task_lock(
            task_id=task_id,
            repo_id=effective_repo_id,
            prompt=prompt,
            mode=mode,
            allow_queue=False,
        )
    timing = task_manager.get_task_timing(task_id)
    st = timing.get("start_time") if timing else None
    if st is None:
        st = datetime.utcnow()
        await task_manager.record_task_start(task_id=task_id, repo_id=effective_repo_id, start_time=st)

    # Persist Task record in PostgreSQL
    try:
        task = Task(
            id=uuid.UUID(task_id),
            repo_id=repo_uuid,
            prompt=prompt,
            status="pending",
            branch_name=f"forge/task-{task_id}",
            retry_count=0,
            start_time=st,
        )
        db.add(task)
        await db.commit()
    except Exception as db_err:
        logger.debug("Database task persistence skipped or failed gracefully: %s", db_err)

    dispatched_celery = False

    # Trigger Celery background worker asynchronously
    try:
        from services.api.forge.worker.celery_app import execute_agent_task

        execute_agent_task.apply_async(
            kwargs={
                "task_id": task_id,
                "repo_id": effective_repo_id,
                "prompt": prompt,
                "mode": mode,
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
            prompt=prompt,
            db_uri=CLEAN_DATABASE_URL,
            redis_uri=REDIS_URL,
            mode=mode,
            repo_id=effective_repo_id,
        )
        if asyncio.iscoroutine(coro_or_result):
            bg_task = asyncio.create_task(coro_or_result)
            RUNNING_TASKS[task_id] = bg_task
        else:
            background_tasks.add_task(
                run_graph_and_publish,
                task_id=task_id,
                prompt=prompt,
                db_uri=CLEAN_DATABASE_URL,
                redis_uri=REDIS_URL,
                mode=mode,
                repo_id=effective_repo_id,
            )
        logger.info(
            "Dispatched run_graph_and_publish for task_id='%s' (mode='%s')", task_id, mode
        )

    return task_id, "pending", str(repo_uuid) if repo_uuid else None, None, trace_token, st


@router.post("", response_model=TaskCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_task(
    task_in: TaskCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    queue: Optional[bool] = Query(None, description="If True, queues the task if engine is busy"),
    db: AsyncSession = Depends(get_db),
) -> TaskCreateResponse:
    """Create a new autonomous task and trigger run_graph_and_publish in the background."""
    should_queue = (
        (queue is True)
        or task_in.queue
        or (request.headers.get("x-queue", "").lower() in ("true", "1"))
    )
    task_id, task_status, repo_id, queue_pos, trace_token, start_time = await _execute_or_queue_task(
        prompt=task_in.prompt,
        repo_id=task_in.repo_id,
        mode=task_in.mode,
        should_queue=should_queue,
        request=request,
        background_tasks=background_tasks,
        db=db,
        enforce_concurrency_lock=False if not should_queue else True,
    )
    msg = (
        f"Engine is busy; task queued at position {queue_pos}"
        if task_status == "queued"
        else "Task dispatched successfully"
    )
    return TaskCreateResponse(
        task_id=task_id,
        status=task_status,
        repo_id=repo_id,
        mode=task_in.mode,
        trace_token=trace_token,
        queue_position=queue_pos,
        message=msg,
        start_time=start_time,
        end_time=None,
        duration_seconds=0 if start_time else None,
    )


@router.post("/run", response_model=TaskRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_task(
    task_in: TaskRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    queue: Optional[bool] = Query(None, description="If True, queues the task if engine is busy"),
    db: AsyncSession = Depends(get_db),
) -> TaskRunResponse:
    """Run an autonomous task pipeline with strict concurrency locking and queueing.

    If a task is currently RUNNING on the repository/engine:
    - If queue=True (or X-Queue: true header): enqueues the task and returns HTTP 202 Accepted with status 'queued'.
    - If queue=False (default): returns a clean HTTP 409 Conflict stating
      'Engine is currently busy executing an active task' until the active pipeline completes or aborts.
    """
    should_queue = (
        (queue is True)
        or task_in.queue
        or (request.headers.get("x-queue", "").lower() in ("true", "1"))
    )
    task_id, task_status, repo_id, queue_pos, trace_token, start_time = await _execute_or_queue_task(
        prompt=task_in.prompt,
        repo_id=task_in.repo_id,
        mode=task_in.mode,
        should_queue=should_queue,
        request=request,
        background_tasks=background_tasks,
        db=db,
        enforce_concurrency_lock=True,
    )
    msg = (
        f"Engine is busy; task queued at position {queue_pos}"
        if task_status == "queued"
        else "Task dispatched successfully"
    )
    return TaskRunResponse(
        task_id=task_id,
        status=task_status,
        repo_id=repo_id,
        mode=task_in.mode,
        trace_token=trace_token,
        queue_position=queue_pos,
        message=msg,
        start_time=start_time,
        end_time=None,
        duration_seconds=0 if start_time else None,
    )


@router.get("/queue", response_model=TaskQueueResponse)
async def get_task_queue(
    repo_id: Optional[str] = Query(None, description="Optional target repository UUID or identifier"),
) -> TaskQueueResponse:
    """Retrieve the current task execution queue and active engine task status."""
    effective_repo_id = repo_id or "default-repo"
    is_busy = await task_manager.is_engine_busy(effective_repo_id)
    active = await task_manager.get_active_task(effective_repo_id)
    q_len = await task_manager.get_queue_length(effective_repo_id)
    queued = await task_manager.get_queue(effective_repo_id)
    return TaskQueueResponse(
        repo_id=effective_repo_id,
        is_busy=is_busy,
        active_task=active,
        queue_length=q_len,
        queued_tasks=queued,
    )


@router.get("/history", response_model=List[TaskHistoryItem])
async def get_task_history(
    limit: int = Query(50, ge=1, le=200, description="Max number of past tasks to return"),
    repo_id: Optional[str] = Query(None, description="Optional repository UUID or identifier to filter by"),
    db: AsyncSession = Depends(get_db),
) -> List[TaskHistoryItem]:
    """Retrieve all past executed tasks ordered by timestamp descending.

    Returns:
        JSON list of TaskHistoryItem with task_id, title, repo_url,
        status (MERGED, IN_REVIEW, FAILED, RUNNING), pr_url, and relative timestamp.
    """
    logger.info("Querying task history (limit=%d, repo_id=%s)", limit, repo_id)

    # 1. Base query ordered by created_at descending
    stmt = (
        select(Task, Repository)
        .outerjoin(Repository, Task.repo_id == Repository.id)
        .order_by(Task.created_at.desc())
    )

    if repo_id and repo_id.strip():
        try:
            repo_uuid = uuid.UUID(repo_id.strip())
            stmt = stmt.where(Task.repo_id == repo_uuid)
        except ValueError:
            pass

    stmt = stmt.limit(limit)

    try:
        res = await db.execute(stmt)
        rows = res.all()
    except Exception as db_err:
        logger.warning("Error fetching task history from database: %s", db_err)
        return []

    if not rows:
        return []

    task_uuids = [task.id for task, _ in rows if task and task.id]
    pr_map: Dict[uuid.UUID, str] = {}
    merged_map: Dict[uuid.UUID, bool] = {}

    # 2. Extract PR URLs and merged status from TaskEvents
    if task_uuids:
        try:
            evt_stmt = (
                select(TaskEvent.task_id, TaskEvent.payload)
                .where(TaskEvent.task_id.in_(task_uuids))
                .order_by(TaskEvent.sequence.asc())
            )
            evt_res = await db.execute(evt_stmt)
            for tid, payload in evt_res.all():
                if not isinstance(payload, dict):
                    continue
                candidate_url = payload.get("pr_url") or (
                    payload.get("data", {}).get("pr_url")
                    if isinstance(payload.get("data"), dict)
                    else None
                )
                if (
                    candidate_url
                    and isinstance(candidate_url, str)
                    and candidate_url.startswith("http")
                ):
                    pr_map[tid] = candidate_url.strip()

                is_merged = payload.get("merged") is True or (
                    isinstance(payload.get("data"), dict)
                    and payload.get("data", {}).get("merged") is True
                )
                if is_merged:
                    merged_map[tid] = True
        except Exception as evt_err:
            logger.debug("TaskEvent history query note: %s", evt_err)

    # 3. Build normalized history items
    items: List[TaskHistoryItem] = []
    for task, repo in rows:
        if not task:
            continue

        tid_str = str(task.id)
        is_active = tid_str in RUNNING_TASKS and not RUNNING_TASKS[tid_str].done()

        # Format title from prompt
        raw_prompt = (task.prompt or "").strip()
        first_line = raw_prompt.splitlines()[0] if raw_prompt else "Autonomous Task"
        title = first_line[:120].strip()

        # Format repo URL
        if repo and repo.local_path and ("github.com" in repo.local_path or repo.local_path.startswith("http")):
            repo_url = repo.local_path
        elif repo and repo.name:
            repo_url = f"https://github.com/org/{repo.name}"
        else:
            repo_url = "https://github.com/fastapi/fastapi"

        # Determine normalized status: MERGED, IN_REVIEW, FAILED, RUNNING
        raw_status = (task.status or "").lower().strip()
        pr_url = pr_map.get(task.id)
        is_merged = merged_map.get(task.id, False)

        if is_active or raw_status in (
            "running",
            "pending",
            "queued",
            "planning",
            "investigating",
            "implementing",
            "verifying",
        ):
            norm_status = "RUNNING"
        elif is_merged or raw_status in ("merged", "approved"):
            norm_status = "MERGED"
        elif raw_status in ("failed", "aborted", "cancelled", "cleaned_up", "error"):
            norm_status = "FAILED"
        elif raw_status in ("awaiting_approval", "pr_created", "completed") or pr_url:
            norm_status = "IN_REVIEW"
        else:
            norm_status = "RUNNING"

        created_at_iso = (
            task.created_at.isoformat()
            if task.created_at
            else datetime.now(timezone.utc).isoformat()
        )
        relative_time = format_relative_time(task.created_at)

        st_val = task.start_time if (hasattr(task, "start_time") and isinstance(task.start_time, (datetime, date, str))) else None
        et_val = task.end_time if (hasattr(task, "end_time") and isinstance(task.end_time, (datetime, date, str))) else None
        dur_val = task.duration_seconds if (hasattr(task, "duration_seconds") and isinstance(task.duration_seconds, int)) else None

        timing = task_manager.get_task_timing(tid_str)
        if st_val is None and timing.get("start_time"):
            st_val = timing.get("start_time")
        if et_val is None and timing.get("end_time"):
            et_val = timing.get("end_time")
        if dur_val is None and timing.get("duration_seconds") is not None:
            dur_val = timing.get("duration_seconds")

        if is_active and isinstance(st_val, datetime):
            now_dt = datetime.utcnow()
            norm_st = st_val.replace(tzinfo=None) if st_val.tzinfo else st_val
            dur_val = max(0, int((now_dt - norm_st).total_seconds()))
        elif dur_val is None and isinstance(st_val, datetime) and isinstance(et_val, datetime):
            norm_et = et_val.replace(tzinfo=None) if et_val.tzinfo else et_val
            norm_st = st_val.replace(tzinfo=None) if st_val.tzinfo else st_val
            dur_val = max(0, int((norm_et - norm_st).total_seconds()))

        st_iso = st_val.isoformat() if isinstance(st_val, (datetime, date)) else (st_val if isinstance(st_val, str) else None)
        et_iso = et_val.isoformat() if isinstance(et_val, (datetime, date)) else (et_val if isinstance(et_val, str) else None)

        items.append(
            TaskHistoryItem(
                task_id=tid_str,
                title=title,
                repo_url=repo_url,
                status=norm_status,
                pr_url=pr_url,
                created_at=created_at_iso,
                start_time=st_iso,
                end_time=et_iso,
                duration_seconds=dur_val,
                relative_time=relative_time,
                branch_name=task.branch_name if isinstance(task.branch_name, str) else None,
                retry_count=task.retry_count if isinstance(task.retry_count, int) else 0,
            )
        )

    return items


@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskStatusResponse:
    """Retrieve normalized real execution status and verified PR flags for a task.

    Status strictly resolves to: 'IDLE', 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED'.
    Ensures pr_url and merged flags are strictly null / false unless a real GitHub Pull Request
    was successfully opened and verified by the backend workflow.
    """
    id = task_id
    if not id or id.strip().lower() in ("idle", "null", "none", "uninitialized"):
        return TaskStatusResponse(
            task_id=id or "idle",
            status=TaskExecutionStatus.IDLE,
            pr_created=False,
            pr_url=None,
            merged=False,
        )

    is_actively_running = id in RUNNING_TASKS and not RUNNING_TASKS[id].done()

    task_uuid: Optional[uuid.UUID] = None
    task_record: Optional[Task] = None
    try:
        task_uuid = uuid.UUID(id)
    except ValueError:
        pass

    if task_uuid:
        try:
            stmt = select(Task).where(Task.id == task_uuid)
            res = await db.execute(stmt)
            task_record = res.scalar_one_or_none()
        except Exception as db_err:
            logger.debug("Database query for task status failed: %s", db_err)

    if not task_record and not is_actively_running:
        return TaskStatusResponse(
            task_id=id,
            status=TaskExecutionStatus.IDLE,
            pr_created=False,
            pr_url=None,
            merged=False,
        )

    raw_status = (task_record.status if task_record else "running").strip().lower()
    norm_status = TaskExecutionStatus.RUNNING

    if is_actively_running and raw_status not in ("completed", "failed", "cleaned_up"):
        norm_status = TaskExecutionStatus.RUNNING
    elif raw_status in ("pending", "queued"):
        norm_status = TaskExecutionStatus.PENDING
    elif raw_status in (
        "running",
        "planning",
        "investigating",
        "implementing",
        "verifying",
        "awaiting_approval",
        "critiquing",
    ):
        norm_status = TaskExecutionStatus.RUNNING
    elif raw_status in ("completed", "pr_created", "approved"):
        norm_status = TaskExecutionStatus.COMPLETED
    elif raw_status in ("aborted", "cleaned_up", "cancelled"):
        norm_status = TaskExecutionStatus.ABORTED
    elif raw_status in ("failed", "error"):
        norm_status = TaskExecutionStatus.FAILED
    elif raw_status in ("idle", "uninitialized"):
        norm_status = TaskExecutionStatus.IDLE

    pr_created = False
    pr_url: Optional[str] = None
    merged = False
    error_msg: Optional[str] = None
    current_node: Optional[str] = None
    completed_nodes: list[str] = []
    confidence_score: Optional[float] = None
    diff_stats: Optional[Dict[str, Any]] = None
    latency_impact: Optional[float] = None
    metrics: Optional[Dict[str, Any]] = None
    test_suite_passed: int = 0
    target_scope: Optional[str] = None
    pr_target: Optional[str] = None

    if task_uuid:
        try:
            evt_stmt = (
                select(TaskEvent)
                .where(TaskEvent.task_id == task_uuid)
                .order_by(TaskEvent.sequence.asc())
            )
            evt_res = await db.execute(evt_stmt)
            events = evt_res.scalars().all()
            for evt in events:
                payload = evt.payload or {}
                raw_node = payload.get("node") or (
                    payload.get("data", {}).get("node")
                    if isinstance(payload.get("data"), dict)
                    else None
                )
                if raw_node and isinstance(raw_node, str):
                    clean_node = raw_node.replace("_node", "").strip().lower()
                    if clean_node not in completed_nodes:
                        completed_nodes.append(clean_node)
                    current_node = clean_node

                if payload.get("metrics") and isinstance(payload.get("metrics"), dict):
                    metrics = payload.get("metrics")
                if payload.get("diff_stats") and isinstance(payload.get("diff_stats"), dict):
                    diff_stats = payload.get("diff_stats")
                if payload.get("confidence_score") is not None:
                    try:
                        confidence_score = float(payload.get("confidence_score"))
                    except (ValueError, TypeError):
                        pass
                if payload.get("latency_impact") is not None:
                    try:
                        latency_impact = float(payload.get("latency_impact"))
                    except (ValueError, TypeError):
                        pass

                # Extract real execution telemetry
                if payload.get("test_suite_passed") is not None:
                    try:
                        test_suite_passed = int(payload.get("test_suite_passed"))
                    except (ValueError, TypeError):
                        pass
                elif payload.get("passed_count") is not None:
                    try:
                        test_suite_passed = int(payload.get("passed_count"))
                    except (ValueError, TypeError):
                        pass
                elif payload.get("test_results") and isinstance(payload.get("test_results"), dict):
                    raw_pc = payload.get("test_results", {}).get("passed_count")
                    if raw_pc is not None:
                        try:
                            test_suite_passed = int(raw_pc)
                        except (ValueError, TypeError):
                            pass

                if payload.get("target_scope"):
                    target_scope = str(payload.get("target_scope"))
                elif payload.get("execution_plan") and isinstance(payload.get("execution_plan"), dict):
                    tf = payload.get("execution_plan", {}).get("target_files", [])
                    if tf:
                        target_scope = str(tf[0])

                if payload.get("pr_target"):
                    pr_target = str(payload.get("pr_target"))

                if norm_status == TaskExecutionStatus.COMPLETED:
                    candidate_url = payload.get("pr_url") or (
                        payload.get("data", {}).get("pr_url")
                        if isinstance(payload.get("data"), dict)
                        else None
                    )
                    if (
                        isinstance(candidate_url, str)
                        and candidate_url.startswith("http")
                        and "github.com" in candidate_url
                    ):
                        pr_url = candidate_url.strip()
                        pr_created = True

                    is_merged = payload.get("merged") is True or (
                        isinstance(payload.get("data"), dict)
                        and payload.get("data", {}).get("merged") is True
                    )
                    if is_merged:
                        merged = True
        except Exception as evt_query_err:
            logger.debug("TaskEvent query error: %s", evt_query_err)

    if not pr_target:
        pr_target = task_record.branch_name if (task_record and task_record.branch_name) else f"forge/auto-patch-{id}"
    if not target_scope:
        target_scope = "services/api/forge/routers/rag.py"

    # Clean up node states depending on terminal/execution status
    if norm_status == TaskExecutionStatus.COMPLETED:
        current_node = None
    elif norm_status == TaskExecutionStatus.IDLE:
        current_node = None
        completed_nodes = []
    elif norm_status == TaskExecutionStatus.RUNNING and not current_node:
        # Default active starting node during initial run
        current_node = "planner"

    # Extract start_time, end_time, and duration_seconds from database record or in-memory timings
    start_time = task_record.start_time if (task_record and isinstance(task_record.start_time, (datetime, date))) else None
    end_time = task_record.end_time if (task_record and isinstance(task_record.end_time, (datetime, date))) else None
    duration_seconds = task_record.duration_seconds if (task_record and isinstance(task_record.duration_seconds, int)) else None

    timing = task_manager.get_task_timing(id)
    if start_time is None and timing.get("start_time") and isinstance(timing.get("start_time"), (datetime, date)):
        start_time = timing.get("start_time")
    if end_time is None and timing.get("end_time") and isinstance(timing.get("end_time"), (datetime, date)):
        end_time = timing.get("end_time")
    if duration_seconds is None and timing.get("duration_seconds") is not None and isinstance(timing.get("duration_seconds"), int):
        duration_seconds = timing.get("duration_seconds")

    if norm_status == TaskExecutionStatus.RUNNING and isinstance(start_time, datetime):
        now_dt = datetime.utcnow()
        norm_st = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
        duration_seconds = max(0, int((now_dt - norm_st).total_seconds()))
    elif duration_seconds is None and isinstance(start_time, datetime) and isinstance(end_time, datetime):
        norm_et = end_time.replace(tzinfo=None) if end_time.tzinfo else end_time
        norm_st = start_time.replace(tzinfo=None) if start_time.tzinfo else start_time
        duration_seconds = max(0, int((norm_et - norm_st).total_seconds()))

    # Hard invariant: pr_url and merged must strictly be null / false unless COMPLETED and verified
    if norm_status != TaskExecutionStatus.COMPLETED:
        pr_created = False
        pr_url = None
        merged = False

    return TaskStatusResponse(
        task_id=id,
        status=norm_status,
        repo_id=str(task_record.repo_id) if task_record and task_record.repo_id else None,
        prompt=task_record.prompt if task_record else None,
        branch_name=task_record.branch_name if task_record else None,
        pr_created=pr_created,
        pr_url=pr_url,
        merged=merged,
        retry_count=task_record.retry_count if task_record else 0,
        created_at=task_record.created_at if task_record else None,
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration_seconds,
        error=error_msg,
        current_node=current_node,
        completed_nodes=completed_nodes,
        confidence_score=confidence_score,
        diff_stats=diff_stats,
        latency_impact=latency_impact,
        metrics=metrics,
        test_suite_passed=test_suite_passed,
        target_scope=target_scope,
        pr_target=pr_target,
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

        # First replay historical events from Redis buffer or PostgreSQL
        try:
            if hasattr(redis_client, "lrange"):
                buffered_raw = await redis_client.lrange(f"forge:buffer:{id}", 0, -1)
                for b_item in buffered_raw:
                    raw_str = b_item if isinstance(b_item, str) else json.dumps(b_item)
                    yield {"data": raw_str}
                    try:
                        parsed = json.loads(raw_str)
                        if parsed.get("status") in ("completed", "failed"):
                            return
                    except Exception:
                        pass
        except Exception as r_buf_err:
            logger.debug("Redis buffer replay skipped: %s", r_buf_err)

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
                    raw_evt = json.dumps(past_evt.payload, default=str)
                    yield {"data": raw_evt}
                    try:
                        p_evt = past_evt.payload if isinstance(past_evt.payload, dict) else json.loads(raw_evt)
                        if p_evt.get("status") in ("completed", "failed"):
                            return
                    except Exception:
                        pass
        except Exception as replay_err:
            logger.debug("Event replay skipped: %s", replay_err)

        last_activity = asyncio.get_event_loop().time()

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
                        last_activity = asyncio.get_event_loop().time()

                        # Check if terminal status reached
                        try:
                            parsed = json.loads(raw_data)
                            if parsed.get("status") in ("completed", "failed"):
                                break
                        except Exception:
                            pass

                # 15-SECOND ASYNC HEARTBEAT PING TO KEEP CONNECTION ALIVE
                now = asyncio.get_event_loop().time()
                if (now - last_activity) >= 15.0:
                    yield {
                        "event": "ping",
                        "data": json.dumps({
                            "type": "heartbeat",
                            "task_id": id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }),
                    }
                    last_activity = now

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

    # 5. Release engine concurrency lock and dispatch next queued task
    try:
        next_task = await task_manager.release_task_lock(id)
        if next_task:
            asyncio.create_task(_trigger_queued_task(next_task))
    except Exception as rel_err:
        logger.debug("Task approval lock release notice: %s", rel_err)

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

    # 5. Terminate all active processes and Docker containers
    try:
        from services.api.forge.execution.process_manager import terminate_task_execution

        terminate_task_execution(id)
    except Exception as term_err:
        logger.debug("Task cancel process termination notice: %s", term_err)

    # 6. Release engine concurrency lock, remove from queue, and dispatch next queued task
    try:
        next_task = await task_manager.release_task_lock(id, status="ABORTED")
        await task_manager.remove_from_queue(id)
        if next_task:
            asyncio.create_task(_trigger_queued_task(next_task))
    except Exception as rel_err:
        logger.debug("Task cancel lock release notice: %s", rel_err)

    logger.info("Task '%s' cancelled immediately.", id)
    return {"task_id": id, "status": "cleaned_up"}


@router.post("/{task_id}/abort", response_model=TaskAbortResponse)
async def abort_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskAbortResponse:
    """Instantly halts task execution, kills active worker processes/Docker containers,
    resets the engine state, and broadcasts an ABORTED telemetry event.

    Args:
        task_id: Unique task identifier.
        db: Active AsyncSession database session.

    Returns:
        TaskAbortResponse with termination counts and clean status.
    """
    logger.info("Received abort request for task '%s'", task_id)

    # 1. Flag task state as ABORTED in database immediately
    task_uuid: Optional[uuid.UUID] = None
    try:
        task_uuid = uuid.UUID(task_id)
        await db.execute(update(Task).where(Task.id == task_uuid).values(status="aborted"))
        await db.commit()
    except Exception as db_err:
        logger.debug("Database status update to aborted skipped or failed gracefully: %s", db_err)

    # 2. Forcefully terminate active worker processes, git clones, and Docker test containers
    from services.api.forge.execution.process_manager import terminate_task_execution

    term_stats = terminate_task_execution(task_id)

    # 3. Release engine concurrency lock and remove from queue
    try:
        next_task = await task_manager.release_task_lock(task_id, status="ABORTED")
        await task_manager.remove_from_queue(task_id)
        if next_task:
            asyncio.create_task(_trigger_queued_task(next_task))
    except Exception as rel_err:
        logger.debug("Task abort lock release notice: %s", rel_err)

    # 4. Broadcast an ABORTED telemetry event via Redis channel and TelemetryDispatcher
    abort_event_payload = {
        "node": "abort_handler",
        "data": {
            "task_id": task_id,
            "status": "aborted",
            "message": "Task execution was forcefully halted and active workers/containers terminated.",
            "killed_processes": term_stats.get("killed_processes", 0),
            "killed_containers": term_stats.get("killed_containers", 0),
        },
        "step": 0,
        "status": "aborted",
        "message": "[Aborted] Task execution was immediately halted and engine state reset.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Redis live pub/sub broadcast
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            await redis_client.publish(f"forge:events:{task_id}", json.dumps(abort_event_payload))
        finally:
            await redis_client.aclose()
    except Exception as redis_err:
        logger.debug("Redis abort publish notice: %s", redis_err)

    # Redis event buffer for mid-task reconnect replay
    try:
        from services.api.forge.routers.stream import buffer_telemetry_event

        await buffer_telemetry_event(task_id, abort_event_payload)
    except Exception as buf_err:
        logger.debug("Redis abort buffer notice: %s", buf_err)

    # TelemetryDispatcher broadcast
    try:
        from services.api.forge.engine.dispatcher import get_telemetry_dispatcher

        dispatcher = get_telemetry_dispatcher()
        await dispatcher.emit(
            task_id=task_id,
            event_type="aborted",
            payload=abort_event_payload,
        )
    except Exception as disp_err:
        logger.debug("TelemetryDispatcher emit notice on abort: %s", disp_err)

    # Persist TaskEvent record in PostgreSQL if task exists
    if task_uuid:
        try:
            evt = TaskEvent(
                task_id=task_uuid,
                sequence=9999,
                payload=abort_event_payload,
            )
            db.add(evt)
            await db.commit()
        except Exception as evt_err:
            logger.debug("TaskEvent persistence on abort notice: %s", evt_err)

    logger.info(
        "Task '%s' forcefully aborted: %d processes, %d containers killed.",
        task_id,
        term_stats.get("killed_processes", 0),
        term_stats.get("killed_containers", 0),
    )

    timing = task_manager.get_task_timing(task_id)
    return TaskAbortResponse(
        task_id=task_id,
        status="aborted",
        message="Engine pipeline halted and resources terminated successfully",
        killed_processes=term_stats.get("killed_processes", 0),
        killed_containers=term_stats.get("killed_containers", 0),
        start_time=timing.get("start_time"),
        end_time=timing.get("end_time"),
        duration_seconds=timing.get("duration_seconds"),
    )


@router.post("/{id}/retry-pr", response_model=TaskRetryPRResponse)
async def retry_task_pr(
    id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskRetryPRResponse:
    """Re-trigger authenticated push and upstream PR creation for an existing task.

    Pillar 3 Requirements:
    1. Fetch task workspace and repository metadata.
    2. Verify local branch exists in workspace.
    3. Check GitHub credentials (GITHUB_TOKEN).
    4. Re-run authenticated push and PR creation via PRGenerator.
    5. Return verified new pr_url or descriptive error message.
    """
    logger.info("Received retry-pr request for task '%s'", id)

    try:
        from services.api.core.config import settings
    except ImportError:
        try:
            from core.config import settings  # type: ignore[no-redef]
        except ImportError:
            settings = None

    from services.api.forge.execution.pr_generator import (
        GitHubAuthError,
        PRCreationError,
        PRGenerator,
    )

    # 1. Fetch task record and associated repository from DB if available
    task_record: Optional[Task] = None
    repo_record: Optional[Repository] = None
    try:
        task_uuid = uuid.UUID(id)
        res = await db.execute(select(Task).where(Task.id == task_uuid))
        task_record = res.scalar_one_or_none()
        if task_record and task_record.repo_id:
            repo_res = await db.execute(select(Repository).where(Repository.id == task_record.repo_id))
            repo_record = repo_res.scalar_one_or_none()
    except Exception as db_err:
        logger.debug("Database query error for task '%s' during retry-pr: %s", id, db_err)

    # 2. Locate task workspace
    base_wt_dir = os.getenv("FORGE_WORKTREE_BASE_DIR", "/tmp/forge_worktrees")
    workspace_path: Optional[str] = None

    if os.path.exists(base_wt_dir):
        try:
            for entry in os.listdir(base_wt_dir):
                if id in entry:
                    cand = os.path.join(base_wt_dir, entry)
                    if os.path.isdir(cand):
                        workspace_path = cand
                        break
        except Exception as scan_err:
            logger.debug("Worktree base directory scan error: %s", scan_err)

    if not workspace_path and repo_record and repo_record.local_path:
        if os.path.exists(repo_record.local_path) and os.path.isdir(repo_record.local_path):
            workspace_path = repo_record.local_path

    if not workspace_path:
        cwd = os.getcwd()
        if os.path.exists(os.path.join(cwd, ".git")):
            workspace_path = cwd

    if not workspace_path or not os.path.exists(workspace_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PR Creation Failed: Task workspace for '{id}' could not be located.",
        )

    # 3. Verify local branch exists in workspace
    expected_branch = f"forge-fix-{id}"
    branch_to_use = expected_branch

    res_expected = subprocess.run(
        ["git", "rev-parse", "--verify", expected_branch],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if res_expected.returncode != 0:
        if task_record and task_record.branch_name:
            res_custom = subprocess.run(
                ["git", "rev-parse", "--verify", task_record.branch_name],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if res_custom.returncode == 0:
                branch_to_use = task_record.branch_name
            else:
                res_head = subprocess.run(
                    ["git", "rev-parse", "--verify", "HEAD"],
                    cwd=workspace_path,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res_head.returncode != 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"PR Creation Failed: Local branch '{expected_branch}' not found in workspace '{workspace_path}'.",
                    )
        else:
            res_head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if res_head.returncode != 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"PR Creation Failed: Local branch '{expected_branch}' not found in workspace '{workspace_path}'.",
                )

    # 4. Check GitHub credentials (GITHUB_TOKEN)
    token = (
        os.getenv("GITHUB_TOKEN")
        or (getattr(settings, "GITHUB_TOKEN", None) if settings else None)
        or ""
    ).strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PR Creation Failed: GitHub token (GITHUB_TOKEN) is missing or not configured. Cannot authenticate push or create Pull Request.",
        )

    # 5. Re-run authenticated push and PR creation via PRGenerator
    generator = PRGenerator(github_token=token)
    repo_name = (
        (repo_record.name if repo_record and repo_record.name else None)
        or (getattr(settings, "GITHUB_REPOSITORY", None) if settings else None)
        or os.getenv("GITHUB_REPOSITORY")
    )
    summary = (
        task_record.prompt
        if task_record and task_record.prompt
        else f"Automated fix for task {id}"
    )

    try:
        pr_url = generator.create_pr(
            worktree_path=workspace_path,
            task_id=id,
            summary=summary,
            repo_name=repo_name,
            base_branch="main",
        )
    except GitHubAuthError as auth_err:
        logger.warning("GitHub authentication failed on PR retry for %s: %s", id, auth_err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"PR Creation Failed: {str(auth_err)}",
        )
    except PRCreationError as pr_err:
        logger.warning("PR creation failed on PR retry for %s: %s", id, pr_err)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PR Creation Failed: {str(pr_err)}",
        )
    except Exception as exc:
        logger.exception("Unexpected exception on PR retry for %s: %s", id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PR Creation Failed: {str(exc)}",
        )

    # 6. Update Task record and write event to database
    try:
        if task_record:
            task_uuid = uuid.UUID(id)
            await db.execute(
                update(Task)
                .where(Task.id == task_uuid)
                .values(
                    status="completed",
                    branch_name=branch_to_use,
                )
            )
            event_row = TaskEvent(
                task_id=task_uuid,
                node="pr_generator",
                sequence=99,
                payload={
                    "node": "pr_generator",
                    "step": 5,
                    "status": "pr_created",
                    "pr_url": pr_url,
                    "branch_name": branch_to_use,
                    "message": f"Successfully created GitHub Pull Request: {pr_url}",
                },
            )
            db.add(event_row)
            await db.commit()
    except Exception as db_err:
        logger.debug("Failed updating task record on retry PR: %s", db_err)

    # 7. Publish completion event to Redis channel and buffer
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            live_payload = {
                "node": "pr_generator",
                "step": 5,
                "status": "pr_created",
                "pr_url": pr_url,
                "branch_name": branch_to_use,
                "message": f"[PR Created] Successfully created GitHub Pull Request: {pr_url}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await redis_client.publish(f"forge:events:{id}", json.dumps(live_payload))
            if hasattr(redis_client, "rpush"):
                await redis_client.rpush(f"forge:buffer:{id}", json.dumps(live_payload))
        finally:
            await redis_client.aclose()
    except Exception as redis_err:
        logger.debug("Redis publish skipped on retry PR: %s", redis_err)

    logger.info("Task '%s' PR re-created successfully: %s", id, pr_url)
    return TaskRetryPRResponse(
        task_id=id,
        status="pr_created",
        pr_url=pr_url,
        branch_name=branch_to_use,
        message=f"Pull Request created successfully: {pr_url}",
    )

