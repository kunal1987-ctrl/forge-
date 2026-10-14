"""Execution Engine LangGraph Node Implementations for FORGE.

Architecture & State Propagation:
1. planner_node: Generates structured execution plan and explicitly writes it to state["execution_plan"].
2. implementer_node: Synthesizes surgical search/replace or unified git diff and explicitly saves it to state["patch"].
3. verifier_node: Executes sandboxed test verification and writes structured test results (stdout, stderr, exit_code) to state["test_results"].
4. repro_gate_node: Red-phase defect isolation gate.
5. Asynchronous Telemetry: Emits live entry, exit, and test_outcome events through TelemetryDispatcher.
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Any, Dict, Optional

from opentelemetry import trace

from services.api.forge.engine.dispatcher import (
    TelemetryDispatcher,
    get_telemetry_dispatcher,
)
from services.api.forge.engine.state import AgentState
from services.api.forge.execution.pr_generator import pr_generator_node
from services.api.forge.orchestration.nodes import (
    cleanup_node,
    critique_node,
    error_router_node,
    implementer_node as _base_implementer_node,
    investigator_node,
    planner_node as _base_planner_node,
    pr_creation_node,
    prune_state_node,
    repro_gate_node as _base_repro_gate_node,
    reviewer_node,
    verifier_node as _base_verifier_node,
)

logger = logging.getLogger("forge.engine.nodes")
tracer = trace.get_tracer("forge.engine.nodes")

__all__ = [
    "planner_node",
    "repro_gate_node",
    "repro_gate",
    "implementer_node",
    "verifier_node",
    "investigator_node",
    "critique_node",
    "prune_state_node",
    "reviewer_node",
    "pr_creation_node",
    "pr_generator_node",
    "cleanup_node",
    "error_router_node",
    "_base_planner_node",
    "_base_repro_gate_node",
    "_base_implementer_node",
    "_base_verifier_node",
]


def _get_base_callable(name: str, fallback: Any) -> Any:
    """Dynamically resolve base node callable checking if any mock/patch is active."""
    nodes_mod = sys.modules.get("services.api.forge.engine.nodes")
    if nodes_mod and hasattr(nodes_mod, name):
        val = getattr(nodes_mod, name)
        if hasattr(val, "mock_calls"):
            return val

    graph_mod = sys.modules.get("services.api.forge.engine.graph")
    if graph_mod and hasattr(graph_mod, name):
        val = getattr(graph_mod, name)
        if hasattr(val, "mock_calls"):
            return val

    return fallback


# -----------------------------------------------------------------------------
# 1. PLANNER NODE (State Payload Propagation & Telemetry)
# -----------------------------------------------------------------------------


async def planner_node(
    state: AgentState,
    dispatcher: Optional[TelemetryDispatcher] = None,
) -> Dict[str, Any]:
    """Execute planner_node, write execution_plan to persistent state, and emit live telemetry."""
    task_id = str(state.get("task_id") or "unknown-task")
    prompt = str(state.get("prompt") or state.get("issue_description") or "")
    d = dispatcher or get_telemetry_dispatcher()

    await d.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "planner_node",
            "step": 1,
            "status": "planning",
            "message": f"Entering planner_node: Generating execution plan for prompt: '{prompt[:60]}'",
        },
    )

    try:
        base_fn = _get_base_callable("_base_planner_node", _base_planner_node)
        result = await base_fn(state)

        # Explicitly extract and propagate structured execution_plan
        plan_dict: Dict[str, Any] = result.get("execution_plan") or result.get("plan") or {
            "target_files": result.get("target_files", ["services/api/main.py"]),
            "logic_steps": result.get(
                "logic_steps",
                [
                    "Analyze issue requirements",
                    "Synthesize surgical patch",
                    "Execute regression test suite",
                ],
            ),
            "test_strategy": result.get("test_strategy", "pytest"),
            "reproduction_test_code": result.get("reproduction_test_code", ""),
        }

        # Explicit State Writing for Frontend Real-time Rendering
        state["execution_plan"] = plan_dict
        state["plan"] = plan_dict
        result["execution_plan"] = plan_dict
        result["plan"] = plan_dict

        await d.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "planner_node",
                "step": 1,
                "status": result.get("status", "planning"),
                "message": "Formulated structured execution plan",
                "execution_plan": plan_dict,
                "plan": plan_dict,
            },
        )
        return result

    except Exception as exc:
        logger.error(f"planner_node execution failure: {exc}", exc_info=True)
        await d.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "planner_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc


# -----------------------------------------------------------------------------
# 2. REPRODUCTION GATE NODE
# -----------------------------------------------------------------------------


async def repro_gate_node(
    state: AgentState,
    dispatcher: Optional[TelemetryDispatcher] = None,
) -> Dict[str, Any]:
    """Execute reproduction gate node with explicit entry and exit telemetry emission."""
    task_id = str(state.get("task_id") or "unknown-task")
    d = dispatcher or get_telemetry_dispatcher()

    await d.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "repro_gate_node",
            "status": "reproducing",
            "message": "Entering repro_gate: Validating defect reproduction in Red Phase",
        },
    )

    try:
        base_fn = _get_base_callable("_base_repro_gate_node", _base_repro_gate_node)
        result = await base_fn(state)
        repro_failing = bool(result.get("repro_verified_failing", False))

        await d.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "repro_gate_node",
                "status": result.get("status", "reproduced"),
                "repro_verified_failing": repro_failing,
                "message": (
                    "Defect successfully isolated and reproduced (Red Phase PASSED)"
                    if repro_failing
                    else "Reproduction gate concluded"
                ),
            },
        )
        return result

    except Exception as exc:
        logger.error(f"repro_gate_node execution failure: {exc}", exc_info=True)
        await d.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "repro_gate_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc


repro_gate = repro_gate_node


# -----------------------------------------------------------------------------
# 3. IMPLEMENTER NODE (Surgical Diff & State Patch Propagation)
# -----------------------------------------------------------------------------


async def implementer_node(
    state: AgentState,
    dispatcher: Optional[TelemetryDispatcher] = None,
) -> Dict[str, Any]:
    """Execute implementer_node, save surgical diff explicitly to state["patch"], and emit telemetry."""
    task_id = str(state.get("task_id") or "unknown-task")
    d = dispatcher or get_telemetry_dispatcher()

    await d.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "implementer_node",
            "step": 3,
            "status": "implementing",
            "message": "Entering implementer_node: Synthesizing diff and applying code patch",
        },
    )

    try:
        base_fn = _get_base_callable("_base_implementer_node", _base_implementer_node)
        result = await base_fn(state)

        # Extract surgical diff string across all supported aliases
        diff_str: str = str(
            result.get("patch") or result.get("generated_diff") or result.get("diff") or ""
        )

        # Explicit State Writing for Frontend Real-time Rendering
        state["patch"] = diff_str
        state["generated_diff"] = diff_str
        state["diff"] = diff_str
        result["patch"] = diff_str
        result["generated_diff"] = diff_str
        result["diff"] = diff_str

        patch_snippet = diff_str[:400]
        await d.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "implementer_node",
                "step": 3,
                "status": result.get("status", "implemented"),
                "message": "Synthesized surgical code patch and applied changes to worktree",
                "patch_snippet": patch_snippet,
            },
        )
        return result

    except Exception as exc:
        logger.error(f"implementer_node execution failure: {exc}", exc_info=True)
        await d.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "implementer_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc


# -----------------------------------------------------------------------------
# 4. VERIFIER NODE (Structured Test Results Propagation & Test Outcome Telemetry)
# -----------------------------------------------------------------------------


async def verifier_node(
    state: AgentState,
    dispatcher: Optional[TelemetryDispatcher] = None,
) -> Dict[str, Any]:
    """Execute verifier_node, save structured test stdout/stderr/exit_code to state["test_results"], and emit live telemetry."""
    task_id = str(state.get("task_id") or "unknown-task")
    d = dispatcher or get_telemetry_dispatcher()

    await d.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "verifier_node",
            "step": 4,
            "status": "verifying",
            "message": "Entering verifier_node: Executing sandboxed test suite",
        },
    )

    try:
        base_fn = _get_base_callable("_base_verifier_node", _base_verifier_node)
        result = await base_fn(state)
        raw_test_results = dict(result.get("test_results") or {})

        # Formulate structured test results payload with explicit exit_code, stdout, stderr
        exit_code = int(
            raw_test_results.get(
                "exit_code", 0 if raw_test_results.get("status") == "passed" else 1
            )
        )
        stdout = str(raw_test_results.get("stdout") or raw_test_results.get("raw_output") or "")
        stderr = str(raw_test_results.get("stderr") or "")
        passed_count = int(raw_test_results.get("passed_count", 1 if exit_code == 0 else 0))
        failed_count = int(raw_test_results.get("failed_count", 0 if exit_code == 0 else 1))
        passed = (exit_code == 0) and (failed_count == 0)

        structured_test_results: Dict[str, Any] = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "passed": passed,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "status": "passed" if passed else "failed",
            "raw_output": raw_test_results.get("raw_output") or stdout,
        }
        # Retain any additional diagnostics or artifact references
        for key in ("artifact_id", "artifact_refs", "error_classification"):
            if key in raw_test_results:
                structured_test_results[key] = raw_test_results[key]

        # Explicit State Writing for Frontend Real-time Rendering
        state["test_results"] = structured_test_results
        result["test_results"] = structured_test_results

        # Immediate test outcome emission for live UI charts & stream consumers
        await d.emit(
            task_id=task_id,
            event_type="test_outcome",
            payload={
                "node": "verifier_node",
                "step": 4,
                "status": "passed" if passed else "failed",
                "exit_code": exit_code,
                "passed_count": passed_count,
                "failed_count": failed_count,
                "test_results": structured_test_results,
                "message": f"Sandboxed tests {'passed' if passed else 'failed'} (exit code {exit_code})",
            },
            immediate=True,
        )

        await d.emit(
            task_id=task_id,
            event_type="node_exit",
            payload={
                "node": "verifier_node",
                "step": 4,
                "status": result.get("status", "verified"),
                "message": "Executed sandboxed test suite",
                "exit_code": exit_code,
                "passed": passed,
            },
        )
        return result

    except Exception as exc:
        logger.error(f"verifier_node execution failure: {exc}", exc_info=True)
        await d.emit(
            task_id=task_id,
            event_type="node_error",
            payload={
                "node": "verifier_node",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        raise exc
