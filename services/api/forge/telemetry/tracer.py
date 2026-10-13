"""OpenTelemetry distributed tracing, context propagation, and agent telemetry for FORGE."""

from __future__ import annotations

import functools
import inspect
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from opentelemetry import context, propagate, trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)

# Tracer identifier
TRACER_NAME = "forge.agent"


def get_tracer(name: str = TRACER_NAME) -> Tracer:
    """Retrieve an OpenTelemetry tracer instance."""
    return trace.get_tracer(name)


tracer: Tracer = get_tracer()


def init_trace_context(trace_id: Optional[str] = None) -> str:
    """Initialize or extract a unique root trace_id and return a standardized 32-character hex string.

    Args:
        trace_id: Optional preexisting trace ID (e.g. from incoming HTTP header).

    Returns:
        32-character hex trace ID string.
    """
    if trace_id:
        clean_id = trace_id.replace("-", "").strip().lower()
        if len(clean_id) == 32:
            return clean_id
    # Generate new random 32-character hex trace ID
    return uuid.uuid4().hex


def get_current_trace_id() -> str:
    """Return the active OpenTelemetry trace ID as a 32-character hex string, or a generated UUID."""
    span = trace.get_current_span()
    if span and span.get_span_context().is_valid:
        return format(span.get_span_context().trace_id, "032x")
    return uuid.uuid4().hex


def get_current_span_id() -> str:
    """Return the active OpenTelemetry span ID as a 16-character hex string, or an empty string."""
    span = trace.get_current_span()
    if span and span.get_span_context().is_valid:
        return format(span.get_span_context().span_id, "016x")
    return ""


def inject_trace_headers(carrier: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Inject W3C trace context into carrier dictionary for propagation across Redis / Celery."""
    headers = dict(carrier or {})
    propagate.inject(headers)
    trace_id = get_current_trace_id()
    headers["x-trace-id"] = trace_id
    headers["x-correlation-id"] = headers.get("x-correlation-id", trace_id)
    return headers


def extract_trace_headers(carrier: Dict[str, Any]) -> context.Context:
    """Extract W3C trace context from carrier dictionary to restore distributed trace context."""
    str_carrier = {str(k).lower(): str(v) for k, v in carrier.items()}
    return propagate.extract(str_carrier)


class NodeExecutionSpan:
    """Context manager for instrumenting LangGraph nodes with OpenTelemetry spans.

    Captures inputs, outputs, execution latency, status, and token usage.
    """

    def __init__(
        self,
        node_name: str,
        state: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        repo_id: Optional[str] = None,
    ) -> None:
        self.node_name = node_name
        self.state = state or {}
        self.task_id = str(task_id or self.state.get("task_id", "unknown"))
        self.repo_id = str(repo_id or self.state.get("repo_id", "unknown"))
        self.start_time: float = 0.0
        self.span: Optional[Span] = None
        self._token: Optional[Any] = None

    def __enter__(self) -> NodeExecutionSpan:
        self.start_time = time.perf_counter()
        span_ctx = tracer.start_span(f"langgraph.node.{self.node_name}")
        self.span = span_ctx
        self._token = context.attach(trace.set_span_in_context(span_ctx))

        # Mandatory span attributes
        self.span.set_attribute("forge.node_name", self.node_name)
        self.span.set_attribute("forge.task_id", self.task_id)
        self.span.set_attribute("forge.repo_id", self.repo_id)
        self.span.set_attribute("forge.trace_id", get_current_trace_id())

        prompt = str(self.state.get("prompt", ""))
        if prompt:
            self.span.set_attribute("forge.input_prompt_len", len(prompt))

        retry_count = self.state.get("retry_count", 0)
        self.span.set_attribute("forge.retry_count", int(retry_count))
        return self

    def record_output(self, output: Dict[str, Any]) -> None:
        """Record node outputs and token/status metrics onto the active span."""
        if not self.span or not self.span.is_recording():
            return

        status_val = str(output.get("status", "completed"))
        self.span.set_attribute("forge.output_status", status_val)

        if "confidence_score" in output:
            try:
                self.span.set_attribute("forge.confidence_score", float(output["confidence_score"]))
            except (ValueError, TypeError):
                pass

        if "generated_diff" in output:
            diff_len = len(str(output["generated_diff"]))
            self.span.set_attribute("forge.diff_length", diff_len)

        if "tokens_used" in output:
            try:
                self.span.set_attribute("forge.tokens_used", int(output["tokens_used"]))
            except (ValueError, TypeError):
                pass

    def record_exception(self, exc: BaseException) -> None:
        """Record runtime exception and set span error status."""
        if self.span and self.span.is_recording():
            self.span.record_exception(exc)
            self.span.set_status(Status(StatusCode.ERROR, str(exc)))

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        latency_ms = (time.perf_counter() - self.start_time) * 1000.0
        if self.span:
            self.span.set_attribute("forge.latency_ms", latency_ms)
            if exc_val is not None:
                self.record_exception(exc_val)
            else:
                self.span.set_status(Status(StatusCode.OK))
            self.span.end()

        if self._token is not None:
            context.detach(self._token)


def trace_node(node_name: str) -> Callable[..., Any]:
    """Decorator for LangGraph node functions to automatically emit telemetry spans."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(
                state: Dict[str, Any], *args: Any, **kwargs: Any
            ) -> Dict[str, Any]:
                with NodeExecutionSpan(node_name=node_name, state=state) as node_span:
                    res = await fn(state, *args, **kwargs)
                    if isinstance(res, dict):
                        node_span.record_output(res)
                    return res

            return async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(state: Dict[str, Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
                with NodeExecutionSpan(node_name=node_name, state=state) as node_span:
                    res = fn(state, *args, **kwargs)
                    if isinstance(res, dict):
                        node_span.record_output(res)
                    return res

            return sync_wrapper

    return decorator


class ForgeTelemetryCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler recording token usage, latency, and tool invocations to OpenTelemetry."""

    def __init__(self, task_id: str = "unknown", correlation_id: str = "") -> None:
        super().__init__()
        self.task_id = task_id
        self.correlation_id = correlation_id or get_current_trace_id()
        self._llm_start_times: Dict[str, float] = {}
        self._tool_start_times: Dict[str, float] = {}

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        self._llm_start_times[str(run_id)] = time.perf_counter()
        span = trace.get_current_span()
        if span and span.is_recording():
            model_name = serialized.get("name") or serialized.get("id", ["unknown"])[-1]
            span.set_attribute("llm.model", str(model_name))
            span.set_attribute("llm.prompt_count", len(prompts))

    def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        start_time = self._llm_start_times.pop(str(run_id), None)
        latency_ms = (time.perf_counter() - start_time) * 1000.0 if start_time else 0.0

        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute("llm.latency_ms", latency_ms)
            token_usage = response.llm_output.get("token_usage", {}) if response.llm_output else {}
            if token_usage:
                span.set_attribute("llm.prompt_tokens", token_usage.get("prompt_tokens", 0))
                span.set_attribute("llm.completion_tokens", token_usage.get("completion_tokens", 0))
                span.set_attribute("llm.total_tokens", token_usage.get("total_tokens", 0))

    def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        span = trace.get_current_span()
        if span and span.is_recording():
            span.record_exception(error)
            span.set_attribute("llm.error", str(error))

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        self._tool_start_times[str(run_id)] = time.perf_counter()
        span = trace.get_current_span()
        if span and span.is_recording():
            tool_name = serialized.get("name", "unknown_tool")
            span.set_attribute("tool.name", tool_name)
            span.set_attribute("tool.input_snippet", input_str[:200])

    def on_tool_end(self, output: str, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        start = self._tool_start_times.pop(str(run_id), None)
        dur = (time.perf_counter() - start) * 1000.0 if start else 0.0
        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute("tool.duration_ms", dur)
            span.set_attribute("tool.output_snippet", str(output)[:200])

    def on_tool_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        span = trace.get_current_span()
        if span and span.is_recording():
            span.record_exception(error)
            span.set_attribute("tool.error", str(error))


def get_telemetry_callbacks(task_id: str, correlation_id: str = "") -> List[BaseCallbackHandler]:
    """Retrieve LangChain callbacks configured for telemetry and observability."""
    callbacks: List[BaseCallbackHandler] = [
        ForgeTelemetryCallbackHandler(task_id=task_id, correlation_id=correlation_id)
    ]

    # Optional Langfuse callback if configured
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        try:
            from langfuse.callback import CallbackHandler as LangfuseCallbackHandler  # type: ignore[import-untyped]

            callbacks.append(
                LangfuseCallbackHandler(
                    trace_name=f"forge-task-{task_id}",
                    metadata={"task_id": task_id, "correlation_id": correlation_id},
                )
            )
        except ImportError:
            pass

    return callbacks
