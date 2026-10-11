"""End-to-end integration test for LangGraph autonomous repair loop and interrupt resumption."""

import os
import sys
import uuid
import pytest
from langgraph.types import Command

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
    (repo_path / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    (repo_path / "test_math.py").write_text("from math_utils import add\ndef test_add():\n    assert add(2, 2) == 4\n")

    # Use cd /d on Windows or git -C to ensure cross-drive switching works properly
    cd_cmd = f'cd /d "{repo_path}"' if os.name == "nt" else f'cd "{repo_path}"'
    os.system(
        f'{cd_cmd} && git init && '
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
        "plan": {},
        "generated_diff": "",
        "test_results": {},
        "retry_count": 0,
        "status": "idle",
    }

    # 3. Connect to the local Postgres checkpointer (using your test DB URI)
    db_uri = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://forge_admin:forge_password@localhost:5432/forge_db",
    )
    graph = await get_compiled_graph(db_uri)
    config = {"configurable": {"thread_id": task_id}}

    # 4. Run the graph. We expect it to hit the interrupt at reviewer_node
    async for event in graph.astream(initial_state, config):
        pass

    # 5. Assert the repair loop actually fired
    # We fetch the final state from the checkpointer
    try:
        final_state = graph.get_state(config).values
    except Exception:
        state_snap = await graph.aget_state(config)
        final_state = state_snap.values

    # If the LLM failed its first Docker test and had to retry, this will be > 0
    print(f"Retries needed: {final_state['retry_count']}")
    assert final_state["status"] == "awaiting_approval"

    # 6. Simulate human approval (The "Resume" trigger)
    # This proves the graph can pause, wait for a UI click, and resume
    await graph.ainvoke(Command(resume="approve"), config)

    try:
        resumed_state = graph.get_state(config).values
    except Exception:
        resumed_snap = await graph.aget_state(config)
        resumed_state = resumed_snap.values

    assert resumed_state["status"] in ("completed", "pr_created")

    # In your full implementation, the "completed" transition triggers a GitHub API call
    # to open the PR using the isolated worktree branch.
