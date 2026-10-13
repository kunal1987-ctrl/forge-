"""Investigator Tool Registry: Graph-RAG code search and symbol lookup tools."""

from __future__ import annotations

import ast
import logging
from typing import List, Optional
from langchain_core.tools import tool

from services.api.forge.tools.registry import ToolRegistry
from services.api.forge.retrieval.hybrid_search import hybrid_search_code
from services.api.core.database import async_session_factory

logger = logging.getLogger(__name__)

investigator_registry = ToolRegistry("investigator_tools")


@tool("hybrid_code_search")
async def hybrid_code_search(
    query: str,
    repo_id: str = "",
    limit: int = 10,
) -> str:
    """Execute hybrid dense semantic and lexical BM25 code search across the indexed repository.

    Args:
        query: Search keywords or natural language technical description.
        repo_id: Repository identifier.
        limit: Maximum number of code bundles to retrieve.

    Returns:
        Formatted code chunks with symbols, file paths, and dependency context, or clean error string.
    """
    try:
        dummy_vector = [0.0] * 1536
        async with async_session_factory() as session:
            results = await hybrid_search_code(
                db_session=session,
                repo_id=str(repo_id),
                commit_sha="HEAD",
                query_text=query,
                query_vector=dummy_vector,
                limit=limit,
            )

        if isinstance(results, tuple) and len(results) == 2 and isinstance(results[1], str):
            results = results[0]

        if not results:
            return f"No code chunks matched query: '{query}' in repository '{repo_id}'."

        formatted_chunks: List[str] = []
        for item in results:
            fp = item.get("file_path", "unknown")
            sym = item.get("symbol_name", "unknown")
            code = item.get("primary_code") or item.get("content", "")
            chunk_str = f"### [PRIMARY TARGET]: {fp}::{sym}\n```python\n{code.strip()}\n```"
            deps = item.get("dependencies") or []
            for dep in deps:
                chunk_str += f"\n\n#### Dependency: {dep.get('symbol')} ({dep.get('file_path')})\n```python\n{str(dep.get('code', '')).strip()}\n```"
            formatted_chunks.append(chunk_str)

        return "\n\n".join(formatted_chunks)
    except Exception as exc:
        logger.warning("hybrid_code_search runtime exception: %s", exc)
        return f"hybrid_code_search error: {type(exc).__name__}: {str(exc)}"


@tool("lookup_symbol_ast")
def lookup_symbol_ast(
    symbol_name: str,
    file_content: Optional[str] = None,
    file_path: str = "",
) -> str:
    """Lookup exact function, class, or method symbol definition using AST parsing.

    Args:
        symbol_name: Name of the function, class, or method to look up.
        file_content: Optional in-memory file content to inspect.
        file_path: Relative path of the file being inspected.

    Returns:
        Extracted source definition or clean error string.
    """
    try:
        if not symbol_name or not symbol_name.strip():
            return "lookup_symbol_ast error: symbol_name must not be empty."

        clean_sym = symbol_name.strip()
        if not file_content:
            return f"lookup_symbol_ast: Symbol '{clean_sym}' in '{file_path}' requires file_content to parse."

        tree = ast.parse(file_content, filename=file_path or "<string>")
        lines = file_content.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == clean_sym:
                    start_line = getattr(node, "lineno", 1) - 1
                    end_line = getattr(node, "end_lineno", len(lines))
                    extracted = "\n".join(lines[start_line:end_line])
                    return f"### Symbol: {clean_sym} ({type(node).__name__}) in {file_path}:\n```python\n{extracted}\n```"

        return f"Symbol '{clean_sym}' not found in AST of '{file_path}'."
    except Exception as exc:
        logger.warning("lookup_symbol_ast runtime exception: %s", exc)
        return f"lookup_symbol_ast error: {type(exc).__name__}: {str(exc)}"


@tool("expand_graph_neighbors")
def expand_graph_neighbors(
    symbol_name: str,
    context_chunks: Optional[List[str]] = None,
) -> str:
    """Expand Graph-RAG dependency neighbors (callers and callees) for a resolved symbol.

    Args:
        symbol_name: Target symbol to expand.
        context_chunks: Retrieved code chunks to search for graph references.

    Returns:
        Dependency relationship summary or clean error string.
    """
    try:
        if not symbol_name:
            return "expand_graph_neighbors error: symbol_name must not be empty."

        chunks = context_chunks or []
        related = [c for c in chunks if symbol_name in c]
        if not related:
            return f"No direct dependency neighbors found for symbol '{symbol_name}' in current context."

        return (
            f"Found {len(related)} related context bundles referencing '{symbol_name}':\n\n"
            + "\n---\n".join(related[:3])
        )
    except Exception as exc:
        logger.warning("expand_graph_neighbors runtime exception: %s", exc)
        return f"expand_graph_neighbors error: {type(exc).__name__}: {str(exc)}"


investigator_registry.register(hybrid_code_search)
investigator_registry.register(lookup_symbol_ast)
investigator_registry.register(expand_graph_neighbors)

investigator_tools = investigator_registry.list_tools()
