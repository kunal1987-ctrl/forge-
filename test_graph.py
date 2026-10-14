"""Test script for FORGE LangGraph architecture with Postgres Checkpointing and Human-in-the-Loop interrupt resumption.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

# Ensure selector event loop on Windows for psycopg async compatibility
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from langgraph.types import Command
import pytest
from services.api.forge.orchestration.graph import get_compiled_graph

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://forge:forge_password@localhost:5432/forge_db",
)


@pytest.mark.asyncio
async def test_agent_loop():
    task_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": task_id}}

    # 1. Compile graph using Postgres checkpointing factory
    print(f"📦 Compiling LangGraph with Postgres Checkpointer (DB: {DATABASE_URL.split('@')[-1]})...")
    graph = await get_compiled_graph(DATABASE_URL)

    # 2. Provide the initial input state
    initial_state = {
        "task_id": task_id,
        "repo_id": "dummy-repo-id",
        "commit_sha": "main",
        "prompt": "Write a basic Python function that adds two numbers, and write a pytest for it.",
        "worktree_path": "test_worktree",
        "messages": [],
        "context_retrieved": [],
        "plan": {},
        "generated_diff": "",
        "test_results": {},
        "retry_count": 0,
        "status": "idle",
    }

    print(f"\n🚀 [Phase 1] Starting FORGE Autonomous Run for Thread: {task_id}\n")

    interrupted = False
    # 3. Stream through the graph asynchronously until completion or interrupt
    async for event in graph.astream(initial_state, config=config):
        for node_name, node_output in event.items():
            if node_name == "__interrupt__":
                interrupted = True
                interrupts = node_output
                interrupt_val = interrupts[0].value if interrupts else {}
                print("\n⏸️ Graph execution PAUSED by Human-in-the-Loop Interrupt!")
                print(f"   Review Feedback: {interrupt_val.get('review_feedback')}")
                diff_snippet = interrupt_val.get("diff", "")
                if len(diff_snippet) > 200:
                    diff_snippet = diff_snippet[:200] + "... [truncated]"
                print(f"   Diff for Review:\n{diff_snippet}")
            else:
                print(f"✅ Finished Step: {node_name.upper()}")
                print(f"   Status: {node_output.get('status')}")

                # Print specific node outputs to prove the data is flowing
                if node_name == "planner_node":
                    print(f"   Plan: {node_output.get('plan')}")
                elif node_name == "verifier_node":
                    print(f"   Exit Code: {node_output.get('test_results', {}).get('exit_code')}")
                elif node_name == "implementer_node":
                    print(f"   Retries Used: {node_output.get('retry_count', 0)}/3")

            print("-" * 40)

    # 4. Resume the graph after human review approval
    if interrupted:
        print("\n🧑‍💻 [Phase 2] Simulating Human Reviewer Approval...")
        print("   Resuming graph execution via Command(resume='approve')...\n")

        async for event in graph.astream(Command(resume="approve"), config=config):
            for node_name, node_output in event.items():
                print(f"✅ Resumed Step: {node_name.upper()}")
                print(f"   Status: {node_output.get('status')}")
                print("-" * 40)

        # Inspect the persisted state stored in PostgreSQL checkpoint tables
        checkpoint_state = await graph.aget_state(config)
        print("\n🎉 FORGE Run Completed Successfully!")
        print("   Final State in Postgres Checkpointer:")
        print(f"   - Task ID: {checkpoint_state.values.get('task_id')}")
        print(f"   - Status:  {checkpoint_state.values.get('status')}")


if __name__ == "__main__":
    asyncio.run(test_agent_loop())
