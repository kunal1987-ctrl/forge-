"""Comprehensive Health Check and Circuit Breaker Router for FORGE backend services.

Proactively probes PostgreSQL checkpointer database, Redis broker, Docker daemon socket,
and LLM gateway connectivity, returning structured JSON status payloads and HTTP 200/503.
Supports both /api/health (liveness / component check) and /api/ready (readiness probe).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import time
from typing import Dict, Optional

import docker
from fastapi import APIRouter, Response, status
import httpx
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from sqlalchemy import text

try:
    from services.api.core.config import settings

    DEFAULT_REDIS_URL = settings.REDIS_URL
    OPENAI_API_KEY = settings.OPENAI_API_KEY
    OPENROUTER_API_KEY = settings.OPENROUTER_API_KEY
except ImportError:
    DEFAULT_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

from services.api.core.database import async_session_factory

logger = logging.getLogger("forge.health")

router = APIRouter(tags=["Health & Observability"])


class ComponentHealth(BaseModel):
    """Health status and telemetry for an individual backend component."""

    status: str = Field(description="'up', 'down', or 'degraded'")
    latency_ms: Optional[float] = Field(
        default=None, description="Round-trip probe latency in milliseconds"
    )
    details: Optional[str] = Field(
        default=None, description="Diagnostic human-readable detail message"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if component probe failed"
    )


class HealthCheckResponse(BaseModel):
    """Structured production health check payload for load balancers and observability scrapers."""

    status: str = Field(description="'healthy', 'degraded', or 'unhealthy'")
    timestamp: str = Field(description="ISO-8601 UTC timestamp of the health probe")
    version: str = Field(default="0.1.0", description="Backend service version")
    components: Dict[str, ComponentHealth] = Field(
        description="Detailed health metrics per component"
    )


class ReadinessResponse(BaseModel):
    """Structured production readiness probe response for traffic orchestration."""

    status: str = Field(description="'ready' or 'not_ready'")
    timestamp: str = Field(description="ISO-8601 UTC timestamp of the readiness probe")
    version: str = Field(default="0.1.0", description="Backend service version")
    components: Dict[str, ComponentHealth] = Field(
        description="Detailed health metrics per component"
    )


async def probe_postgres(timeout_seconds: float = 2.0) -> ComponentHealth:
    """Proactively probe PostgreSQL database and calculate query round-trip latency."""
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            async with async_session_factory() as session:
                res = await session.execute(text("SELECT 1;"))
                _ = res.scalar()
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        return ComponentHealth(
            status="up",
            latency_ms=latency,
            details="PostgreSQL checkpointer database is connected and responsive.",
        )
    except Exception as exc:
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        logger.warning("Health check failed for PostgreSQL: %s", exc)
        return ComponentHealth(
            status="down",
            latency_ms=latency,
            error=str(exc),
            details="PostgreSQL connection failed or timed out.",
        )


async def probe_redis(timeout_seconds: float = 2.0) -> ComponentHealth:
    """Proactively probe Redis message broker and calculate ping latency."""
    start = time.perf_counter()
    client: Optional[aioredis.Redis] = None
    try:
        async with asyncio.timeout(timeout_seconds):
            client = aioredis.from_url(DEFAULT_REDIS_URL, decode_responses=True)
            pong = await client.ping()
            if not pong:
                raise RuntimeError("Redis ping returned False")
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        return ComponentHealth(
            status="up",
            latency_ms=latency,
            details="Redis broker is connected and responsive.",
        )
    except Exception as exc:
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        logger.warning("Health check failed for Redis: %s", exc)
        return ComponentHealth(
            status="down",
            latency_ms=latency,
            error=str(exc),
            details="Redis connection failed or timed out.",
        )
    finally:
        if client:
            try:
                await client.aclose()
            except Exception as close_err:
                logger.debug("health: redis client.aclose failed: %s", close_err)


async def probe_docker(timeout_seconds: float = 2.0) -> ComponentHealth:
    """Proactively verify the configured sandbox worker or local Docker socket."""
    start = time.perf_counter()

    def _sync_ping() -> bool:
        client = docker.from_env()
        return bool(client.ping())

    try:
        async with asyncio.timeout(timeout_seconds):
            sandbox_url = os.getenv("FORGE_SANDBOX_URL", "").rstrip("/")
            if sandbox_url:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.get(f"{sandbox_url}/health")
                    response.raise_for_status()
                    is_up = response.json().get("status") == "ok"
            else:
                is_up = await asyncio.to_thread(_sync_ping)
            if not is_up:
                raise RuntimeError("Sandbox worker health check returned unavailable")
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        return ComponentHealth(
            status="up",
            latency_ms=latency,
            details="Sandbox execution service is responsive.",
        )
    except Exception as exc:
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        logger.warning("Health check failed for Docker socket: %s", exc)
        return ComponentHealth(
            status="down",
            latency_ms=latency,
            error=str(exc),
            details="Configured sandbox execution service is unresponsive.",
        )


async def probe_llm_gateway(timeout_seconds: float = 2.5) -> ComponentHealth:
    """Proactively verify OpenAI or OpenRouter API gateway reachability."""
    start = time.perf_counter()
    api_url = (
        "https://openrouter.ai/api/v1/models"
        if OPENROUTER_API_KEY
        else "https://api.openai.com/v1/models"
    )
    api_key = OPENROUTER_API_KEY or OPENAI_API_KEY

    if not api_key:
        return ComponentHealth(
            status="degraded",
            details="No live LLM API keys configured in environment; running in offline simulation mode.",
        )

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(api_url, headers=headers)
            latency = round((time.perf_counter() - start) * 1000.0, 2)

            if resp.status_code in (200, 429):
                # 429 quota exhaustion still proves gateway connectivity and network reachability
                status_val = "up" if resp.status_code == 200 else "degraded"
                detail_val = (
                    "LLM API gateway connected and models reachable."
                    if resp.status_code == 200
                    else "LLM API gateway connected (rate limit / quota notice received)."
                )
                return ComponentHealth(
                    status=status_val,
                    latency_ms=latency,
                    details=detail_val,
                )
            else:
                return ComponentHealth(
                    status="degraded",
                    latency_ms=latency,
                    details=f"LLM API returned status {resp.status_code}.",
                )
    except Exception as exc:
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        logger.warning("Health check warning for LLM gateway: %s", exc)
        return ComponentHealth(
            status="down",
            latency_ms=latency,
            error=str(exc),
            details="LLM API gateway connection timed out or unreachable.",
        )


@router.get(
    "/api/health",
    response_model=HealthCheckResponse,
    summary="Comprehensive infrastructure health check and circuit breaker",
)
@router.get(
    "/health",
    response_model=HealthCheckResponse,
    summary="Root alias for health check",
)
async def health_check_endpoint(response: Response) -> HealthCheckResponse:
    """Proactively probe PostgreSQL, Redis, Docker, and LLM gateway.

    Returns HTTP 200 if all critical components are healthy, or HTTP 503 if any
    critical dependency (database, redis, docker) is down to trigger load balancer failover.
    """
    pg_task = probe_postgres()
    redis_task = probe_redis()
    docker_task = probe_docker()
    llm_task = probe_llm_gateway()

    pg_res, redis_res, docker_res, llm_res = await asyncio.gather(
        pg_task, redis_task, docker_task, llm_task
    )

    components: Dict[str, ComponentHealth] = {
        "database": pg_res,
        "redis": redis_res,
        "docker": docker_res,
        "llm_gateway": llm_res,
    }

    critical_statuses = [pg_res.status, redis_res.status, docker_res.status]
    has_critical_down = any(s == "down" for s in critical_statuses)
    has_degraded = any(c.status in ("degraded", "down") for c in components.values())

    if has_critical_down:
        overall_status = "unhealthy"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif has_degraded:
        overall_status = "degraded"
        response.status_code = status.HTTP_200_OK
    else:
        overall_status = "healthy"
        response.status_code = status.HTTP_200_OK

    return HealthCheckResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        version="0.1.0",
        components=components,
    )


@router.get(
    "/api/ready",
    response_model=ReadinessResponse,
    summary="Comprehensive readiness probe verifying all critical backend dependencies",
)
@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Root alias for readiness probe",
)
async def readiness_check_endpoint(response: Response) -> ReadinessResponse:
    """Proactively query and verify connectivity to PostgreSQL checkpointer,
    Redis message broker, Docker daemon socket, and LLM gateway.

    Returns HTTP 200 (status='ready') if all critical dependencies are up,
    or HTTP 503 (status='not_ready') if any critical service is down.
    """
    pg_task = probe_postgres()
    redis_task = probe_redis()
    docker_task = probe_docker()
    llm_task = probe_llm_gateway()

    pg_res, redis_res, docker_res, llm_res = await asyncio.gather(
        pg_task, redis_task, docker_task, llm_task
    )

    components: Dict[str, ComponentHealth] = {
        "database": pg_res,
        "redis": redis_res,
        "docker": docker_res,
        "llm_gateway": llm_res,
    }

    critical_statuses = [pg_res.status, redis_res.status, docker_res.status]
    has_critical_down = any(s == "down" for s in critical_statuses)

    if has_critical_down:
        overall_status = "not_ready"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        overall_status = "ready"
        response.status_code = status.HTTP_200_OK

    return ReadinessResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        version="0.1.0",
        components=components,
    )


__all__ = [
    "ComponentHealth",
    "HealthCheckResponse",
    "ReadinessResponse",
    "health_check_endpoint",
    "readiness_check_endpoint",
    "probe_docker",
    "probe_llm_gateway",
    "probe_postgres",
    "probe_redis",
    "router",
]
