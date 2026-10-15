"""Unit and integration tests for TelemetryDispatcher and LangGraph execution engine.

Verifies:
1. TelemetryDispatcher live WebSocket/SSE streaming via subscribe/emit without batch delay.
2. Explicit `await dispatcher.emit(task_id, event_type, payload)` upon entry and exit
   across planner_node, repro_gate, implementer_node, and verifier_node.
3. Verifier node emitting dedicated 'test_outcome' telemetry.
4. AgentState immutability contracts with default values for retry_count, error_classification, test_results.
5. Deterministic conditional edge routing preventing deadlocks or infinite loops in verifier-to-implementer retry phase.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from services.api.forge.engine.dispatcher import TelemetryDispatcher, get_telemetry_dispatcher
from services.api.forge.engine.graph import (
    AgentState,
    create_initial_agent_state,
    implementer_node,
    planner_node,
    repro_gate,
    verifier_node,
    verify_router,
)


# ==============================================================================
# 1. TELEMETRY DISPATCHER LIVE WEBSOCKET/SSE STREAMING TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_dispatcher_emit_live_streaming_no_lag():
    """Verify dispatcher.emit broadcasts instantaneously to subscribed queues without waiting for flush_interval."""
    # Set high flush_interval (60s) to prove live delivery does NOT wait for batch timer
    dispatcher = TelemetryDispatcher(flush_interval=60.0, max_batch=100)
    task_id = "test-live-task-123"

    # Subscribe to live SSE/WebSocket stream
    stream_queue = dispatcher.subscribe(task_id)

    assert stream_queue.empty()

    # Emit node_entry event
    await dispatcher.emit(
        task_id=task_id,
        event_type="node_entry",
        payload={
            "node": "planner_node",
            "step": 1,
            "status": "planning",
            "message": "Starting planning phase",
        },
        immediate=True,
    )

    # Must be available immediately in the subscriber queue
    event = stream_queue.get_nowait()
    assert event["task_id"] == task_id
    assert event["event_type"] == "node_entry"
    assert event["node"] == "planner_node"
    assert event["status"] == "planning"
    assert event["step"] == 1
    assert "timestamp" in event

    # Emit node_exit event
    await dispatcher.emit(
        task_id=task_id,
        event_type="node_exit",
        payload={"node": "planner_node", "status": "completed"},
        immediate=True,
    )

    exit_event = stream_queue.get_nowait()
    assert exit_event["event_type"] == "node_exit"
    assert exit_event["status"] == "completed"

    # Unsubscribe cleanly
    dispatcher.unsubscribe(stream_queue, task_id)


# ==============================================================================
# 2. LANGGRAPH NODES EXPLICIT DISPATCHER.EMIT CALLS UPON ENTRY AND EXIT
# ==============================================================================


@pytest.mark.asyncio
async def test_planner_node_emits_entry_and_exit_telemetry():
    """Verify planner_node explicitly calls dispatcher.emit upon entry and exit."""
    dispatcher = get_telemetry_dispatcher()
    emitted_events: List[Dict[str, Any]] = []

    async def mock_emit(task_id: str, event_type: str, payload: Any = None, **kwargs: Any) -> bool:
        emitted_events.append({"task_id": task_id, "event_type": event_type, "payload": payload})
        return True

    state = create_initial_agent_state(
        task_id="task-planner-test",
        prompt="Fix division by zero error in core math engine",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )

    with patch.object(dispatcher, "emit", side_effect=mock_emit):
        res = await planner_node(state)

    assert res["status"] == "planning"
    assert len(emitted_events) >= 2

    # Entry event
    entry = emitted_events[0]
    assert entry["task_id"] == "task-planner-test"
    assert entry["event_type"] == "node_entry"
    assert entry["payload"]["node"] == "planner_node"

    # Exit event
    exit_evt = emitted_events[-1]
    assert exit_evt["task_id"] == "task-planner-test"
    assert exit_evt["event_type"] == "node_exit"
    assert exit_evt["payload"]["node"] == "planner_node"


@pytest.mark.asyncio
async def test_planner_node_preserves_legacy_plan_and_structured_steps():
    """Keep plan metadata as an object while exposing tactical steps separately."""
    state = create_initial_agent_state(
        task_id="task-planner-plan-shape",
        prompt="Fix division by zero",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    structured_steps = [
        {"step": 1, "title": "Inspect", "description": "Inspect divide()"},
    ]
    legacy_plan = {
        "target_files": ["calculator.py"],
        "logic_steps": ["Inspect divide()"],
        "test_strategy": "pytest",
        "reproduction_test_code": "",
    }
    dispatcher = AsyncMock()

    with (
        patch(
            "services.api.forge.engine.graph._base_planner_node",
            new_callable=AsyncMock,
            return_value={
                "execution_plan": structured_steps,
                "plan": legacy_plan,
                "status": "planning",
            },
        ),
        patch(
            "services.api.forge.engine.graph.get_telemetry_dispatcher",
            return_value=dispatcher,
        ),
    ):
        result = await planner_node(state)

    assert result["execution_plan"] == structured_steps
    assert result["plan"] == legacy_plan
    assert state["execution_plan"] == structured_steps
    assert state["plan"] == legacy_plan
    assert result["target_scope"] == "calculator.py"


@pytest.mark.asyncio
async def test_repro_gate_emits_entry_and_exit_telemetry():
    """Verify repro_gate (repro_gate_node) explicitly calls dispatcher.emit upon entry and exit."""
    dispatcher = get_telemetry_dispatcher()
    emitted_events: List[Dict[str, Any]] = []

    async def mock_emit(task_id: str, event_type: str, payload: Any = None, **kwargs: Any) -> bool:
        emitted_events.append({"task_id": task_id, "event_type": event_type, "payload": payload})
        return True

    state = create_initial_agent_state(
        task_id="task-repro-test",
        prompt="Write a reproducing test for memory leak",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )

    # Mock the underlying base node to return a verified reproduction outcome
    with (
        patch(
            "services.api.forge.engine.graph._base_repro_gate_node", new_callable=AsyncMock
        ) as mock_base,
        patch.object(dispatcher, "emit", side_effect=mock_emit),
    ):
        mock_base.return_value = {
            "repro_verified_failing": True,
            "status": "reproduced",
            "repro_test_code": "def test_fail(): assert False",
        }
        res = await repro_gate(state)

    assert res["repro_verified_failing"] is True
    assert len(emitted_events) == 2

    assert emitted_events[0]["event_type"] == "node_entry"
    assert emitted_events[0]["payload"]["node"] == "repro_gate_node"

    assert emitted_events[1]["event_type"] == "node_exit"
    assert emitted_events[1]["payload"]["node"] == "repro_gate_node"
    assert emitted_events[1]["payload"]["repro_verified_failing"] is True


@pytest.mark.asyncio
async def test_implementer_node_emits_entry_and_exit_telemetry():
    """Verify implementer_node explicitly calls dispatcher.emit upon entry and exit."""
    dispatcher = get_telemetry_dispatcher()
    emitted_events: List[Dict[str, Any]] = []

    async def mock_emit(task_id: str, event_type: str, payload: Any = None, **kwargs: Any) -> bool:
        emitted_events.append({"task_id": task_id, "event_type": event_type, "payload": payload})
        return True

    state = create_initial_agent_state(
        task_id="task-implement-test",
        prompt="Generate bugfix diff",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )

    with (
        patch(
            "services.api.forge.engine.graph._base_implementer_node", new_callable=AsyncMock
        ) as mock_base,
        patch.object(dispatcher, "emit", side_effect=mock_emit),
    ):
        mock_base.return_value = {
            "patch": "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-def bad():\n+def good():",
            "status": "implemented",
        }
        res = await implementer_node(state)

    assert res["status"] == "implemented"
    assert len(emitted_events) == 2

    assert emitted_events[0]["event_type"] == "node_entry"
    assert emitted_events[0]["payload"]["node"] == "implementer_node"

    assert emitted_events[1]["event_type"] == "node_exit"
    assert emitted_events[1]["payload"]["node"] == "implementer_node"
    assert "patch_snippet" in emitted_events[1]["payload"]


@pytest.mark.asyncio
async def test_verifier_node_emits_test_outcome_and_exit_telemetry():
    """Verify verifier_node explicitly calls dispatcher.emit for test_outcome and node_exit."""
    dispatcher = get_telemetry_dispatcher()
    emitted_events: List[Dict[str, Any]] = []

    async def mock_emit(task_id: str, event_type: str, payload: Any = None, **kwargs: Any) -> bool:
        emitted_events.append({"task_id": task_id, "event_type": event_type, "payload": payload})
        return True

    state = create_initial_agent_state(
        task_id="task-verifier-test",
        prompt="Run pytest verification",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )

    test_payload = {
        "exit_code": 0,
        "passed_count": 5,
        "failed_count": 0,
        "raw_output": "5 passed in 0.2s",
    }

    with (
        patch(
            "services.api.forge.engine.graph._base_verifier_node", new_callable=AsyncMock
        ) as mock_base,
        patch.object(dispatcher, "emit", side_effect=mock_emit),
    ):
        mock_base.return_value = {
            "test_results": test_payload,
            "status": "verified",
        }
        res = await verifier_node(state)

    assert res["status"] == "verified"
    # Expected: node_entry, test_outcome, node_exit
    assert len(emitted_events) == 3

    assert emitted_events[0]["event_type"] == "node_entry"
    assert emitted_events[0]["payload"]["node"] == "verifier_node"

    # Specific live test outcome event for frontend charts & streaming
    assert emitted_events[1]["event_type"] == "test_outcome"
    assert emitted_events[1]["payload"]["status"] == "passed"
    assert emitted_events[1]["payload"]["exit_code"] == 0

    assert emitted_events[2]["event_type"] == "node_exit"
    assert emitted_events[2]["payload"]["node"] == "verifier_node"


@pytest.mark.asyncio
async def test_verifier_node_preserves_retry_and_classified_traceback():
    dispatcher = get_telemetry_dispatcher()
    state = create_initial_agent_state(
        task_id="task-verifier-retry",
        prompt="Fix the failing assertion",
        worktree_path="/tmp/test_wt",
        mode="fast",
    )
    state["retry_count"] = 0

    async def mock_emit(*args: Any, **kwargs: Any) -> bool:
        return True

    with (
        patch(
            "services.api.forge.engine.graph._base_verifier_node", new_callable=AsyncMock
        ) as mock_base,
        patch.object(dispatcher, "emit", side_effect=mock_emit),
    ):
        mock_base.return_value = {
            "test_results": {
                "exit_code": 1,
                "passed_count": 0,
                "failed_count": 1,
                "raw_output": "FAILED tests/test_calc.py::test_add\nE   AssertionError: assert 1 == 2",
                "stderr": "AssertionError: assert 1 == 2",
            },
            "retry_count": 1,
            "status": "verifying",
        }

        result = await verifier_node(state)

    assert state["retry_count"] == result["retry_count"] == 1
    assert result["error_classification"] == "ASSERTION_FAILURE"
    assert result["error_payload"]["traceback"] == "AssertionError: assert 1 == 2"


# ==============================================================================
# 3. GRAPH STATE IMMUTABILITY & RETRY LOOP STABILITY TESTS
# ==============================================================================


def test_agent_state_immutable_defaults():
    """Verify create_initial_agent_state initializes immutable defaults for retry_count, error_classification, test_results."""
    state = create_initial_agent_state(
        task_id="immutability-task",
        prompt="Ensure clean defaults",
        worktree_path="/tmp/wt",
    )

    # Defaults required by architectural spec
    assert state["retry_count"] == 0
    assert state["error_classification"] == ""
    assert state["test_results"] == {}
    assert state["patch"] == ""
    assert state["pr_url"] == ""


def test_verify_router_deterministic_edge_routing_and_budget_bounds():
    """Verify routing is deterministic and the retry count is not incremented twice."""
    # 1. Success -> routes to pr_generator
    pass_state: AgentState = {
        "test_results": {"exit_code": 0, "passed_count": 3},
        "retry_count": 0,
    }
    assert verify_router(pass_state) == "pr_generator"

    # 2. Failure with retry budget remaining routes to implementer.
    retry_state: AgentState = {
        "test_results": {"exit_code": 1, "raw_output": "AssertionError: expected True got False"},
        "retry_count": 1,
    }
    dest_retry = verify_router(retry_state)
    assert dest_retry == "implementer"
    assert retry_state["retry_count"] == 1
    assert retry_state["error_classification"] == "ASSERTION_FAILURE"

    # 3. Failure with retry budget exhausted (retry_count >= 3) -> routes to failed (cleanup_node)
    exhausted_state: AgentState = {
        "test_results": {"exit_code": 1, "raw_output": "SyntaxError: unexpected EOF"},
        "retry_count": 3,
    }
    dest_exhausted = verify_router(exhausted_state)
    assert dest_exhausted == "failed"
    assert dest_exhausted == "cleanup_node"
    assert exhausted_state["error_classification"] == "SYNTAX_ERROR"
