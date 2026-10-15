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
    PlannerOutput,
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


def test_agent_state_preserves_repository_id_separately_from_worktree_path():
    """Repository lookups need the database UUID, not the local checkout path."""
    state = create_initial_agent_state(
        task_id="task-repo-id",
        repo_path="/tmp/checkout",
        repo_id="repository-uuid",
        worktree_path="/tmp/worktree",
    )

    assert state["repo_id"] == "repository-uuid"
    assert state["repo_path"] == "/tmp/checkout"


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
async def test_planner_node_propagates_structured_steps_and_legacy_plan_to_state():
    """Persist tactical steps in execution_plan and legacy plan details for downstream nodes."""
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
    assert isinstance(state["execution_plan"], list)
    assert [step["description"] for step in state["execution_plan"]] == [
        "Inspect token",
        "Enforce claims",
    ]
    assert state["plan"]["target_files"] == [
        "services/api/main.py",
        "services/api/auth.py",
    ]

    # Result dictionary returned by LangGraph node must also contain execution_plan
    assert "execution_plan" in res
    assert res["execution_plan"] == state["execution_plan"]
    assert res["plan"]["test_strategy"] == "pytest tests/test_auth.py"


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
    mock_structured_llm = MagicMock()
    mock_structured_llm.ainvoke = AsyncMock(
        side_effect=asyncio.TimeoutError("LLM call timed out after 20s")
    )
    mock_llm.bind.return_value = mock_llm
    mock_llm.with_structured_output.return_value = mock_structured_llm

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
    mock_llm.bind.assert_called_once_with(max_tokens=1024)
    mock_llm.with_structured_output.assert_called_once_with(
        PlannerOutput,
        method="json_mode",
    )

    # TelemetryDispatcher must emit plan_generated and node_exit events
    event_types = [call.kwargs.get("event_type") for call in mock_dispatcher.emit.call_args_list]
    assert "node_entry" in event_types
    assert "plan_preview" in event_types
    assert "plan_generated" in event_types
    assert "node_exit" in event_types
    preview_event = next(
        call
        for call in mock_dispatcher.emit.call_args_list
        if call.kwargs.get("event_type") == "plan_preview"
    )
    assert preview_event.kwargs["payload"]["plan_phase"] == "draft"
    assert preview_event.kwargs["payload"]["execution_plan"]
    assert (
        "authentication router"
        in preview_event.kwargs["payload"]["execution_plan"][0]["description"]
    )


@pytest.mark.asyncio
async def test_planner_scope_timeout_does_not_block_plan_generation():
    import asyncio

    state = create_initial_agent_state(
        task_id="task-planner-scope-timeout",
        prompt="Fix missing execution plan in task status",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    dispatcher = AsyncMock()
    dispatcher.emit = AsyncMock()

    with (
        patch("services.api.core.config.settings.OPENAI_API_KEY", ""),
        patch("services.api.forge.engine.nodes.TASK_SCOPE_TIMEOUT_SECONDS", 0.01),
        patch(
            "services.api.forge.engine.nodes._apply_task_scope",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ),
    ):
        result = await planner_node(state, dispatcher=dispatcher)

    assert result["execution_plan"]
    assert state["execution_plan"] == result["execution_plan"]
    event_types = [call.kwargs.get("event_type") for call in dispatcher.emit.call_args_list]
    assert "plan_preview" in event_types
    assert "plan_generated" in event_types


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
    mock_structured_llm = MagicMock()
    mock_structured_llm.ainvoke = AsyncMock(
        side_effect=RuntimeError("OpenAI API 503 Service Unavailable")
    )
    mock_llm.bind.return_value = mock_llm
    mock_llm.with_structured_output.return_value = mock_structured_llm

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


@pytest.mark.asyncio
async def test_planner_node_uses_structured_llm_output_and_ast_context():
    """Use structured steps grounded in the task and repository AST metadata."""
    from unittest.mock import MagicMock

    state = create_initial_agent_state(
        task_id="task-planner-structured-1",
        prompt="Fix the authentication token expiry bug",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    state["ast_context"] = {
        "symbols": [
            {
                "file_path": "services/api/auth.py",
                "symbol_name": "validate_token",
                "content": "def validate_token(token): ...",
            }
        ]
    }

    structured_output = {
        "steps": [
            {
                "step": 9,
                "title": "Inspect token validation",
                "description": "Trace expiry checks in validate_token.",
            },
            {
                "step": 10,
                "title": "Add regression coverage",
                "description": "Test expired and valid tokens.",
            },
        ]
    }
    mock_structured_llm = MagicMock()
    mock_structured_llm.ainvoke = AsyncMock(return_value=structured_output)
    mock_llm = MagicMock()
    mock_llm.bind.return_value = mock_llm
    mock_llm.with_structured_output.return_value = mock_structured_llm
    mock_dispatcher = MagicMock()
    mock_dispatcher.emit = AsyncMock()

    with (
        patch("services.api.core.config.settings.OPENAI_API_KEY", "test-key"),
        patch("services.api.forge.engine.nodes.get_llm", return_value=mock_llm),
    ):
        result = await planner_node(state, dispatcher=mock_dispatcher)

    assert result["execution_plan"] == [
        {
            "step": 1,
            "title": "Inspect token validation",
            "description": "Trace expiry checks in validate_token.",
        },
        {
            "step": 2,
            "title": "Add regression coverage",
            "description": "Test expired and valid tokens.",
        },
    ]
    assert state["execution_plan"] == result["execution_plan"]
    assert state["plan"]["target_files"] == [
        "services/api/auth.py",
        "tests/test_main.py",
    ]
    messages = mock_structured_llm.ainvoke.call_args.args[0]
    assert "Fix the authentication token expiry bug" in messages[1].content
    assert "validate_token" in messages[1].content
    assert "File: services/api/auth.py" in messages[1].content
    mock_llm.bind.assert_called_once_with(max_tokens=1024)
    mock_llm.with_structured_output.assert_called_once_with(
        PlannerOutput,
        method="json_mode",
    )


@pytest.mark.asyncio
async def test_planner_node_limits_ast_context_to_five_items():
    """Only the five highest-ranked retrieved AST items enter the planner prompt."""
    from unittest.mock import MagicMock

    state = create_initial_agent_state(
        task_id="task-planner-top-five",
        prompt="Update the repository API",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    state["ast_context"] = [
        {
            "file_path": f"services/api/module_{index}.py",
            "symbol_name": f"symbol_{index}",
            "content": "Relevant snippet",
        }
        for index in range(6)
    ]

    mock_structured_llm = MagicMock()
    mock_structured_llm.ainvoke = AsyncMock(
        return_value={
            "steps": [
                {
                    "step": 1,
                    "title": "Update API",
                    "description": "Update the relevant API implementation.",
                }
            ]
        }
    )
    mock_llm = MagicMock()
    mock_llm.bind.return_value = mock_llm
    mock_llm.with_structured_output.return_value = mock_structured_llm
    mock_dispatcher = MagicMock()
    mock_dispatcher.emit = AsyncMock()

    with (
        patch("services.api.core.config.settings.OPENAI_API_KEY", "test-key"),
        patch("services.api.forge.engine.nodes.get_llm", return_value=mock_llm),
    ):
        await planner_node(state, dispatcher=mock_dispatcher)

    messages = mock_structured_llm.ainvoke.call_args.args[0]
    prompt = messages[1].content
    assert all(f"symbol_{index}" in prompt for index in range(5))
    assert "symbol_5" not in prompt


@pytest.mark.asyncio
async def test_planner_and_implementer_nodes_use_only_scoped_repository_context():
    """Both LLM nodes replace broad context with chunks loaded from the task scope."""
    import uuid
    from unittest.mock import MagicMock

    repo_id = str(uuid.uuid4())
    target_files = ["services/api/auth.py", "services/api/tokens.py"]
    scoped_chunks = [
        {
            "file_path": "services/api/auth.py",
            "symbol_name": "validate_token",
            "content": "def validate_token(token): return token.is_valid",
        }
    ]
    state = create_initial_agent_state(
        task_id="task-scoped-context",
        prompt="Fix validate_token expiry behavior",
        worktree_path="/tmp/test_wt",
        repo_id=repo_id,
        mode="fast",
    )
    state["context_retrieved"] = [
        {"file_path": "unrelated.py", "symbol_name": "unrelated", "content": "ignore"}
    ]
    dispatcher = MagicMock()
    dispatcher.emit = AsyncMock()

    planner_base = AsyncMock(
        return_value={
            "plan": {
                "target_files": target_files,
                "logic_steps": ["Fix validation"],
                "test_strategy": "pytest",
            },
            "status": "planning",
        }
    )
    implementer_base = AsyncMock(return_value={"patch": "targeted patch", "status": "implemented"})

    with (
        patch(
            "services.api.forge.engine.nodes.extract_task_scope",
            new_callable=AsyncMock,
            return_value=target_files,
        ) as extract_scope,
        patch(
            "services.api.forge.engine.nodes.load_scoped_file_context",
            new_callable=AsyncMock,
            return_value=scoped_chunks,
        ) as load_context,
        patch("services.api.forge.engine.nodes._base_planner_node", planner_base),
        patch("services.api.forge.engine.nodes._base_implementer_node", implementer_base),
    ):
        planner_result = await planner_node(state, dispatcher=dispatcher)
        assert state["context_retrieved"] == scoped_chunks
        assert planner_result["context_retrieved"] == {"replace": scoped_chunks}

        implementer_result = await implementer_node(state, dispatcher=dispatcher)

    extract_scope.assert_any_await("Fix validate_token expiry behavior", repo_id)
    assert load_context.await_count == 2
    implementer_base.assert_awaited_once()
    assert implementer_base.await_args.args[0]["context_retrieved"] == scoped_chunks
    assert implementer_result["context_retrieved"] == {"replace": scoped_chunks}
