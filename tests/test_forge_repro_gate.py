"""Unit and integration tests for FORGE Reproduction-First (TDD) Gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from services.api.forge.orchestration.graph import (
    investigation_router,
    planner_node as live_planner_node,
    verify_router,
    workflow,
)
from services.api.forge.orchestration.nodes import repro_gate_node, verifier_node
from services.api.forge.orchestration.state import AgentState


@pytest.mark.asyncio
async def test_live_graph_planner_streams_prompt_specific_draft_before_llm():
    emitted_events = []
    call_order = []

    async def record_emit(**kwargs):
        emitted_events.append(kwargs)
        call_order.append(kwargs["event_type"])

    async def run_base_planner(_state):
        call_order.append("base_planner")
        return {
            "execution_plan": {"target_files": ["src/example.py"]},
            "plan": {"target_files": ["src/example.py"]},
        }

    state: AgentState = {
        "task_id": "task-live-plan-preview",
        "prompt": "Fix the planner timeout in services/api/forge/engine/nodes.py",
        "repo_id": "repo-1",
        "mode": "fast",
    }
    dispatcher = MagicMock()
    dispatcher.emit = AsyncMock(side_effect=record_emit)

    with (
        patch(
            "services.api.forge.engine.graph.get_telemetry_dispatcher",
            return_value=dispatcher,
        ),
        patch(
            "services.api.forge.engine.graph._base_planner_node",
            side_effect=run_base_planner,
        ),
    ):
        await live_planner_node(state)

    preview = next(event for event in emitted_events if event["event_type"] == "plan_preview")
    assert call_order.index("plan_preview") < call_order.index("base_planner")
    assert preview["payload"]["plan_phase"] == "draft"
    assert "Fix the planner timeout" in preview["payload"]["execution_plan"][0]["title"]
    assert preview["payload"]["execution_plan"]


@pytest.mark.asyncio
async def test_repro_gate_red_phase_failure_verified(tmp_path):
    """Test that when test fails (exit_code != 0), repro_verified_failing is True and status is repro_verified."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    state: AgentState = {
        "task_id": "task-tdd-1",
        "worktree_path": str(worktree),
        "prompt": "Fix bug in calculate_discount() when price is 0",
        "repro_test_code": (
            "from calculator import calculate_discount\n\n"
            "def test_zero_price_discount():\n"
            "    assert calculate_discount(0) == 0\n"
        ),
        "repro_test_path": "tests/_forge_repro_test.py",
        "status": "planning",
    }

    mock_run = MagicMock(
        return_value={
            "exit_code": 1,
            "passed_count": 0,
            "failed_count": 1,
            "stderr": "AssertionError",
        }
    )
    with patch("services.api.forge.execution.sandbox.DockerSandbox.run_tests", mock_run):
        res = await repro_gate_node(state)

    assert res["repro_verified_failing"] is True
    assert res["status"] == "repro_verified"
    assert res["repro_test_path"] == "tests/_forge_repro_test.py"

    test_file = worktree / "tests" / "_forge_repro_test.py"
    assert test_file.exists()
    assert "test_zero_price_discount" in test_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_repro_gate_red_phase_retries_on_passing_test(tmp_path):
    """Test that if test unexpectedly passes (exit_code == 0), repro_gate retries with stricter assertion."""
    worktree = tmp_path / "worktree_pass"
    worktree.mkdir()

    state: AgentState = {
        "task_id": "task-tdd-2",
        "worktree_path": str(worktree),
        "prompt": "Fix off-by-one error",
        "repro_test_code": "def test_lax():\n    assert True\n",
        "repro_test_path": "tests/_forge_repro_test.py",
        "status": "planning",
        "mode": "fast",
    }

    # Simulate: 1st run passes (invalid repro), 2nd run after rewrite fails (valid repro)
    run_mock = MagicMock(
        side_effect=[
            {"exit_code": 0, "passed_count": 1, "failed_count": 0, "stdout": "1 passed"},
            {
                "exit_code": 1,
                "passed_count": 0,
                "failed_count": 1,
                "stderr": "Stricter assertion failed",
            },
        ]
    )

    llm_mock = MagicMock()
    llm_mock.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=(
                "```python\n"
                "from app import item_at\n\n"
                "def test_off_by_one_index():\n"
                "    assert item_at([1, 2, 3], 2) == 3\n"
                "```"
            )
        )
    )

    with (
        patch("services.api.forge.execution.sandbox.DockerSandbox.run_tests", run_mock),
        patch("services.api.forge.orchestration.nodes.get_llm", return_value=llm_mock),
    ):
        res = await repro_gate_node(state)

    assert res["repro_verified_failing"] is True
    assert res["status"] == "repro_verified"
    assert run_mock.call_count == 2
    assert "test_off_by_one_index" in (worktree / "tests" / "_forge_repro_test.py").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_verifier_node_green_phase_success(tmp_path):
    """Test that verifier_node requires repro test pass + suite pass."""
    worktree = tmp_path / "worktree_green"
    worktree.mkdir()
    (worktree / "tests").mkdir()
    (worktree / "tests" / "_forge_repro_test.py").write_text(
        "def test_repro(): pass\n", encoding="utf-8"
    )

    state: AgentState = {
        "task_id": "task-green-1",
        "worktree_path": str(worktree),
        "repro_test_path": "tests/_forge_repro_test.py",
        "retry_count": 0,
        "status": "implementing",
    }

    def mock_run(wt, cmd, timeout_seconds=30, auto_detect=False, task_id=None):
        if "_forge_repro_test.py" in cmd:
            return {"exit_code": 0, "passed_count": 1, "failed_count": 0, "stdout": "1 passed"}
        return {"exit_code": 0, "passed_count": 10, "failed_count": 0, "stdout": "10 passed"}

    with patch(
        "services.api.forge.execution.sandbox.DockerSandbox.run_tests", side_effect=mock_run
    ):
        res = await verifier_node(state)

    assert res["status"] == "verified"
    assert res["test_results"]["exit_code"] == 0
    assert res["test_results"]["passed_count"] == 11
    assert res["retry_count"] == 0
    assert "REPRODUCTION TEST VERIFICATION" in res["verification_log"]
    assert "FULL TEST SUITE VERIFICATION" in res["verification_log"]


@pytest.mark.asyncio
async def test_verifier_node_fails_when_repro_test_fails(tmp_path):
    """Test that if repro test fails after fix, verifier_node routes back to repair loop."""
    worktree = tmp_path / "worktree_repro_fail"
    worktree.mkdir()
    (worktree / "tests").mkdir()
    (worktree / "tests" / "_forge_repro_test.py").write_text(
        "from calculator import divide\n\n"
        "def test_divide_returns_quotient():\n"
        "    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )

    state: AgentState = {
        "task_id": "task-green-fail",
        "worktree_path": str(worktree),
        "repro_test_path": "tests/_forge_repro_test.py",
        "retry_count": 0,
        "status": "implementing",
    }

    mock_run = MagicMock(
        return_value={
            "exit_code": 1,
            "passed_count": 0,
            "failed_count": 1,
            "stderr": "Repro still failing",
        }
    )

    with patch("services.api.forge.execution.sandbox.DockerSandbox.run_tests", mock_run):
        res = await verifier_node(state)

    assert res["status"] == "verifying"
    assert res["test_results"]["exit_code"] == 1
    assert res["retry_count"] == 1  # Incremented to trigger repair


def test_graph_topology_rewired():
    """Verify LangGraph nodes and linear edge sequence."""
    nodes = workflow.nodes
    assert "repro_gate_node" in nodes
    assert "planner_node" in nodes
    assert "investigator_node" in nodes
    assert "implementer_node" in nodes
    assert "verifier_node" in nodes

    # Check investigation_router routes to implementer_node
    st: AgentState = {
        "investigation_complete": True,
        "investigation_step": 1,
        "max_investigation_steps": 3,
    }
    assert investigation_router(st) == "implementer_node"

    # Check verify_router routes to reviewer_node on pass
    pass_state: AgentState = {"test_results": {"exit_code": 0, "passed_count": 5}, "retry_count": 0}
    assert verify_router(pass_state) == "reviewer_node"

    # Check verify_router routes to implementer_node on failure.
    fail_state: AgentState = {"test_results": {"exit_code": 1, "passed_count": 0}, "retry_count": 1}
    assert verify_router(fail_state) == "implementer_node"
