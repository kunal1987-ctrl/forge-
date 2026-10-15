"""Unit tests for FORGE Engine Nodes and State Schema Enforcement.

Validates:
1. State Payload Propagation (services/api/forge/engine/nodes.py):
   - planner_node explicitly writes generated execution plan dictionary to state["execution_plan"].
   - implementer_node explicitly saves surgical diff string to state["patch"].
   - verifier_node explicitly saves structured test stdout, stderr, and exit codes to state["test_results"].
2. State Schema Enforcement (services/api/forge/engine/state.py):
   - AgentState TypedDict defaults for execution_plan, patch, test_results, and pr_url.
   - validate_agent_state_contract enforces strict typing across mandatory keys.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.api.forge.engine.nodes import (
    implementer_node,
    planner_node,
    verifier_node,
)
from services.api.forge.engine.state import (
    create_initial_agent_state,
    validate_agent_state_contract,
)


# ==============================================================================
# 1. STATE SCHEMA ENFORCEMENT & DEFAULT INITIALIZERS
# ==============================================================================


def test_agent_state_default_initializers():
    """Verify AgentState initializes default values for execution_plan, patch, test_results, and pr_url."""
    state = create_initial_agent_state(
        task_id="task-state-schema-1",
        prompt="Test state initializers",
        worktree_path="/tmp/worktree_test",
    )

    # Core 4 required fields with default initializers
    assert "execution_plan" in state
    assert state["execution_plan"] == {}

    assert "patch" in state
    assert state["patch"] == ""

    assert "test_results" in state
    assert state["test_results"] == {}

    assert "pr_url" in state
    assert state["pr_url"] == ""

    # Required contract keys
    assert state["task_id"] == "task-state-schema-1"
    assert state["repo_path"] == "/tmp/worktree_test"
    assert state["retry_count"] == 0
    assert state["error_classification"] == ""

    # Contract validation succeeds on clean initialization
    validate_agent_state_contract(state)


def test_agent_state_contract_type_enforcement():
    """Verify validate_agent_state_contract rejects invalid data types."""
    state = create_initial_agent_state(
        task_id="task-state-schema-2",
        prompt="Verify contract types",
        worktree_path="/tmp/worktree_test",
    )

    # Invalid execution_plan type
    bad_plan_state = dict(state)
    bad_plan_state["execution_plan"] = "not-a-dict"  # type: ignore[assignment]
    with pytest.raises(TypeError, match="AgentState contract violation: key 'execution_plan'"):
        validate_agent_state_contract(bad_plan_state)  # type: ignore[arg-type]

    # Invalid patch type
    bad_patch_state = dict(state)
    bad_patch_state["patch"] = 12345  # type: ignore[assignment]
    with pytest.raises(TypeError, match="AgentState contract violation: key 'patch'"):
        validate_agent_state_contract(bad_patch_state)  # type: ignore[arg-type]

    # Invalid test_results type
    bad_test_state = dict(state)
    bad_test_state["test_results"] = ["not", "a", "dict"]  # type: ignore[assignment]
    with pytest.raises(TypeError, match="AgentState contract violation: key 'test_results'"):
        validate_agent_state_contract(bad_test_state)  # type: ignore[arg-type]


# ==============================================================================
# 2. STATE PAYLOAD PROPAGATION IN ENGINE NODES
# ==============================================================================


@pytest.mark.asyncio
async def test_planner_node_propagates_execution_plan_to_state():
    """Verify planner_node explicitly writes generated execution plan dictionary to state['execution_plan']."""
    state = create_initial_agent_state(
        task_id="task-planner-prop-1",
        prompt="Refactor authentication middleware",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    assert state["execution_plan"] == {}

    mock_plan_payload = {
        "target_files": ["services/api/main.py", "services/api/auth.py"],
        "logic_steps": ["Inspect token", "Enforce claims"],
        "test_strategy": "pytest tests/test_auth.py",
        "reproduction_test_code": "def test_auth(): pass",
    }

    with patch(
        "services.api.forge.engine.nodes._base_planner_node",
        new_callable=AsyncMock,
    ) as mock_base:
        mock_base.return_value = {
            "plan": mock_plan_payload,
            "status": "planning",
        }
        res = await planner_node(state)

    # State must be mutated explicitly
    assert "execution_plan" in state
    assert state["execution_plan"] == mock_plan_payload
    assert state["execution_plan"]["target_files"] == [
        "services/api/main.py",
        "services/api/auth.py",
    ]

    # Result dictionary returned by LangGraph node must also contain execution_plan
    assert "execution_plan" in res
    assert res["execution_plan"] == mock_plan_payload


@pytest.mark.asyncio
async def test_implementer_node_propagates_patch_to_state():
    """Verify implementer_node explicitly writes surgical diff string to state['patch']."""
    state = create_initial_agent_state(
        task_id="task-implementer-prop-1",
        prompt="Fix division by zero bug",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    assert state["patch"] == ""

    diff_content = (
        "--- a/services/api/main.py\n"
        "+++ b/services/api/main.py\n"
        "@@ -10,3 +10,3 @@\n"
        "-def divide(a, b): return a / b\n"
        "+def divide(a, b): return a / b if b != 0 else 0\n"
    )

    with patch(
        "services.api.forge.engine.nodes._base_implementer_node",
        new_callable=AsyncMock,
    ) as mock_base:
        mock_base.return_value = {
            "patch": diff_content,
            "generated_diff": diff_content,
            "status": "implemented",
        }
        res = await implementer_node(state)

    # State must be mutated explicitly
    assert "patch" in state
    assert state["patch"] == diff_content

    # Result dictionary returned by LangGraph node must also contain patch
    assert "patch" in res
    assert res["patch"] == diff_content


@pytest.mark.asyncio
async def test_verifier_node_propagates_structured_test_results_to_state():
    """Verify verifier_node explicitly saves structured test stdout, stderr, and exit codes to state['test_results']."""
    state = create_initial_agent_state(
        task_id="task-verifier-prop-1",
        prompt="Run pytest verification suite",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    assert state["test_results"] == {}

    raw_test_outcome = {
        "exit_code": 0,
        "stdout": "================ 5 passed in 0.12s ================",
        "stderr": "",
        "passed_count": 5,
        "failed_count": 0,
        "status": "passed",
    }

    with patch(
        "services.api.forge.engine.nodes._base_verifier_node",
        new_callable=AsyncMock,
    ) as mock_base:
        mock_base.return_value = {
            "test_results": raw_test_outcome,
            "status": "verified",
        }
        res = await verifier_node(state)

    # State must be mutated explicitly with structured keys
    assert "test_results" in state
    assert state["test_results"]["exit_code"] == 0
    assert "================ 5 passed" in state["test_results"]["stdout"]
    assert state["test_results"]["stderr"] == ""
    assert state["test_results"]["passed"] is True
    assert state["test_results"]["passed_count"] == 5

    # Result dictionary returned by LangGraph node must also contain test_results
    assert "test_results" in res
    assert res["test_results"]["exit_code"] == 0
    assert res["test_results"]["stdout"] == raw_test_outcome["stdout"]


# ==============================================================================
# 3. PLANNER NODE LLM TIMEOUT & HEURISTIC FALLBACK TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_planner_node_timeout_falls_back_to_heuristic_plan_and_emits_telemetry():
    """Verify planner_node falls back to deterministic heuristic plan targeting entry/test files when LLM times out."""
    import asyncio
    from unittest.mock import MagicMock

    state = create_initial_agent_state(
        task_id="task-planner-timeout-1",
        prompt="Fix regression in authentication router",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    state["ast_context"] = [
        {"file_path": "services/api/routers/auth.py", "symbol_name": "login"},
        {"file_path": "tests/test_auth.py", "symbol_name": "test_login"},
    ]

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=asyncio.TimeoutError("LLM call timed out after 30s"))

    mock_dispatcher = MagicMock()
    mock_dispatcher.emit = AsyncMock()

    with (
        patch("services.api.core.config.settings.OPENAI_API_KEY", "test-key"),
        patch("services.api.forge.engine.nodes.get_llm", return_value=mock_llm),
    ):
        result = await planner_node(state, dispatcher=mock_dispatcher)

    # State must contain the deterministic heuristic plan
    assert "execution_plan" in state
    assert isinstance(state["execution_plan"], list)
    assert len(state["execution_plan"]) == 3
    assert state["execution_plan"] == result["execution_plan"]

    # Must target isolated source and test files
    assert state["target_files"] == ["services/api/routers/auth.py", "tests/test_auth.py"]
    assert "services/api/routers/auth.py" in state["execution_plan"][0]["title"]
    assert "tests/test_auth.py" in state["execution_plan"][2]["title"]

    # TelemetryDispatcher must emit plan_generated and node_exit events
    event_types = [call.kwargs.get("event_type") for call in mock_dispatcher.emit.call_args_list]
    assert "node_entry" in event_types
    assert "plan_generated" in event_types
    assert "node_exit" in event_types


@pytest.mark.asyncio
async def test_planner_node_llm_failure_falls_back_to_heuristic_plan():
    """Verify planner_node falls back to deterministic heuristic plan when LLM raises exception."""
    from unittest.mock import MagicMock

    state = create_initial_agent_state(
        task_id="task-planner-fail-1",
        prompt="Address memory leak in background worker",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    state["target_files"] = ["services/worker.py"]

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("OpenAI API 503 Service Unavailable"))

    mock_dispatcher = MagicMock()
    mock_dispatcher.emit = AsyncMock()

    with (
        patch("services.api.core.config.settings.OPENAI_API_KEY", "test-key"),
        patch("services.api.forge.engine.nodes.get_llm", return_value=mock_llm),
    ):
        result = await planner_node(state, dispatcher=mock_dispatcher)

    assert "execution_plan" in state
    assert isinstance(state["execution_plan"], list)
    assert len(state["execution_plan"]) == 3
    assert "services/worker.py" in state["execution_plan"][0]["title"]
    assert result["status"] == "planning"
