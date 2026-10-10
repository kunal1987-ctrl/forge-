"""Background worker executing the LangGraph agent and publishing events to Redis.

Streams execution progress from forge_app.astream, constructs standardized task
events, and publishes JSON payloads to Redis Pub/Sub channels for real-time SSE streaming.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from sqlalchemy import select, update

from services.api.core.database import async_session_factory
from services.api.forge.orchestration.graph import forge_app
from services.api.forge.orchestration.state import AgentState
from services.api.models.task import Task, TaskEvent

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Mapping of LangGraph node names to UI step indices and descriptive log tags
STEP_METADATA: Dict[str, Dict[str, Any]] = {
    "planner_node": {
        "step": 1,
        "tag": "Plan",
        "default_message": "Formulated structured execution plan and search queries",
    },
    "investigator_node": {
        "step": 2,
        "tag": "Investigate",
        "default_message": "Retrieved relevant code symbols and hybrid RAG context",
    },
    "implementer_node": {
        "step": 3,
        "tag": "Code",
        "default_message": "Synthesized unified diff and applied changes to worktree",
    },
    "verifier_node": {
        "step": 4,
        "tag": "Tests",
        "default_message": "Executed sandboxed test suite inside Docker",
    },
    "reviewer_node": {
        "step": 5,
        "tag": "Review",
        "default_message": "Completed architectural and security review",
    },
}


async def run_task_and_publish(task_id: str, prompt: str, repo_id: str) -> None:
    """Execute the FORGE LangGraph workflow and stream live events to Redis.

    Args:
        task_id: Unique string identifier of the task.
        prompt: Task goal and instructions.
        repo_id: UUID string of the repository being targeted.
    """
    logger.info("Starting background task run for task_id='%s'", task_id)
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    channel = f"task_events:{task_id}"

    # Initialize graph state
    initial_state: AgentState = {
        "task_id": task_id,
        "repo_id": repo_id,
        "commit_sha": "HEAD",
        "prompt": prompt,
        "worktree_path": f"/tmp/forge/worktrees/{task_id}",
        "retry_count": 0,
        "status": "running",
    }

    # Initial start event
    start_payload = {
        "step": 1,
        "message": f"[Init] Started autonomous task: {prompt[:80]}",
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await redis_client.publish(channel, json.dumps(start_payload))

    sequence = 1
    current_status = "running"

    try:
        # Update Task status in PostgreSQL
        async with async_session_factory() as session:
            try:
                task_uuid = uuid.UUID(task_id)
                await session.execute(
                    update(Task).where(Task.id == task_uuid).values(status="running")
                )
                await session.commit()
            except Exception as db_err:
                logger.warning("Could not update task initial status in DB: %s", db_err)

        # Stream through the LangGraph StateGraph
        async for output in forge_app.astream(initial_state):
            # output is a dict like {'node_name': {state_updates}}
            for node_name, node_output in output.items():
                meta = STEP_METADATA.get(node_name, {"step": 1, "tag": node_name, "default_message": "Working..."})
                step_num = meta["step"]
                tag = meta["tag"]
                node_status = node_output.get("status", "running")

                # Build descriptive message
                if node_name == "planner_node":
                    plan = node_output.get("plan", {})
                    summary = plan.get("summary") or meta["default_message"]
                    message = f"[{tag}] {summary}"
                elif node_name == "investigator_node":
                    chunks = node_output.get("context_retrieved", [])
                    message = f"[{tag}] Found {len(chunks)} relevant context blocks"
                elif node_name == "implementer_node":
                    diff = node_output.get("generated_diff", "")
                    lines = len(diff.splitlines())
                    message = f"[{tag}] Generated unified diff ({lines} lines)"
                elif node_name == "verifier_node":
                    test_res = node_output.get("test_results", {})
                    code = test_res.get("exit_code", 0)
                    if code == 0:
                        message = f"[{tag}] Pytest test suite passed successfully (exit_code=0)"
                    else:
                        message = f"[{tag}] Test failures detected (exit_code={code}), triggering repair loop"
                elif node_name == "reviewer_node":
                    current_status = "awaiting_approval"
                    node_status = "awaiting_approval"
                    message = f"[{tag}] Ready for engineer review and approval"
                else:
                    message = f"[{tag}] {meta['default_message']}"

                event_payload = {
                    "step": step_num,
                    "message": message,
                    "status": node_status,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                # Publish to Redis channel for SSE
                await redis_client.publish(channel, json.dumps(event_payload))
                sequence += 1

                # Persist TaskEvent to Postgres
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
                                update(Task).where(Task.id == task_uuid).values(status=node_status)
                            )
                        await session.commit()
                except Exception as evt_err:
                    logger.debug("Failed saving task event to DB: %s", evt_err)

    except Exception as exc:
        logger.exception("Error executing task in worker: %s", exc)
        error_payload = {
            "step": 5,
            "message": f"[Error] Task encountered exception: {exc}",
            "status": "failed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await redis_client.publish(channel, json.dumps(error_payload))
        try:
            async with async_session_factory() as session:
                task_uuid = uuid.UUID(task_id)
                await session.execute(
                    update(Task).where(Task.id == task_uuid).values(status="failed")
                )
                await session.commit()
        except Exception:
            pass
    finally:
        await redis_client.aclose()
        logger.info("Completed task execution and event publishing for task_id='%s'", task_id)
