"""LangGraph workflow graph definition for the FORGE orchestration engine.

Wires the autonomous coding repair workflow:
START -> planner -> investigator -> implementer -> verifier -> (verify_router)
    -> reviewer -> END
    -> (on failure with retry_count < 3) -> implementer
    -> (on failure with retry_count >= 3) -> END
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph

from services.api.forge.orchestration.nodes import (
    implementer_node,
    investigator_node,
    planner_node,
    reviewer_node,
    verifier_node,
)
from services.api.forge.orchestration.state import AgentState

logger = logging.getLogger(__name__)


def verify_router(state: AgentState) -> str:
    """Evaluate test verification results and route to reviewer or retry implementer loop.

    Args:
        state: Current AgentState containing test_results and retry_count.

    Returns:
        Destination node name ('reviewer_node', 'implementer_node', or END).
    """
    test_results = state.get("test_results") or {}
    exit_code = test_results.get("exit_code", -1)
    retry_count = state.get("retry_count", 0)

    # 1. Success path: All tests passed cleanly
    if exit_code == 0:
        logger.info("verify_router: Tests passed (exit_code=0). Routing to reviewer_node.")
        return "reviewer_node"

    # 2. Repair loop: Tests failed, retry budget available
    if retry_count < 3:
        state["retry_count"] = retry_count + 1
        logger.warning(
            "verify_router: Test failed (exit_code=%d). Incrementing retry_count to %d. "
            "Routing back to implementer_node for repair.",
            exit_code,
            state["retry_count"],
        )
        return "implementer_node"

    # 3. Exhausted retries: Blocked state
    logger.error(
        "verify_router: Maximum retries exceeded (%d attempts). Routing to END.",
        retry_count,
    )
    return END


def build_graph() -> StateGraph:
    """Construct and configure the FORGE StateGraph with all 5 nodes and conditional repair loops.

    Returns:
        The uncompiled StateGraph instance.
    """
    workflow = StateGraph(AgentState)

    # Add 5 core execution nodes
    workflow.add_node("planner_node", planner_node)
    workflow.add_node("investigator_node", investigator_node)
    workflow.add_node("implementer_node", implementer_node)
    workflow.add_node("verifier_node", verifier_node)
    workflow.add_node("reviewer_node", reviewer_node)

    # Wire linear pipeline edges
    workflow.add_edge(START, "planner_node")
    workflow.add_edge("planner_node", "investigator_node")
    workflow.add_edge("investigator_node", "implementer_node")
    workflow.add_edge("implementer_node", "verifier_node")

    # Wire conditional repair edge
    workflow.add_conditional_edges(
        "verifier_node",
        verify_router,
        {
            "reviewer_node": "reviewer_node",
            "implementer_node": "implementer_node",
            END: END,
        },
    )

    # Wire review completion edge
    workflow.add_edge("reviewer_node", END)

    return workflow


# Compile the graph workflow and expose the runnable app
workflow = build_graph()
forge_app = workflow.compile()
