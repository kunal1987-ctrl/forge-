"""Main FastAPI application entry point for FORGE.

Initializes FastAPI with an enterprise-grade secure middleware stack,
OpenTelemetry distributed tracing, correlation ID tracking, structured JSON
structlog logging, unified global exception handlers, and mounts domain sub-routers.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException as FastAPIHTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Explicitly load .env from project root
env_file = Path(__file__).resolve().parents[2] / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file)
else:
    load_dotenv()

from services.api.forge.exceptions import register_exception_handlers  # noqa: E402
from services.api.forge.logging.logger import (  # noqa: E402
    CorrelationIdMiddleware,
    configure_structlog,
    get_logger,
)

try:
    from services.api.core.telemetry import setup_telemetry
except ImportError:
    from core.telemetry import setup_telemetry  # type: ignore[no-redef]

from services.api.core.database import get_db  # noqa: E402
from services.api.routers.repositories import router as repositories_router  # noqa: E402
from services.api.routers.retrieval import router as retrieval_router  # noqa: E402
from services.api.routers.tasks import router as tasks_router  # noqa: E402
from services.api.forge.routers.health import router as health_router  # noqa: E402
from services.api.forge.routers.status import router as status_router  # noqa: E402
from services.api.forge.routers.rag import router as rag_router  # noqa: E402
from services.api.forge.routers.stream import router as stream_router  # noqa: E402

# ------------------------------------------------------------------------------
# 1. APPLICATION INITIALIZATION & TELEMETRY
# ------------------------------------------------------------------------------

app = FastAPI(
    title="FORGE Autonomous AI Platform API",
    version="0.1.0",
    description="Backend API for FORGE autonomous software engineering platform",
)

# Configure standardized JSON structlog logging pipeline
configure_structlog()
logger = get_logger("services.api.main")

# Initialize OpenTelemetry distributed tracing and background TelemetryDispatcher
setup_telemetry(app)
from services.api.forge.telemetry.dispatcher import setup_dispatcher_lifecycle  # noqa: E402

setup_dispatcher_lifecycle(app)


@app.on_event("startup")
async def on_startup() -> None:
    """Ensure pgvector extension and tables are auto-created when FastAPI app boots up."""
    try:
        from services.api.forge.db.session import init_db

        await init_db()
        logger.info("PostgreSQL pgvector extension and tables initialized on boot.")
    except Exception as exc:
        logger.warning("Startup database initialization note (continuing boot): %s", exc)



# ------------------------------------------------------------------------------
# 2. CUSTOM SECURITY RESPONSE HEADERS MIDDLEWARE (ASGI)
# ------------------------------------------------------------------------------


class SecurityHeadersMiddleware:
    """Enforces enterprise security headers on all HTTP responses via zero-overhead ASGI wrapper.

    Applies defenses against MIME-sniffing, clickjacking, and browser XSS attacks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                headers.setdefault("x-xss-protection", "1; mode=block")
                headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
                headers.setdefault(
                    "permissions-policy",
                    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
                    "microphone=(), payment=(), usb=()",
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ------------------------------------------------------------------------------
# 3. SECURE MIDDLEWARE STACK & ORDERING CONFIGURATION
# ------------------------------------------------------------------------------

# Origins configuration: explicit trusted origins with regex fallback for local Vite dev servers
raw_origins = os.getenv("CORS_ALLOWED_ORIGINS", os.getenv("ALLOWED_ORIGINS", ""))
if raw_origins.strip():
    allowed_origins = [orig.strip() for orig in raw_origins.split(",") if orig.strip()]
else:
    allowed_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost",
        "http://127.0.0.1",
    ]

# Regex pattern matching any port on localhost or 127.0.0.1 (safely handling Vite dynamic port assignment)
allow_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

# Strict HTTP methods permitted across the API
allowed_methods = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
]

# Explicit allowed request headers avoiding loose wildcards
allowed_headers = [
    "Content-Type",
    "Authorization",
    "Accept",
    "Origin",
    "User-Agent",
    "X-Requested-With",
    "X-Request-ID",
    "X-Correlation-ID",
    "X-Trace-ID",
    "X-Task-ID",
    "X-Repo-ID",
    "traceparent",
    "tracestate",
]

# Response headers exposed to browser clients
exposed_headers = [
    "X-Request-ID",
    "X-Correlation-ID",
    "X-Trace-ID",
    "X-Task-ID",
    "X-Repo-ID",
    "X-Content-Type-Options",
    "X-Frame-Options",
]

# Host header whitelist to prevent Host Header Poisoning & cache poisoning attacks
raw_hosts = os.getenv("ALLOWED_HOSTS", "")
if raw_hosts.strip():
    allowed_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
else:
    allowed_hosts = [
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "test",
        "testserver",
        "*.localhost",
        "forge_api",
        "api",
        "forge_frontend",
        "frontend",
        "host.docker.internal",
    ]

# MIDDLEWARE ORDERING (Starlette executes middlewares added via add_middleware in reverse order of addition):
# Request Flow:
#   1. TrustedHostMiddleware (Outermost: rejects spoofed Host headers immediately)
#   2. CORSMiddleware (Intercepts and resolves preflight OPTIONS immediately; injects CORS headers on responses)
#   3. SecurityHeadersMiddleware (Attaches nosniff, DENY, referrer-policy, permissions-policy headers)
#   4. CorrelationIdMiddleware (Innermost: assigns/extracts X-Request-ID and binds structlog contextvars)
#   5. FastAPI Routers & Exception Handlers

app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=allow_origin_regex,
    allow_credentials=True,
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    expose_headers=exposed_headers,
    max_age=600,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=allowed_hosts,
)


# ------------------------------------------------------------------------------
# 4. UNIFIED ERROR ENVELOPE BUILDER & HELPERS
# ------------------------------------------------------------------------------

HTTP_ERROR_CODES: Dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    408: "REQUEST_TIMEOUT",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_SERVER_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}


def create_error_envelope(
    status_code: int,
    error_code: str,
    message: str,
    details: Optional[Any] = None,
    correlation_id: Optional[str] = None,
    exc_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a unified, consistent enterprise JSON error envelope.

    Guarantees presence of status, error_code, message, timestamp, and diagnostic correlation keys.
    """
    cid = correlation_id or uuid.uuid4().hex
    ts = datetime.now(timezone.utc).isoformat()
    det = details if details is not None else {}

    return {
        "status": status_code,
        "error_code": error_code,
        "message": message,
        "timestamp": ts,
        "status_code": status_code,
        "details": det,
        "correlation_id": cid,
        "request_id": cid,
        "error": {
            "type": exc_type or error_code,
            "code": error_code,
            "message": message,
            "details": det,
            "correlation_id": cid,
            "request_id": cid,
            "status_code": status_code,
            "timestamp": ts,
        },
        "detail": message,
    }


def _extract_request_correlation_id(request: Request) -> str:
    """Extract correlation ID from request state, request headers, or generate a fresh UUID."""
    cid = getattr(request.state, "correlation_id", None) or getattr(
        request.state, "request_id", None
    )
    if cid:
        return str(cid)

    header_val = (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or request.headers.get("x-trace-id")
        or request.headers.get("traceparent")
    )
    if header_val:
        if header_val.startswith("00-") and len(header_val.split("-")) >= 3:
            return header_val.split("-")[1]
        return header_val.strip()

    return uuid.uuid4().hex


def _record_otel_span_exception(exc: BaseException, status_code: int = 500) -> None:
    """Record exception and error status on the active OpenTelemetry span."""
    try:
        span = trace.get_current_span()
        if span and span.is_recording():
            span.record_exception(exc)
            if status_code >= 500:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.set_attribute("http.status_code", status_code)
            span.set_attribute("error.type", exc.__class__.__name__)
    except Exception:
        pass


# ------------------------------------------------------------------------------
# 5. UNIFIED GLOBAL EXCEPTION HANDLERS
# ------------------------------------------------------------------------------


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Unified handler for Pydantic request validation failures (HTTP 422)."""
    cid = _extract_request_correlation_id(request)
    _record_otel_span_exception(exc, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    raw_errors = exc.errors()
    formatted_errors: List[Dict[str, Any]] = []
    for err in raw_errors:
        loc = " -> ".join(str(p) for p in err.get("loc", []))
        msg = err.get("msg", "Invalid value")
        err_type = err.get("type", "value_error")
        formatted_errors.append({"field": loc, "message": msg, "type": err_type})

    first_msg = formatted_errors[0]["message"] if formatted_errors else "Validation failed"
    first_field = formatted_errors[0]["field"] if formatted_errors else ""
    summary_msg = (
        f"Validation error: {first_msg} (field: {first_field})"
        if first_field
        else f"Validation error: {first_msg}"
    )

    logger.warning(
        "Request validation error intercepted",
        correlation_id=cid,
        path=request.url.path,
        method=request.method,
        errors=formatted_errors,
    )

    content = create_error_envelope(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error_code="VALIDATION_ERROR",
        message=summary_msg,
        details={"errors": formatted_errors, "body": raw_errors},
        correlation_id=cid,
        exc_type="RequestValidationError",
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=content,
        headers={
            "x-request-id": cid,
            "x-correlation-id": cid,
            "x-trace-id": cid,
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
        },
    )


@app.exception_handler(StarletteHTTPException)
@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException | FastAPIHTTPException
) -> JSONResponse:
    """Unified handler for explicit HTTP exceptions."""
    cid = _extract_request_correlation_id(request)
    _record_otel_span_exception(exc, status_code=exc.status_code)

    error_code = HTTP_ERROR_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")

    details: Dict[str, Any]
    if isinstance(exc.detail, dict):
        message = str(exc.detail.get("message") or exc.detail.get("detail") or "HTTP Error")
        details = exc.detail
    elif isinstance(exc.detail, list):
        message = "HTTP request failed with multiple errors"
        details = {"errors": exc.detail}
    else:
        message = str(exc.detail) if exc.detail else error_code.replace("_", " ").title()
        details = {}

    log_fn = logger.error if exc.status_code >= 500 else logger.warning
    log_fn(
        "HTTP exception intercepted",
        status_code=exc.status_code,
        error_code=error_code,
        message=message,
        correlation_id=cid,
        path=request.url.path,
        method=request.method,
    )

    content = create_error_envelope(
        status_code=exc.status_code,
        error_code=error_code,
        message=message,
        details=details,
        correlation_id=cid,
        exc_type=exc.__class__.__name__,
    )

    response_headers: Dict[str, str] = {
        "x-request-id": cid,
        "x-correlation-id": cid,
        "x-trace-id": cid,
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
    }
    if getattr(exc, "headers", None):
        response_headers.update(exc.headers or {})

    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=response_headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global catch-all exception handler preventing stack trace leaks while logging full tracebacks."""
    cid = _extract_request_correlation_id(request)
    _record_otel_span_exception(exc, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Log complete exception traceback with structured diagnostics
    logger.error(
        "Unhandled internal server exception intercepted",
        exc_info=exc,
        correlation_id=cid,
        path=request.url.path,
        method=request.method,
        exception_type=exc.__class__.__name__,
        exception_message=str(exc),
    )

    # Safe error envelope: does NOT leak database credentials or internal stack traces
    content = create_error_envelope(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code="INTERNAL_SERVER_ERROR",
        message="An unexpected internal server error occurred. Please contact support with the correlation ID.",
        details={"correlation_id": cid},
        correlation_id=cid,
        exc_type=exc.__class__.__name__,
    )

    response_headers: Dict[str, str] = {
        "x-request-id": cid,
        "x-correlation-id": cid,
        "x-trace-id": cid,
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
    }

    # Ensure CORS origin is preserved on unhandled 500 error responses if provided
    origin = request.headers.get("origin")
    if origin:
        response_headers["access-control-allow-origin"] = origin
        response_headers["access-control-allow-credentials"] = "true"

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=content,
        headers=response_headers,
    )


# Register domain-specific custom exception handlers (SandboxExecutionError, PatchApplicationError, etc.)
register_exception_handlers(app)


# ------------------------------------------------------------------------------
# 6. ROUTER MOUNTING & HEALTH PROBES
# ------------------------------------------------------------------------------

app.include_router(tasks_router)
app.include_router(repositories_router)
app.include_router(retrieval_router)
app.include_router(health_router)
app.include_router(status_router)
app.include_router(rag_router)
app.include_router(stream_router)


@app.get("/health/db", status_code=status.HTTP_200_OK, tags=["Health"])
async def db_health_check(db: AsyncSession = Depends(get_db)) -> Any:
    """Database connectivity health check."""
    try:
        result = await db.execute(text("SELECT 1"))
        scalar = result.scalar()
        return {
            "status": "healthy",
            "database": "connected",
            "result": scalar,
        }
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "database": "disconnected", "error": str(exc)},
        )
