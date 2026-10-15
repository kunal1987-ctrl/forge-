"""Resilient Celery background worker configuration with strict timeouts,
Redis Dead-Letter Queue (DLQ), distributed locking, and ephemeral worktree lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from celery import Celery, Task, shared_task
from celery.exceptions import SoftTimeLimitExceeded
import redis

try:
    from services.api.core.config import settings

    DEFAULT_REDIS_URL = settings.REDIS_URL
except ImportError:
    DEFAULT_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

from services.api.forge.logging import bind_context, clear_context, get_logger, log_exception
from services.api.forge.concurrency.locks import repo_lock
from services.api.forge.execution.cleanup import cleanup_worktree_atomic
from services.api.forge.telemetry import init_trace_context

logger = get_logger("forge.worker")

# Dead-Letter Queue Keys in Redis
REDIS_DLQ_KEY = "forge:dlq:tasks"
REDIS_DLQ_EVENTS = "forge:events:dlq"

# Initialize Celery Application
celery_app = Celery(
    "forge_worker",
    broker=DEFAULT_REDIS_URL,
    backend=DEFAULT_REDIS_URL,
)

# Hard-coded strict timeframe configuration and Redis DLQ route configuration
celery_app.conf.update(
    task_time_limit=300,  # 5 minutes hard limit (SIGKILL)
    task_soft_time_limit=270,  # 4.5 minutes soft limit (SoftTimeLimitExceeded)
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "forge.execute_agent_task": {"queue": "forge_tasks"},
        "forge.dlq.*": {"queue": "forge_dlq"},
    },
    task_default_queue="forge_tasks",
)


def get_redis_client() -> redis.Redis:
    """Return synchronous Redis client for Celery / DLQ operations."""
    return redis.Redis.from_url(DEFAULT_REDIS_URL, decode_responses=True)


def push_to_dlq(
    task_id: str,
    task_name: str,
    args: tuple[Any, ...],
    kwargs: Dict[str, Any],
    error_msg: str,
    exception_type: str,
    traceback_str: str,
    retry_count: int = 0,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Push failed or timed-out task metadata to Redis Dead-Letter Queue (DLQ).

    Args:
        task_id: Task unique identifier.
        task_name: Celery task name.
        args: Positional arguments passed to task.
        kwargs: Keyword arguments passed to task.
        error_msg: Sanitized error description.
        exception_type: Class name of exception.
        traceback_str: Formatted traceback stack.
        retry_count: Number of retries attempted before DLQ.
        correlation_id: Distributed correlation ID.

    Returns:
        Structured DLQ record dict.
    """
    dlq_record: Dict[str, Any] = {
        "dlq_id": uuid.uuid4().hex,
        "task_id": str(task_id),
        "task_name": str(task_name),
        "args": list(args),
        "kwargs": {k: str(v) for k, v in kwargs.items()},
        "error": str(error_msg),
        "exception_type": str(exception_type),
        "traceback": str(traceback_str),
        "retry_count": int(retry_count),
        "correlation_id": str(correlation_id or uuid.uuid4().hex),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        r = get_redis_client()
        payload = json.dumps(dlq_record)
        r.lpush(REDIS_DLQ_KEY, payload)
        r.publish(REDIS_DLQ_EVENTS, payload)
        logger.warning(
            "Task dispatched to Dead-Letter Queue (DLQ)",
            task_id=task_id,
            exception_type=exception_type,
            dlq_id=dlq_record["dlq_id"],
        )
    except Exception as redis_exc:
        logger.error(
            "Failed to push record to Redis DLQ",
            task_id=task_id,
            error=str(redis_exc),
            exc_info=True,
        )

    return dlq_record


def get_dlq_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve recent failed task records from Redis DLQ."""
    try:
        r = get_redis_client()
        raw_items = r.lrange(REDIS_DLQ_KEY, 0, limit - 1)
        tasks: List[Dict[str, Any]] = []
        for item in raw_items:
            try:
                tasks.append(json.loads(item))
            except (json.JSONDecodeError, TypeError) as parse_err:
                logger.warning("Failed parsing DLQ item: %s", parse_err)
        return tasks
    except Exception as exc:
        logger.error("Failed to read from Redis DLQ: %s", exc)
        return []


def clear_dlq_tasks() -> int:
    """Clear all records from Redis DLQ (e.g. after replay or during testing)."""
    try:
        r = get_redis_client()
        count = r.delete(REDIS_DLQ_KEY)
        return int(count)
    except Exception as exc:
        logger.error("Failed to clear Redis DLQ: %s", exc)
        return 0


class ResilientForgeTask(Task):
    """Custom Celery Task base class implementing DLQ failover and structured exception logging."""

    abstract = True

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: Dict[str, Any],
        einfo: Any,
    ) -> None:
        """Invoked when the task fails completely or exceeds hard time limits."""
        tb_str = einfo.traceback if einfo else traceback.format_exc()
        correlation_id = kwargs.get("correlation_id", "") or task_id

        bind_context(task_id=task_id, correlation_id=correlation_id)
        logger.error(
            "Celery task failed with unhandled exception",
            task_id=task_id,
            task_name=self.name,
            exception_type=type(exc).__name__,
            error=str(exc),
            traceback=tb_str,
        )

        push_to_dlq(
            task_id=task_id,
            task_name=self.name,
            args=args,
            kwargs=kwargs,
            error_msg=str(exc),
            exception_type=type(exc).__name__,
            traceback_str=tb_str,
            retry_count=getattr(self.request, "retries", 0),
            correlation_id=correlation_id,
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)

    def on_retry(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: Dict[str, Any],
        einfo: Any,
    ) -> None:
        """Invoked when the task is retried after a transient failure."""
        correlation_id = kwargs.get("correlation_id", "") or task_id
        bind_context(task_id=task_id, correlation_id=correlation_id)
        logger.warning(
            "Celery task retrying",
            task_id=task_id,
            task_name=self.name,
            retries=getattr(self.request, "retries", 0),
            error=str(exc),
        )
        super().on_retry(exc, task_id, args, kwargs, einfo)


def worker_execution_lifecycle(
    task_id: str,
    repo_id: str,
    prompt: str,
    mode: str = "fast",
    worktree_path: str = "",
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Execute autonomous agent repair loop inside worker lifecycle with distributed locking,
    ephemeral worktree isolation, and guaranteed atomic resource cleanup in finally.

    Lifecycle contract:
    1. Enforce acquiring exclusive Redis distributed lock (lock:repo:{repo_id}) before provisioning resources.
    2. Provision unique UUID-suffixed ephemeral worktree (/tmp/forge_worktrees/{task_id}_{uuid.uuid4().hex[:8]}).
    3. Run autonomous agent execution within try...finally.
    4. In finally block, guarantee git worktree remove --force and atomic cleanup always execute.
    5. Exiting with repo_lock ensures Redis mutex lock is guaranteed released.
    """
    from services.api.forge.orchestration.worker import run_graph_and_publish

    # 1. Enforce acquiring Redis distributed lock before provisioning resources
    with repo_lock(repo_id, timeout=120.0, blocking=True, blocking_timeout=30.0):
        # 2. Provision unique UUID-suffixed ephemeral worktree directory
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", task_id)
        base_wt_dir = os.getenv("FORGE_WORKTREE_BASE_DIR", "/tmp/forge_worktrees")
        unique_suffix = uuid.uuid4().hex[:8]
        effective_wt = (
            worktree_path
            if worktree_path
            else os.path.abspath(os.path.join(base_wt_dir, f"{safe_id}_{unique_suffix}"))
        )

        repo_dir = repo_id if (repo_id and os.path.exists(repo_id)) else os.getcwd()

        # 3. Robust try...finally execution block ensuring cleanup always executes
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    run_graph_and_publish(
                        task_id=task_id,
                        prompt=prompt,
                        mode=mode,
                        worktree_path=effective_wt,
                        repo_id=repo_id,
                    )
                )
                return {
                    "status": "success",
                    "task_id": task_id,
                    "worktree_path": effective_wt,
                    "correlation_id": correlation_id,
                }
            finally:
                loop.close()
        finally:
            # Guarantee git worktree remove --force and resource pruning always execute,
            # even if a container crashes, test timeouts, or an exception occurs.
            try:
                cleanup_worktree_atomic(repo_dir, effective_wt, task_id)
            except Exception as wt_err:
                logger.warning("Celery worktree cleanup notice: %s", wt_err)


# Re-export alias for backward compatibility
worker_execution_wrapper = worker_execution_lifecycle


@shared_task(
    base=ResilientForgeTask,
    bind=True,
    name="forge.execute_agent_task",
    time_limit=300,  # Hard limit: 300s
    soft_time_limit=270,  # Soft limit: 270s
    max_retries=3,
)
def execute_agent_task(
    self: Any,
    task_id: str,
    repo_id: str,
    prompt: str,
    worktree_path: str = "",
    commit_sha: str = "HEAD",
    mode: str = "fast",
    correlation_id: str = "",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Execute autonomous agent repair loop inside Celery worker with timeout resilience."""
    corr_id = correlation_id or init_trace_context()
    bind_context(
        task_id=task_id,
        repo_id=repo_id,
        node_name="celery_worker",
        correlation_id=corr_id,
    )

    logger.info(
        "Starting agent task execution in worker",
        task_id=task_id,
        repo_id=repo_id,
        prompt_preview=prompt[:60],
        mode=mode,
    )

    try:
        return worker_execution_lifecycle(
            task_id=task_id,
            repo_id=repo_id,
            prompt=prompt,
            mode=mode,
            worktree_path=worktree_path,
            correlation_id=corr_id,
        )

    except SoftTimeLimitExceeded as timeout_exc:
        tb_str = traceback.format_exc()
        logger.error(
            "Task exceeded soft time limit (270s) - terminating to prevent Docker deadlock",
            task_id=task_id,
            repo_id=repo_id,
            traceback=tb_str,
        )
        push_to_dlq(
            task_id=task_id,
            task_name="forge.execute_agent_task",
            args=(),
            kwargs={
                "task_id": task_id,
                "repo_id": repo_id,
                "prompt": prompt,
            },
            error_msg="Execution exceeded soft time limit (270s). Worker terminated to prevent deadlock.",
            exception_type="SoftTimeLimitExceeded",
            traceback_str=tb_str,
            retry_count=getattr(self.request, "retries", 0),
            correlation_id=corr_id,
        )
        raise timeout_exc

    except Exception as exc:
        log_exception(
            logger,
            "Agent task failed during worker execution",
            exc,
            task_id=task_id,
            repo_id=repo_id,
        )
        # Re-raise to trigger Celery retry or on_failure DLQ routing
        raise exc
    finally:
        clear_context()


__all__ = [
    "celery_app",
    "execute_agent_task",
    "worker_execution_lifecycle",
    "worker_execution_wrapper",
    "push_to_dlq",
    "get_dlq_tasks",
    "clear_dlq_tasks",
    "ResilientForgeTask",
    "get_redis_client",
    "REDIS_DLQ_KEY",
    "REDIS_DLQ_EVENTS",
]
