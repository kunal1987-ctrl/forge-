"""Typed custom exception hierarchy and global FastAPI error handlers for FORGE platform.

Provides structured, explicit error classes that eradicate silent failure propagation
by enforcing error codes, structured details dictionaries, and cause/traceback preservation.
Also registers global FastAPI exception handlers returning clean JSON error responses
with accurate HTTP status codes and correlation IDs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import sys
import traceback
from typing import Any, Dict, Optional
import uuid

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger("forge.exceptions")


class ForgeBaseException(Exception):
    """Root base exception for all FORGE domain errors.

    Attributes:
        message: Human-readable failure explanation.
        error_code: Machine-readable error code string.
        details: Structured dictionary containing operational diagnostic metadata.
        __cause__: Original chained exception or cause (if any).
    """

    default_code: str = "FORGE_BASE_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.default_code
        self.details = dict(details) if details is not None else {}

        # Preserve root cause and traceback chain
        if cause is not None:
            self.__cause__ = cause
        elif self.__cause__ is None:
            # If an active exception is currently being handled, capture it as __cause__
            active_exc = sys.exc_info()[1]
            if active_exc is not None and active_exc is not self:
                self.__cause__ = active_exc

    def to_dict(self) -> Dict[str, Any]:
        """Convert exception metadata to structured dictionary."""
        cause_repr: Optional[str] = None
        cause_tb: Optional[str] = None
        if self.__cause__ is not None:
            cause_repr = f"{type(self.__cause__).__name__}: {str(self.__cause__)}"
            cause_tb = "".join(
                traceback.format_exception(
                    type(self.__cause__),
                    self.__cause__,
                    self.__cause__.__traceback__,
                )
            )

        return {
            "error_type": self.__class__.__name__,
            "error_code": self.error_code,
            "message": self.message,
            "details": dict(self.details),
            "cause": cause_repr,
            "cause_traceback": cause_tb,
        }

    def __str__(self) -> str:
        base = f"[{self.error_code}] {self.message}"
        if self.details:
            base += f" | details={self.details}"
        if self.__cause__:
            base += f" (caused by {type(self.__cause__).__name__}: {self.__cause__})"
        return base

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(message={self.message!r}, "
            f"error_code={self.error_code!r}, details={self.details!r})"
        )


class SandboxExecutionError(ForgeBaseException):
    """Raised when Docker sandbox commands timeout, crash, or fail unexpectedly."""

    default_code: str = "SANDBOX_EXECUTION_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        command: Optional[str] = None,
        exit_code: Optional[int] = None,
        timed_out: bool = False,
        container_id: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if command is not None:
            det["command"] = command
        if exit_code is not None:
            det["exit_code"] = exit_code
        if timed_out:
            det["timed_out"] = True
        if container_id is not None:
            det["container_id"] = container_id
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )


class PatchApplicationError(ForgeBaseException):
    """Raised when search/replace block parsing or diff matching fails."""

    default_code: str = "PATCH_APPLICATION_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        target_file: Optional[str] = None,
        diff_snippet: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if target_file is not None:
            det["target_file"] = target_file
        if diff_snippet is not None:
            det["diff_snippet"] = diff_snippet[:200]
        if reason is not None:
            det["reason"] = reason
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )


class GraphRAGSearchError(ForgeBaseException):
    """Raised during symbol extraction or vector retrieval failures."""

    default_code: str = "GRAPH_RAG_SEARCH_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        repo_id: Optional[str] = None,
        query: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if repo_id is not None:
            det["repo_id"] = repo_id
        if query is not None:
            det["query"] = query
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )


class LLMAPIError(ForgeBaseException):
    """Raised during rate limits, malformed structured outputs, or gateway panics."""

    default_code: str = "LLM_API_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        model: Optional[str] = None,
        status_code: Optional[int] = None,
        prompt_snippet: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if model is not None:
            det["model"] = model
        if status_code is not None:
            det["status_code"] = status_code
        if prompt_snippet is not None:
            det["prompt_snippet"] = prompt_snippet[:200]
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )


class StateRoutingError(ForgeBaseException):
    """Raised when graph state routing fails or reaches an illegal router branch."""

    default_code: str = "STATE_ROUTING_ERROR"


class WorktreeIsolationError(ForgeBaseException):
    """Raised when Git worktree creation, reset, or isolation fails."""

    default_code: str = "WORKTREE_ISOLATION_ERROR"


class PRCreationError(ForgeBaseException):
    """Raised when git operations, worktree commits, or GitHub API pull request creation fails."""

    default_code: str = "PR_CREATION_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        task_id: Optional[str] = None,
        branch_name: Optional[str] = None,
        repo_name: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if task_id is not None:
            det["task_id"] = task_id
        if branch_name is not None:
            det["branch_name"] = branch_name
        if repo_name is not None:
            det["repo_name"] = repo_name
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )


class GitHubAuthError(PRCreationError):
    """Raised when GitHub API authentication fails, token is missing, unauthorized, or lacks required scopes."""

    default_code: str = "GITHUB_AUTH_ERROR"

    def __init__(
        self,
        message: str = "GitHub API token (GITHUB_TOKEN) is missing or unauthorized.",
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        task_id: Optional[str] = None,
        branch_name: Optional[str] = None,
        repo_name: Optional[str] = None,
    ) -> None:
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=details,
            cause=cause,
            task_id=task_id,
            branch_name=branch_name,
            repo_name=repo_name,
        )


class GitIngestionError(ForgeBaseException):
    """Raised when repository cloning, branch probing, or checkout fails."""

    default_code: str = "GIT_INGESTION_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if repo_url is not None:
            det["repo_url"] = repo_url
        if branch is not None:
            det["branch"] = branch
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )


class EngineBusyError(ForgeBaseException):
    """Raised when engine is busy executing an active task and concurrency lock prevents overlapping execution."""

    default_code: str = "ENGINE_BUSY"

    def __init__(
        self,
        message: str = "Engine is currently busy executing an active task",
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        active_task_id: Optional[str] = None,
        repo_id: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if active_task_id is not None:
            det["active_task_id"] = str(active_task_id)
        if repo_id is not None:
            det["repo_id"] = str(repo_id)
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )
        self.active_task_id = active_task_id
        self.repo_id = repo_id


def _extract_correlation_id(request: Request) -> str:
    """Extract correlation ID from request state, headers, or generate a fresh UUID."""
    corr_id = getattr(request.state, "correlation_id", None) or getattr(
        request.state, "request_id", None
    )
    if corr_id:
        return str(corr_id)

    hdr = (
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or request.headers.get("x-trace-id")
        or request.headers.get("traceparent")
    )
    if hdr:
        if hdr.startswith("00-") and len(hdr.split("-")) >= 3:
            return hdr.split("-")[1]
        return hdr.strip()

    return uuid.uuid4().hex


def _build_error_payload(
    exc: Exception,
    correlation_id: str,
    status_code: int,
    error_code: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct a standardized JSON error response payload."""
    code = (
        error_code
        or getattr(exc, "error_code", None)
        or getattr(exc, "default_code", "INTERNAL_SERVER_ERROR")
    )
    det = details if details is not None else getattr(exc, "details", {})
    msg = getattr(exc, "message", str(exc))

    ts = datetime.now(timezone.utc).isoformat()
    return {
        "status": status_code,
        "error_code": code,
        "message": msg,
        "timestamp": ts,
        "status_code": status_code,
        "details": det,
        "correlation_id": correlation_id,
        "request_id": correlation_id,
        "error": {
            "type": exc.__class__.__name__,
            "code": code,
            "message": msg,
            "details": det,
            "correlation_id": correlation_id,
            "request_id": correlation_id,
            "status_code": status_code,
            "timestamp": ts,
        },
        "detail": msg,
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Register global FastAPI exception handlers for all custom FORGE exceptions.

    Handlers intercept domain exceptions, log structured diagnostic events, and return
    clean JSON error bodies mapped to proper HTTP status codes.
    """

    @app.exception_handler(SandboxExecutionError)
    async def sandbox_execution_error_handler(
        request: Request, exc: SandboxExecutionError
    ) -> JSONResponse:
        corr_id = _extract_correlation_id(request)
        is_timeout = bool(getattr(exc, "details", {}).get("timed_out"))
        status_code = (
            status.HTTP_504_GATEWAY_TIMEOUT if is_timeout else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        logger.error(
            "SandboxExecutionError handled in HTTP request: %s (corr_id=%s)",
            exc,
            corr_id,
        )
        payload = _build_error_payload(exc, corr_id, status_code)
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers={
                "x-request-id": corr_id,
                "x-correlation-id": corr_id,
                "x-trace-id": corr_id,
            },
        )

    @app.exception_handler(PatchApplicationError)
    async def patch_application_error_handler(
        request: Request, exc: PatchApplicationError
    ) -> JSONResponse:
        corr_id = _extract_correlation_id(request)
        status_code = getattr(
            status, "HTTP_422_UNPROCESSABLE_CONTENT", status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        logger.error(
            "PatchApplicationError handled in HTTP request: %s (corr_id=%s)",
            exc,
            corr_id,
        )
        payload = _build_error_payload(exc, corr_id, status_code)
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers={
                "x-request-id": corr_id,
                "x-correlation-id": corr_id,
                "x-trace-id": corr_id,
            },
        )

    @app.exception_handler(LLMAPIError)
    async def llm_api_error_handler(request: Request, exc: LLMAPIError) -> JSONResponse:
        corr_id = _extract_correlation_id(request)
        llm_status = getattr(exc, "details", {}).get("status_code")
        status_code = (
            status.HTTP_429_TOO_MANY_REQUESTS if llm_status == 429 else status.HTTP_502_BAD_GATEWAY
        )
        logger.error("LLMAPIError handled in HTTP request: %s (corr_id=%s)", exc, corr_id)
        payload = _build_error_payload(exc, corr_id, status_code)
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers={
                "x-request-id": corr_id,
                "x-correlation-id": corr_id,
                "x-trace-id": corr_id,
            },
        )

    @app.exception_handler(GraphRAGSearchError)
    async def graph_rag_search_error_handler(
        request: Request, exc: GraphRAGSearchError
    ) -> JSONResponse:
        corr_id = _extract_correlation_id(request)
        status_code = status.HTTP_502_BAD_GATEWAY
        logger.error(
            "GraphRAGSearchError handled in HTTP request: %s (corr_id=%s)",
            exc,
            corr_id,
        )
        payload = _build_error_payload(exc, corr_id, status_code)
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers={
                "x-request-id": corr_id,
                "x-correlation-id": corr_id,
                "x-trace-id": corr_id,
            },
        )

    try:
        from services.api.forge.concurrency.locks import ConcurrencyLockError

        @app.exception_handler(ConcurrencyLockError)
        async def concurrency_lock_error_handler(
            request: Request, exc: ConcurrencyLockError
        ) -> JSONResponse:
            corr_id = _extract_correlation_id(request)
            status_code = status.HTTP_409_CONFLICT
            logger.error(
                "ConcurrencyLockError handled in HTTP request: %s (corr_id=%s)",
                exc,
                corr_id,
            )
            payload = _build_error_payload(
                exc,
                corr_id,
                status_code,
                error_code="CONCURRENCY_LOCK_ERROR",
                details={
                    "repo_identifier": getattr(exc, "repo_identifier", ""),
                    "lock_key": getattr(exc, "lock_key", ""),
                    "timeout": getattr(exc, "timeout", None),
                },
            )
            return JSONResponse(
                status_code=status_code,
                content=payload,
                headers={
                    "x-request-id": corr_id,
                    "x-correlation-id": corr_id,
                    "x-trace-id": corr_id,
                },
            )
    except ImportError:
        pass

    @app.exception_handler(GitIngestionError)
    async def git_ingestion_error_handler(request: Request, exc: GitIngestionError) -> JSONResponse:
        corr_id = _extract_correlation_id(request)
        status_code = status.HTTP_400_BAD_REQUEST
        logger.error(
            "GitIngestionError handled in HTTP request: %s (corr_id=%s)",
            exc,
            corr_id,
        )
        payload = _build_error_payload(exc, corr_id, status_code)
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers={
                "x-request-id": corr_id,
                "x-correlation-id": corr_id,
                "x-trace-id": corr_id,
                "x-content-type-options": "nosniff",
                "x-frame-options": "DENY",
            },
        )

    @app.exception_handler(EngineBusyError)
    async def engine_busy_error_handler(request: Request, exc: EngineBusyError) -> JSONResponse:
        corr_id = _extract_correlation_id(request)
        status_code = status.HTTP_409_CONFLICT
        logger.warning(
            "EngineBusyError handled in HTTP request: %s (corr_id=%s)",
            exc,
            corr_id,
        )
        payload = _build_error_payload(
            exc,
            corr_id,
            status_code,
            error_code="ENGINE_BUSY",
            details=exc.details,
        )
        payload["detail"] = exc.message
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers={
                "x-request-id": corr_id,
                "x-correlation-id": corr_id,
                "x-trace-id": corr_id,
                "x-content-type-options": "nosniff",
                "x-frame-options": "DENY",
            },
        )

    @app.exception_handler(ForgeBaseException)
    async def forge_base_error_handler(request: Request, exc: ForgeBaseException) -> JSONResponse:
        corr_id = _extract_correlation_id(request)
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        logger.error(
            "ForgeBaseException handled in HTTP request: %s (corr_id=%s)",
            exc,
            corr_id,
        )
        payload = _build_error_payload(exc, corr_id, status_code)
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers={
                "x-request-id": corr_id,
                "x-correlation-id": corr_id,
                "x-trace-id": corr_id,
            },
        )


__all__ = [
    "ForgeBaseException",
    "SandboxExecutionError",
    "PatchApplicationError",
    "GraphRAGSearchError",
    "LLMAPIError",
    "StateRoutingError",
    "WorktreeIsolationError",
    "PRCreationError",
    "GitHubAuthError",
    "GitIngestionError",
    "EngineBusyError",
    "register_exception_handlers",
]
