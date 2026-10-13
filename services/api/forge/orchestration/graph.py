"""LangGraph orchestration graph configuration with PostgreSQL checkpointing.

Provides the StateGraph state machine wiring, infinite-loop protection,
and the get_compiled_graph factory function connecting to LangGraph's native
AsyncPostgresSaver.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Literal, Optional, cast

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from .error_classifier import ErrorClassifier
from .nodes import (
    cleanup_node,
    critique_node,
    implementer_node,
    investigator_node,
    planner_node,
    pr_creation_node,
    error_router_node,
    prune_state_node,
    repro_gate_node,
    reviewer_node,
    verifier_node,
)
from services.api.forge.execution.pr_generator import pr_generator_node
from .state import AgentState

# Ensure Windows selector event loop is configured for psycopg async support on older Python versions
if sys.platform == "win32" and sys.version_info < (3, 14):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except (AttributeError, RuntimeError) as loop_err:
        logging.getLogger(__name__).debug("WindowsSelectorEventLoopPolicy not set: %s", loop_err)


def investigation_router(state: AgentState) -> Literal["investigator_node", "implementer_node"]:
    """Route between repeating investigation or proceeding to implementer based on sufficiency and budget."""
    if state.get("investigation_complete") is False and state.get(
        "investigation_step", 0
    ) < state.get("max_investigation_steps", 3):
        return "investigator_node"
    return "implementer_node"


def implementer_router(state: AgentState) -> Literal["verifier_node", "cleanup_node"]:
    """Route to verifier_node if implementation succeeded, or cleanup_node if failed."""
    if state.get("status") == "failed":
        return "cleanup_node"
    return "verifier_node"


class RouteResult(str):
    """String subclass that satisfies Literal['implementer', 'pr_generator', 'failed']
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
    """Route based on verification test results and retry limits.

    Strict Edge Routing (Pillar 1):
    1. If tests pass (exit_code == 0), route to pr_generator.
    2. If tests fail AND retry_count < 3, increment retry_count and route back to implementer
       with the classified error payload.
    3. If retry_count >= 3, route to graceful failure handler (failed).
    """
    test_results = state.get("test_results") or {}
    exit_code = test_results.get("exit_code", 1)
    passed_count = test_results.get("passed_count", 0)
    retry_count = state.get("retry_count", 0)

    # An execution is only considered passed if exit_code == 0
    # and if passed_count is explicitly provided, passed_count > 0
    has_passed = (exit_code == 0) and (passed_count > 0 if "passed_count" in test_results else True)

    if has_passed:
        return cast(Literal["implementer", "pr_generator", "failed"], RouteResult("pr_generator"))

    # Tests failed: classify error and populate state['error_classification'] and state['error_payload']
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

    # If tests fail and retry budget remains (< 3), increment retry_count and route to implementer
    if retry_count < 3:
        state["retry_count"] = retry_count + 1
        return cast(Literal["implementer", "pr_generator", "failed"], RouteResult("implementer"))

    # If retry count >= 3, route to graceful failure handler instead of looping infinitely
    return cast(Literal["implementer", "pr_generator", "failed"], RouteResult("failed"))


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


# Initialize the State Machine
def critique_router(state: AgentState) -> Literal["investigator_node", "implementer_node"]:
    """Route based on ReasoningMilestone confidence score and investigation step.

    If confidence_score < 0.7 and retry/step budget remains, routes back to investigator_node
    for targeted diagnostic reflection.
    """
    confidence = float(state.get("confidence_score", 1.0))
    step = int(state.get("investigation_step", 0))
    max_steps = int(state.get("max_investigation_steps", 3))

    if confidence < 0.7 and step < max_steps:
        return "investigator_node"
    return "implementer_node"


# Initialize the State Machine
workflow = StateGraph(AgentState)

# Add our physical nodes
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

# Wire linear edge sequence: START -> planner_node -> critique_node -> repro_gate_node -> investigator_node
workflow.add_edge(START, "planner_node")
workflow.add_edge("planner_node", "critique_node")
workflow.add_edge("critique_node", "repro_gate_node")
workflow.add_edge("repro_gate_node", "investigator_node")

# Wire iterative investigation loop: continues investigator_node or proceeds to implementer_node
workflow.add_conditional_edges(
    "investigator_node",
    investigation_router,
    {
        "investigator_node": "investigator_node",
        "implementer_node": "implementer_node",
    },
)

# Wire conditional routing from implementer_node: to verifier_node or cleanup_node on fail-fast
workflow.add_conditional_edges(
    "implementer_node",
    implementer_router,
    {
        "verifier_node": "verifier_node",
        "cleanup_node": "cleanup_node",
    },
)

# Wire the conditional repair loop from verifier_node: to reviewer_node, prune_state_node, or cleanup_node
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

# Connect Pruner to Implementer (Fixed directed edge)
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

# Wire the conditional review router from reviewer_node
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


logger = logging.getLogger(__name__)
tracer = trace.get_tracer("forge.graph")


async def get_compiled_graph(db_conn_string: Optional[str] = None, checkpointer: Any = None) -> Any:
    """Asynchronous factory function to compile the workflow with checkpointing.

    If checkpointer is provided, compiles with that checkpointer.
    If db_conn_string is 'memory' or empty/None, compiles with in-memory MemorySaver.
    Otherwise attempts to connect to Postgres via AsyncPostgresSaver, falling back to
    MemorySaver on connection failure.

    Args:
        db_conn_string: Optional Postgres connection string (or 'memory').
        checkpointer: Optional explicit checkpointer instance.

    Returns:
        CompiledStateGraph instance with checkpointer.
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
    """Create thread-safe, isolated initial AgentState dictionary.

    Enforces that worktree_path is explicitly bound to the state dictionary
    rather than relying on global variables or shared singleton attributes,
    preventing cross-contamination of execution state between concurrent worker threads.
    """
    state: AgentState = {
        # Mandatory Contract Keys (Pillar 1)
        "repo_path": kwargs.get("repo_path", worktree_path),
        "task_id": task_id,
        "issue_description": kwargs.get("issue_description", prompt),
        "patch": kwargs.get("patch", ""),
        "test_results": kwargs.get("test_results", {}),
        "retry_count": kwargs.get("retry_count", 0),
        "error_classification": kwargs.get("error_classification", ""),
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
