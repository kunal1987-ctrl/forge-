"""FORGE domain-specific tool registries and runtime execution contracts."""

from __future__ import annotations

from services.api.forge.tools.registry import (
    ToolRegistry,
    execute_tool_safely,
)
from services.api.forge.tools.investigator_tools import (
    expand_graph_neighbors,
    hybrid_code_search,
    investigator_registry,
    investigator_tools,
    lookup_symbol_ast,
)
from services.api.forge.tools.implementer_tools import (
    apply_search_replace_block,
    implementer_registry,
    implementer_tools,
    parse_search_replace_blocks,
    validate_syntax_in_memory,
)

__all__ = [
    "ToolRegistry",
    "execute_tool_safely",
    "investigator_registry",
    "investigator_tools",
    "hybrid_code_search",
    "lookup_symbol_ast",
    "expand_graph_neighbors",
    "implementer_registry",
    "implementer_tools",
    "apply_search_replace_block",
    "validate_syntax_in_memory",
    "parse_search_replace_blocks",
]
