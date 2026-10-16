"""Background worker executing the LangGraph agent and publishing events to Redis.

Streams execution progress from graph.astream(), serializes node outputs into JSON
(handling datetime, UUID, Pydantic, and LangChain objects), and publishes events to
the Redis channel 'forge:events:{task_id}' for real-time Server-Sent Events (SSE).
"""

from __future__ import annotations

import asyncio
import base64
from datetime import date, datetime, timezone
import json
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from sqlalchemy import select, update

from services.api.core.database import async_session_factory
from services.api.forge.orchestration.graph import get_compiled_graph
from services.api.forge.concurrency.locks import async_repo_lock
from services.api.forge.execution.cleanup import cleanup_worktree_atomic
from services.api.forge.orchestration.state import AgentState
from services.api.models.repository import Repository
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
REPOSITORY_CLONE_TIMEOUT_SECONDS = 60

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


async def _load_repository_source(repo_id: str) -> tuple[str, str]:
    """Resolve the stored clone URL and branch for a task repository."""
    try:
        repository_id = uuid.UUID(repo_id)
    except ValueError:
        if os.path.isdir(repo_id):
            return repo_id, "main"
        raise ValueError(f"Task repository '{repo_id}' is not a repository UUID or local path.")

    async with async_session_factory() as session:
        result = await session.execute(select(Repository).where(Repository.id == repository_id))
        repository = result.scalar_one_or_none()
    if repository is None:
        raise ValueError(f"Repository '{repo_id}' was not found; cannot prepare task checkout.")
    return repository.local_path, repository.default_branch or "main"


async def _clone_repository_to_worktree(
    source: str,
    default_branch: str,
    worktree_path: str,
    timeout_seconds: int = REPOSITORY_CLONE_TIMEOUT_SECONDS,
) -> str:
    """Clone an ingested repository into the shared task volume with bounded branch probing."""
    target = Path(worktree_path).resolve()
    base = Path(os.getenv("FORGE_WORKTREE_BASE_DIR", "/tmp/forge_worktrees")).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Task checkout '{target}' is outside the configured worktree root.") from exc
    if target == base or target.exists():
        raise ValueError(f"Task checkout path already exists or is invalid: '{target}'.")
    target.parent.mkdir(parents=True, exist_ok=True)

    branches = list(dict.fromkeys((default_branch, "main", "master")))
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        try:
            from services.api.core.config import settings

            token = str(getattr(settings, "GITHUB_TOKEN", "") or "").strip()
        except ImportError:
            token = ""

    auth_config: list[str] = []
    if token and "github.com" in source.lower():
        basic_auth = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        auth_config = [
            "-c",
            f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic_auth}",
        ]

    deadline = time.monotonic() + timeout_seconds
    errors: list[str] = []
    try:
        for branch in branches:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append(f"Clone exceeded the {timeout_seconds}-second timeout.")
                break
            command = [
                "git",
                *auth_config,
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                branch,
                "--",
                source,
                str(target),
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                errors.append(f"Clone exceeded the {timeout_seconds}-second timeout.")
                break
            except asyncio.CancelledError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                raise
            if process.returncode == 0:
                for directory, dirs, files in os.walk(target):
                    os.chmod(directory, os.stat(directory).st_mode | 0o222 | 0o111)
                    for name in dirs + files:
                        path = os.path.join(directory, name)
                        os.chmod(path, os.stat(path).st_mode | 0o222)
                return str(target)
            error = (stderr or stdout or b"git clone failed").decode(
                "utf-8",
                errors="replace",
            ).strip()
            if token:
                error = error.replace(token, "[REDACTED_TOKEN]")
            errors.append(f"{branch}: {error[:500]}")
            if target.exists():
                try:
                    shutil.rmtree(target)
                except OSError as cleanup_error:
                    logger.exception("Failed to clean partial repository clone at %s", target)
                    raise RuntimeError(
                        f"Could not clean partial repository clone at '{target}'."
                    ) from cleanup_error

        detail = "; ".join(errors) or "No clone branch candidates were available."
        raise RuntimeError(f"Could not clone repository using {branches}: {detail}")
    except BaseException:
        if target.exists():
            try:
                shutil.rmtree(target)
            except OSError:
                logger.exception("Failed to clean task checkout after clone failure: %s", target)
        raise


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

        objective = " ".join(prompt.split()) or "the requested change"
        await redis_client.publish(
            channel,
            serialize_to_json(
                {
                    "node": "planner_node",
                    "step": 1,
                    "status": "planning",
                    "event_type": "plan_preview",
                    "plan_phase": "draft",
                    "message": f"Draft plan for: {objective[:100]}",
                    "execution_plan": [
                        {
                            "step": 1,
                            "title": f"Scope: {objective[:80]}",
                            "description": "Identify the affected files, symbols, and dependencies.",
                        },
                        {
                            "step": 2,
                            "title": "Implement the requested change",
                            "description": f"Make a focused change that addresses: {objective[:180]}",
                        },
                        {
                            "step": 3,
                            "title": "Verify the change",
                            "description": "Run focused regression tests and inspect the resulting diff.",
                        },
                    ],
                }
            ),
        )

        try:
            source_url, default_branch = await _load_repository_source(repo_id)
            effective_worktree_path = await _clone_repository_to_worktree(
                source_url,
                default_branch,
                effective_worktree_path,
                timeout_seconds=REPOSITORY_CLONE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            logger.info("Task %s was aborted while preparing its repository checkout", task_id)
            try:
                from services.api.forge.engine.manager import task_manager

                await task_manager.record_task_end(
                    task_id=task_id,
                    status="ABORTED",
                    repo_id=repo_id,
                )
                await task_manager.release_task_lock(task_id, repo_id, status="ABORTED")
            except Exception:
                logger.exception("Could not finalize aborted checkout for task %s", task_id)
            try:
                cleanup_worktree_atomic(
                    repo_id if os.path.isdir(repo_id) else os.getcwd(),
                    effective_worktree_path,
                    task_id,
                )
            except Exception as cleanup_error:
                logger.warning(
                    "Could not clean aborted checkout for %s: %s",
                    task_id,
                    cleanup_error,
                )
            RUNNING_TASKS.pop(task_id, None)
            await redis_client.aclose()
            raise
        except Exception as checkout_error:
            logger.exception("Could not prepare task checkout for %s", task_id)
            await redis_client.publish(
                channel,
                serialize_to_json(
                    {
                        "node": "error",
                        "data": {"error": str(checkout_error)},
                        "step": 1,
                        "status": "failed",
                        "message": f"[Error] Could not prepare repository checkout: {checkout_error}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            )
            try:
                from services.api.forge.engine.manager import task_manager

                await task_manager.record_task_end(
                    task_id=task_id,
                    status="FAILED",
                    repo_id=repo_id,
                )
                await task_manager.release_task_lock(task_id, repo_id, status="FAILED")
            except Exception as finalize_error:
                logger.exception("Could not finalize task %s after checkout failure", task_id)
                logger.error("Checkout failure finalization error: %s", finalize_error)
            try:
                cleanup_worktree_atomic(
                    repo_id if os.path.isdir(repo_id) else os.getcwd(),
                    effective_worktree_path,
                    task_id,
                )
            except Exception as cleanup_error:
                logger.warning(
                    "Could not clean failed checkout for %s: %s",
                    task_id,
                    cleanup_error,
                )
            RUNNING_TASKS.pop(task_id, None)
            await redis_client.aclose()
            return

        sequence = 1
        current_status = "running"
        terminal_status = "COMPLETED"
        failed_node: Optional[str] = None
        terminal_error: Optional[str] = None
        dispatcher = None
        dispatcher_started_here = False

        try:
            # Update Task status in PostgreSQL if task record exists
            try:
                async with async_session_factory() as session:
                    task_uuid = uuid.UUID(task_id)
                    await session.execute(
                        update(Task)
                        .where(
                            Task.id == task_uuid,
                            Task.status.notin_(
                                ("aborted", "cancelled", "cleaned_up", "completed", "failed")
                            ),
                        )
                        .values(status="running", start_time=start_time)
                    )
                    await session.commit()
            except Exception as db_err:
                logger.debug("Database task update skipped or non-critical: %s", db_err)

            from services.api.forge.engine.dispatcher import get_telemetry_dispatcher

            dispatcher = get_telemetry_dispatcher()
            if not dispatcher.is_running:
                await dispatcher.start()
                dispatcher_started_here = True

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
                        if node_status == "failed":
                            failed_node = node_name
                            terminal_error = (
                                node_output.get("error_message")
                                or (node_output.get("error_payload") or {}).get("message")
                                or node_output.get("error")
                                or f"{node_name} failed."
                                if isinstance(node_output, dict)
                                else f"{node_name} failed."
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
                            if node_status == "awaiting_approval":
                                await session.execute(
                                    update(Task)
                                    .where(
                                        Task.id == task_uuid,
                                        Task.status.notin_(("aborted", "cancelled", "cleaned_up")),
                                    )
                                    .values(status=node_status)
                                )
                            await session.commit()
                    except Exception as evt_err:
                        logger.debug("Persisting task event skipped: %s", evt_err)

            if current_status != "awaiting_approval":
                terminal_status = "FAILED" if failed_node else "COMPLETED"
                terminal_payload = {
                    "node": failed_node or "workflow",
                    "data": {"error": terminal_error} if terminal_error else {},
                    "step": STEP_METADATA.get(failed_node or "", {}).get("step", 0),
                    "status": terminal_status.lower(),
                    "message": (
                        f"[Failed] {failed_node or 'Workflow'}: {terminal_error}"
                        if terminal_error
                        else "[Completed] Workflow execution finished successfully."
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                await redis_client.publish(channel, serialize_to_json(terminal_payload))
                sequence += 1
                try:
                    async with async_session_factory() as session:
                        task_uuid = uuid.UUID(task_id)
                        session.add(
                            TaskEvent(
                                task_id=task_uuid,
                                sequence=sequence,
                                event_type="workflow_terminal",
                                payload=terminal_payload,
                            )
                        )
                        await session.execute(
                            update(Task)
                            .where(
                                Task.id == task_uuid,
                                Task.status.notin_(("aborted", "cancelled", "cleaned_up")),
                            )
                            .values(
                                status=terminal_status.lower(),
                                end_time=datetime.utcnow(),
                            )
                        )
                        await session.commit()
                except Exception as terminal_err:
                    logger.warning(
                        "Could not persist terminal status for task %s: %s",
                        task_id,
                        terminal_err,
                    )
                try:
                    from services.api.forge.engine.manager import task_manager

                    await task_manager.record_task_end(
                        task_id=task_id, status=terminal_status, repo_id=repo_id
                    )
                except Exception as rec_err:
                    logger.debug("Worker record_task_end notice: %s", rec_err)

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
                        update(Task)
                        .where(
                            Task.id == task_uuid,
                            Task.status.notin_(
                                ("aborted", "cancelled", "cleaned_up", "completed")
                            ),
                        )
                        .values(status="failed")
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

            if dispatcher_started_here and dispatcher is not None:
                try:
                    await dispatcher.stop()
                except Exception as dispatcher_err:
                    logger.warning(
                        "worker: telemetry dispatcher shutdown failed for %s: %s",
                        task_id,
                        dispatcher_err,
                    )
            await redis_client.aclose()
            logger.info("run_graph_and_publish finished for task_id='%s'", task_id)


# Backward-compatibility alias
run_task_and_publish = run_graph_and_publish
