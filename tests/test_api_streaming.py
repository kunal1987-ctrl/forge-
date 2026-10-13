"""Phase 4 FastAPI & Event-Driven SSE Integration Test.

Tests the FastAPI REST API and Server-Sent Events (SSE) endpoints:
- POST /api/tasks: Returns immediate HTTP 202 Accepted with a valid task_id.
- GET /api/tasks/{id}/events: Streams live Server-Sent Events from Redis and verifies
  valid JSON event payloads with correct node transitions.
- POST /api/tasks/{id}/approve: Resumes a paused LangGraph execution and completes the task.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import redis.asyncio as aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from services.api.core.config import settings
from services.api.forge.execution.worktrees import WorktreeManager
from services.api.forge.orchestration.graph import get_compiled_graph
from services.api.forge.orchestration.state import AgentState
from services.api.main import app


@pytest.mark.asyncio
async def test_post_task_immediate_202_accepted():
    """Verify POST /api/tasks returns 202 Accepted immediately with valid task_id."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "prompt": "Fix broken division function in calculator.py",
            "mode": "fast",
        }
        # Mock background task runner to avoid firing full autonomous agent in simple endpoint test
        with patch("services.api.routers.tasks.run_graph_and_publish"):
            response = await client.post("/api/tasks", json=payload)

        assert response.status_code == 202, (
            f"Expected 202 Accepted, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "task_id" in data
        # Validate task_id is a valid UUID
        parsed_uuid = uuid.UUID(data["task_id"])
        assert str(parsed_uuid) == data["task_id"]
        assert data.get("status") == "pending"
        assert data.get("mode") == "fast"


@pytest.mark.asyncio
async def test_get_task_events_sse_streaming():
    """Verify GET /api/tasks/{id}/events streams SSE JSON packets representing node transitions."""
    task_id = f"test-sse-{uuid.uuid4().hex[:8]}"
    redis_url = settings.REDIS_URL or "redis://localhost:6379"

    # Pre-define sequence of node transition events
    expected_transitions = [
        {
            "node": "planner_node",
            "step": 1,
            "status": "planning",
            "message": "Formulated implementation plan with 3 steps",
        },
        {
            "node": "investigator_node",
            "step": 2,
            "status": "investigating",
            "message": "Retrieved 4 relevant codebase context chunks",
        },
        {
            "node": "implementer_node",
            "step": 3,
            "status": "implementing",
            "message": "Synthesized unified git diff",
        },
        {
            "node": "verifier_node",
            "step": 4,
            "status": "verifying",
            "message": "Executed sandboxed test suite",
        },
        {
            "node": "reviewer_node",
            "step": 5,
            "status": "completed",
            "message": "Approved: Patch approved by reviewer.",
        },
    ]

    async def publisher():
        # Wait for client to connect to SSE stream
        await asyncio.sleep(0.2)
        r = aioredis.from_url(redis_url, decode_responses=True)
        channel = f"forge:events:{task_id}"
        for evt in expected_transitions:
            await r.publish(channel, json.dumps(evt))
            await asyncio.sleep(0.05)
        await r.aclose()

    pub_task = asyncio.create_task(publisher())

    transport = httpx.ASGITransport(app=app)
    received_events = []

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=5.0
        ) as client:
            async with client.stream("GET", f"/api/tasks/{task_id}/events") as response:
                assert response.status_code == 200
                assert "text/event-stream" in response.headers.get("content-type", "")

                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        raw_data = line[len("data:") :].strip()
                        if raw_data:
                            parsed = json.loads(raw_data)
                            received_events.append(parsed)
                            if parsed.get("status") in ("completed", "failed"):
                                break
    finally:
        await pub_task

    # Validate received event sequence
    assert len(received_events) >= 1
    received_nodes = [e.get("node") for e in received_events if "node" in e]
    assert "planner_node" in received_nodes
    assert "reviewer_node" in received_nodes


@pytest.mark.asyncio
async def test_post_task_approve_resumes_paused_graph(tmp_path):
    """Verify POST /api/tasks/{id}/approve wakes up a paused LangGraph execution and completes."""
    task_id = f"test-approve-{uuid.uuid4().hex[:8]}"

    # Setup dummy repo as a valid Git repository
    repo_path = tmp_path / "repo_approve"
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "app.py").write_text("def hello(): return 'world'\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tester@forge.ai"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Forge Tester"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-M", "main"], cwd=str(repo_path), check=True, capture_output=True
    )

    wm = WorktreeManager(str(repo_path))
    worktree_path = wm.create_worktree(task_id, base_branch="main")

    checkpointer = MemorySaver()
    graph = await get_compiled_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": task_id}}

    initial_state: AgentState = {
        "task_id": task_id,
        "repo_id": str(repo_path),
        "commit_sha": "main",
        "prompt": "Test approval resumption in app.py",
        "worktree_path": worktree_path,
        "messages": [],
        "context_retrieved": [],
        "investigation_step": 0,
        "max_investigation_steps": 3,
        "investigation_complete": True,
        "investigation_history": [],
        "retrieval_confidence": "high",
        "plan": {"target_files": ["app.py"], "logic_steps": ["test"]},
        "generated_diff": "",
        "test_results": {},
        "retry_count": 0,
        "status": "awaiting_approval",
        "repair_trajectory": [],
        "sanitized_error": "",
    }

    test_runs = 0

    def mock_run_tests(
        worktree_path, command="pytest", timeout_seconds=45, test_command=None, **kwargs
    ):
        nonlocal test_runs
        cmd = test_command or command
        if "tests/_forge_repro_test.py" in str(cmd) and test_runs == 0:
            test_runs += 1
            return {
                "exit_code": 1,
                "passed_count": 0,
                "failed_count": 1,
                "raw_output": "AssertionError: expected failure before fix",
                "stderr": "Defect verified",
                "status": "failed",
            }
        test_runs += 1
        return {
            "exit_code": 0,
            "passed_count": 1,
            "failed_count": 0,
            "raw_output": "1 passed",
            "stdout": "1 passed",
            "stderr": "",
            "status": "passed",
        }

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=(
                "### File: app.py\n"
                "<<<<<<< SEARCH\n"
                "def hello(): return 'world'\n"
                "=======\n"
                "def hello(): return 'world'\n# approved\n"
                ">>>>>>> REPLACE"
            )
        )
    )
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(
        return_value=MagicMock(
            thought="Plan",
            target_files=["app.py"],
            logic_steps=["modify"],
            reproduction_test_code="def test_defect():\n    assert False\n",
            verdict="sufficient",
            missing_info=[],
            next_query=None,
            decision="approve",
            risk_level="LOW",
            confidence_score=1.0,
            feedback="LGTM",
        )
    )
    mock_llm.with_structured_output.return_value = mock_structured

    async def mock_get_graph(uri=None, checkpointer=None):
        return graph

    transport = httpx.ASGITransport(app=app)
    with (
        patch(
            "services.api.forge.execution.sandbox.DockerSandbox.run_tests",
            side_effect=mock_run_tests,
        ),
        patch("services.api.forge.orchestration.nodes.get_llm", return_value=mock_llm),
        patch("services.api.core.config.settings.OPENAI_API_KEY", "mock-test-key"),
        patch("services.api.routers.tasks.get_compiled_graph", side_effect=mock_get_graph),
    ):
        async for _ in graph.astream(initial_state, config=config):
            pass

        state_snap = await graph.aget_state(config)
        assert state_snap.next == ("reviewer_node",)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            approve_resp = await client.post(
                f"/api/tasks/{task_id}/approve",
                json={"decision": "approve", "feedback": "LGTM"},
            )

        assert approve_resp.status_code == 200
        resp_data = approve_resp.json()
        assert resp_data["task_id"] == task_id
        assert resp_data["status"] == "completed"

        # Verify graph resumed and finished
        resumed_snap = await graph.aget_state(config)
        assert resumed_snap.next == ()  # Reached END node
        assert resumed_snap.values.get("status") in ("pr_created", "cleaned_up", "completed")
