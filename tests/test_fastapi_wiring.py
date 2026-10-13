"""Comprehensive End-to-End Tests for FastAPI Observability, Security Logging, Health Checks, and Exception Handlers.

Verifies:
1. Correlation ID & Structured Logging Middleware (logger.py & main.py):
   - Generates and extracts X-Request-ID, binds structlog contextvars, returns correlation headers.
2. Global Exception Handlers (exceptions.py & main.py):
   - SandboxExecutionError (504 on timeout, 500 on crash).
   - PatchApplicationError (422).
   - LLMAPIError (429 on rate limit, 502 on gateway failure).
   - ForgeBaseException (500).
   - Verifies structured JSON error response format with correlation IDs.
3. Health and Readiness Endpoints (health.py):
   - GET /api/health and GET /health (HTTP 200 / 503).
   - GET /api/ready and GET /ready (HTTP 200 ready / HTTP 503 not_ready).
4. Task Submission API Route (routers/tasks.py & forge/routers/tasks.py):
   - POST /api/tasks returns HTTP 202 Accepted with task_id and trace_token.
   - Asynchronously triggers Celery worker (execute_agent_task.apply_async).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from fastapi import APIRouter, Response, status

from services.api.forge.exceptions import (
    ForgeBaseException,
    LLMAPIError,
    PatchApplicationError,
    SandboxExecutionError,
)
from services.api.forge.routers.health import (
    ComponentHealth,
    ReadinessResponse,
    readiness_check_endpoint,
)
from services.api.main import app

# Create a test router to simulate exceptions caught by global exception handlers
mock_error_router = APIRouter(prefix="/test-errors", tags=["Testing"])


@mock_error_router.get("/sandbox-timeout")
async def trigger_sandbox_timeout():
    raise SandboxExecutionError(
        message="Docker sandbox pytest run exceeded deadline",
        details={"command": "pytest", "timed_out": True},
    )


@mock_error_router.get("/sandbox-crash")
async def trigger_sandbox_crash():
    raise SandboxExecutionError(
        message="Docker sandbox exited with fatal error",
        details={"command": "pytest", "exit_code": 137, "timed_out": False},
    )


@mock_error_router.get("/patch-error")
async def trigger_patch_error():
    raise PatchApplicationError(
        message="Failed to match search block in auth.py",
        target_file="auth.py",
        reason="Hunk mismatch at line 42",
    )


@mock_error_router.get("/llm-rate-limit")
async def trigger_llm_rate_limit():
    raise LLMAPIError(
        message="Upstream provider rate limit reached (HTTP 429)",
        model="gpt-4o",
        status_code=429,
    )


@mock_error_router.get("/llm-gateway-panic")
async def trigger_llm_gateway_panic():
    raise LLMAPIError(
        message="Upstream OpenAI gateway timeout (HTTP 502)",
        model="deepseek-r1",
        status_code=502,
    )


@mock_error_router.get("/forge-base-error")
async def trigger_forge_base_error():
    raise ForgeBaseException(
        message="Generic unhandled domain failure",
        error_code="FORGE_UNEXPECTED_STATE",
        details={"subsystem": "orchestrator"},
    )


# Include test route on app if not already included
if not any(getattr(r, "path", "") == "/test-errors/sandbox-timeout" for r in app.routes):
    app.include_router(mock_error_router)


# ==============================================================================
# PILLAR 1: CORRELATION ID & STRUCTURED LOGGING MIDDLEWARE
# ==============================================================================


@pytest.mark.asyncio
async def test_correlation_id_middleware_extracts_and_propagates_request_id():
    """Verify middleware accepts client X-Request-ID and returns it on response headers."""
    transport = ASGITransport(app=app)
    custom_req_id = f"req-{uuid.uuid4().hex[:12]}"
    custom_task_id = f"task-{uuid.uuid4().hex[:8]}"

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/health",
            headers={
                "x-request-id": custom_req_id,
                "x-task-id": custom_task_id,
            },
        )

    assert resp.status_code in (200, 503)
    assert resp.headers.get("x-request-id") == custom_req_id
    assert resp.headers.get("x-correlation-id") == custom_req_id
    assert resp.headers.get("x-trace-id") == custom_req_id
    assert resp.headers.get("x-task-id") == custom_task_id


@pytest.mark.asyncio
async def test_correlation_id_middleware_generates_id_when_missing():
    """Verify middleware generates a unique correlation ID when client does not supply one."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")

    assert resp.status_code in (200, 503)
    gen_req_id = resp.headers.get("x-request-id")
    assert gen_req_id is not None
    assert len(gen_req_id) >= 16
    assert resp.headers.get("x-correlation-id") == gen_req_id
    assert resp.headers.get("x-trace-id") == gen_req_id


# ==============================================================================
# PILLAR 2: GLOBAL EXCEPTION HANDLERS
# ==============================================================================


@pytest.mark.asyncio
async def test_sandbox_execution_error_timeout_returns_504_with_structured_json():
    """Verify SandboxExecutionError with timed_out=True returns HTTP 504 and structured JSON."""
    transport = ASGITransport(app=app)
    req_id = "test-corr-timeout-1"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test-errors/sandbox-timeout", headers={"x-request-id": req_id})

    assert resp.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    data = resp.json()
    assert "error" in data
    assert data["error"]["type"] == "SandboxExecutionError"
    assert data["error"]["code"] == "SANDBOX_EXECUTION_ERROR"
    assert data["error"]["details"].get("timed_out") is True
    assert data["error"]["request_id"] == req_id
    assert "timestamp" in data["error"]
    assert resp.headers.get("x-request-id") == req_id


@pytest.mark.asyncio
async def test_sandbox_execution_error_crash_returns_500():
    """Verify SandboxExecutionError without timeout returns HTTP 500."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test-errors/sandbox-crash")

    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    data = resp.json()
    assert data["error"]["type"] == "SandboxExecutionError"
    assert data["error"]["details"].get("exit_code") == 137


@pytest.mark.asyncio
async def test_patch_application_error_returns_422():
    """Verify PatchApplicationError returns HTTP 422 Unprocessable Entity."""
    transport = ASGITransport(app=app)
    req_id = "test-corr-patch-422"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test-errors/patch-error", headers={"x-request-id": req_id})

    assert resp.status_code in (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422),
    )
    data = resp.json()
    assert data["error"]["type"] == "PatchApplicationError"
    assert data["error"]["code"] == "PATCH_APPLICATION_ERROR"
    assert data["error"]["details"].get("target_file") == "auth.py"
    assert data["error"]["request_id"] == req_id


@pytest.mark.asyncio
async def test_llm_api_error_rate_limit_returns_429():
    """Verify LLMAPIError with HTTP 429 returns HTTP 429 Too Many Requests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test-errors/llm-rate-limit")

    assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    data = resp.json()
    assert data["error"]["type"] == "LLMAPIError"
    assert data["error"]["details"].get("model") == "gpt-4o"


@pytest.mark.asyncio
async def test_llm_api_error_panic_returns_502():
    """Verify LLMAPIError with upstream 502 returns HTTP 502 Bad Gateway."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test-errors/llm-gateway-panic")

    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    data = resp.json()
    assert data["error"]["type"] == "LLMAPIError"


@pytest.mark.asyncio
async def test_forge_base_exception_returns_500():
    """Verify generic ForgeBaseException returns HTTP 500 with custom error code."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test-errors/forge-base-error")

    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    data = resp.json()
    assert data["error"]["type"] == "ForgeBaseException"
    assert data["error"]["code"] == "FORGE_UNEXPECTED_STATE"


# ==============================================================================
# PILLAR 3: HEALTH & READINESS PROBES
# ==============================================================================


@pytest.mark.asyncio
async def test_readiness_probe_returns_200_when_dependencies_healthy():
    """Verify GET /api/ready returns HTTP 200 (ready) when postgres, redis, and docker are up."""
    mock_pg = ComponentHealth(status="up", latency_ms=1.1, details="Postgres OK")
    mock_redis = ComponentHealth(status="up", latency_ms=0.6, details="Redis OK")
    mock_docker = ComponentHealth(status="up", latency_ms=2.4, details="Docker socket OK")
    mock_llm = ComponentHealth(status="up", latency_ms=65.0, details="LLM Gateway OK")

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
        res: ReadinessResponse = await readiness_check_endpoint(response)

    assert res.status == "ready"
    assert response.status_code == status.HTTP_200_OK
    assert res.components["database"].status == "up"
    assert res.components["redis"].status == "up"
    assert res.components["docker"].status == "up"


@pytest.mark.asyncio
async def test_readiness_probe_returns_503_when_critical_dependency_down():
    """Verify GET /api/ready returns HTTP 503 (not_ready) when Docker daemon or PostgreSQL is down."""
    mock_pg = ComponentHealth(status="up", latency_ms=1.1, details="Postgres OK")
    mock_redis = ComponentHealth(status="up", latency_ms=0.6, details="Redis OK")
    mock_docker_down = ComponentHealth(
        status="down", latency_ms=15.0, error="Docker daemon socket unresponsive"
    )
    mock_llm = ComponentHealth(status="up", latency_ms=65.0, details="LLM Gateway OK")

    with (
        patch("services.api.forge.routers.health.probe_postgres", AsyncMock(return_value=mock_pg)),
        patch("services.api.forge.routers.health.probe_redis", AsyncMock(return_value=mock_redis)),
        patch(
            "services.api.forge.routers.health.probe_docker",
            AsyncMock(return_value=mock_docker_down),
        ),
        patch(
            "services.api.forge.routers.health.probe_llm_gateway", AsyncMock(return_value=mock_llm)
        ),
    ):
        response = Response()
        res: ReadinessResponse = await readiness_check_endpoint(response)

    assert res.status == "not_ready"
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert res.components["docker"].status == "down"


@pytest.mark.asyncio
async def test_get_api_ready_http_endpoint():
    """Verify HTTP GET /api/ready endpoint responds over ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/ready")

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_503_SERVICE_UNAVAILABLE)
    data = resp.json()
    assert "status" in data
    assert data["status"] in ("ready", "not_ready")
    assert "components" in data


# ==============================================================================
# PILLAR 4: TASK SUBMISSION API ROUTE & CELERY DISPATCH
# ==============================================================================


@pytest.mark.asyncio
async def test_post_task_dispatches_celery_worker_and_returns_trace_token():
    """Verify POST /api/tasks asynchronously dispatches Celery worker with trace_token."""
    transport = ASGITransport(app=app)
    req_trace = "0123456789abcdef0123456789abcdef"
    payload = {
        "prompt": "Fix concurrency race condition in cache.py",
        "mode": "deep",
    }

    mock_apply_async = MagicMock()

    with patch(
        "services.api.forge.worker.celery_app.execute_agent_task.apply_async",
        mock_apply_async,
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/tasks",
                json=payload,
                headers={"x-trace-id": req_trace},
            )

    assert resp.status_code == status.HTTP_202_ACCEPTED
    data = resp.json()
    assert "task_id" in data
    assert data.get("status") == "pending"
    assert data.get("mode") == "deep"
    assert data.get("trace_token") == req_trace

    # Verify Celery task was dispatched with exact arguments
    assert mock_apply_async.called
    call_kwargs = mock_apply_async.call_args[1]["kwargs"]
    assert call_kwargs["task_id"] == data["task_id"]
    assert call_kwargs["prompt"] == payload["prompt"]
    assert call_kwargs["mode"] == "deep"
    assert call_kwargs["correlation_id"] == req_trace


@pytest.mark.asyncio
async def test_get_task_status_returns_idle_for_uninitialized_task():
    """Verify GET /api/tasks/{task_id} strictly returns IDLE with null/false PR flags when uninitialized."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/tasks/idle")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["status"] == "IDLE"
        assert data["pr_created"] is False
        assert data["pr_url"] is None
        assert data["merged"] is False

        # Random non-existent UUID also resolves to IDLE
        non_existent_id = str(uuid.uuid4())
        resp_rand = await client.get(f"/api/tasks/{non_existent_id}")
        assert resp_rand.status_code == status.HTTP_200_OK
        data_rand = resp_rand.json()
        assert data_rand["status"] == "IDLE"
        assert data_rand["pr_created"] is False
        assert data_rand["pr_url"] is None
        assert data_rand["merged"] is False


@pytest.mark.asyncio
async def test_get_task_status_strict_enums_and_pr_fidelity():
    """Verify GET /api/tasks/{task_id} strictly enforces execution enums and suppresses fake PR flags."""
    transport = ASGITransport(app=app)
    mock_task_id = str(uuid.uuid4())

    # Case A: Task running -> PR flags MUST be null and false
    mock_task = MagicMock()
    mock_task.id = uuid.UUID(mock_task_id)
    mock_task.repo_id = uuid.uuid4()
    mock_task.prompt = "Fix bug"
    mock_task.status = "running"
    mock_task.branch_name = "main"
    mock_task.retry_count = 0
    mock_task.created_at = None

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_task
    mock_db.execute.return_value = mock_result

    from services.api.core.database import get_db

    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/tasks/{mock_task_id}")
            assert resp.status_code == status.HTTP_200_OK
            data = resp.json()
            assert data["status"] == "RUNNING"
            assert data["pr_created"] is False
            assert data["pr_url"] is None
            assert data["merged"] is False

            # Case B: Task failed -> Status strictly FAILED, no PR flags
            mock_task.status = "failed"
            resp_failed = await client.get(f"/api/tasks/{mock_task_id}")
            assert resp_failed.status_code == status.HTTP_200_OK
            data_failed = resp_failed.json()
            assert data_failed["status"] == "FAILED"
            assert data_failed["pr_created"] is False
            assert data_failed["pr_url"] is None
            assert data_failed["merged"] is False

            # Case C: Task completed with real verified PR
            mock_task.status = "completed"
            real_pr_event = MagicMock()
            real_pr_event.sequence = 5
            real_pr_event.payload = {
                "pr_url": "https://github.com/kunal1987-ctrl/forge-/pull/42",
                "merged": True,
            }
            mock_evt_result = MagicMock()
            mock_evt_result.scalars.return_value.all.return_value = [real_pr_event]

            async def mock_execute(stmt):
                if "task_events" in str(stmt):
                    return mock_evt_result
                return mock_result

            mock_db.execute = AsyncMock(side_effect=mock_execute)

            resp_completed = await client.get(f"/api/tasks/{mock_task_id}")
            assert resp_completed.status_code == status.HTTP_200_OK
            data_completed = resp_completed.json()
            assert data_completed["status"] == "COMPLETED"
            assert data_completed["pr_created"] is True
            assert data_completed["pr_url"] == "https://github.com/kunal1987-ctrl/forge-/pull/42"
            assert data_completed["merged"] is True
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_engine_status_operational_states():
    """Verify GET /api/engine/status reflects server, repo connection, and active task states."""
    transport = ASGITransport(app=app)

    mock_db = AsyncMock()
    mock_empty_res = MagicMock()
    mock_empty_res.scalar_one_or_none.return_value = None
    mock_empty_res.scalar.return_value = 0
    mock_db.execute.return_value = mock_empty_res

    from services.api.core.database import get_db

    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Idle state: no repo, no active tasks
            resp = await client.get("/api/engine/status")
            assert resp.status_code == status.HTTP_200_OK
            data = resp.json()
            assert data["is_server_connected"] is True
            assert data["repo_connected"] is False
            assert data["task_active"] is False

            # 2. Repo connected state
            mock_repo = MagicMock()
            mock_repo.id = uuid.uuid4()
            mock_repo.name = "fastapi/fastapi"

            def mock_execute_repo(stmt):
                m = MagicMock()
                if "repositories" in str(stmt):
                    m.scalar_one_or_none.return_value = mock_repo
                    return m
                m.scalar_one_or_none.return_value = None
                m.scalar.return_value = 0
                return m

            mock_db.execute = AsyncMock(side_effect=mock_execute_repo)

            resp_repo = await client.get(f"/api/engine/status?repo_id={mock_repo.id}")
            assert resp_repo.status_code == status.HTTP_200_OK
            data_repo = resp_repo.json()
            assert data_repo["is_server_connected"] is True
            assert data_repo["repo_connected"] is True
            assert data_repo["task_active"] is False

            # 3. Active task executing state
            def mock_execute_active(stmt):
                m = MagicMock()
                if "repositories" in str(stmt):
                    m.scalar_one_or_none.return_value = mock_repo
                    return m
                if "count" in str(stmt) and "tasks" in str(stmt):
                    m.scalar.return_value = 2  # 2 active tasks
                    return m
                m.scalar_one_or_none.return_value = None
                m.scalar.return_value = 0
                return m

            mock_db.execute = AsyncMock(side_effect=mock_execute_active)

            resp_active = await client.get("/api/engine/status")
            assert resp_active.status_code == status.HTTP_200_OK
            data_active = resp_active.json()
            assert data_active["is_server_connected"] is True
            assert data_active["repo_connected"] is True
            assert data_active["task_active"] is True
            assert data_active["active_tasks_count"] == 2
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_rag_search_post_and_get():
    """Verify POST and GET /api/rag/search returns structured results with file_path, snippet, score, symbol_name, start_line."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Test 1: POST /api/rag/search
        resp_post = await client.post("/api/rag/search", json={"query": "flush_all", "limit": 3})
        assert resp_post.status_code == status.HTTP_200_OK
        data_post = resp_post.json()
        assert isinstance(data_post, list)
        if len(data_post) > 0:
            item = data_post[0]
            assert "file_path" in item
            assert "snippet" in item
            assert "score" in item
            assert "symbol_name" in item
            assert "start_line" in item
            assert isinstance(item["start_line"], int)
            assert isinstance(item["score"], float)

        # Test 2: GET /api/rag/search
        resp_get = await client.get("/api/rag/search?query=dispatcher&limit=2")
        assert resp_get.status_code == status.HTTP_200_OK
        data_get = resp_get.json()
        assert isinstance(data_get, list)

        # Test 3: Zero matching query returns clean empty list
        resp_empty = await client.post(
            "/api/rag/search",
            json={"query": "nonexistent_symbol_qwerty_9999", "limit": 5},
        )
        assert resp_empty.status_code == status.HTTP_200_OK
        assert resp_empty.json() == []
