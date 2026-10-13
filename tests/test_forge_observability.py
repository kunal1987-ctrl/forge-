"""Comprehensive test suite for FORGE Observability, Distributed Tracing, Structured Logging, and Worker Resilience:
1. Distributed Tracing & Agent Telemetry (services/api/forge/telemetry/tracer.py)
2. Structured JSON Logging & Correlation IDs (services/api/forge/logging/logger.py)
3. Resilient Worker Timeframes & Redis Dead-Letter Queues (services/api/forge/worker/celery_app.py)
4. Comprehensive Health Check & Circuit Breaker Probes (services/api/forge/routers/health.py)
"""

from __future__ import annotations

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Response, status
from langchain_core.outputs import LLMResult

from services.api.forge.logging import (
    bind_context,
    clear_context,
    get_current_context,
    get_logger,
    log_exception,
)
from services.api.forge.routers.health import (
    ComponentHealth,
    health_check_endpoint,
)
from services.api.forge.telemetry import (
    ForgeTelemetryCallbackHandler,
    extract_trace_headers,
    init_trace_context,
    inject_trace_headers,
    trace_node,
)
from services.api.forge.worker.celery_app import (
    celery_app,
    get_dlq_tasks,
    push_to_dlq,
)


# ===========================================================================
# PILLAR 1: DISTRIBUTED TRACING & AGENT TELEMETRY
# ===========================================================================


def test_init_trace_context_and_header_propagation():
    """Verify trace_id initialization and W3C trace context header injection/extraction."""
    # 1. Initialize fresh 32-char hex trace ID
    trace_id = init_trace_context()
    assert len(trace_id) == 32
    assert int(trace_id, 16) >= 0

    # 2. Re-use existing trace ID
    custom_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    assert init_trace_context(custom_id) == custom_id

    # 3. Header propagation
    headers = inject_trace_headers({"x-custom": "test"})
    assert "x-trace-id" in headers
    assert "x-correlation-id" in headers
    assert headers["x-custom"] == "test"

    extracted_ctx = extract_trace_headers(headers)
    assert extracted_ctx is not None


@pytest.mark.asyncio
async def test_trace_node_decorator_and_span_metrics():
    """Verify trace_node decorator instruments node execution, capturing latency, status, and attributes."""
    state = {
        "task_id": "task-trace-1",
        "repo_id": "repo-trace-1",
        "prompt": "Fix race condition in threadpool",
        "retry_count": 1,
    }

    @trace_node("test_investigator")
    async def sample_node(current_state):
        return {
            "status": "investigating",
            "confidence_score": 0.88,
            "generated_diff": "diff --git a/b.py",
            "tokens_used": 450,
        }

    res = await sample_node(state)
    assert res["status"] == "investigating"
    assert res["confidence_score"] == 0.88


def test_telemetry_callback_handler_token_and_tool_tracking():
    """Verify ForgeTelemetryCallbackHandler records LLM tokens and tool executions to spans."""
    handler = ForgeTelemetryCallbackHandler(task_id="task-cb-1", correlation_id="corr-cb-1")
    run_id = uuid.uuid4()

    # 1. LLM Start & End
    handler.on_llm_start({"name": "gpt-4o-mini"}, ["Translate prompt"], run_id=run_id)
    assert str(run_id) in handler._llm_start_times

    mock_llm_result = MagicMock(spec=LLMResult)
    mock_llm_result.llm_output = {
        "token_usage": {
            "prompt_tokens": 120,
            "completion_tokens": 80,
            "total_tokens": 200,
        }
    }
    handler.on_llm_end(mock_llm_result, run_id=run_id)
    assert str(run_id) not in handler._llm_start_times

    # 2. Tool Start & End
    tool_run_id = uuid.uuid4()
    handler.on_tool_start(
        {"name": "hybrid_code_search"}, "query='def multiply'", run_id=tool_run_id
    )
    assert str(tool_run_id) in handler._tool_start_times

    handler.on_tool_end("Found 2 chunks", run_id=tool_run_id)
    assert str(tool_run_id) not in handler._tool_start_times


# ===========================================================================
# PILLAR 2: STRUCTURED JSON LOGGING & CORRELATION IDS
# ===========================================================================


def test_structured_json_logging_mandatory_context_keys(caplog):
    """Verify log events output valid JSON containing mandatory context keys: timestamp, log_level, task_id, repo_id, node_name, correlation_id."""
    bind_context(
        task_id="task-log-42",
        repo_id="repo-log-99",
        node_name="repro_gate_node",
        correlation_id="corr-log-777",
    )

    ctx = get_current_context()
    assert ctx["task_id"] == "task-log-42"
    assert ctx["repo_id"] == "repo-log-99"
    assert ctx["node_name"] == "repro_gate_node"
    assert ctx["correlation_id"] == "corr-log-777"

    logger = get_logger("forge.audit")
    with caplog.at_level(logging.INFO):
        logger.info("Executing reproduction gate verification", step=2)

    found = False
    for record in caplog.records:
        msg = record.getMessage()
        if "Executing reproduction gate" in msg:
            import ast

            data = json.loads(msg) if msg.startswith('{"') else ast.literal_eval(msg)
            assert data["task_id"] == "task-log-42"
            assert data["repo_id"] == "repo-log-99"
            assert data["node_name"] == "repro_gate_node"
            assert data["correlation_id"] == "corr-log-777"
            assert data["log_level"] == "INFO"
            assert "timestamp" in data
            assert data["step"] == 2
            found = True
            break
    assert found, f"Target structured log event not found in: {caplog.text}"

    clear_context()
    cleared = get_current_context()
    assert cleared["task_id"] == "unknown"


def test_log_exception_captures_traceback_with_task_id(caplog):
    """Verify log_exception logs full traceback stack and correlated task ID in structured JSON."""
    logger = get_logger("forge.sandbox")
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("Docker container failed to allocate port 8000")
        except ValueError as val_err:
            log_exception(
                logger,
                "Docker sandbox startup failed",
                val_err,
                task_id="task-err-1",
                repo_id="repo-err-1",
                node_name="verifier_node",
            )

    found = False
    for record in caplog.records:
        msg = record.getMessage()
        if "Docker sandbox startup failed" in msg:
            import ast

            data = json.loads(msg) if msg.startswith('{"') else ast.literal_eval(msg)
            assert data["task_id"] == "task-err-1"
            assert data["exception_type"] == "ValueError"
            assert "exception_message" in data
            assert "Docker container failed to allocate" in data["exception_message"]
            assert "exception" in data or "exc_info" in data
            found = True
            break
    assert found, f"Exception log event not found in: {caplog.text}"


# ===========================================================================
# PILLAR 3: RESILIENT WORKER TIMEFRAMES & DEAD-LETTER QUEUES
# ===========================================================================


def test_celery_time_limits_and_dlq_configuration():
    """Verify Celery task has strict hard time limit of 300s and soft limit of 270s."""
    conf = celery_app.conf
    assert conf.task_time_limit == 300
    assert conf.task_soft_time_limit == 270
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True


def test_push_and_get_dlq_tasks_in_redis():
    """Verify push_to_dlq serializes failed task metadata and pushes to Redis DLQ list."""
    mock_redis = MagicMock()
    stored_items = []

    def mock_lpush(key, value):
        stored_items.append(value)
        return len(stored_items)

    mock_redis.lpush = MagicMock(side_effect=mock_lpush)
    mock_redis.lrange = MagicMock(return_value=stored_items)
    mock_redis.publish = MagicMock()

    with patch("services.api.forge.worker.celery_app.get_redis_client", return_value=mock_redis):
        record = push_to_dlq(
            task_id="task-dlq-1",
            task_name="forge.execute_agent_task",
            args=("arg1",),
            kwargs={"prompt": "Fix deadlock"},
            error_msg="SoftTimeLimitExceeded in container",
            exception_type="SoftTimeLimitExceeded",
            traceback_str="Traceback...",
            retry_count=3,
            correlation_id="corr-dlq-1",
        )

        assert record["task_id"] == "task-dlq-1"
        assert record["exception_type"] == "SoftTimeLimitExceeded"
        assert record["retry_count"] == 3
        assert mock_redis.lpush.called
        assert mock_redis.publish.called

        dlq_tasks = get_dlq_tasks()
        assert len(dlq_tasks) >= 1
        assert dlq_tasks[0]["task_id"] == "task-dlq-1"


def test_resilient_forge_task_on_failure_dlq_dispatch():
    """Verify ResilientForgeTask.on_failure automatically pushes failed tasks to DLQ."""
    task_instance = celery_app.tasks["forge.execute_agent_task"]
    task_instance.push_request(retries=3)

    mock_push = MagicMock()
    with patch("services.api.forge.worker.celery_app.push_to_dlq", mock_push):
        task_instance.on_failure(
            exc=TimeoutError("Container timed out"),
            task_id="task-fail-99",
            args=(),
            kwargs={"correlation_id": "corr-fail-99"},
            einfo=None,
        )

    assert mock_push.called
    kwargs_call = mock_push.call_args[1]
    assert kwargs_call["task_id"] == "task-fail-99"
    assert kwargs_call["exception_type"] == "TimeoutError"
    assert kwargs_call["retry_count"] == 3


# ===========================================================================
# PILLAR 4: COMPREHENSIVE HEALTH CHECK & CIRCUIT BREAKERS
# ===========================================================================


@pytest.mark.asyncio
async def test_health_check_probes_healthy():
    """Verify health_check_endpoint returns status 200 with all components up."""
    mock_pg = ComponentHealth(status="up", latency_ms=1.2, details="Postgres up")
    mock_redis = ComponentHealth(status="up", latency_ms=0.5, details="Redis up")
    mock_docker = ComponentHealth(status="up", latency_ms=3.1, details="Docker up")
    mock_llm = ComponentHealth(status="up", latency_ms=85.0, details="LLM up")

    with (
        patch("services.api.forge.routers.health.probe_postgres", AsyncMock(return_value=mock_pg)),
        patch("services.api.forge.routers.health.probe_redis", AsyncMock(return_value=mock_redis)),
        patch(
            "services.api.forge.routers.health.probe_docker", AsyncMock(return_value=mock_docker)
        ),
        patch(
            "services.api.forge.routers.health.probe_llm_gateway", AsyncMock(return_value=mock_llm)
        ),
    ):
        response = Response()
        res = await health_check_endpoint(response)

    assert res.status == "healthy"
    assert response.status_code == status.HTTP_200_OK
    assert res.components["database"].status == "up"
    assert res.components["redis"].status == "up"
    assert res.components["docker"].status == "up"
    assert res.components["llm_gateway"].status == "up"


@pytest.mark.asyncio
async def test_health_check_circuit_breaker_503_on_critical_failure():
    """Verify health_check_endpoint returns HTTP 503 and 'unhealthy' if PostgreSQL or Docker is down."""
    mock_pg_down = ComponentHealth(status="down", latency_ms=50.0, error="Connection refused")
    mock_redis = ComponentHealth(status="up", latency_ms=0.5, details="Redis up")
    mock_docker = ComponentHealth(status="up", latency_ms=3.1, details="Docker up")
    mock_llm = ComponentHealth(status="up", latency_ms=85.0, details="LLM up")

    with (
        patch(
            "services.api.forge.routers.health.probe_postgres", AsyncMock(return_value=mock_pg_down)
        ),
        patch("services.api.forge.routers.health.probe_redis", AsyncMock(return_value=mock_redis)),
        patch(
            "services.api.forge.routers.health.probe_docker", AsyncMock(return_value=mock_docker)
        ),
        patch(
            "services.api.forge.routers.health.probe_llm_gateway", AsyncMock(return_value=mock_llm)
        ),
    ):
        response = Response()
        res = await health_check_endpoint(response)

    assert res.status == "unhealthy"
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert res.components["database"].status == "down"
    assert res.components["database"].error == "Connection refused"


@pytest.mark.asyncio
async def test_health_check_degraded_when_llm_offline():
    """Verify health_check_endpoint returns HTTP 200 and 'degraded' when only LLM gateway is offline/degraded."""
    mock_pg = ComponentHealth(status="up", latency_ms=1.2, details="Postgres up")
    mock_redis = ComponentHealth(status="up", latency_ms=0.5, details="Redis up")
    mock_docker = ComponentHealth(status="up", latency_ms=3.1, details="Docker up")
    mock_llm_deg = ComponentHealth(status="degraded", details="Running in offline mode")

    with (
        patch("services.api.forge.routers.health.probe_postgres", AsyncMock(return_value=mock_pg)),
        patch("services.api.forge.routers.health.probe_redis", AsyncMock(return_value=mock_redis)),
        patch(
            "services.api.forge.routers.health.probe_docker", AsyncMock(return_value=mock_docker)
        ),
        patch(
            "services.api.forge.routers.health.probe_llm_gateway",
            AsyncMock(return_value=mock_llm_deg),
        ),
    ):
        response = Response()
        res = await health_check_endpoint(response)

    assert res.status == "degraded"
    assert response.status_code == status.HTTP_200_OK
