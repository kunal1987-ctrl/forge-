from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Dict, List, Optional, TypedDict, Union
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from services.api.forge.orchestration.pruner import (
    PrunedList,
    deduplicate_and_cap_context,
)


def extract_chunk_key(chunk: Any) -> str:
    """Extract file_path::symbol_name or unique identifier from chunk dictionary or string."""
    if isinstance(chunk, dict):
        fp = chunk.get("file_path", "")
        sym = chunk.get("symbol_name") or chunk.get("symbol") or chunk.get("name", "")
        if fp and sym:
            return f"{fp}::{sym}"
        if fp:
            return str(fp)
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


def merge_context_chunks(
    existing: Optional[List[Any]], incoming: Optional[Union[List[Any], Dict[str, Any]]]
) -> List[Any]:
    """Idempotently merge retrieved context chunks, deduplicating by file_path::symbol_name.

    Supports direct replacement if incoming is a PrunedList or wrapped in {'replace': chunks}.
    """
    if incoming is None:
        return list(existing or [])

    if isinstance(incoming, dict) and "replace" in incoming:
        return deduplicate_and_cap_context(incoming["replace"])

    if isinstance(incoming, PrunedList):
        return deduplicate_and_cap_context(incoming)

    in_list = list(incoming if isinstance(incoming, (list, tuple)) else [incoming])
    combined = list(existing or []) + in_list
    return deduplicate_and_cap_context(combined)


class AgentState(TypedDict, total=False):
    # Core inputs
    task_id: str
    repo_id: str
    commit_sha: str
    prompt: str
    sanitized_prompt: str
    wrapped_prompt: str
    worktree_path: str
    mode: str

    # Reducers: add_messages appends instead of overwriting
    messages: Annotated[List[BaseMessage], add_messages]

    # Idempotent deduplicating reducer for retrieved code chunks
    context_retrieved: Annotated[List[Any], merge_context_chunks]

    # Investigation Loop State Tracking
    investigation_step: int
    max_investigation_steps: int
    investigation_complete: bool
    investigation_history: List[Dict[str, Any]]
    retrieval_confidence: str  # "high", "medium", or "low"
    investigator_contract: Dict[str, Any]

    # State tracking
    plan: Dict[str, Any]
    generated_diff: str
    test_results: Dict[str, Any]
    retry_count: int
    status: str

    # Memory Optimization, Pruning, and Artifact Offloading
    summary: str
    repair_trajectory: List[Dict[str, Any]]
    sanitized_error: str
    rolling_summary: Dict[str, Any]
    artifact_id: str
    artifact_refs: Dict[str, str]

    # Error Classification Engine
    error_classification: str  # SYNTAX_ERROR, IMPORT_ERROR, ASSERTION_FAILURE, TIMEOUT, UNKNOWN
    error_details: Dict[str, Any]
    error_payload: Dict[str, Any]

    # Reproduction-First (TDD) Gate & Verification Tracking
    repro_test_code: str
    repro_test_path: str
    repro_verified_failing: bool
    reproduction_test_code: str
    reproduction_verified: bool
    verification_log: str
    pr_url: str
    pr_body: str

    # Collaborative Governance, Risk Scoring, and Human-in-the-Loop Review
    impact_analysis: Dict[str, Any]
    review_payload: Dict[str, Any]
    human_feedback: str
    error_message: str
    decision: str
    review_decision: str

    # Reasoning & Stateful Reflection Loop (Pillar 1)
    reasoning_milestone: Dict[str, Any]
    current_hypothesis: str
    next_action: str
    confidence_score: float
    diagnostic_questions: List[str]
    reflection_history: List[Dict[str, Any]]
