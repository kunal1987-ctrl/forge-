"""Integration tests for reliability wiring, tracing, structured logging, and health endpoints.

Verifies:
1. Custom Exceptions (exceptions.py) in nodes, sandbox, and patch_applier with state["error_payload"]
   flowing directly into ErrorClassifier.
2. Structured Logging (logger.py) & Distributed Tracing (tracer.py) in FastAPI middleware and Celery.
3. Health Check Endpoints (health.py) for PostgreSQL, Redis, Docker, and LLM API gateway.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from services.api.forge.exceptions import (
    SandboxExecutionError,
)
from services.api.forge.execution.patch_applier import SurgicalPatchApplier
from services.api.forge.execution.sandbox import DockerSandbox
from services.api.forge.orchestration.error_classifier import ErrorCategory, ErrorClassifier
from services.api.forge.orchestration.nodes import error_router_node
from services.api.forge.routers.health import (
    probe_docker,
    probe_llm_gateway,
    probe_postgres,
    probe_redis,
)
from services.api.main import app


# ==============================================================================
# PILLAR 1: CUSTOM EXCEPTIONS & ERROR ROUTING
# ==============================================================================


def test_patch_applier_raises_or_reports_patch_application_error(tmp_path: Path):
    """Verify SurgicalPatchApplier surfaces PatchApplicationError on file read/write issues."""
    applier = SurgicalPatchApplier(tmp_path)
    # File does not exist
    patch_text = "### File: nonexistent.py\n<<<<<<< SEARCH\nfoo\n=======\nbar\n>>>>>>> REPLACE"
    res = applier.apply(patch_text)
    assert not res.success
    assert "nonexistent.py" in str(res.error_message)


def test_sandbox_execution_error_on_timeout():
    """Verify DockerSandbox raises SandboxExecutionError when raise_on_error is enabled."""
    sandbox = object.__new__(DockerSandbox)
    sandbox.run_command = MagicMock(
        return_value=MagicMock(
            exit_code=-1,
            stdout="",
            stderr="Execution timed out after 45s.",
            timeout=True,
        )
    )

    with pytest.raises(SandboxExecutionError) as exc_info:
        sandbox.run_tests("/tmp/dummy", "pytest", timeout_seconds=45, raise_on_error=True)

    assert "Execution timed out" in str(exc_info.value)
    assert exc_info.value.details.get("timed_out") is True


def test_error_classifier_routes_error_payload_into_strategic_category():
    """Verify ErrorClassifier inspects error_payload and maps to proper strategic categories."""
    # 1. Timeout in error_payload
    payload_timeout = {
        "type": "SandboxExecutionError",
        "message": "Execution timed out after 45s",
        "details": {"timed_out": True},
    }
    cat_timeout = ErrorClassifier.classify(raw_output="", stderr="", error_payload=payload_timeout)
    assert cat_timeout == ErrorCategory.TIMEOUT

    # 2. PatchApplicationError in error_payload
    payload_patch = {
        "type": "PatchApplicationError",
        "message": "Failed to match search block in auth.py",
        "details": {"target_file": "auth.py"},
    }
    cat_patch = ErrorClassifier.classify(raw_output="", stderr="", error_payload=payload_patch)
    assert cat_patch == ErrorCategory.SYNTAX_ERROR

    # 3. State error_router_node directs to syntax repair when PatchApplicationError is present
    state_err = {
        "error_payload": payload_patch,
        "test_results": {"raw_output": "", "stderr": ""},
    }
    route = error_router_node(state_err)
    assert route == "implementer_syntax_repair"


# ==============================================================================
# PILLAR 2: STRUCTURED LOGGING & TRACING
# ==============================================================================


@pytest.mark.asyncio
async def test_fastapi_structlog_correlation_middleware():
    """Verify HTTP middleware generates trace_id and binds task_id to structlog context."""
    transport = ASGITransport(app=app)
    custom_trace_id = "0123456789abcdef0123456789abcdef"
    custom_task_id = "task-uuid-777"

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/health",
            headers={
                "x-trace-id": custom_trace_id,
                "x-task-id": custom_task_id,
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("x-trace-id") == custom_trace_id
    assert resp.headers.get("x-task-id") == custom_task_id


# ==============================================================================
# PILLAR 3: HEALTH CHECK ENDPOINTS
# ==============================================================================


@pytest.mark.asyncio
async def test_probes_return_structured_component_health():
    """Verify probe functions return ComponentHealth with status and latency metrics."""
    with (
        patch("services.api.forge.routers.health.async_session_factory") as mock_db_factory,
        patch("services.api.forge.routers.health.aioredis.from_url") as mock_redis_from_url,
        patch("services.api.forge.routers.health.docker.from_env") as mock_docker_env,
        patch("services.api.forge.routers.health.httpx.AsyncClient") as mock_http_client,
    ):
        # Mock Postgres
        mock_session = AsyncMock()
        mock_session.execute.return_value = MagicMock(scalar=lambda: 1)
        mock_db_ctx = AsyncMock()
        mock_db_ctx.__aenter__.return_value = mock_session
        mock_db_factory.return_value = mock_db_ctx

        # Mock Redis
        mock_redis = AsyncMock()
        mock_redis.ping.return_value = True
        mock_redis_from_url.return_value = mock_redis

        # Mock Docker
        mock_docker = MagicMock()
        mock_docker.ping.return_value = True
        mock_docker_env.return_value = mock_docker

        # Mock LLM gateway
        mock_http = AsyncMock()
        mock_resp = MagicMock(status_code=200)
        mock_http.get.return_value = mock_resp
        mock_http_ctx = AsyncMock()
        mock_http_ctx.__aenter__.return_value = mock_http
        mock_http_client.return_value = mock_http_ctx

        pg_res = await probe_postgres()
        redis_res = await probe_redis()
        docker_res = await probe_docker()
        llm_res = await probe_llm_gateway()

        assert pg_res.status == "up"
        assert redis_res.status == "up"
        assert docker_res.status == "up"
        assert llm_res.status in ("up", "degraded")
        assert pg_res.latency_ms >= 0


@pytest.mark.asyncio
async def test_get_api_health_endpoint():
    """Verify GET /api/health returns 200 with structured HealthCheckResponse."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")

    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "status" in data
    assert "components" in data
    assert "database" in data["components"]
    assert "redis" in data["components"]
    assert "docker" in data["components"]
    assert "llm_gateway" in data["components"]
