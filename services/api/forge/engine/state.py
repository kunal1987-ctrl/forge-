"""State contracts, schema enforcement, and immutable initializers for FORGE.

Architecture:
- Explicit TypedDict schema definitions for AgentState, ExecutionPlan, and TestResults.
- Default initializers for execution_plan, patch, test_results, and pr_url to ensure
  safe state propagation to frontend and persistent storage.
- Contract validation asserting type compliance across all mandatory lifecycle keys.
"""

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
    """Idempotently merge retrieved context chunks, deduplicating by file_path::symbol_name."""
    if incoming is None:
        return list(existing or [])

    if isinstance(incoming, dict) and "replace" in incoming:
        return deduplicate_and_cap_context(incoming["replace"])

    if isinstance(incoming, PrunedList):
        return deduplicate_and_cap_context(incoming)

    in_list = list(incoming if isinstance(incoming, (list, tuple)) else [incoming])
    combined = list(existing or []) + in_list
    return deduplicate_and_cap_context(combined)


# -----------------------------------------------------------------------------
# 1. Structured Payload Schema Definitions
# -----------------------------------------------------------------------------


class ExecutionPlan(TypedDict, total=False):
    """Structured architectural execution plan produced by planner_node."""

    target_files: List[str]
    logic_steps: List[str]
    test_strategy: str
    reproduction_test_code: str


class TestResults(TypedDict, total=False):
    """Structured sandboxed test outcomes produced by verifier_node.

    Populated by the polyglot sandbox runner; ``language``/``framework``/``runner``
    identify which native suite executed, and ``failures`` carries normalized
    tracebacks so repair prompts stay language-agnostic.
    """

    exit_code: int
    stdout: str
    stderr: str
    passed: bool
    passed_count: int
    failed_count: int
    status: str
    raw_output: str
    errors_count: int
    skipped_count: int
    total_count: int
    error: Optional[str]
    language: str
    framework: str
    runner: str
    command: str
    count_granularity: str
    tests_detected: bool
    failures: List[Dict[str, Any]]
    normalized_output: str


class AgentState(TypedDict, total=False):
    """Explicit, strongly typed LangGraph state contract for FORGE execution engine."""

    # Mandatory Contract Keys (Pillars 1 & 2)
    execution_plan: Union[List[Dict[str, Any]], Dict[str, Any], Any]
    patch: str
    test_results: Dict[str, Any]
    pr_url: str

    repo_path: str
    task_id: str
    issue_description: str
    retry_count: int
    error_classification: str

    # Core inputs & backwards-compatible aliases
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

    # Plan and Diff aliases for UI / downstream consumer compatibility
    plan: Union[List[Dict[str, Any]], Dict[str, Any], Any]
    target_files: List[str]
    generated_diff: str
    diff: str
    status: str
    logs: List[str]
    history: List[Dict[str, Any]]
    iteration: int

    # Investigation Loop State Tracking
    investigation_step: int
    max_investigation_steps: int
    investigation_complete: bool
    investigation_history: List[Dict[str, Any]]
    retrieval_confidence: str
    investigator_contract: Dict[str, Any]

    # Memory Optimization, Pruning, and Artifact Offloading
    summary: str
    repair_trajectory: List[Dict[str, Any]]
    sanitized_error: str
    rolling_summary: Dict[str, Any]
    artifact_id: str
    artifact_refs: Dict[str, str]

    # Error Classification Engine
    error_details: Dict[str, Any]
    error_payload: Dict[str, Any]
    error_message: str

    # Reproduction-First (TDD) Gate & Verification Tracking
    reproduction_script: str
    repro_passed: bool
    repro_test_code: str
    repro_test_path: str
    repro_verified_failing: bool
    reproduction_test_code: str
    reproduction_verified: bool
    verification_log: str
    pr_body: str

    # Collaborative Governance, Risk Scoring, and Human-in-the-Loop Review
    impact_analysis: Dict[str, Any]
    review_payload: Dict[str, Any]
    human_feedback: str
    decision: str
    review_decision: str
    review_approved: bool
    critique_feedback: str

    # Reasoning & Stateful Reflection Loop
    reasoning_milestone: Dict[str, Any]
    current_hypothesis: str
    next_action: str
    confidence_score: float
    diagnostic_questions: List[str]
    reflection_history: List[Dict[str, Any]]

    # Computed Dynamic Metrics (Diff stats, Latency, Confidence)
    metrics: Dict[str, Any]
    diff_stats: Dict[str, Any]
    latency_impact: float

    # Execution Telemetry State (Pillars 1 & 2)
    test_suite_passed: int
    target_scope: str
    pr_target: str


# -----------------------------------------------------------------------------
# 2. Default Initializers & State Factory
# -----------------------------------------------------------------------------


def get_default_execution_plan() -> Dict[str, Any]:
    """Default structured execution plan initializer."""
    return {
        "target_files": [],
        "logic_steps": [],
        "test_strategy": "",
        "reproduction_test_code": "",
    }


def get_default_test_results() -> Dict[str, Any]:
    """Default structured test results initializer."""
    return {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "passed": False,
        "passed_count": 0,
        "failed_count": 0,
        "status": "pending",
        "raw_output": "",
    }


def create_initial_agent_state(
    task_id: str = "",
    repo_path: str = "",
    issue_description: str = "",
    *,
    execution_plan: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    patch: Optional[str] = None,
    test_results: Optional[Dict[str, Any]] = None,
    pr_url: Optional[str] = None,
    prompt: Optional[str] = None,
    worktree_path: Optional[str] = None,
    repo_id: Optional[str] = None,
    mode: str = "fast",
    **kwargs: Any,
) -> AgentState:
    """Instantiate a clean, immutable initial state with strictly typed default initializers.

    Default initializers for execution_plan, patch, test_results, and pr_url are enforced:
    - execution_plan: {} (or provided plan dict/list)
    - patch: ""
    - test_results: {} (or provided test results dict)
    - pr_url: ""
    - retry_count: 0
    - error_classification: ""
    """
    effective_task_id = str(task_id or kwargs.get("id") or "task-init")
    effective_repo_path = str(repo_path or worktree_path or repo_id or "")
    effective_prompt = str(prompt or issue_description or "")

    # Resolve execution plan and patch defaults
    init_plan: Any = (
        list(execution_plan)
        if isinstance(execution_plan, list)
        else (dict(execution_plan) if execution_plan is not None else {})
    )
    init_patch = str(patch if patch is not None else "")
    init_test_results = dict(test_results) if test_results is not None else {}
    init_pr_url = str(pr_url if pr_url is not None else "")

    init_state: AgentState = {
        # Core 4 Required Keys with Default Initializers
        "execution_plan": init_plan,
        "patch": init_patch,
        "test_results": init_test_results,
        "pr_url": init_pr_url,
        # Identity & Core Inputs
        "task_id": effective_task_id,
        "repo_path": effective_repo_path,
        "issue_description": effective_prompt,
        "prompt": effective_prompt,
        "worktree_path": effective_repo_path,
        "repo_id": effective_repo_path,
        "mode": str(mode),
        # Deterministic Retry & Classification
        "retry_count": int(kwargs.get("retry_count", 0)),
        "error_classification": str(kwargs.get("error_classification", "")),
        # Real Execution Telemetry (Pillars 1 & 2)
        "test_suite_passed": int(kwargs.get("test_suite_passed", 0)),
        "target_scope": str(kwargs.get("target_scope", "services/api/forge/routers/rag.py")),
        "pr_target": str(kwargs.get("pr_target", f"forge/auto-patch-{effective_task_id}")),
        # Routing & Workflow
        "status": str(kwargs.get("status", "initialized")),
        "logs": list(kwargs.get("logs", [])),
        "history": list(kwargs.get("history", [])),
        "iteration": int(kwargs.get("iteration", 0)),
        # Aliases for UI & Downstream Consumers
        "plan": init_plan,
        "generated_diff": init_patch,
        "diff": init_patch,
        "reproduction_script": str(kwargs.get("reproduction_script", "")),
        "repro_passed": bool(kwargs.get("repro_passed", False)),
        "review_approved": bool(kwargs.get("review_approved", False)),
        "critique_feedback": str(kwargs.get("critique_feedback", "")),
    }

    # Add any extra custom fields
    for k, v in kwargs.items():
        if k not in init_state:
            init_state[k] = v  # type: ignore[literal-required]

    return init_state


# -----------------------------------------------------------------------------
# 3. State Contract Validation
# -----------------------------------------------------------------------------


def validate_agent_state_contract(state: AgentState) -> None:
    """Enforce strict contract and type validation for mandatory AgentState keys.

    Mandatory keys validated:
    - task_id: str
    - repo_path: str
    - issue_description: str
    - execution_plan: dict
    - patch: str
    - test_results: dict
    - retry_count: int
    - error_classification: str
    - pr_url: str
    """
    contract_specs = [
        ("task_id", str, ["task_id"]),
        ("repo_path", str, ["repo_path", "worktree_path", "repo_id"]),
        ("issue_description", str, ["issue_description", "prompt"]),
        ("execution_plan", (dict, list), ["execution_plan", "plan"]),
        ("patch", str, ["patch", "generated_diff", "diff"]),
        ("test_results", dict, ["test_results"]),
        ("retry_count", int, ["retry_count"]),
        ("error_classification", str, ["error_classification"]),
        ("pr_url", str, ["pr_url"]),
    ]
    for key, expected_type, aliases in contract_specs:
        val = None
        for alias in aliases:
            if alias in state and state[alias] is not None:  # type: ignore[literal-required]
                val = state[alias]  # type: ignore[literal-required]
                break
        if val is not None and not isinstance(val, expected_type):  # type: ignore[arg-type]
            type_name = (
                "/".join(t.__name__ for t in expected_type)
                if isinstance(expected_type, tuple)
                else getattr(expected_type, "__name__", str(expected_type))
            )
            raise TypeError(
                f"AgentState contract violation: key '{key}' expected {type_name}, got {type(val).__name__}"
            )
