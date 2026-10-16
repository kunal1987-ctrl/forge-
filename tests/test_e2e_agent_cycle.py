"""Phase 3 Full Synthetic End-to-End Workflow Integration Test.

Tests the complete 7-layer FORGE orchestration cycle:
- Planner -> Investigator -> Implementer -> Verifier
- Repair Loop: Simulation of Pass 1 failure -> Implementer retry -> Re-implementation
- Pass 2 success -> direct PR creation -> Cleanup
"""

import os
import subprocess
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

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
async def test_e2e_agent_cycle_repair_and_pr_creation(tmp_path):
    """Test repair retries and direct PR creation in the synthetic agent cycle."""
    # 1. Setup local temporary Git repository containing intentional bug
    repo_path = tmp_path / "repo_calculator"
    repo_path.mkdir(parents=True, exist_ok=True)

    calc_file = repo_path / "calculator.py"
    calc_file.write_text(
        "def divide(a, b):\n    return a * b  # Intentional bug\n", encoding="utf-8"
    )

    test_file = repo_path / "test_calculator.py"
    test_file.write_text(
        "from calculator import divide\n\ndef test_divide():\n    assert divide(10, 2) == 5\n",
        encoding="utf-8",
    )

    # Initialize Git repository
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
        ["git", "commit", "-m", "Initial commit with intentional calculator bug"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-M", "main"], cwd=str(repo_path), check=True, capture_output=True
    )

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
        "repro_test_code": (
            "from calculator import divide\n\n"
            "def test_divide_returns_quotient():\n"
            "    assert divide(10, 2) == 5\n"
        ),
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
                "raw_output": "=== FAILURES ===\n_forge_repro_test failed before patch",
                "stdout": "",
                "stderr": "Defect confirmed",
                "status": "failed",
            }
        test_runs += 1
        if test_runs <= 2:
            return {
                "exit_code": 1,
                "passed_count": 0,
                "failed_count": 1,
                "raw_output": "=== FAILURES ===\nAssertionError: assert 20 == 5",
                "stdout": "=== FAILURES ===\nAssertionError: assert 20 == 5",
                "stderr": "",
                "status": "failed",
            }
        else:
            return {
                "exit_code": 0,
                "passed_count": 1,
                "failed_count": 0,
                "raw_output": "1 passed in 0.02s",
                "stdout": "1 passed in 0.02s",
                "stderr": "",
                "status": "passed",
            }

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=(
                "### File: calculator.py\n"
                "<<<<<<< SEARCH\n"
                "def divide(a, b):\n"
                "    return a * b  # Intentional bug\n"
                "=======\n"
                "def divide(a, b):\n"
                "    return a / b\n"
                ">>>>>>> REPLACE"
            )
        )
    )

    with (
        patch(
            "services.api.forge.execution.sandbox.DockerSandbox.run_tests",
            side_effect=mock_run_tests,
        ),
        patch("services.api.forge.orchestration.nodes.get_llm", return_value=mock_llm),
        patch(
            "services.api.forge.engine.pr_generator.PRGenerator.create_pr",
            new_callable=AsyncMock,
            return_value="https://github.com/acme/project/pull/1",
        ),
        patch("services.api.core.config.settings.OPENAI_API_KEY", "mock-test-key"),
    ):
        # Run through verification and assert the compile-time reviewer gate pauses execution.
        async for event in graph.astream(initial_state, config=config):
            for node_name in event.keys():
                executed_nodes.append(node_name)
        review_snapshot = await graph.aget_state(config)
        assert review_snapshot.next == ("reviewer_node",)
        assert "pr_generator" not in executed_nodes

        # Persist a human approval and resume the static breakpoint once.
        await graph.aupdate_state(
            config,
            {
                "decision": "approve",
                "review_decision": "approve",
                "review_status": "approved",
                "review_resume": {
                    "approved": True,
                    "action": "approve",
                    "decision": "approve",
                    "feedback": "LGTM",
                },
                "human_feedback": "LGTM",
            },
            as_node="verifier_node",
        )
        async for event in graph.astream(None, config=config):
            for node_name in event.keys():
                executed_nodes.append(node_name)

        # Verify executed nodes sequence includes:
        # planner -> investigator -> implementer -> verifier -> implementer -> verifier -> pr_generator
        assert "planner_node" in executed_nodes
        assert "investigator_node" in executed_nodes
        assert "repro_gate_node" in executed_nodes or "reproduction_gate_node" in executed_nodes
        assert "implementer_node" in executed_nodes
        assert executed_nodes.count("verifier_node") >= 2, (
            f"Expected verifier_node to run at least twice, ran: {executed_nodes}"
        )
        assert "pr_generator" in executed_nodes

        # Verify state snapshot after the completed workflow.
        state_snapshot = await graph.aget_state(config)
        final_state = state_snapshot.values

        # Node 1 verification: Planner generated structured plan with calculator.py
        assert "plan" in final_state
        plan = final_state["plan"]
        assert (
            "calculator.py" in plan.get("target_files", []) or len(plan.get("target_files", [])) > 0
        )
        assert len(plan.get("logic_steps", [])) > 0

        # Node 2 verification: Investigator marked investigation_complete = True
        assert final_state.get("investigation_complete") is True

        # Node 3 & 4 verification: Implementer generated diff and repair loop incremented retry_count
        assert final_state.get("retry_count") >= 1, (
            f"Expected retry_count >= 1, got {final_state.get('retry_count')}"
        )
        assert final_state.get("generated_diff") != ""
        assert final_state.get("status") == "pr_created"
        assert final_state.get("pr_url") == "https://github.com/acme/project/pull/1"

        # Verify worktree directory is wiped from disk
        assert not os.path.exists(worktree_path), (
            f"Worktree directory {worktree_path} was not cleaned up!"
        )
