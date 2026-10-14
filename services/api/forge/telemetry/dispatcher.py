"""Production-grade asynchronous TelemetryDispatcher for FORGE.

Re-exports TelemetryDispatcher, operational constants, and lifecycle hooks
from services.api.forge.engine.dispatcher for backward compatibility and domain alignment.
"""

from __future__ import annotations

from services.api.forge.engine.dispatcher import (
    DEFAULT_BUFFER_CAPACITY,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    TelemetryDispatcher,
    TelemetrySink,
    get_telemetry_dispatcher,
    set_telemetry_dispatcher,
    setup_dispatcher_lifecycle,
)

__all__ = [
    "DEFAULT_BUFFER_CAPACITY",
    "DEFAULT_FLUSH_INTERVAL",
    "DEFAULT_MAX_BATCH",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BACKOFF",
    "TelemetryDispatcher",
    "TelemetrySink",
    "get_telemetry_dispatcher",
    "set_telemetry_dispatcher",
    "setup_dispatcher_lifecycle",
]
