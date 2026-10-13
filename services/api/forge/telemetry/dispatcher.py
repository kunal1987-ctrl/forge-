"""Production-grade asynchronous TelemetryDispatcher for FORGE.

Features:
- Active background flushing loop running continuously via asyncio.create_task().
- Batch flushing triggered either when the buffer reaches MAX_BATCH or on FLUSH_INTERVAL.
- Thread-safe buffering using asyncio.Lock() to prevent race conditions during concurrent ingestion.
- Concrete emission sinks: structured logging, local JSONL file writing, and HTTP collector emission with exponential backoff retry.
- Lifecycle management (start/stop) with graceful buffer draining on FastAPI shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

import httpx

logger = logging.getLogger("forge.telemetry.dispatcher")

# Default Operational Constants
DEFAULT_MAX_BATCH: int = 50
DEFAULT_FLUSH_INTERVAL: float = 5.0
DEFAULT_BUFFER_CAPACITY: int = 1000
DEFAULT_RETRY_BACKOFF: float = 0.5
DEFAULT_MAX_RETRIES: int = 3

TelemetrySink = Callable[[List[Dict[str, Any]]], Coroutine[Any, Any, None]]


class TelemetryDispatcher:
    """Asynchronous, thread-safe background telemetry dispatcher with batching and retry logic."""

    def __init__(
        self,
        max_batch: int = DEFAULT_MAX_BATCH,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        buffer_capacity: int = DEFAULT_BUFFER_CAPACITY,
        collector_url: Optional[str] = None,
        log_file_path: Optional[Union[str, Path]] = None,
        sink: Optional[TelemetrySink] = None,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        """Initialize the TelemetryDispatcher.

        Args:
            max_batch: Threshold of items triggering an immediate batch flush.
            flush_interval: Maximum interval in seconds between flushes.
            buffer_capacity: Maximum items held in memory before oldest items are dropped.
            collector_url: Optional HTTP collector endpoint (e.g. OpenTelemetry collector).
            log_file_path: Optional local file path for appending JSONL telemetry logs.
            sink: Optional custom async callable for batch emission.
            retry_backoff: Base delay in seconds for exponential backoff on emission failure.
            max_retries: Maximum attempts to emit a batch before dropping.
        """
        self.max_batch: int = max_batch
        self.flush_interval: float = flush_interval
        self.buffer_capacity: int = buffer_capacity
        self.collector_url: Optional[str] = collector_url or os.getenv("TELEMETRY_COLLECTOR_URL")
        self.log_file_path: Optional[Path] = Path(log_file_path) if log_file_path else None
        self._custom_sink: Optional[TelemetrySink] = sink
        self.retry_backoff: float = retry_backoff
        self.max_retries: int = max_retries

        # Thread-safe buffer and synchronization primitives
        self._buffer: List[Dict[str, Any]] = []
        self._lock: asyncio.Lock = asyncio.Lock()
        self._flush_event: asyncio.Event = asyncio.Event()

        # Lifecycle & Background Task State
        self._running: bool = False
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._http_client: Optional[httpx.AsyncClient] = None

        # Telemetry Metrics
        self._total_recorded: int = 0
        self._total_emitted: int = 0
        self._dropped_count: int = 0
        self._failed_emissions_count: int = 0

    @property
    def is_running(self) -> bool:
        """Return True if the background flush loop is active."""
        return self._running

    @property
    def buffer_size(self) -> int:
        """Return the current number of events queued in the buffer."""
        return len(self._buffer)

    @property
    def stats(self) -> Dict[str, Any]:
        """Return operational telemetry dispatcher metrics."""
        return {
            "is_running": self._running,
            "buffer_size": len(self._buffer),
            "total_recorded": self._total_recorded,
            "total_emitted": self._total_emitted,
            "dropped_count": self._dropped_count,
            "failed_emissions_count": self._failed_emissions_count,
            "max_batch": self.max_batch,
            "flush_interval": self.flush_interval,
            "buffer_capacity": self.buffer_capacity,
        }

    async def start(self) -> None:
        """Start the background flushing loop.

        Should be called during application startup (e.g., FastAPI lifespan / startup event).
        """
        if self._running:
            logger.debug("TelemetryDispatcher is already running.")
            return

        self._running = True
        self._flush_event.clear()

        # Initialize async HTTP client if collector URL is specified
        if self.collector_url and self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)

        # Start continuous background task
        loop = asyncio.get_running_loop()
        self._flush_task = loop.create_task(
            self._flush_loop(),
            name="forge_telemetry_flush_loop",
        )
        logger.info(
            "TelemetryDispatcher started (max_batch=%d, flush_interval=%.1fs, capacity=%d)",
            self.max_batch,
            self.flush_interval,
            self.buffer_capacity,
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop the background flushing loop and drain remaining buffered events.

        Args:
            timeout: Maximum seconds to wait for remaining items to drain and task to stop.
        """
        if not self._running and len(self._buffer) == 0:
            return

        logger.info("Stopping TelemetryDispatcher and draining remaining buffer...")
        self._running = False
        self._flush_event.set()

        # Wait for the background loop task to finish
        if self._flush_task and not self._flush_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._flush_task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("Flush task timed out during shutdown; cancelling task.")
                self._flush_task.cancel()
                try:
                    await self._flush_task
                except asyncio.CancelledError:
                    pass

        # Drain any items remaining in buffer
        remaining = len(self._buffer)
        if remaining > 0:
            logger.info("Draining %d remaining telemetry items from buffer...", remaining)
            while True:
                flushed = await self.flush(force_all=True)
                if flushed == 0:
                    break

        # Cleanup HTTP client
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("TelemetryDispatcher stopped successfully.")

    async def record(self, event: Union[Dict[str, Any], Any]) -> bool:
        """Thread-safely append a telemetry event to the buffer.

        If the buffer reaches max_batch, signals the background flush loop immediately.
        If the buffer reaches buffer_capacity, applies drop-oldest overflow policy.

        Args:
            event: Event payload dictionary or serializable object.

        Returns:
            True if recorded successfully.
        """
        payload: Dict[str, Any]
        if isinstance(event, dict):
            payload = dict(event)
        else:
            payload = {"data": event}

        if "timestamp" not in payload:
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()

        async with self._lock:
            # Enforce overflow capacity policy
            if len(self._buffer) >= self.buffer_capacity:
                self._buffer.pop(0)
                self._dropped_count += 1
                logger.warning(
                    "Telemetry buffer full (%d items). Dropped oldest event to prevent memory leak.",
                    self.buffer_capacity,
                )

            self._buffer.append(payload)
            self._total_recorded += 1
            current_len = len(self._buffer)

        # Signal background loop if threshold reached
        if current_len >= self.max_batch:
            self._flush_event.set()

        return True

    def record_nowait(self, event: Union[Dict[str, Any], Any]) -> bool:
        """Synchronously schedule an event record task onto the running event loop."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.record(event))
            return True
        except RuntimeError:
            # Fallback if called outside of an active asyncio event loop
            if isinstance(event, dict):
                p = dict(event)
            else:
                p = {"data": event}
            if "timestamp" not in p:
                p["timestamp"] = datetime.now(timezone.utc).isoformat()
            if len(self._buffer) >= self.buffer_capacity:
                self._buffer.pop(0)
                self._dropped_count += 1
            self._buffer.append(p)
            self._total_recorded += 1
            return True

    async def flush(self, force_all: bool = False) -> int:
        """Slice and emit a batch from the buffer.

        Releases the buffer lock before performing I/O to avoid blocking concurrent writers.

        Args:
            force_all: If True, slices all items in buffer; otherwise slices up to max_batch.

        Returns:
            Number of items flushed in this invocation.
        """
        batch: List[Dict[str, Any]] = []

        async with self._lock:
            if not self._buffer:
                return 0

            if force_all:
                batch = list(self._buffer)
                self._buffer.clear()
            else:
                batch = self._buffer[: self.max_batch]
                del self._buffer[: self.max_batch]

        if not batch:
            return 0

        # Perform sink emission outside the lock with retry resilience
        await self._emit_batch_with_retry(batch)
        self._total_emitted += len(batch)
        return len(batch)

    async def _flush_loop(self) -> None:
        """Continuous background loop triggering flushes on threshold or interval."""
        logger.debug("TelemetryDispatcher background flush loop entered.")

        while self._running:
            try:
                # Wait for batch threshold notification OR time interval expiration
                try:
                    await asyncio.wait_for(
                        self._flush_event.wait(),
                        timeout=self.flush_interval,
                    )
                except asyncio.TimeoutError:
                    pass

                self._flush_event.clear()

                # Process available batches
                while self._running:
                    flushed = await self.flush(force_all=False)
                    if flushed == 0:
                        break

                    # Check remaining count without holding lock during iteration
                    async with self._lock:
                        remaining = len(self._buffer)
                    if remaining < self.max_batch:
                        break

            except asyncio.CancelledError:
                logger.debug("Flush loop received cancellation request.")
                break
            except Exception as loop_err:
                logger.error(
                    "Unexpected error in TelemetryDispatcher flush loop: %s",
                    loop_err,
                    exc_info=True,
                )
                await asyncio.sleep(0.5)

        logger.debug("TelemetryDispatcher background flush loop exited.")

    async def _emit_batch_with_retry(self, batch: List[Dict[str, Any]]) -> None:
        """Emit a batch to configured sinks with exponential backoff retry."""
        attempt = 0
        while attempt < self.max_retries:
            try:
                await self._emit_batch(batch)
                return
            except Exception as emit_err:
                attempt += 1
                backoff = self.retry_backoff * (2 ** (attempt - 1))
                if attempt < self.max_retries:
                    logger.warning(
                        "Telemetry sink emission failed (attempt %d/%d): %s. Retrying in %.2fs...",
                        attempt,
                        self.max_retries,
                        emit_err,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    self._failed_emissions_count += len(batch)
                    logger.error(
                        "Telemetry sink emission failed permanently after %d attempts for %d events: %s",
                        self.max_retries,
                        len(batch),
                        emit_err,
                    )
                    # Fallback to local structured error log to prevent data loss
                    self._fallback_emit(batch, str(emit_err))

    async def _emit_batch(self, batch: List[Dict[str, Any]]) -> None:
        """Route batch to configured sink handlers."""
        # 1. Custom Sink Callable
        if self._custom_sink:
            await self._custom_sink(batch)
            return

        # 2. HTTP Collector Sink
        if self.collector_url:
            await self._emit_to_http_collector(batch)
            return

        # 3. Local JSONL File Sink
        if self.log_file_path:
            await self._emit_to_file(batch)
            return

        # 4. Standard Concrete Structured Log Sink
        self._emit_to_structured_logger(batch)

    async def _emit_to_http_collector(self, batch: List[Dict[str, Any]]) -> None:
        """Post telemetry batch to remote collector endpoint."""
        if not self._http_client or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)

        assert self.collector_url is not None
        payload = {
            "batch_size": len(batch),
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
            "events": batch,
        }
        resp = await self._http_client.post(
            self.collector_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()

    async def _emit_to_file(self, batch: List[Dict[str, Any]]) -> None:
        """Append telemetry events as JSON lines to the configured log file."""
        assert self.log_file_path is not None
        file_path = self.log_file_path

        def _write() -> None:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "a", encoding="utf-8") as f:
                for item in batch:
                    f.write(json.dumps(item, default=str) + "\n")

        await asyncio.to_thread(_write)

    def _emit_to_structured_logger(self, batch: List[Dict[str, Any]]) -> None:
        """Default sink: output structured batch summary to Python logging pipeline."""
        logger.info(
            "Emitted telemetry batch of %d items (first event type=%s)",
            len(batch),
            batch[0].get("event", batch[0].get("name", "unknown")) if batch else "empty",
            extra={"batch_size": len(batch), "timestamp": time.time()},
        )

    def _fallback_emit(self, batch: List[Dict[str, Any]], error_detail: str) -> None:
        """Fallback emergency logger when primary sinks fail."""
        logger.critical(
            "DEAD-LETTER TELEMETRY SINK: Failed to emit %d items. Reason: %s",
            len(batch),
            error_detail,
            extra={"dropped_items_count": len(batch)},
        )

    async def __aenter__(self) -> "TelemetryDispatcher":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit with graceful drain."""
        await self.stop()


# Global Singleton Instance and Accessors
_GLOBAL_DISPATCHER: Optional[TelemetryDispatcher] = None


def get_telemetry_dispatcher() -> TelemetryDispatcher:
    """Retrieve or create the global TelemetryDispatcher instance."""
    global _GLOBAL_DISPATCHER
    if _GLOBAL_DISPATCHER is None:
        _GLOBAL_DISPATCHER = TelemetryDispatcher()
    return _GLOBAL_DISPATCHER


def set_telemetry_dispatcher(dispatcher: TelemetryDispatcher) -> None:
    """Set the global TelemetryDispatcher instance."""
    global _GLOBAL_DISPATCHER
    _GLOBAL_DISPATCHER = dispatcher


def setup_dispatcher_lifecycle(app: Any, dispatcher: Optional[TelemetryDispatcher] = None) -> TelemetryDispatcher:
    """Register startup and shutdown lifecycle hooks on a FastAPI app instance.

    Args:
        app: FastAPI application instance.
        dispatcher: Optional TelemetryDispatcher instance (uses global singleton if None).

    Returns:
        The registered TelemetryDispatcher.
    """
    disp = dispatcher or get_telemetry_dispatcher()

    @app.on_event("startup")
    async def _dispatcher_startup() -> None:
        await disp.start()

    @app.on_event("shutdown")
    async def _dispatcher_shutdown() -> None:
        await disp.stop()

    return disp
