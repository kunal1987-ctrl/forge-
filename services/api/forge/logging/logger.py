"""Structured JSON logging with mandatory correlation IDs and task context for FORGE."""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from typing import Any, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
import structlog
from structlog.types import EventDict, WrappedLogger

# Thread-safe and async-safe ContextVars for mandatory context keys
_TASK_ID: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="")
_REPO_ID: contextvars.ContextVar[str] = contextvars.ContextVar("repo_id", default="")
_NODE_NAME: contextvars.ContextVar[str] = contextvars.ContextVar("node_name", default="")
_CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")


def bind_context(
    task_id: Optional[str] = None,
    repo_id: Optional[str] = None,
    node_name: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> None:
    """Bind contextual attributes into contextvars for automatic injection into all log events."""
    if task_id is not None:
        _TASK_ID.set(str(task_id))
    if repo_id is not None:
        _REPO_ID.set(str(repo_id))
    if node_name is not None:
        _NODE_NAME.set(str(node_name))
    if correlation_id is not None:
        _CORRELATION_ID.set(str(correlation_id))
    elif not _CORRELATION_ID.get():
        _CORRELATION_ID.set(uuid.uuid4().hex)

    structlog.contextvars.bind_contextvars(
        task_id=_TASK_ID.get(),
        repo_id=_REPO_ID.get(),
        node_name=_NODE_NAME.get(),
        correlation_id=_CORRELATION_ID.get(),
    )


def clear_context() -> None:
    """Clear context variables across turn or worker transitions."""
    _TASK_ID.set("")
    _REPO_ID.set("")
    _NODE_NAME.set("")
    _CORRELATION_ID.set("")
    structlog.contextvars.clear_contextvars()


def get_current_context() -> Dict[str, str]:
    """Retrieve currently bound context keys."""
    return {
        "task_id": _TASK_ID.get() or "unknown",
        "repo_id": _REPO_ID.get() or "unknown",
        "node_name": _NODE_NAME.get() or "unknown",
        "correlation_id": _CORRELATION_ID.get() or "unknown",
    }


def add_mandatory_forge_context(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Processor ensuring mandatory context keys exist in every log event.

    Mandatory keys: timestamp, log_level, task_id, repo_id, node_name, correlation_id.
    """
    event_dict.setdefault("task_id", _TASK_ID.get() or "unknown")
    event_dict.setdefault("repo_id", _REPO_ID.get() or "unknown")
    event_dict.setdefault("node_name", _NODE_NAME.get() or "unknown")
    event_dict.setdefault("correlation_id", _CORRELATION_ID.get() or uuid.uuid4().hex)

    if "level" in event_dict and "log_level" not in event_dict:
        event_dict["log_level"] = event_dict.pop("level")
    elif "log_level" not in event_dict:
        event_dict["log_level"] = method_name.upper()

    return event_dict


_CONFIGURED = False


def configure_structlog() -> None:
    """Configure structlog processor pipeline for standardized JSON outputs."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        add_mandatory_forge_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
    else:
        for h in root_logger.handlers:
            h.setFormatter(formatter)

    _CONFIGURED = True


configure_structlog()


def get_logger(name: Optional[str] = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger instance equipped with mandatory context keys."""
    configure_structlog()
    return structlog.get_logger(name or "forge")


def log_exception(
    logger_instance: Any,
    event: str,
    exc: BaseException,
    task_id: Optional[str] = None,
    repo_id: Optional[str] = None,
    node_name: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Log an exception with full traceback stack and correlated task ID in structured JSON."""
    if task_id or repo_id or node_name:
        bind_context(task_id=task_id, repo_id=repo_id, node_name=node_name)

    log = logger_instance if hasattr(logger_instance, "error") else get_logger("forge")
    log.error(
        event,
        exc_info=exc,
        exception_type=type(exc).__name__,
        exception_message=str(exc),
        **kwargs,
    )


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware that intercepts every incoming request,
    generates or extracts an X-Request-ID (correlation ID), and binds it to structlog
    context for the duration of the request lifecycle.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw_corr_id = (
            request.headers.get("x-request-id")
            or request.headers.get("x-correlation-id")
            or request.headers.get("x-trace-id")
            or request.headers.get("traceparent")
        )
        if raw_corr_id:
            if raw_corr_id.startswith("00-") and len(raw_corr_id.split("-")) >= 3:
                correlation_id = raw_corr_id.split("-")[1]
            else:
                correlation_id = raw_corr_id.strip()
        else:
            correlation_id = uuid.uuid4().hex

        # Extract task_id and repo_id from headers or URL path
        task_id = request.headers.get("x-task-id") or ""
        if not task_id and "/tasks" in request.url.path:
            parts = request.url.path.strip("/").split("/")
            for part in parts:
                if len(part) in (32, 36) and ("-" in part or part.isalnum()):
                    task_id = part
                    break

        repo_id = request.headers.get("x-repo-id") or ""

        # Bind to structlog contextvars for the duration of this request
        bind_context(
            task_id=task_id,
            repo_id=repo_id,
            node_name="fastapi_http",
            correlation_id=correlation_id,
        )

        # Store on request state for endpoint and exception handlers
        request.state.correlation_id = correlation_id
        request.state.request_id = correlation_id
        if task_id:
            request.state.task_id = task_id

        response: Response = await call_next(request)

        # Inject correlation headers on outbound response
        response.headers["x-request-id"] = correlation_id
        response.headers["x-correlation-id"] = correlation_id
        response.headers["x-trace-id"] = correlation_id
        if task_id:
            response.headers["x-task-id"] = task_id

        return response


# Re-export alias for backward compatibility
structlog_correlation_middleware = CorrelationIdMiddleware

__all__ = [
    "bind_context",
    "clear_context",
    "get_current_context",
    "configure_structlog",
    "get_logger",
    "log_exception",
    "CorrelationIdMiddleware",
    "structlog_correlation_middleware",
]
