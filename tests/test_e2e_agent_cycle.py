"""Phase 3 Full Synthetic End-to-End Workflow Integration Test.

Tests the complete 7-layer FORGE orchestration cycle:
- Planner -> Investigator -> Implementer -> Verifier
- Repair Loop: Simulation of Pass 1 failure -> Pruner -> Retry increment -> Re-implementation
- Pass 2 success -> Reviewer interrupt ('awaiting_approval')
- Human resume via Command(resume={'decision': 'approve'}) -> PR creation -> Cleanup
"""

import os
import subprocess
import sys
import uuid
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

# Ensure Windows selector event loop policy on Python < 3.14 for psycopg compatibility
if sys.platform == "win32" and sys.version_info < (3, 14):
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from services.api.forge.execution.worktrees import WorktreeManager
from services.api.forge.orchestration.graph import get_compiled_graph
from services.api.forge.orchestration.state import AgentState


@pytest.mark.asyncio
async def test_e2e_agent_cycle_repair_and_approval(tmp_path):
    """Test full synthetic end-to-end agent cycle with repair loop and human-in-the-loop approval."""
    # 1. Setup local temporary Git repository containing intentional bug
    repo_path = tmp_path / "repo_calculator"
    repo_path.mkdir(parents=True, exist_ok=True)

    calc_file = repo_path / "calculator.py"
    calc_file.write_text("def divide(a, b):\n    return a * b  # Intentional bug\n", encoding="utf-8")

    test_file = repo_path / "test_calculator.py"
    test_file.write_text(
        "from calculator import divide\n\ndef test_divide():\n    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )

    # Initialize Git repository
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@forge.ai"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Forge Tester"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with intentional calculator bug"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo_path), check=True, capture_output=True)

    task_id = f"test-cycle-{uuid.uuid4().hex[:8]}"

    # Setup isolated worktree for task
    wm = WorktreeManager(str(repo_path))
    worktree_path = wm.create_worktree(task_id, base_branch="main")
    assert os.path.exists(worktree_path), f"Worktree was not created at {worktree_path}"

    # 2. Initialize LangGraph AgentState
    prompt = "Fix the divide function in calculator.py so test_divide passes."
    initial_state: AgentState = {
        "task_id": task_id,
        "repo_id": str(repo_path),
        "commit_sha": "main",
        "prompt": prompt,
        "worktree_path": worktree_path,
        "messages": [],
        "context_retrieved": [],
        "investigation_step": 0,
        "max_investigation_steps": 3,
        "investigation_complete": False,
        "investigation_history": [],
        "retrieval_confidence": "low",
        "plan": {},
        "generated_diff": "",
        "test_results": {},
        "retry_count": 0,
        "status": "idle",
        "repair_trajectory": [],
        "sanitized_error": "",
    }

    # 3. Setup compiled graph with in-memory checkpointer
    checkpointer = MemorySaver()
    graph = await get_compiled_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": task_id}}

    # 4. Simulate test execution: Pass 1 fails (exit code 1); Pass 2 succeeds (exit code 0)
    test_runs = 0
    executed_nodes = []

    def mock_run_tests(self_sandbox, target_worktree, test_cmd="pytest", timeout_seconds=45):
        nonlocal test_runs
        test_runs += 1
        if test_runs == 1:
            # Simulate initial test failure
            return {
                "exit_code": 1,
                "passed_count": 0,
                "failed_count": 1,
                "raw_output": (
                    "=== FAILURES ===\n"
                    "____________________ test_divide ____________________\n"
                    ">       assert divide(10, 2) == 5\n"
                    "E       AssertionError: assert 20 == 5\n"
                    "test_calculator.py:4: AssertionError\n"
                    "===================== 1 failed in 0.05s ====================="
                ),
                "stdout": "=== FAILURES ===\ntest_calculator.py:4: AssertionError: assert 20 == 5\n=== 1 failed in 0.05s ===",
                "stderr": "",
                "status": "failed",
            }
        else:
            # Pass 2: tests succeed
            return {
                "exit_code": 0,
                "passed_count": 1,
                "failed_count": 0,
                "raw_output": "===================== 1 passed in 0.02s =====================",
                "stdout": "===================== 1 passed in 0.02s =====================",
                "stderr": "",
                "status": "passed",
            }

    with patch("services.api.forge.execution.sandbox.DockerSandbox.run_tests", side_effect=mock_run_tests):
        # Run graph stream until interrupt is encountered
        async for event in graph.astream(initial_state, config=config):
            for node_name in event.keys():
                executed_nodes.append(node_name)

    # Verify executed nodes sequence includes:
    # planner -> investigator -> implementer -> verifier -> prune_state_node -> implementer -> verifier -> reviewer
    assert "planner_node" in executed_nodes
    assert "investigator_node" in executed_nodes
    assert "prune_state_node" in executed_nodes
    assert executed_nodes.count("verifier_node") >= 2, f"Expected verifier_node to run at least twice, ran: {executed_nodes}"

    # Verify state snapshot at the interrupt gate
    state_snapshot = await graph.aget_state(config)
    paused_state = state_snapshot.values

    # Node 1 verification: Planner generated structured plan with calculator.py
    assert "plan" in paused_state
    plan = paused_state["plan"]
    assert "calculator.py" in plan.get("target_files", []) or len(plan.get("target_files", [])) > 0
    assert len(plan.get("logic_steps", [])) > 0

    # Node 2 verification: Investigator marked investigation_complete = True
    assert paused_state.get("investigation_complete") is True

    # Node 3 & 4 verification: Implementer generated diff and repair loop incremented retry_count
    assert paused_state.get("retry_count") >= 1, f"Expected retry_count >= 1, got {paused_state.get('retry_count')}"
    assert paused_state.get("generated_diff") != ""

    # Verify Review Gate: Status is 'awaiting_approval' and interrupt payload is active
    assert paused_state.get("status") == "awaiting_approval"
    assert len(state_snapshot.tasks) > 0
    assert state_snapshot.next == ("reviewer_node",)

    # 5. Human Resume: Issue Command(resume={'decision': 'approve'})
    resume_command = Command(resume={"decision": "approve"})
    resumed_nodes = []
    async for event in graph.astream(resume_command, config=config):
        for node_name in event.keys():
            resumed_nodes.append(node_name)

    assert "pr_creation_node" in resumed_nodes
    assert "cleanup_node" in resumed_nodes

    final_snapshot = await graph.aget_state(config)
    final_state = final_snapshot.values

    # Verify completion status: pr_created
    assert final_state.get("status") == "pr_created"
    assert final_state.get("decision") == "approve"

    # Verify worktree directory is wiped from disk
    assert not os.path.exists(worktree_path), f"Worktree directory {worktree_path} was not cleaned up!"
