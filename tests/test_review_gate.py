"""Integration coverage for the static human review gate and approval API."""

from __future__ import annotations

import uuid
from typing import Any, Dict, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from services.api.forge.engine.graph import is_static_reviewer_checkpoint, review_router
from services.api.routers import tasks as tasks_router


PR_URL = "https://github.com/acme/forge/pull/42"


class ReviewGateState(TypedDict, total=False):
    task_id: str
    test_results: Dict[str, Any]
    generated_diff: str
    decision: str
    review_decision: str
    review_status: str
    review_resume: Dict[str, Any]
    human_feedback: str
    pr_url: str
    status: str


@pytest.mark.asyncio
async def test_static_review_pause_and_approval_api_create_pr() -> None:
    """Pause before reviewer_node, approve through the API, and continue to PR creation."""
    received_decision: Dict[str, Any] = {}

    async def verifier_node(state: ReviewGateState) -> Dict[str, Any]:
        return {
            "test_results": {"exit_code": 0, "passed_count": 3},
            "status": "verified",
        }

    async def reviewer_node(state: ReviewGateState) -> Dict[str, Any]:
        nonlocal received_decision
        received_decision = dict(state.get("review_resume") or {})
        assert received_decision.get("approved") is True
        return {
            "decision": "approve",
            "review_decision": "approve",
            "review_status": "approved",
            "status": "approved",
        }

    async def pr_generator(state: ReviewGateState) -> Dict[str, Any]:
        assert state.get("review_status") == "approved"
        return {"pr_url": PR_URL, "status": "pr_created"}

    async def cleanup_node(state: ReviewGateState) -> Dict[str, Any]:
        return {"status": "cleaned_up"}

    builder = StateGraph(ReviewGateState)
    builder.add_node("verifier_node", verifier_node)
    builder.add_node("reviewer_node", reviewer_node)
    builder.add_node("pr_generator", pr_generator)
    builder.add_node("cleanup_node", cleanup_node)
    builder.add_edge(START, "verifier_node")
    builder.add_edge("verifier_node", "reviewer_node")
    builder.add_conditional_edges(
        "reviewer_node",
        review_router,
        {
            "pr_generator": "pr_generator",
            "implementer_node": "cleanup_node",
            "cleanup_node": "cleanup_node",
        },
    )
    builder.add_edge("pr_generator", END)
    builder.add_edge("cleanup_node", END)

    graph = builder.compile(
        checkpointer=MemorySaver(),
        interrupt_before=["reviewer_node"],
    )
    task_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": task_id}}

    await graph.ainvoke(
        {"task_id": task_id, "generated_diff": "diff --git a/app.py b/app.py"},
        config=config,
    )
    paused_snapshot = await graph.aget_state(config)
    assert paused_snapshot.next == ("reviewer_node",)
    assert is_static_reviewer_checkpoint(paused_snapshot)
    assert paused_snapshot.values.get("pr_url") is None

    db = AsyncMock()
    db.get.return_value = MagicMock(status="awaiting_approval")
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    redis.aclose = AsyncMock()

    with (
        patch.object(tasks_router, "get_compiled_graph", new_callable=AsyncMock, return_value=graph),
        patch.object(tasks_router.aioredis, "from_url", return_value=redis),
        patch.object(tasks_router.task_manager, "release_task_lock", new_callable=AsyncMock, return_value=None),
    ):
        response = await tasks_router.approve_task(
            task_id,
            tasks_router.TaskApproveRequest(approved=True),
            db,
        )

    final_snapshot = await graph.aget_state(config)
    assert response["status"] == "completed"
    assert response["pr_url"] == PR_URL
    assert final_snapshot.next == ()
    assert final_snapshot.values["pr_url"] == PR_URL
    assert received_decision == {
        "approved": True,
        "action": "approve",
        "decision": "approve",
        "feedback": "",
    }
    db.commit.assert_awaited_once()
    redis.publish.assert_awaited_once()
