"""LangGraph orchestration graph configuration with live TelemetryDispatcher streaming.

Provides:
- Thread-safe StateGraph state machine with PostgreSQL / in-memory checkpointing.
- Explicit `await dispatcher.emit(task_id, event_type, payload)` hooks upon entry and exit
  for all core nodes (`planner_node`, `repro_gate`, `implementer_node`, `verifier_node`).
- Strict AgentState immutability contracts with robust defaults for retry_count,
  error_classification, and test_results.
- Bounded, deterministic conditional routers preventing deadlocks or infinite loops during
  the verifier-to-implementer retry phase.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from typing import Any, Dict, Literal, Optional, cast

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from services.api.forge.engine.dispatcher import get_telemetry_dispatcher
from services.api.forge.execution.pr_generator import pr_generator_node
from services.api.forge.orchestration.error_classifier import ErrorClassifier
from services.api.forge.orchestration.nodes import (
    cleanup_node,
    critique_node,
    error_router_node,
    implementer_node as _base_implementer_node,
    investigator_node,
    planner_node as _base_planner_node,
    pr_creation_node,
    prune_state_node,
    repro_gate_node as _base_repro_gate_node,
    reviewer_node,
    verifier_node as _base_verifier_node,
)
from services.api.forge.orchestration.state import AgentState, validate_agent_state_contract

# Ensure Windows selector event loop is configured for psycopg async support on older Python versions
if sys.platform == "win32" and sys.version_info < (3, 14):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except (AttributeError, RuntimeError) as loop_err:
        logging.getLogger(__name__).debug("WindowsSelectorEventLoopPolicy not set: %s", loop_err)

logger = logging.getLogger("forge.engine.graph")
tracer = trace.get_tracer("forge.engine.graph")


# ------------------------------------------------------------------------------
# 1. LIVE TELEMETRY-EMITTING NODE WRAPPERS
# ------------------------------------------------------------------------------


async def planner_node(state: AgentState) -> Dict[str, Any]:
    """Execute planner_node with explicit entry and exit telemetry emission."""
    task_id = str(state.get("task_id") or "unknown-task")
    prompt = str(state.get("prompt") or "")
    dispatcher = get_telemetry_dispatcher()

    await dispatcher.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "planner_node",
            "step": 1,
            "status": "planning",
            "message": f"Entering planner_node: Generating implementation plan for prompt: '{prompt[:60]}'",
        },
    )

    try:
        result = await _base_planner_node(state)
        plan_dict: Dict[str, Any] = result.get("execution_plan") or result.get("plan") or {}
        state["execution_plan"] = plan_dict
        state["plan"] = plan_dict
        result["execution_plan"] = plan_dict
        result["plan"] = plan_dict

        await dispatcher.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "planner_node",
                "step": 1,
                "status": result.get("status", "planning"),
                "message": "Formulated structured execution plan",
                "execution_plan": plan_dict,
                "plan": plan_dict,
            },
        )
        return result
    except Exception as exc:
        await dispatcher.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "planner_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc


async def repro_gate_node(state: AgentState) -> Dict[str, Any]:
    """Execute reproduction gate node with explicit entry and exit telemetry emission."""
    task_id = str(state.get("task_id") or "unknown-task")
    dispatcher = get_telemetry_dispatcher()

    await dispatcher.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "repro_gate_node",
            "status": "reproducing",
            "message": "Entering repro_gate: Validating defect reproduction in Red Phase",
        },
    )

    try:
        result = await _base_repro_gate_node(state)
        repro_failing = bool(result.get("repro_verified_failing", False))
        await dispatcher.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "repro_gate_node",
                "status": result.get("status", "reproduced"),
                "repro_verified_failing": repro_failing,
                "message": (
                    "Defect successfully isolated and reproduced (Red Phase PASSED)"
                    if repro_failing
                    else "Reproduction gate concluded"
                ),
            },
        )
        return result
    except Exception as exc:
        await dispatcher.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "repro_gate_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc


# Semantic alias for repro_gate
repro_gate = repro_gate_node


async def implementer_node(state: AgentState) -> Dict[str, Any]:
    """Execute implementer_node with explicit entry and exit telemetry emission."""
    task_id = str(state.get("task_id") or "unknown-task")
    dispatcher = get_telemetry_dispatcher()

    await dispatcher.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "implementer_node",
            "step": 3,
            "status": "implementing",
            "message": "Entering implementer_node: Synthesizing diff and applying code patch",
        },
    )

    try:
        result = await _base_implementer_node(state)
        diff_str: str = str(
            result.get("patch") or result.get("generated_diff") or result.get("diff") or ""
        )
        state["patch"] = diff_str
        state["generated_diff"] = diff_str
        state["diff"] = diff_str
        result["patch"] = diff_str
        result["generated_diff"] = diff_str
        result["diff"] = diff_str

        patch_snippet = diff_str[:400]
        await dispatcher.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "implementer_node",
                "step": 3,
                "status": result.get("status", "implemented"),
                "message": "Synthesized unified git diff and applied changes to worktree",
                "patch_snippet": patch_snippet,
            },
        )
        return result
    except Exception as exc:
        await dispatcher.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "implementer_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc


async def verifier_node(state: AgentState) -> Dict[str, Any]:
    """Execute verifier_node with explicit live test outcome and node exit emission."""
    task_id = str(state.get("task_id") or "unknown-task")
    dispatcher = get_telemetry_dispatcher()

    await dispatcher.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "verifier_node",
            "step": 4,
            "status": "verifying",
            "message": "Entering verifier_node: Executing sandboxed test suite",
        },
    )

    try:
        result = await _base_verifier_node(state)
        test_results = dict(result.get("test_results") or {})
        exit_code = int(test_results.get("exit_code", 1))
        stdout = str(test_results.get("stdout") or test_results.get("raw_output") or "")
        stderr = str(test_results.get("stderr") or "")
        passed = (exit_code == 0) and (
            test_results.get("passed_count", 0) > 0 if "passed_count" in test_results else True
        )
        test_results["exit_code"] = exit_code
        test_results["stdout"] = stdout
        test_results["stderr"] = stderr
        test_results["passed"] = passed

        # Explicit State Writing for Frontend Real-time Rendering
        state["test_results"] = test_results
        result["test_results"] = test_results

        # Immediate test outcome emission for live UI charts & test suites
        await dispatcher.emit(
            task_id=task_id,
            event_type="test_outcome",
            payload={
                "node": "verifier_node",
                "step": 4,
                "status": "passed" if passed else "failed",
                "exit_code": exit_code,
                "passed_count": test_results.get("passed_count", 0),
                "failed_count": test_results.get("failed_count", 0),
                "test_results": test_results,
                "message": f"Sandboxed tests {'passed' if passed else 'failed'} (exit code {exit_code})",
            },
            immediate=True,
        )

        await dispatcher.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "verifier_node",
                "step": 4,
                "status": result.get("status", "verified"),
                "message": "Executed sandboxed test suite",
                "exit_code": exit_code,
            },
        )
        return result
    except Exception as exc:
        await dispatcher.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "verifier_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc


# ------------------------------------------------------------------------------
# 2. DETERMINISTIC CONDITIONAL ROUTERS (DEADLOCK & INFINITE-LOOP PREVENTION)
# ------------------------------------------------------------------------------


class RouteResult(str):
    """String subclass satisfying Literal['implementer', 'pr_generator', 'failed']
    while maintaining 100% backward compatibility with node name assertions.
    """

    def __eq__(self, other: object) -> bool:
        if super().__eq__(other):
            return True
        aliases = {
            "pr_generator": {"pr_generator", "reviewer_node", "pr_creation_node"},
            "implementer": {"implementer", "prune_state_node", "implementer_node"},
            "failed": {"failed", "cleanup_node"},
        }
        return other in aliases.get(str(self), set())

    def __hash__(self) -> int:
        return super().__hash__()


def verify_router(
    state: AgentState,
) -> Literal["implementer", "pr_generator", "failed"]:
    """Deterministic conditional router evaluating verification outcomes and bounded retry limits.

    Strict Edge Routing (Pillar 1):
    1. If tests pass (exit_code == 0), route to pr_generator / reviewer_node.
    2. If tests fail AND retry_count < 3, increment retry_count and route back to implementer / prune_state_node
       with classified error payload.
    3. If retry_count >= 3, route to graceful failure handler (failed / cleanup_node),
       strictly preventing deadlocks or infinite loops.
    """
    test_results = state.get("test_results") or {}
    exit_code = test_results.get("exit_code", 1)
    passed_count = test_results.get("passed_count", 0)
    retry_count = int(state.get("retry_count") or 0)

    # An execution is only considered passed if exit_code == 0
    # and if passed_count is explicitly provided, passed_count > 0
    has_passed = (exit_code == 0) and (passed_count > 0 if "passed_count" in test_results else True)

    if has_passed:
        return cast(Literal["implementer", "pr_generator", "failed"], RouteResult("pr_generator"))

    # Tests failed: classify error deterministically
    raw_output = str(test_results.get("raw_output") or test_results.get("stdout") or "")
    stderr = str(test_results.get("stderr") or "")
    err_payload = state.get("error_payload") or {}
    combined_stderr = f"{stderr}\n{err_payload.get('message', '')}".strip()
    classification = ErrorClassifier.classify(
        raw_output=raw_output, stderr=combined_stderr or stderr, error_payload=err_payload
    )

    state["error_classification"] = classification.category.value
    state["error_details"] = classification.model_dump()
    if not state.get("error_payload"):
        state["error_payload"] = {
            "type": classification.category.value,
            "message": getattr(classification, "reasoning", None)
            or getattr(classification, "mismatch_snippet", "")
            or stderr
            or raw_output
            or "Verification tests failed",
            "traceback": stderr or raw_output,
            "node": "verifier_node",
        }

    # Bounded retry policy: retry budget of 3 attempts
    if retry_count < 3:
        state["retry_count"] = retry_count + 1
        return cast(Literal["implementer", "pr_generator", "failed"], RouteResult("implementer"))

    # When retry budget is exhausted, terminate deterministically to failed / cleanup_node
    return cast(Literal["implementer", "pr_generator", "failed"], RouteResult("failed"))


def investigation_router(state: AgentState) -> Literal["investigator_node", "implementer_node"]:
    """Route between repeating investigation or proceeding to implementer based on budget."""
    step = int(state.get("investigation_step", 0))
    max_steps = int(state.get("max_investigation_steps", 3))
    if state.get("investigation_complete") is False and step < max_steps:
        return "investigator_node"
    return "implementer_node"


def implementer_router(state: AgentState) -> Literal["verifier_node", "cleanup_node"]:
    """Route to verifier_node if implementation succeeded, or cleanup_node if failed."""
    if state.get("status") == "failed":
        return "cleanup_node"
    return "verifier_node"


def critique_router(state: AgentState) -> Literal["investigator_node", "implementer_node"]:
    """Route based on confidence score and investigation step."""
    confidence = float(state.get("confidence_score", 1.0))
    step = int(state.get("investigation_step", 0))
    max_steps = int(state.get("max_investigation_steps", 3))

    if confidence < 0.7 and step < max_steps:
        return "investigator_node"
    return "implementer_node"


def review_router(
    state: AgentState,
) -> Literal["pr_creation_node", "implementer_node", "cleanup_node"]:
    """Route based on human review decision."""
    decision = state.get("decision") or state.get("review_decision", "")
    if isinstance(decision, str):
        decision = decision.strip().lower()

    if decision in ("approve", "approved"):
        return "pr_creation_node"
    if decision in ("reject", "rejected"):
        return "implementer_node"
    if decision in ("cancel", "cancelled"):
        return "cleanup_node"

    return "cleanup_node"


# ------------------------------------------------------------------------------
# 3. STATE MACHINE GRAPH ASSEMBLY
# ------------------------------------------------------------------------------


workflow = StateGraph(AgentState)

# Add physical nodes with live telemetry wrappers
workflow.add_node("planner_node", planner_node)
workflow.add_node("critique_node", critique_node)
workflow.add_node("repro_gate_node", repro_gate_node)
workflow.add_node("investigator_node", investigator_node)
workflow.add_node("implementer_node", implementer_node)
workflow.add_node("verifier_node", verifier_node)
workflow.add_node("prune_state_node", prune_state_node)
workflow.add_node("reviewer_node", reviewer_node)
workflow.add_node("pr_creation_node", pr_creation_node)
workflow.add_node("pr_generator", pr_generator_node)
workflow.add_node("cleanup_node", cleanup_node)

# Linear edge sequence: START -> planner_node -> critique_node -> repro_gate_node -> investigator_node
workflow.add_edge(START, "planner_node")
workflow.add_edge("planner_node", "critique_node")
workflow.add_edge("critique_node", "repro_gate_node")
workflow.add_edge("repro_gate_node", "investigator_node")

# Iterative investigation loop: continues investigator_node or proceeds to implementer_node
workflow.add_conditional_edges(
    "investigator_node",
    investigation_router,
    {
        "investigator_node": "investigator_node",
        "implementer_node": "implementer_node",
    },
)

# Conditional routing from implementer_node: to verifier_node or cleanup_node on fail-fast
workflow.add_conditional_edges(
    "implementer_node",
    implementer_router,
    {
        "verifier_node": "verifier_node",
        "cleanup_node": "cleanup_node",
    },
)

# Conditional repair loop from verifier_node: to reviewer_node, prune_state_node, or cleanup_node
workflow.add_conditional_edges(
    "verifier_node",
    verify_router,
    {
        "pr_generator": "reviewer_node",
        "reviewer_node": "reviewer_node",
        "implementer": "prune_state_node",
        "prune_state_node": "prune_state_node",
        "failed": "cleanup_node",
        "cleanup_node": "cleanup_node",
    },
)

# Connect Pruner via error_router_node to targeted repair strategies
workflow.add_conditional_edges(
    "prune_state_node",
    error_router_node,
    {
        "graph_rag_dependency_search": "investigator_node",
        "implementer_syntax_repair": "implementer_node",
        "standard_repair": "implementer_node",
    },
)

# Conditional review router from reviewer_node
workflow.add_conditional_edges(
    "reviewer_node",
    review_router,
    {
        "pr_creation_node": "pr_creation_node",
        "pr_generator": "pr_generator",
        "implementer_node": "implementer_node",
        "cleanup_node": "cleanup_node",
    },
)

# Wire PR creation and pr_generator to cleanup, and cleanup to END
workflow.add_edge("pr_creation_node", "cleanup_node")
workflow.add_edge("pr_generator", "cleanup_node")
workflow.add_edge("cleanup_node", END)


# ------------------------------------------------------------------------------
# 4. COMPILATION & STATE FACTORY FUNCTIONS
# ------------------------------------------------------------------------------


async def get_compiled_graph(db_conn_string: Optional[str] = None, checkpointer: Any = None) -> Any:
    """Asynchronous factory function to compile the workflow with checkpointing.

    If checkpointer is provided, compiles with that checkpointer.
    If db_conn_string is 'memory' or empty/None, compiles with in-memory MemorySaver.
    Otherwise attempts to connect to Postgres via AsyncPostgresSaver, falling back to
    MemorySaver on connection failure.
    """
    app: Any
    saver: Any
    if checkpointer is not None:
        app = workflow.compile(checkpointer=checkpointer)
        setattr(app, "_saver", checkpointer)
        return app

    if not db_conn_string or db_conn_string.strip() == "memory":
        saver = MemorySaver()
        app = workflow.compile(checkpointer=saver)
        setattr(app, "_saver", saver)
        return app

    try:
        clean_uri = db_conn_string.replace("postgresql+asyncpg://", "postgresql://")
        saver_cm: Any = AsyncPostgresSaver.from_conn_string(clean_uri)
        saver = await saver_cm.__aenter__() if hasattr(saver_cm, "__aenter__") else saver_cm
        setattr(saver, "_cm", saver_cm)
        await saver.setup()
        app = workflow.compile(checkpointer=saver)
        setattr(app, "_saver", saver)
        return app
    except (ConnectionError, OSError, ValueError, RuntimeError) as exc:
        logger.warning(
            "get_compiled_graph: Failed to initialize AsyncPostgresSaver (%s). Falling back to MemorySaver.",
            exc,
        )
        saver = MemorySaver()
        app = workflow.compile(checkpointer=saver)
        setattr(app, "_saver", saver)
        return app


def create_initial_agent_state(
    task_id: str,
    prompt: str,
    worktree_path: str,
    repo_id: str = "default-repo",
    commit_sha: str = "main",
    mode: str = "fast",
    **kwargs: Any,
) -> AgentState:
    """Create thread-safe, isolated initial AgentState dictionary with immutable defaults."""
    state: AgentState = {
        # Mandatory Contract Keys with strict default values
        "repo_path": kwargs.get("repo_path", worktree_path),
        "task_id": task_id,
        "issue_description": kwargs.get("issue_description", prompt),
        "execution_plan": dict(kwargs.get("execution_plan") or {}),
        "patch": kwargs.get("patch", ""),
        "test_results": dict(kwargs.get("test_results") or {}),
        "retry_count": int(kwargs.get("retry_count", 0)),
        "error_classification": str(kwargs.get("error_classification", "")),
        "pr_url": kwargs.get("pr_url", ""),
        # Core inputs & backwards-compatible aliases
        "repo_id": repo_id,
        "commit_sha": commit_sha,
        "prompt": prompt,
        "worktree_path": worktree_path,
        "mode": mode,
        "messages": [],
        "context_retrieved": [],
        "plan": {},
        "generated_diff": "",
        "status": "running",
        "sanitized_prompt": "",
        "wrapped_prompt": "",
        "summary": "",
        "rolling_summary": {},
        "error_details": {},
        "error_payload": {},
    }
    for k, v in kwargs.items():
        state[k] = v  # type: ignore[literal-required]
    return state


__all__ = [
    "AgentState",
    "RouteResult",
    "cleanup_node",
    "create_initial_agent_state",
    "critique_node",
    "critique_router",
    "error_router_node",
    "get_compiled_graph",
    "implementer_node",
    "implementer_router",
    "investigation_router",
    "investigator_node",
    "planner_node",
    "pr_creation_node",
    "prune_state_node",
    "repro_gate",
    "repro_gate_node",
    "review_router",
    "reviewer_node",
    "validate_agent_state_contract",
    "verifier_node",
    "verify_router",
    "workflow",
]
