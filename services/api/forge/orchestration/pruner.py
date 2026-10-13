"""State pruning, context deduplication, and test traceback isolation module.

Resolves State Bloat and Context Window Collapse in FORGE's repair loop by:
1. Isolating actionable failure tracebacks from raw test execution logs.
2. Deduplicating retrieved code context and enforcing strict token budgets.
3. Evicting superseded AIMessages containing obsolete diffs from LangGraph memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)

logger = logging.getLogger(__name__)


class PrunedList(list):
    """Marked list subclass indicating an explicitly pruned context payload for state reducers."""

    pass


def sanitize_test_logs(raw_output: str, max_lines: int = 120) -> str:
    r"""Sanitize and isolate actionable test failures from raw execution logs.

    1. Strips ANSI color escape sequences (\x1b\[[0-9;]*m).
    2. Isolates pytest failure banner blocks (from FAILURES/ERRORS up to short summary or execution line).
    3. If no standard pytest banner is present, retains the last max_lines lines.
    4. Enforces an absolute hard cap of 6,000 characters (~1,500 tokens). If exceeded,
       preserves the first 20 lines and last 100 lines, injecting a brevity notice in between.

    Args:
        raw_output: Raw test output string from stdout and/or stderr.
        max_lines: Maximum fallback lines if no failure banner is found.

    Returns:
        Clean, isolated traceback string strictly under 6,000 characters.
    """
    if not raw_output or not raw_output.strip():
        return ""

    # 1. Strip ANSI escape codes
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_output)

    # 2. Pytest Failure Banner Extraction
    # Matches lines starting with === FAILURES === or === ERRORS === or ___ FAILURES ___
    banner_match = re.search(
        r"(?:^|\n)[ \t]*(={2,}\s*(?:FAILURES|ERRORS)\s*={2,}.*)",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    if banner_match:
        target_text = banner_match.group(1).strip()
        # Find summary line ending: '=== short test summary info ===' or '=== ... in ...s ==='
        summary_end = re.search(
            r"(?:^|\n)[ \t]*(={2,}\s*(?:short test summary info|.*(?:failed|error|passed).*in\s+[\d\.]+\s*s(?:econds)?)\s*={2,}.*?)(?:\n(?!=)|\Z)",
            target_text,
            re.DOTALL | re.IGNORECASE,
        )
        if summary_end:
            end_pos = summary_end.end(1)
            target_text = target_text[:end_pos].strip()
    else:
        # Fallback for non-pytest panics, syntax errors, or uncaught exceptions
        lines = text.strip().splitlines()
        if len(lines) > max_lines and len(text) <= 6000:
            target_text = "\n".join(lines[-max_lines:])
        else:
            target_text = text.strip()

    # 3. Enforce strict hard cap of 6,000 characters (~1,500 tokens)
    brevity_notice = "\n... [intermediate build logs stripped for brevity] ...\n"
    lines = target_text.splitlines()

    if len(target_text) > 6000 or (not banner_match and len(lines) > max_lines):
        if len(lines) > 120:
            head = "\n".join(lines[:20])
            tail = "\n".join(lines[-100:])
            target_text = head + brevity_notice + tail
        elif len(lines) > 20:
            head = "\n".join(lines[:20])
            tail = "\n".join(lines[max(20, len(lines) - 100) :])
            target_text = head + brevity_notice + tail
        elif not banner_match and len(lines) > max_lines:
            target_text = "\n".join(lines[-max_lines:])

    # Extra safety check to strictly ensure <= 6,000 chars if lines are exceptionally wide
    if len(target_text) > 6000:
        head_part = target_text[:1200]
        tail_part = target_text[-4600:]
        target_text = head_part + brevity_notice + tail_part

    return target_text.strip()


def extract_error_details(sanitized_logs: str) -> Tuple[str, str]:
    """Extract root-cause error class and failing test file path from test logs.

    Args:
        sanitized_logs: Cleaned traceback string.

    Returns:
        Tuple of (error_type, failing_file).
    """
    if not sanitized_logs:
        return "UnknownError", "unknown"

    # Extract failing file path
    file_match = re.search(r"(?:FAILED|ERROR)\s+([^\s:]+\.py)", sanitized_logs)
    if not file_match:
        file_match = re.search(r"File\s+[\"']([^\"']+\.py)[\"']", sanitized_logs)
    if not file_match:
        file_match = re.search(r"([a-zA-Z0-9_\-\./\\]+\.py):\d+", sanitized_logs)

    failing_file = file_match.group(1).replace("\\", "/") if file_match else "unknown"

    # Extract root-cause error class (e.g., AssertionError, TypeError, IndentationError)
    error_match = re.search(
        r"(?:E\s+)?(?:[a-zA-Z_]\w*\.)?([A-Z]\w*(?:Error|Exception|Failure|Panic|Fault|Timeout))(?::|\s)",
        sanitized_logs,
    )
    if not error_match:
        error_match = re.search(r"([A-Z]\w*(?:Error|Exception|Failure))\b", sanitized_logs)

    error_type = error_match.group(1) if error_match else "TestFailure"

    return error_type, failing_file


def _get_chunk_canonical_key(chunk: Union[Dict[str, Any], str]) -> str:
    """Extract canonical unique key for a context chunk dictionary or string."""
    if isinstance(chunk, dict):
        fp = chunk.get("file_path", "")
        sym = chunk.get("symbol_name") or chunk.get("symbol") or chunk.get("name", "")
        if fp and sym:
            return f"{fp}::{sym}"
        if fp:
            return fp
        return hashlib.md5(json.dumps(chunk, sort_keys=True).encode("utf-8")).hexdigest()

    chunk_str = str(chunk).strip()
    match = re.search(r"### \[PRIMARY TARGET\]:\s*([^\n\r]+)", chunk_str)
    if match:
        return match.group(1).strip()

    match = re.search(r"File:\s*([^|\n]+)\s*\|\s*Symbol:\s*([^\n\r]+)", chunk_str)
    if match:
        return f"{match.group(1).strip()}::{match.group(2).strip()}"

    match = re.search(r"^([a-zA-Z0-9_\-\./\\]+::[a-zA-Z0-9_]+)", chunk_str)
    if match:
        return match.group(1).strip()

    return hashlib.md5(chunk_str.encode("utf-8")).hexdigest()


def _is_primary_symbol(chunk: Union[Dict[str, Any], str]) -> bool:
    """Determine whether a chunk represents a primary target or a 1-hop dependency."""
    if isinstance(chunk, dict):
        if chunk.get("is_primary") is True:
            return True
        if chunk.get("hop", 0) == 0:
            return True
        chunk_type = str(chunk.get("chunk_type", "")).lower()
        if chunk_type in ("primary", "target"):
            return True
        if chunk.get("is_dependency") is True:
            return False
        return True

    chunk_str = str(chunk)
    if "[PRIMARY TARGET]" in chunk_str or "[PRIMARY]" in chunk_str:
        return True
    if (
        "[1-HOP" in chunk_str
        or "[DEPENDENCY]" in chunk_str
        or "[CALLEE]" in chunk_str
        or "[PARENT CLASS]" in chunk_str
    ):
        return False
    return True


def deduplicate_and_cap_context(
    context_list: List[Union[Dict[str, Any], str]], max_tokens: int = 6000
) -> List[Union[Dict[str, Any], str]]:
    """Deduplicate context items and enforce a token budget (~4 chars per token).

    1. Deduplicates items by canonical key (file_path::symbol_name or hash).
    2. Calculates an approximate character budget (~4 chars per token -> max_tokens * 4).
    3. If total size exceeds budget, retains all primary target symbols and drops
       lowest-ranked 1-hop dependencies until within budget.

    Args:
        context_list: List of retrieved chunk dictionaries or formatted strings.
        max_tokens: Maximum token budget (default 6,000 tokens = ~24,000 chars).

    Returns:
        Deduplicated and budget-capped list of chunks.
    """
    if not context_list:
        return PrunedList()

    max_chars = max_tokens * 4

    # 1. Deduplicate by canonical key
    seen_keys = set()
    deduped: List[Union[Dict[str, Any], str]] = []

    for item in context_list:
        key = _get_chunk_canonical_key(item)
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(item)

    def item_len(item: Union[Dict[str, Any], str]) -> int:
        if isinstance(item, dict):
            return len(item.get("content", "")) or len(json.dumps(item))
        return len(str(item))

    total_chars = sum(item_len(item) for item in deduped)
    if total_chars <= max_chars:
        return PrunedList(deduped)

    # 2. Budget exceeded -> partition into primaries and 1-hop dependencies
    primaries: List[Union[Dict[str, Any], str]] = []
    dependencies: List[Union[Dict[str, Any], str]] = []

    for item in deduped:
        if _is_primary_symbol(item):
            primaries.append(item)
        else:
            dependencies.append(item)

    result: List[Union[Dict[str, Any], str]] = []
    current_chars = 0

    # Retain all primary target symbols first
    for p in primaries:
        p_len = item_len(p)
        if current_chars + p_len <= max_chars or not result:
            result.append(p)
            current_chars += p_len
        else:
            break

    # Fill remaining budget with highest-ranked dependencies
    for d in dependencies:
        d_len = item_len(d)
        if current_chars + d_len <= max_chars:
            result.append(d)
            current_chars += d_len
        else:
            break

    return PrunedList(result)


def compact_messages(
    messages: List[BaseMessage],
    repair_trajectory: Optional[List[Dict[str, Any]]] = None,
) -> List[Union[BaseMessage, RemoveMessage]]:
    """Evict superseded diffs and obsolete error messages from checkpointed state memory.

    1. Preserves the original SystemMessage and the root user HumanMessage.
    2. Evicts all previous AIMessage instances containing superseded unified diffs
       and intermediate verbose messages by issuing LangChain RemoveMessage(id=msg.id).
    3. Synthesizes a structured HumanMessage containing the repair trajectory.

    Args:
        messages: Full message history from state["messages"].
        repair_trajectory: Optional list of past repair attempt metadata dictionaries.

    Returns:
        List of RemoveMessage instances and synthesized HumanMessage to apply to state.
    """
    if not messages:
        return []

    eviction_list: List[Union[BaseMessage, RemoveMessage]] = []
    has_system = False
    has_root_user = False

    for msg in messages:
        # Keep the original root SystemMessage
        if isinstance(msg, SystemMessage) and not has_system:
            has_system = True
            continue

        # Keep the root user HumanMessage
        if isinstance(msg, HumanMessage) and not has_root_user:
            has_root_user = True
            continue

        # Evict all intermediate AIMessages (superseded diffs) and intermediate errors
        msg_id = getattr(msg, "id", None)
        if msg_id:
            eviction_list.append(RemoveMessage(id=str(msg_id)))

    # Synthesize a single structured HumanMessage summarizing the repair trajectory
    if repair_trajectory:
        traj_lines = [
            f"- Attempt {item.get('attempt', i + 1)}: [{item.get('failed_file', 'unknown')}] "
            f"{item.get('failure_type', 'Error')}: {item.get('summary', '')}"
            for i, item in enumerate(repair_trajectory)
        ]
        summary_text = (
            "STRUCTURED REPAIR TRAJECTORY (Past Attempts):\n"
            + "\n".join(traj_lines)
            + "\n\nPlease resolve the active test failure without repeating previous mistakes."
        )
        eviction_list.append(HumanMessage(content=summary_text, name="repair_trajectory"))

    return eviction_list
