"""LangGraph Orchestration Engine for FORGE."""

from typing import Any


def __getattr__(name: str) -> Any:
    if name in ("get_compiled_graph", "workflow"):
        from services.api.forge.orchestration.graph import get_compiled_graph, workflow

        if name == "get_compiled_graph":
            return get_compiled_graph
        return workflow
    if name == "AgentState":
        from services.api.forge.orchestration.state import AgentState

        return AgentState
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AgentState", "workflow", "get_compiled_graph"]
