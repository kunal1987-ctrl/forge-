"""End-to-end integration test for LangGraph autonomous repair loop and interrupt resumption."""

import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

# Ensure Windows selector event loop on win32 for psycopg async compatibility
if sys.platform == "win32" and sys.version_info < (3, 14):
    import asyncio

    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from services.api.forge.orchestration.graph import get_compiled_graph


# We use pytest-asyncio to run async tests
@pytest.mark.asyncio
async def test_langgraph_repair_loop_and_pr(tmp_path):
    # 1. Setup a dummy local Git repository in the temp path
    repo_path = tmp_path / "dummy_repo"
    repo_path.mkdir()
    (repo_path / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo_path / "test_math.py").write_text(
        "from math_utils import add\ndef test_add():\n    assert add(2, 2) == 4\n", encoding="utf-8"
    )

    # Use cd /d on Windows or git -C to ensure cross-drive switching works properly
    cd_cmd = f'cd /d "{repo_path}"' if os.name == "nt" else f'cd "{repo_path}"'
    os.system(
        f"{cd_cmd} && git init && "
        f'git config user.email "test@forge.ai" && '
        f'git config user.name "ForgeTester" && '
        f'git add . && git commit -m "Initial"'
    )

    # 2. Define the initial state for the LangGraph
    task_id = f"test-repair-{uuid.uuid4().hex[:8]}"
    initial_state = {
        "task_id": task_id,
        "repo_id": str(repo_path),
        "commit_sha": "main",
        "prompt": "Modify add() to also multiply by 2. Update the tests to reflect this.",
        "worktree_path": str(repo_path),
        "messages": [],
        "context_retrieved": ["def add(a, b):\n    return a + b\n"],
        "repro_test_code": (
            "from math_utils import add\n\n"
            "def test_add_scales_sum_by_two():\n"
            "    assert add(2, 3) == 10\n"
        ),
        "plan": {},
        "generated_diff": "",
        "test_results": {},
        "retry_count": 0,
        "status": "idle",
        "mode": "fast",
    }

    # 3. Setup compiled graph with in-memory checkpointer for deterministic test isolation
    checkpointer = MemorySaver()
    graph = await get_compiled_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": task_id}}

    # 4. Mock test execution and LLM responses to ensure complete test isolation
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
        if test_runs == 2:
            # Pass 1: test fails, triggering prune_state_node repair loop
            return {
                "exit_code": 1,
                "passed_count": 0,
                "failed_count": 1,
                "raw_output": "AssertionError: assert 4 == 8",
                "stderr": "AssertionError: assert 4 == 8",
                "status": "failed",
            }
        else:
            # Pass 2: test passes cleanly
            return {
                "exit_code": 0,
                "passed_count": 1,
                "failed_count": 0,
                "raw_output": "1 passed in 0.05s",
                "stdout": "1 passed in 0.05s",
                "stderr": "",
                "status": "passed",
            }

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=(
                "### File: math_utils.py\n"
                "<<<<<<< SEARCH\n"
                "def add(a, b):\n"
                "    return a + b\n"
                "=======\n"
                "def add(a, b):\n"
                "    return (a + b) * 2\n"
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
        # Run through verification and pause before reviewer_node for human approval.
        async for _ in graph.astream(initial_state, config):
            pass

        review_snapshot = await graph.aget_state(config)
        assert review_snapshot.next == ("reviewer_node",)
        assert review_snapshot.values.get("pr_url") is None

        # Persist approval, then resume the static breakpoint to continue PR creation.
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
        async for _ in graph.astream(None, config=config):
            pass

        # 5. Assert the repair loop completed after approval and created the PR.
        state_snap = await graph.aget_state(config)
        final_state = state_snap.values

        print(f"Retries needed: {final_state.get('retry_count', 0)}")
        assert final_state["status"] == "pr_created"
        assert final_state.get("retry_count", 0) >= 1
        assert final_state.get("pr_url") == "https://github.com/acme/project/pull/1"
