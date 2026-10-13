"""Implementer Tool Registry: Search/Replace patch application and syntax validation tools."""

from __future__ import annotations

import ast
import logging
from langchain_core.tools import tool

from services.api.forge.tools.registry import ToolRegistry
from services.api.forge.execution.patch_applier import SurgicalPatchApplier

logger = logging.getLogger(__name__)

implementer_registry = ToolRegistry("implementer_tools")


@tool("apply_search_replace_block")
def apply_search_replace_block(
    patch_content: str,
    worktree_path: str,
) -> str:
    """Safely apply surgical SEARCH/REPLACE blocks to the target worktree.

    Args:
        patch_content: Surgical patch string with <<<<<<< SEARCH / ======= / >>>>>>> REPLACE blocks.
        worktree_path: Root filesystem directory of the repository worktree.

    Returns:
        Success confirmation message or clean error string describing the mismatch.
    """
    try:
        if not patch_content or not patch_content.strip():
            return "apply_search_replace_block error: patch_content cannot be empty."

        if not worktree_path:
            return "apply_search_replace_block error: worktree_path must be specified."

        applier = SurgicalPatchApplier(repo_root=worktree_path)
        res = applier.apply(patch_content)
        if res.success:
            applied_cnt = getattr(res, "applied_blocks", getattr(res, "applied_count", 1))
            return f"Successfully applied surgical patch: modified {applied_cnt} block(s)."
        else:
            return f"Patch application failed: {res.error or 'Target content not found in file.'}"
    except Exception as exc:
        logger.warning("apply_search_replace_block runtime exception: %s", exc)
        return f"apply_search_replace_block error: {type(exc).__name__}: {str(exc)}"


@tool("validate_syntax_in_memory")
def validate_syntax_in_memory(
    code_content: str,
    filename: str = "<memory>",
) -> str:
    """Validate Python syntax in memory using AST parsing without writing to disk.

    Args:
        code_content: Python source code or replaced file content to validate.
        filename: Optional descriptive filename for error reporting.

    Returns:
        'SYNTAX_VALID' if clean, or clean error string describing line and column syntax errors.
    """
    try:
        if not code_content:
            return "validate_syntax_in_memory: Empty code content provided."

        ast.parse(code_content, filename=filename)
        return "SYNTAX_VALID: Python code parsed cleanly with zero syntax errors."
    except SyntaxError as syn_err:
        return (
            f"SYNTAX_ERROR in {filename} line {syn_err.lineno}, col {syn_err.offset}: {syn_err.msg}"
        )
    except Exception as exc:
        logger.warning("validate_syntax_in_memory runtime exception: %s", exc)
        return f"validate_syntax_in_memory error: {type(exc).__name__}: {str(exc)}"


@tool("parse_search_replace_blocks")
def parse_search_replace_blocks(
    patch_content: str,
) -> str:
    """Parse and validate structural search/replace blocks without modifying disk.

    Args:
        patch_content: Search/replace patch formatted text.

    Returns:
        Structured breakdown of target files and block count, or clean error string.
    """
    try:
        if not SurgicalPatchApplier.is_search_replace_patch(patch_content):
            return "parse_search_replace_blocks: Content is not a SEARCH/REPLACE block format."

        blocks = SurgicalPatchApplier.parse_blocks(patch_content)
        if not blocks:
            return "parse_search_replace_blocks: No valid blocks parsed from content."

        target_files = list(
            {getattr(b, "target_file", "") for b in blocks if getattr(b, "target_file", "")}
        )
        return (
            f"Parsed {len(blocks)} surgical block(s) targeting file(s): {', '.join(target_files)}."
        )
    except Exception as exc:
        logger.warning("parse_search_replace_blocks runtime exception: %s", exc)
        return f"parse_search_replace_blocks error: {type(exc).__name__}: {str(exc)}"


implementer_registry.register(apply_search_replace_block)
implementer_registry.register(validate_syntax_in_memory)
implementer_registry.register(parse_search_replace_blocks)

implementer_tools = implementer_registry.list_tools()
