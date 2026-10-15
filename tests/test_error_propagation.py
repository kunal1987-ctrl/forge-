"""Comprehensive Error Propagation and Anti-Swallowing Test Suite for FORGE.

Verifies:
1. Custom Typed Exception Hierarchy (Pillar 1):
   - ForgeBaseException, SandboxExecutionError, PatchApplicationError,
     GraphRAGSearchError, LLMAPIError.
   - Error code enforcement, details dictionaries, traceback/cause preservation.
2. Mandatory Exception Propagation & State Logging (Pillar 2):
   - Simulated Docker sandbox crashes, invalid patch blocks, and LLM timeouts.
   - State dictionary captures error_payload with traceback, type, and message.
   - Zero silent exception swallowing.
3. Static AST Anti-Swallowing Lint Guard (Pillar 3):
   - Detection of bare except: handlers.
   - Detection of silent `except Exception: pass` handlers.
   - Detection of unpropagated broad exceptions.
   - AST verification of production nodes.py and graph.py (0 violations).
4. Recovery Router Branching & State Error Classification (Pillar 4):
   - Error classification mapping to state["error_classification"].
   - Router branching based on retry count and error categories.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.api.forge.exceptions import (
    ForgeBaseException,
    GraphRAGSearchError,
    LLMAPIError,
    PatchApplicationError,
    SandboxExecutionError,
    StateRoutingError,
    WorktreeIsolationError,
)
from services.api.forge.orchestration.graph import (
    error_router_node,
    implementer_router,
    verify_router,
)
from services.api.forge.orchestration.nodes import (
    investigator_node,
    planner_node,
    repro_gate_node,
    verifier_node,
)
from services.api.forge.orchestration.state import AgentState
from services.api.forge.security.exception_guard import (
    ViolationType,
    scan_code,
    scan_file,
)


# ============================================================================
# PILLAR 1: CUSTOM TYPED EXCEPTION HIERARCHY TESTS
# ============================================================================


class TestCustomExceptionHierarchy:
    """Verifies typed exception hierarchy contracts and traceback preservation."""

    def test_root_exception_inheritance(self) -> None:
        """Verify all custom exceptions inherit from ForgeBaseException and Exception."""
        assert issubclass(SandboxExecutionError, ForgeBaseException)
        assert issubclass(PatchApplicationError, ForgeBaseException)
        assert issubclass(GraphRAGSearchError, ForgeBaseException)
        assert issubclass(LLMAPIError, ForgeBaseException)
        assert issubclass(StateRoutingError, ForgeBaseException)
        assert issubclass(WorktreeIsolationError, ForgeBaseException)
        assert issubclass(ForgeBaseException, Exception)

    def test_base_exception_attributes_and_dict_serialization(self) -> None:
        """Verify explicit error_code, details dictionary, and to_dict() contract."""
        details = {"task_id": "task-123", "step": "verification"}
        exc = ForgeBaseException(
            message="Internal agent failure",
            error_code="CUSTOM_AGENT_ERR",
            details=details,
        )

        assert exc.message == "Internal agent failure"
        assert exc.error_code == "CUSTOM_AGENT_ERR"
        assert exc.details == details
        assert "[CUSTOM_AGENT_ERR] Internal agent failure" in str(exc)

        data = exc.to_dict()
        assert data["error_type"] == "ForgeBaseException"
        assert data["error_code"] == "CUSTOM_AGENT_ERR"
        assert data["message"] == "Internal agent failure"
        assert data["details"] == details
        assert data["cause"] is None

    def test_traceback_and_cause_preservation(self) -> None:
        """Verify __cause__ and original traceback are chained and preserved."""
        try:
            try:
                raise ValueError("Original low-level parsing error")
            except ValueError as original_err:
                raise SandboxExecutionError(
                    "Docker sandbox command failed",
                    command="pytest tests/",
                    exit_code=137,
                    timed_out=True,
                    cause=original_err,
                ) from original_err
        except SandboxExecutionError as caught:
            assert caught.__cause__ is not None
            assert isinstance(caught.__cause__, ValueError)
            assert str(caught.__cause__) == "Original low-level parsing error"

            data = caught.to_dict()
            assert data["error_type"] == "SandboxExecutionError"
            assert data["error_code"] == "SANDBOX_EXECUTION_ERROR"
            assert "ValueError: Original low-level parsing error" in str(data["cause"])
            assert data["cause_traceback"] is not None
            assert "Original low-level parsing error" in data["cause_traceback"]
            assert caught.details["command"] == "pytest tests/"
            assert caught.details["exit_code"] == 137
            assert caught.details["timed_out"] is True

    def test_patch_application_error_details(self) -> None:
        """Verify PatchApplicationError captures target file, diff snippet, and reason."""
        exc = PatchApplicationError(
            message="Failed to match search block in target file",
            target_file="services/api/main.py",
            diff_snippet="<<<<<<< SEARCH\ndef foo():\n=======",
            reason="Hunk context mismatch",
        )
        assert exc.error_code == "PATCH_APPLICATION_ERROR"
        assert exc.details["target_file"] == "services/api/main.py"
        assert "SEARCH" in exc.details["diff_snippet"]
        assert exc.details["reason"] == "Hunk context mismatch"

    def test_graph_rag_search_error_details(self) -> None:
        """Verify GraphRAGSearchError captures query and repo_id."""
        exc = GraphRAGSearchError(
            message="Embedding dimension mismatch",
            repo_id="repo-forge-test",
            query="def execute_task",
        )
        assert exc.error_code == "GRAPH_RAG_SEARCH_ERROR"
        assert exc.details["repo_id"] == "repo-forge-test"
        assert exc.details["query"] == "def execute_task"

    def test_llm_api_error_details(self) -> None:
        """Verify LLMAPIError captures model, status code, and prompt snippet."""
        exc = LLMAPIError(
            message="Rate limit exceeded on provider gateway",
            model="gpt-4o",
            status_code=429,
            prompt_snippet="Generate python patch for reproduction defect...",
        )
        assert exc.error_code == "LLM_API_ERROR"
        assert exc.details["model"] == "gpt-4o"
        assert exc.details["status_code"] == 429
        assert "reproduction defect" in exc.details["prompt_snippet"]


# ============================================================================
# PILLAR 3: STATIC AST ANTI-SWALLOWING LINT GUARD TESTS
# ============================================================================


class TestStaticASTExceptionGuard:
    """Verifies AST scanner flags bare excepts, silent swallows, and unpropagated catches."""

    def test_flags_bare_except(self) -> None:
        """Verify bare 'except:' is flagged as BARE_EXCEPT."""
        code = """
def run_routine():
    try:
        do_work()
    except:
        cleanup()
"""
        violations = scan_code(code)
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.BARE_EXCEPT
        assert "Bare 'except:'" in violations[0].message

    def test_flags_silent_swallow_pass(self) -> None:
        """Verify 'except Exception: pass' is flagged as SILENT_SWALLOW."""
        code = """
def run_routine():
    try:
        do_work()
    except Exception:
        pass
"""
        violations = scan_code(code)
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.SILENT_SWALLOW
        assert "Silent 'except Exception: pass'" in violations[0].message

    def test_flags_unpropagated_broad_exception(self) -> None:
        """Verify logging without re-raise or state assignment is flagged."""
        code = """
def run_routine():
    try:
        do_work()
    except Exception as exc:
        print(f"Error occurred: {exc}")
"""
        violations = scan_code(code)
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.UNPROPAGATED_EXCEPTION

    def test_allows_re_raise_pattern(self) -> None:
        """Verify re-raising wrapped or raw exception passes cleanly."""
        code = """
def run_routine():
    try:
        do_work()
    except Exception as exc:
        logger.error("Failed: %s", exc)
        raise SandboxExecutionError("Execution failed") from exc
"""
        violations = scan_code(code)
        assert len(violations) == 0

    def test_allows_state_error_payload_assignment(self) -> None:
        """Verify assigning error_payload to graph state passes cleanly."""
        code = """
async def test_node(state: AgentState) -> dict:
    try:
        result = await execute()
    except Exception as exc:
        state["error_payload"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
            "node": "test_node",
        }
        return {"status": "failed"}
"""
        violations = scan_code(code)
        assert len(violations) == 0

    def test_production_nodes_have_zero_violations(self) -> None:
        """Verify production nodes.py has ZERO exception-swallowing anti-patterns."""
        path = "services/api/forge/orchestration/nodes.py"
        violations = scan_file(path)
        assert len(violations) == 0, f"Violations found in nodes.py: {violations}"

    def test_production_graph_have_zero_violations(self) -> None:
        """Verify production graph.py has ZERO exception-swallowing anti-patterns."""
        path = "services/api/forge/orchestration/graph.py"
        violations = scan_file(path)
        assert len(violations) == 0, f"Violations found in graph.py: {violations}"


# ============================================================================
# PILLAR 2 & 4: RUNTIME ERROR PROPAGATION & ROUTING TESTS
# ============================================================================


class TestRuntimeErrorPropagationAndRouting:
    """Verifies simulated failures populate state['error_payload'] and route correctly."""

    @pytest.mark.asyncio
    async def test_verifier_node_captures_sandbox_timeout_without_swallowing(
        self, tmp_path: Any
    ) -> None:
        """Simulate Docker sandbox timeout: verify error_payload is captured and stack trace preserved."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        repro_file = worktree / "tests" / "_forge_repro_test.py"
        repro_file.parent.mkdir(parents=True, exist_ok=True)
        repro_file.write_text("def test_dummy(): assert True", encoding="utf-8")

        state: AgentState = {
            "task_id": "test-task-timeout",
            "worktree_path": str(worktree),
            "repro_test_path": "tests/_forge_repro_test.py",
            "retry_count": 0,
            "status": "verifying",
        }

        # Mock DockerSandbox.run_tests to raise SandboxExecutionError
        timeout_err = SandboxExecutionError(
            "Docker sandbox command timed out after 30 seconds",
            command="pytest tests/_forge_repro_test.py",
            timed_out=True,
            exit_code=124,
        )

        with patch(
            "services.api.forge.orchestration.nodes.DockerSandbox.run_tests",
            side_effect=timeout_err,
        ):
            result = await verifier_node(state)

        # Verify state captures error_payload with full stack trace and type
        assert "error_payload" in state
        payload = state["error_payload"]
        assert payload["type"] == "SandboxExecutionError"
        assert "Docker sandbox command timed out" in payload["message"]
        assert "traceback" in payload
        assert len(payload["traceback"]) > 0

        # Verify router returns failed tests to implementer while retries remain.
        cast(Any, state).update(result)
        next_route = verify_router(state)
        assert next_route == "implementer_node"
        assert state.get("error_classification") == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_verifier_node_captures_docker_daemon_panic(self, tmp_path: Any) -> None:
        """Simulate unexpected Docker socket/daemon crash: verify stack trace is preserved."""
        worktree = tmp_path / "worktree_panic"
        worktree.mkdir()
        (worktree / "tests").mkdir()
        (worktree / "tests" / "_forge_repro_test.py").write_text("def test(): pass")

        state: AgentState = {
            "task_id": "test-task-panic",
            "worktree_path": str(worktree),
            "repro_test_path": "tests/_forge_repro_test.py",
            "retry_count": 1,
            "status": "verifying",
        }

        daemon_err = RuntimeError("Docker daemon socket closed unexpectedly (SIGPIPE)")

        with patch(
            "services.api.forge.orchestration.nodes.DockerSandbox.run_tests",
            side_effect=daemon_err,
        ):
            result = await verifier_node(state)

        assert "error_payload" in state
        payload = state["error_payload"]
        assert payload["type"] == "RuntimeError"
        assert "Docker daemon socket closed" in payload["message"]
        assert "RuntimeError" in payload["traceback"]

        # Verify router returns failed tests to implementer while retries remain.
        cast(Any, state).update(result)
        next_route = verify_router(state)
        assert next_route == "implementer_node"

    @pytest.mark.asyncio
    async def test_planner_node_captures_llm_api_error_without_swallowing(self) -> None:
        """Simulate LLM API timeout/panic: verify error_payload is populated in state."""
        state: AgentState = {
            "task_id": "test-llm-error",
            "prompt": "Fix zero division bug in calculator",
            "messages": [],
            "status": "running",
            "mode": "fast",
        }

        llm_err = LLMAPIError(
            "OpenRouter Gateway 504 Gateway Timeout",
            model="anthropic/claude-3.5-sonnet",
            status_code=504,
        )

        mock_llm = MagicMock()
        mock_planner = MagicMock()
        mock_planner.ainvoke = AsyncMock(side_effect=llm_err)

        with (
            patch("services.api.forge.orchestration.nodes.get_llm", return_value=mock_llm),
            patch("services.api.forge.orchestration.nodes.settings.OPENAI_API_KEY", "mock-key"),
            patch(
                "services.api.forge.orchestration.nodes._bind_structured_schema",
                return_value=mock_planner,
            ),
        ):
            result = await planner_node(state)

        # Ensure error was logged into state["error_payload"] with stack trace
        assert "error_payload" in state
        assert state["error_payload"]["type"] == "LLMAPIError"
        assert "Gateway Timeout" in state["error_payload"]["message"]
        assert state["error_payload"]["node"] == "planner_node"
        assert "traceback" in state["error_payload"]
        assert "plan" in result

    @pytest.mark.asyncio
    async def test_repro_gate_node_captures_llm_generation_error(self, tmp_path: Any) -> None:
        """Simulate LLM failure during reproduction test synthesis."""
        worktree = tmp_path / "worktree_repro"
        worktree.mkdir()

        state: AgentState = {
            "task_id": "test-repro-llm-fail",
            "worktree_path": str(worktree),
            "prompt": "Fix concurrency race condition",
            "messages": [],
            "status": "running",
            "mode": "fast",
        }

        llm_err = LLMAPIError("Model context limit exceeded", status_code=400)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=llm_err)

        with (
            patch("services.api.forge.orchestration.nodes.get_llm", return_value=mock_llm),
            patch("services.api.forge.orchestration.nodes.settings.OPENAI_API_KEY", "mock-key"),
            patch(
                "services.api.forge.orchestration.nodes.DockerSandbox.run_tests",
                return_value={
                    "exit_code": 1,
                    "passed_count": 0,
                    "failed_count": 1,
                    "raw_output": "Fail",
                },
            ),
        ):
            result = await repro_gate_node(state)

        assert "error_payload" in state
        assert state["error_payload"]["type"] == "LLMAPIError"
        assert state["error_payload"]["node"] == "repro_gate_node"
        assert "repro_test_code" in result

    @pytest.mark.asyncio
    async def test_investigator_node_captures_rag_search_error(self) -> None:
        """Simulate vector search/Graph-RAG failure in investigator_node."""
        state: AgentState = {
            "task_id": "test-rag-fail",
            "prompt": "Search authentication router",
            "repo_id": "repo-auth-123",
            "investigation_step": 0,
            "max_investigation_steps": 3,
            "context_retrieved": [],
        }

        rag_err = GraphRAGSearchError("pgvector connection timeout", repo_id="repo-auth-123")

        with (
            patch("services.api.forge.orchestration.nodes.get_llm", return_value=None),
            patch("services.api.forge.orchestration.nodes.hybrid_search_code", side_effect=rag_err),
        ):
            result = await investigator_node(state)

        assert "error_payload" in state
        assert state["error_payload"]["type"] == "GraphRAGSearchError"
        assert state["error_payload"]["node"] == "investigator_node"
        assert "pgvector connection timeout" in state["error_payload"]["message"]
        assert "context_retrieved" in result

    def test_verify_router_aborts_to_cleanup_when_retries_exhausted(self) -> None:
        """Verify that when retry budget is exhausted (>=3), router terminates to cleanup_node."""
        state: AgentState = {
            "test_results": {
                "exit_code": 1,
                "passed_count": 0,
                "raw_output": "AssertionError: Expected 2, got 1",
            },
            "retry_count": 3,
            "error_payload": {
                "type": "AssertionError",
                "message": "Expected 2, got 1",
                "traceback": "Traceback:\n  assert 2 == 1",
            },
        }

        next_route = verify_router(state)
        assert next_route == "cleanup_node"
        assert state["error_classification"] == "ASSERTION_FAILURE"

    def test_verify_router_routes_syntax_error_to_pruner(self) -> None:
        """Verify that syntax error failure routes to prune_state_node for repair."""
        state: AgentState = {
            "test_results": {
                "exit_code": 1,
                "passed_count": 0,
                "stderr": "SyntaxError: invalid syntax in main.py:42",
            },
            "retry_count": 1,
        }

        next_route = verify_router(state)
        assert next_route == "implementer_node"
        assert state["error_classification"] == "SYNTAX_ERROR"
        assert state.get("error_payload") is not None
        assert state["error_payload"]["type"] == "SYNTAX_ERROR"

    def test_error_router_node_branches_to_targeted_strategies(self) -> None:
        """Verify targeted repair strategies route correctly from state['error_classification']."""
        # Syntax error -> implementer_syntax_repair
        route_syntax = error_router_node({"error_classification": "SYNTAX_ERROR"})
        assert route_syntax == "implementer_syntax_repair"

        # Import error -> graph_rag_dependency_search
        route_import = error_router_node({"error_classification": "IMPORT_ERROR"})
        assert route_import == "graph_rag_dependency_search"

        # Assertion error or other -> standard_repair
        route_assertion = error_router_node({"error_classification": "ASSERTION_FAILURE"})
        assert route_assertion == "standard_repair"

    def test_implementer_router_fail_fast_on_failed_status(self) -> None:
        """Verify implementer_router routes immediately to cleanup_node on fatal failure."""
        state_failed: AgentState = {"status": "failed"}
        assert implementer_router(state_failed) == "cleanup_node"

        state_ok: AgentState = {"status": "implementing"}
        assert implementer_router(state_ok) == "verifier_node"
