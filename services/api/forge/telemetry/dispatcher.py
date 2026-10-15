"""Production-grade asynchronous TelemetryDispatcher for FORGE.

Re-exports TelemetryDispatcher, operational constants, and lifecycle hooks
from services.api.forge.engine.dispatcher for backward compatibility and domain alignment.
"""

from __future__ import annotations

from services.api.forge.engine.dispatcher import (
    DEFAULT_BUFFER_CAPACITY,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TRACKED_TASKS,
    DEFAULT_PUBLISH_BATCH,
    DEFAULT_PUBLISH_QUEUE_CAPACITY,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_SUBSCRIBER_QUEUE_CAPACITY,
    TelemetryDispatcher,
    TelemetrySink,
    get_telemetry_dispatcher,
    set_telemetry_dispatcher,
    setup_dispatcher_lifecycle,
)

__all__ = [
    "DEFAULT_BUFFER_CAPACITY",
    "DEFAULT_FLUSH_INTERVAL",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_MAX_BATCH",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_TRACKED_TASKS",
    "DEFAULT_PUBLISH_BATCH",
    "DEFAULT_PUBLISH_QUEUE_CAPACITY",
    "DEFAULT_RETRY_BACKOFF",
    "DEFAULT_SUBSCRIBER_QUEUE_CAPACITY",
    "TelemetryDispatcher",
    "TelemetrySink",
    "get_telemetry_dispatcher",
    "set_telemetry_dispatcher",
    "setup_dispatcher_lifecycle",
]
