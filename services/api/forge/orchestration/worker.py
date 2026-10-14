"""Background worker executing the LangGraph agent and publishing events to Redis.

Streams execution progress from graph.astream(), serializes node outputs into JSON
(handling datetime, UUID, Pydantic, and LangChain objects), and publishes events to
the Redis channel 'forge:events:{task_id}' for real-time Server-Sent Events (SSE).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import json
import logging
import os
import sys
import uuid
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from sqlalchemy import update

from services.api.core.database import async_session_factory
from services.api.forge.orchestration.graph import get_compiled_graph
from services.api.forge.concurrency.locks import async_repo_lock
from services.api.forge.execution.cleanup import cleanup_worktree_atomic
from services.api.forge.orchestration.state import AgentState
from services.api.models.task import Task, TaskEvent

logger = logging.getLogger(__name__)

# Ensure selector event loop on Windows for psycopg async compatibility on older Python versions
if sys.platform == "win32" and sys.version_info < (3, 14):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except (AttributeError, RuntimeError) as loop_err:
        logger.debug("WindowsSelectorEventLoopPolicy not set: %s", loop_err)

DEFAULT_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DEFAULT_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://forge:forge_password@localhost:5432/forge_db",
)

# Global registry of active background asyncio tasks for immediate cancellation
RUNNING_TASKS: Dict[str, asyncio.Task[Any]] = {}

# Step metadata mapping for UI visualization and standardized logging
STEP_METADATA: Dict[str, Dict[str, Any]] = {
    "planner_node": {
        "step": 1,
        "tag": "Plan",
        "description": "Formulated structured execution plan",
    },
    "investigator_node": {
        "step": 2,
        "tag": "Investigate",
        "description": "Retrieved codebase context and symbol references",
    },
    "implementer_node": {
        "step": 3,
        "tag": "Code",
        "description": "Synthesized unified diff and applied changes to worktree",
    },
    "verifier_node": {
        "step": 4,
        "tag": "Tests",
        "description": "Executed sandboxed test suite",
    },
    "reviewer_node": {
        "step": 5,
        "tag": "Review",
        "description": "Completed architectural and security review",
    },
}


def serialize_to_json(data: Any) -> str:
    """Safely serialize nested state dictionaries including datetime, UUID, and Pydantic models to JSON."""

    def default_serializer(obj: Any) -> Any:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return str(obj)

    return json.dumps(data, default=default_serializer)


async def run_graph_and_publish(
    task_id: str,
    prompt: str,
    db_uri: str = DEFAULT_DATABASE_URL,
    redis_uri: str = DEFAULT_REDIS_URL,
    mode: str = "fast",
    worktree_path: Optional[str] = None,
    repo_id: str = "default-repo",
) -> None:
    """Execute the FORGE LangGraph workflow and publish node state updates to Redis.

    Connects to Redis Pub/Sub, compiles the StateGraph with PostgreSQL checkpointing,
    and streams through nodes while publishing JSON payloads to 'forge:events:{task_id}'.

    Args:
        task_id: Unique string identifier of the task (thread_id).
        prompt: Task goal and instruction prompt.
        db_uri: PostgreSQL connection URI for LangGraph checkpointer and persistence.
        redis_uri: Redis connection URI for real-time pub/sub messaging.
        mode: Execution model mode ('fast' or 'deep'). Defaults to 'fast'.
        worktree_path: Optional explicit worktree path override.
        repo_id: Repository identifier string for distributed locking.
    """
    logger.info("run_graph_and_publish started for task_id='%s' (mode='%s')", task_id, mode)
    effective_redis_url = redis_uri or DEFAULT_REDIS_URL
    effective_db_url = db_uri or DEFAULT_DATABASE_URL

    redis_client = aioredis.from_url(effective_redis_url, decode_responses=True)
    channel = f"forge:events:{task_id}"

    # 1. Acquire exclusive Redis distributed lock before provisioning git worktree
    async with async_repo_lock(
        repo_id,
        redis_url=effective_redis_url,
        timeout=120.0,
        blocking=True,
        blocking_timeout=30.0,
    ):
        # 2. Provision UUID-suffixed ephemeral worktree path
        import re

        safe_task_id = re.sub(r"[^a-zA-Z0-9_-]", "", task_id)
        base_wt_dir = os.getenv("FORGE_WORKTREE_BASE_DIR", "/tmp/forge_worktrees")
        effective_worktree_path = (
            worktree_path
            if worktree_path
            else os.path.abspath(
                os.path.join(base_wt_dir, f"{safe_task_id}_{uuid.uuid4().hex[:8]}")
            )
        )

        # Initial graph state explicitly bound to thread-safe AgentState
        from services.api.forge.orchestration.graph import create_initial_agent_state
        from services.api.forge.engine.manager import task_manager

        # Record start_time when task pipeline is dispatched and execution begins
        start_time = datetime.utcnow()
        await task_manager.record_task_start(
            task_id=task_id, repo_id=repo_id, start_time=start_time
        )

        initial_state: AgentState = create_initial_agent_state(
            task_id=task_id,
            repo_id=repo_id,
            commit_sha="main",
            prompt=prompt,
            worktree_path=effective_worktree_path,
            mode=mode,
        )

        # Publish initialization event
        start_payload = {
            "node": "init",
            "data": {"task_id": task_id, "prompt": prompt},
            "step": 1,
            "status": "running",
            "message": f"[Init] Started autonomous task: {prompt[:80]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "start_time": start_time.isoformat(),
        }
        await redis_client.publish(channel, serialize_to_json(start_payload))

        sequence = 1
        current_status = "running"
        terminal_status = "COMPLETED"

        try:
            # Update Task status in PostgreSQL if task record exists
            try:
                async with async_session_factory() as session:
                    task_uuid = uuid.UUID(task_id)
                    await session.execute(
                        update(Task)
                        .where(Task.id == task_uuid)
                        .values(status="running", start_time=start_time)
                    )
                    await session.commit()
            except Exception as db_err:
                logger.debug("Database task update skipped or non-critical: %s", db_err)

            # Compile LangGraph state machine with PostgreSQL checkpointing
            graph = await get_compiled_graph(effective_db_url)
            config = {"configurable": {"thread_id": task_id}}

            # Stream graph node execution
            async for output in graph.astream(initial_state, config=config):
                for node_name, node_output in output.items():
                    if node_name == "__interrupt__":
                        current_status = "awaiting_approval"
                        node_status = "awaiting_approval"
                        step_num = 5
                        interrupt_val = (
                            node_output[0].value
                            if (node_output and hasattr(node_output[0], "value"))
                            else (node_output[0] if node_output else {})
                        )
                        state_data = interrupt_val
                        message = "[Review] Graph paused: Awaiting human approval for code diff"
                        logger.info("Task %s paused at interrupt for human review", task_id)
                    else:
                        meta = STEP_METADATA.get(
                            node_name,
                            {"step": 1, "tag": node_name, "description": "Processing step"},
                        )
                        step_num = meta["step"]
                        tag = meta["tag"]
                        node_status = (
                            node_output.get("status", "running")
                            if isinstance(node_output, dict)
                            else "running"
                        )
                        state_data = node_output

                        # Format human-readable progress message
                        if node_name == "planner_node":
                            plan = (
                                node_output.get("plan", {}) if isinstance(node_output, dict) else {}
                            )
                            steps = plan.get("logic_steps", [])
                            message = (
                                f"[{tag}] Formulated implementation plan with {len(steps)} steps"
                                if steps
                                else f"[{tag}] {meta['description']}"
                            )
                        elif node_name == "investigator_node":
                            chunks = (
                                node_output.get("context_retrieved", [])
                                if isinstance(node_output, dict)
                                else []
                            )
                            message = (
                                f"[{tag}] Retrieved {len(chunks)} relevant codebase context chunks"
                            )
                        elif node_name == "implementer_node":
                            diff = (
                                node_output.get("generated_diff", "")
                                if isinstance(node_output, dict)
                                else ""
                            )
                            diff_lines = len(diff.splitlines())
                            message = f"[{tag}] Synthesized unified git diff ({diff_lines} lines)"
                        elif node_name == "verifier_node":
                            test_res = (
                                node_output.get("test_results", {})
                                if isinstance(node_output, dict)
                                else {}
                            )
                            code = test_res.get("exit_code", 0)
                            message = (
                                f"[{tag}] Verification tests passed (exit_code=0)"
                                if code == 0
                                else f"[{tag}] Test failures detected (exit_code={code}), entering repair loop"
                            )
                        elif node_name == "reviewer_node":
                            current_status = node_status
                            message = f"[{tag}] Security review completed: {node_status}"
                        else:
                            message = f"[{tag}] {meta['description']}"

                    event_payload = {
                        "node": node_name,
                        "data": state_data,
                        "step": step_num,
                        "status": node_status,
                        "message": message,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

                    if isinstance(state_data, dict):
                        if "test_suite_passed" in state_data:
                            event_payload["test_suite_passed"] = state_data["test_suite_passed"]
                        elif "passed_count" in state_data:
                            event_payload["test_suite_passed"] = state_data["passed_count"]
                        elif "test_results" in state_data and isinstance(
                            state_data["test_results"], dict
                        ):
                            event_payload["test_suite_passed"] = state_data["test_results"].get(
                                "passed_count", 0
                            )

                        if "retry_count" in state_data:
                            event_payload["retry_count"] = state_data["retry_count"]

                        if "target_scope" in state_data:
                            event_payload["target_scope"] = state_data["target_scope"]
                        elif "execution_plan" in state_data and isinstance(
                            state_data["execution_plan"], dict
                        ):
                            tf = state_data["execution_plan"].get("target_files", [])
                            if tf:
                                event_payload["target_scope"] = tf[0]

                        if "pr_target" in state_data:
                            event_payload["pr_target"] = state_data["pr_target"]
                        else:
                            event_payload["pr_target"] = f"forge/auto-patch-{task_id}"

                    # Publish event to Redis channel
                    await redis_client.publish(channel, serialize_to_json(event_payload))
                    sequence += 1

                    # Persist event in PostgreSQL if configured
                    try:
                        async with async_session_factory() as session:
                            task_uuid = uuid.UUID(task_id)
                            evt = TaskEvent(
                                task_id=task_uuid,
                                sequence=sequence,
                                event_type=f"node_{node_name}",
                                payload=event_payload,
                            )
                            session.add(evt)
                            if node_status in ("awaiting_approval", "completed", "failed"):
                                await session.execute(
                                    update(Task)
                                    .where(Task.id == task_uuid)
                                    .values(status=node_status)
                                )
                            await session.commit()
                    except Exception as evt_err:
                        logger.debug("Persisting task event skipped: %s", evt_err)

            if current_status != "awaiting_approval":
                terminal_status = "COMPLETED"
                try:
                    from services.api.forge.engine.manager import task_manager

                    await task_manager.record_task_end(
                        task_id=task_id, status=terminal_status, repo_id=repo_id
                    )
                except Exception as rec_err:
                    logger.debug("Worker record_task_end completed notice: %s", rec_err)

        except asyncio.CancelledError:
            logger.info("run_graph_and_publish cancelled for task %s", task_id)
            terminal_status = "ABORTED"
            try:
                from services.api.forge.engine.manager import task_manager

                await task_manager.record_task_end(
                    task_id=task_id, status="ABORTED", repo_id=repo_id
                )
            except Exception as rec_err:
                logger.debug("Worker record_task_end aborted notice: %s", rec_err)
            cancel_payload = {
                "node": "cleanup_node",
                "data": {"status": "cleaned_up"},
                "step": 0,
                "status": "cleaned_up",
                "message": "[Cancelled] Task execution was cancelled by user.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await redis_client.publish(channel, serialize_to_json(cancel_payload))
            except Exception as pub_err:
                logger.warning("worker: cancel publish failed: %s", pub_err)
            try:
                async with async_session_factory() as session:
                    task_uuid = uuid.UUID(task_id)
                    from sqlalchemy import select

                    curr_stmt = select(Task.status).where(Task.id == task_uuid)
                    curr_res = await session.execute(curr_stmt)
                    curr_status = curr_res.scalar_one_or_none()
                    if curr_status != "aborted":
                        await session.execute(
                            update(Task).where(Task.id == task_uuid).values(status="cleaned_up")
                        )
                        await session.commit()
            except Exception as db_err:
                logger.warning("worker: task cleaned_up update failed: %s", db_err)
            raise
        except Exception as exc:
            logger.exception("Error executing graph worker for task %s: %s", task_id, exc)
            terminal_status = "FAILED"
            try:
                from services.api.forge.engine.manager import task_manager

                await task_manager.record_task_end(
                    task_id=task_id, status="FAILED", repo_id=repo_id
                )
            except Exception as rec_err:
                logger.debug("Worker record_task_end failed notice: %s", rec_err)
            error_payload = {
                "node": "error",
                "data": {"error": str(exc)},
                "step": 5,
                "status": "failed",
                "message": f"[Error] Task execution error: {exc}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await redis_client.publish(channel, serialize_to_json(error_payload))
            try:
                async with async_session_factory() as session:
                    task_uuid = uuid.UUID(task_id)
                    await session.execute(
                        update(Task).where(Task.id == task_uuid).values(status="failed")
                    )
                    await session.commit()
            except Exception as db_err:
                logger.warning("worker: task failed update failed: %s", db_err)
        finally:
            # 3. Guaranteed ephemeral worktree cleanup calling git worktree remove --force
            repo_dir = repo_id if (repo_id and os.path.exists(repo_id)) else os.getcwd()
            try:
                cleanup_worktree_atomic(repo_dir, effective_worktree_path, task_id)
            except Exception as wt_err:
                logger.debug("Worker worktree cleanup notice: %s", wt_err)

            RUNNING_TASKS.pop(task_id, None)

            # Release engine concurrency lock and dispatch next queued task if present
            try:
                from services.api.forge.engine.manager import task_manager

                next_queued = await task_manager.release_task_lock(
                    task_id, repo_id, status=terminal_status
                )
                if next_queued:
                    try:
                        from services.api.routers.tasks import _trigger_queued_task

                        asyncio.create_task(_trigger_queued_task(next_queued))
                    except Exception as trig_err:
                        logger.debug("Worker trigger queued task notice: %s", trig_err)
            except Exception as lock_err:
                logger.debug("Worker lock release notice: %s", lock_err)

            await redis_client.aclose()
            logger.info("run_graph_and_publish finished for task_id='%s'", task_id)


# Backward-compatibility alias
run_task_and_publish = run_graph_and_publish
