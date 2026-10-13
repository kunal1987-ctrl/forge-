"""Domain-specific tool registries and safe execution contracts for FORGE."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Union
from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry maintaining domain-specific tools with runtime boundaries."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool_or_func: Union[BaseTool, Callable[..., Any]]) -> BaseTool:
        """Register a BaseTool or convert a callable into a tool."""
        if isinstance(tool_or_func, BaseTool):
            t = tool_or_func
        elif callable(tool_or_func) and hasattr(tool_or_func, "name"):
            t = tool_or_func  # type: ignore[assignment]
        else:
            raise TypeError(
                f"Expected BaseTool or LangChain tool function, got {type(tool_or_func)}"
            )

        self._tools[t.name] = t
        return t

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Retrieve tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[BaseTool]:
        """Return list of registered BaseTools."""
        return list(self._tools.values())

    @property
    def tools(self) -> List[BaseTool]:
        return list(self._tools.values())

    def __repr__(self) -> str:
        return f"<ToolRegistry '{self.name}' tools={list(self._tools.keys())}>"


async def execute_tool_safely(
    tool_fn: Union[BaseTool, Callable[..., Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Wrap tool execution in robust try/except handler.

    Catches runtime exceptions and returns a clean, structured error string
    to prevent breaking the LangGraph execution loop.
    """
    tool_name = getattr(tool_fn, "name", getattr(tool_fn, "__name__", "unknown_tool"))
    try:
        if isinstance(tool_fn, BaseTool):
            if hasattr(tool_fn, "ainvoke"):
                tool_input = kwargs if kwargs else (args[0] if args else {})
                return await tool_fn.ainvoke(tool_input)
            elif hasattr(tool_fn, "invoke"):
                tool_input = kwargs if kwargs else (args[0] if args else {})
                return await asyncio.to_thread(tool_fn.invoke, tool_input)
            elif hasattr(tool_fn, "_run"):
                return await asyncio.to_thread(tool_fn._run, *args, **kwargs)

        if inspect.iscoroutinefunction(tool_fn):
            return await tool_fn(*args, **kwargs)
        elif callable(tool_fn):
            res = tool_fn(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res
        else:
            return f"Tool execution failed: object '{tool_name}' is not callable."
    except Exception as exc:
        logger.warning(
            "execute_tool_safely caught runtime exception in '%s': %s",
            tool_name,
            exc,
            exc_info=True,
        )
        return f"Tool '{tool_name}' execution error: {type(exc).__name__}: {str(exc)}"
