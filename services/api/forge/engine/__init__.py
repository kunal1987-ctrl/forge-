"""FORGE Autonomous Execution Engine & LangGraph State Machine."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .dispatcher import (
        TelemetryDispatcher,
        get_telemetry_dispatcher,
        set_telemetry_dispatcher,
        setup_dispatcher_lifecycle,
    )
    from .manager import (
        ActiveTask,
        EngineBusyError,
        QueuedTask,
        TaskExecutionManager,
        get_task_manager,
        task_manager,
    )
    from .graph import (
        AgentState,
        RouteResult,
        cleanup_node,
        create_initial_agent_state,
        critique_node,
        critique_router,
        error_router_node,
        get_compiled_graph,
        implementer_node,
        implementer_router,
        investigation_router,
        investigator_node,
        planner_node,
        pr_creation_node,
        prune_state_node,
        repro_gate,
        repro_gate_node,
        review_router,
        reviewer_node,
        validate_agent_state_contract,
        verifier_node,
        verify_router,
        workflow,
    )


def __getattr__(name: str) -> Any:
    """Lazy module attribute resolver to eliminate circular import dependencies."""
    if name in (
        "TelemetryDispatcher",
        "get_telemetry_dispatcher",
        "set_telemetry_dispatcher",
        "setup_dispatcher_lifecycle",
        "DEFAULT_BUFFER_CAPACITY",
        "DEFAULT_FLUSH_INTERVAL",
        "DEFAULT_MAX_BATCH",
        "DEFAULT_MAX_RETRIES",
        "DEFAULT_RETRY_BACKOFF",
    ):
        from . import dispatcher

        return getattr(dispatcher, name)

    if name in (
        "ExecutionPlan",
        "TestResults",
        "get_default_execution_plan",
        "get_default_test_results",
    ):
        from . import state

        return getattr(state, name)

    if name in (
        "ActiveTask",
        "EngineBusyError",
        "QueuedTask",
        "TaskExecutionManager",
        "get_task_manager",
        "task_manager",
    ):
        from . import manager

        return getattr(manager, name)

    if name in (
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
    ):
        from . import graph

        return getattr(graph, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ActiveTask",
    "AgentState",
    "EngineBusyError",
    "QueuedTask",
    "RouteResult",
    "TaskExecutionManager",
    "TelemetryDispatcher",
    "cleanup_node",
    "create_initial_agent_state",
    "critique_node",
    "critique_router",
    "error_router_node",
    "get_compiled_graph",
    "get_task_manager",
    "get_telemetry_dispatcher",
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
    "set_telemetry_dispatcher",
    "setup_dispatcher_lifecycle",
    "task_manager",
    "validate_agent_state_contract",
    "verifier_node",
    "verify_router",
    "workflow",
]
