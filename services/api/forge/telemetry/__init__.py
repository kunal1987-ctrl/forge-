"""FORGE Telemetry and Distributed Tracing Module."""

from __future__ import annotations

from services.api.forge.telemetry.dispatcher import (
    TelemetryDispatcher,
    get_telemetry_dispatcher,
    set_telemetry_dispatcher,
    setup_dispatcher_lifecycle,
)
from services.api.forge.telemetry.tracer import (
    ForgeTelemetryCallbackHandler,
    NodeExecutionSpan,
    extract_trace_headers,
    get_current_span_id,
    get_current_trace_id,
    get_telemetry_callbacks,
    get_tracer,
    init_trace_context,
    inject_trace_headers,
    trace_node,
    tracer,
)

__all__ = [
    "tracer",
    "get_tracer",
    "init_trace_context",
    "get_current_trace_id",
    "get_current_span_id",
    "inject_trace_headers",
    "extract_trace_headers",
    "NodeExecutionSpan",
    "trace_node",
    "ForgeTelemetryCallbackHandler",
    "get_telemetry_callbacks",
    "TelemetryDispatcher",
    "get_telemetry_dispatcher",
    "set_telemetry_dispatcher",
    "setup_dispatcher_lifecycle",
]
