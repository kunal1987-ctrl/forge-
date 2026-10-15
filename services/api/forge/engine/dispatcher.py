"""Production-grade asynchronous TelemetryDispatcher for FORGE.

Features:
- Hot-path ``emit()`` that never awaits I/O: events are fanned out to in-memory
  subscriber queues and appended to a per-task replay ring, both synchronously.
- Bounded per-task replay ring (last 100 events) so newly connected or reconnected
  clients receive execution history instantly, with no Redis or database round trip.
- Dedicated background Redis publisher task fed by a bounded queue, so pub/sub
  latency or a slow/down Redis can never stall a LangGraph node transition.
- Thread-safe, asynchronous event enqueueing and live WebSocket/SSE stream broadcasting.
- Continuous background worker queue and batch flushing loop via asyncio.create_task().
- In-memory subscriber management (`subscribe` / `unsubscribe`) for direct SSE / WebSocket delivery.
- Redis pub/sub integration ('forge:events:{task_id}') for multi-process distributed streaming.
- Concrete emission sinks: structured logging, local JSONL file append, and HTTP collector emission with exponential backoff retry.
- Robust lifecycle management (start/stop) with graceful buffer draining on application shutdown.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import (
    Any,
    Callable,
    Coroutine,
    Deque,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import httpx

logger = logging.getLogger("forge.engine.dispatcher")

# Default Operational Constants
DEFAULT_MAX_BATCH: int = 50
DEFAULT_FLUSH_INTERVAL: float = 5.0
DEFAULT_BUFFER_CAPACITY: int = 1000
DEFAULT_RETRY_BACKOFF: float = 0.5
DEFAULT_MAX_RETRIES: int = 3

# Live streaming constants
DEFAULT_HISTORY_LIMIT: int = 100
DEFAULT_MAX_TRACKED_TASKS: int = 512
DEFAULT_PUBLISH_QUEUE_CAPACITY: int = 512
DEFAULT_PUBLISH_BATCH: int = 32
DEFAULT_SUBSCRIBER_QUEUE_CAPACITY: int = 500
# Kept at 500 to match `BUFFER_MAX_LEN` in forge/routers/stream.py, which reads this
# list on reconnect; the in-memory replay ring is the one held to 100 events.
DEFAULT_REDIS_BUFFER_LIMIT: int = 500
DEFAULT_REDIS_BUFFER_TTL: int = 3600
# After this many consecutive publish failures, pause pub/sub and re-probe later
# instead of spamming Redis (or the log) on every single event.
DEFAULT_REDIS_FAILURE_THRESHOLD: int = 5
DEFAULT_REDIS_RETRY_COOLDOWN: float = 15.0

TelemetrySink = Callable[[List[Dict[str, Any]]], Coroutine[Any, Any, None]]


class TelemetryDispatcher:
    """Asynchronous, thread-safe background telemetry dispatcher with live streaming and batching."""

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
        redis_url: Optional[str] = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        max_tracked_tasks: int = DEFAULT_MAX_TRACKED_TASKS,
        publish_queue_capacity: int = DEFAULT_PUBLISH_QUEUE_CAPACITY,
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
            redis_url: Optional Redis URL for live pub/sub broadcast.
            history_limit: Live events retained per task_id for instant client replay.
            max_tracked_tasks: Most task histories held in memory (LRU-evicted beyond this).
            publish_queue_capacity: Depth of the Redis publish queue before oldest events are dropped.
        """
        self.max_batch: int = max_batch
        self.flush_interval: float = flush_interval
        self.buffer_capacity: int = buffer_capacity
        self.collector_url: Optional[str] = collector_url or os.getenv("TELEMETRY_COLLECTOR_URL")
        self.log_file_path: Optional[Path] = Path(log_file_path) if log_file_path else None
        self._custom_sink: Optional[TelemetrySink] = sink
        self.retry_backoff: float = retry_backoff
        self.max_retries: int = max_retries
        self.redis_url: str = str(redis_url or os.getenv("REDIS_URL") or "redis://localhost:6379")
        self.history_limit: int = max(1, int(history_limit))
        self.max_tracked_tasks: int = max(1, int(max_tracked_tasks))
        self.publish_queue_capacity: int = max(1, int(publish_queue_capacity))

        # Thread-safe buffer, queue, and synchronization primitives
        self._buffer: List[Dict[str, Any]] = []
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=buffer_capacity)
        self._lock: asyncio.Lock = asyncio.Lock()
        self._flush_event: asyncio.Event = asyncio.Event()

        # In-memory WebSocket / SSE subscriber registries
        self._task_subscribers: Dict[str, Set[asyncio.Queue[Dict[str, Any]]]] = {}
        self._global_subscribers: Set[asyncio.Queue[Dict[str, Any]]] = set()

        # Per-task replay ring (last `history_limit` events), LRU-capped by task count.
        # Guarded by a threading lock because record_nowait() can run off the event loop.
        self._history: OrderedDict[str, Deque[Dict[str, Any]]] = OrderedDict()
        self._history_lock: threading.Lock = threading.Lock()

        # Redis publish queue drained by a dedicated background task, so pub/sub
        # latency is never paid on the agent execution path.
        self._publish_queue: asyncio.Queue[Tuple[str, Dict[str, Any]]] = asyncio.Queue(
            maxsize=self.publish_queue_capacity
        )

        # Lifecycle & Background Task State
        self._running: bool = False
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._publish_task: Optional[asyncio.Task[None]] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._redis_client: Optional[Any] = None
        self._redis_disabled: bool = False
        self._redis_retry_at: float = 0.0

        # Telemetry Metrics
        self._total_recorded: int = 0
        self._total_emitted: int = 0
        self._dropped_count: int = 0
        self._failed_emissions_count: int = 0
        self._published_count: int = 0
        self._publish_dropped_count: int = 0

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
        with self._history_lock:
            tracked_tasks = len(self._history)
            buffered_events = sum(len(ring) for ring in self._history.values())

        return {
            "is_running": self._running,
            "buffer_size": len(self._buffer),
            "queue_size": self._queue.qsize(),
            "total_recorded": self._total_recorded,
            "total_emitted": self._total_emitted,
            "dropped_count": self._dropped_count,
            "failed_emissions_count": self._failed_emissions_count,
            "max_batch": self.max_batch,
            "flush_interval": self.flush_interval,
            "buffer_capacity": self.buffer_capacity,
            "active_tasks_subscribed": len(self._task_subscribers),
            "global_subscribers": len(self._global_subscribers),
            "history_limit": self.history_limit,
            "history_tracked_tasks": tracked_tasks,
            "history_buffered_events": buffered_events,
            "publish_queue_size": self._publish_queue.qsize(),
            "publish_queue_capacity": self.publish_queue_capacity,
            "published_count": self._published_count,
            "publish_dropped_count": self._publish_dropped_count,
            "redis_disabled": self._redis_disabled,
        }

    async def start(self) -> None:
        """Start the background flushing loop and event queue worker.

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

        # Dedicated Redis publisher: keeps pub/sub latency off the emit() hot path.
        self._publish_task = loop.create_task(
            self._redis_publish_loop(),
            name="forge_telemetry_publish_loop",
        )

        logger.info(
            "TelemetryDispatcher started (max_batch=%d, flush_interval=%.1fs, capacity=%d, history_limit=%d)",
            self.max_batch,
            self.flush_interval,
            self.buffer_capacity,
            self.history_limit,
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

        # Flush any events still queued for Redis, then stop the publisher task
        if self._publish_task and not self._publish_task.done():
            try:
                await asyncio.wait_for(
                    self._drain_publish_queue(), timeout=min(2.0, max(0.1, timeout))
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.debug("Redis publish queue drain timed out during shutdown.")
            self._publish_task.cancel()
            try:
                await self._publish_task
            except asyncio.CancelledError:
                pass
        self._publish_task = None

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

        # Cleanup Redis client
        if self._redis_client is not None:
            try:
                await self._redis_client.aclose()
            except Exception:
                pass
            self._redis_client = None

        logger.info("TelemetryDispatcher stopped successfully.")

    # --------------------------------------------------------------------------
    # LIVE WEBSOCKET / SSE STREAMING SUBSCRIPTIONS
    # --------------------------------------------------------------------------

    def subscribe(
        self,
        task_id: Optional[str] = None,
        *,
        replay: bool = False,
        replay_limit: Optional[int] = None,
    ) -> asyncio.Queue[Dict[str, Any]]:
        """Register an in-memory queue to receive real-time events for SSE or WebSocket streaming.

        Args:
            task_id: If specified, subscribes only to events matching task_id; otherwise global.
            replay: Pre-seed the queue with this task's buffered history so a newly
                connected or reconnected client renders execution history immediately
                instead of a blank screen. Off by default because callers that already
                replay from Redis would otherwise deliver every event twice.
            replay_limit: Maximum replayed events (defaults to ``history_limit``).

        Returns:
            An asyncio.Queue yielding live event dictionaries.
        """
        q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(
            maxsize=DEFAULT_SUBSCRIBER_QUEUE_CAPACITY
        )

        if replay and task_id:
            for historical_event in self.get_recent_events(task_id, limit=replay_limit):
                try:
                    q.put_nowait(historical_event)
                except asyncio.QueueFull:
                    break

        if task_id:
            self._task_subscribers.setdefault(task_id, set()).add(q)
        else:
            self._global_subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Dict[str, Any]], task_id: Optional[str] = None) -> None:
        """Deregister an in-memory queue subscriber."""
        if task_id and task_id in self._task_subscribers:
            self._task_subscribers[task_id].discard(q)
            if not self._task_subscribers[task_id]:
                del self._task_subscribers[task_id]
        self._global_subscribers.discard(q)

    # --------------------------------------------------------------------------
    # PER-TASK REPLAY RING (last N events, in-process, zero I/O)
    # --------------------------------------------------------------------------

    def _record_history(self, task_id: str, event: Dict[str, Any]) -> None:
        """Append an event to its task's replay ring, evicting the least-recent task.

        Bounded twice over: ``history_limit`` events per task, and
        ``max_tracked_tasks`` tasks overall, so a long-lived server cannot grow this
        structure without bound.
        """
        key = str(task_id)
        with self._history_lock:
            ring = self._history.get(key)
            if ring is None:
                ring = deque(maxlen=self.history_limit)
                self._history[key] = ring
                while len(self._history) > self.max_tracked_tasks:
                    evicted_task, _ = self._history.popitem(last=False)
                    logger.debug("Evicted replay history for task %s (LRU).", evicted_task)
            else:
                self._history.move_to_end(key)
            ring.append(event)

    def get_recent_events(
        self, task_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Return a task's buffered live events in chronological order.

        Serves as an in-process replay source for clients that cannot reach Redis,
        or when this process is the only producer. Performs no I/O, so it is safe to
        call while building an SSE response.

        Args:
            task_id: Task whose history should be returned.
            limit: Maximum number of most-recent events; None returns the whole ring.

        Returns:
            List of event dictionaries, oldest first.
        """
        key = str(task_id)
        with self._history_lock:
            ring = self._history.get(key)
            if not ring:
                return []
            self._history.move_to_end(key)
            events = list(ring)

        if limit is None:
            return events
        if limit <= 0:
            return []
        return events[-limit:]

    def history_size(self, task_id: str) -> int:
        """Return how many events are currently buffered for a task."""
        with self._history_lock:
            return len(self._history.get(str(task_id), ()))

    def clear_history(self, task_id: Optional[str] = None) -> None:
        """Drop buffered replay history for one task, or for all tasks when omitted."""
        with self._history_lock:
            if task_id is None:
                self._history.clear()
            else:
                self._history.pop(str(task_id), None)

    # --------------------------------------------------------------------------
    # LIVE BROADCAST (NON-BLOCKING) & REDIS PUBLISHER
    # --------------------------------------------------------------------------

    async def _broadcast_to_subscribers(self, task_id: str, event: Dict[str, Any]) -> None:
        """Fan an event out to in-memory subscribers and queue it for Redis publication.

        This method deliberately performs **no I/O**: local delivery is a
        non-blocking ``put_nowait``, and Redis publication is handed to the dedicated
        background publisher task. A slow, saturated, or unreachable Redis therefore
        cannot add latency to the calling LangGraph node.
        """
        # 1. In-memory task subscribers (+ global subscribers)
        subscribers = set(self._global_subscribers)
        task_subscribers = self._task_subscribers.get(task_id)
        if task_subscribers:
            subscribers.update(task_subscribers)

        for q in subscribers:
            try:
                if q.full():
                    # Slow consumer: drop its oldest event rather than block the producer.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(event)
            except Exception as q_err:
                logger.debug("Subscriber queue put failed: %s", q_err)

        # 2. Hand off to the background Redis publisher
        self._enqueue_redis_publish(task_id, event)

    def _enqueue_redis_publish(self, task_id: str, event: Dict[str, Any]) -> None:
        """Queue an event for Redis publication, dropping the oldest on overflow.

        While pub/sub is paused after repeated failures, events are skipped until the
        cooldown elapses; the next event then re-probes Redis so a recovered broker
        resumes streaming without a process restart.
        """
        if not self.redis_url:
            return

        if self._redis_disabled:
            if time.monotonic() < self._redis_retry_at:
                return
            self._redis_disabled = False
            logger.info("Redis pub/sub cooldown elapsed; re-probing broker.")

        item = (str(task_id), event)
        try:
            self._publish_queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._publish_queue.get_nowait()
                self._publish_dropped_count += 1
                self._publish_queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                self._publish_dropped_count += 1

    async def _redis_publish_loop(self) -> None:
        """Drain the publish queue into Redis until cancelled.

        Runs off the emit() path so pub/sub round trips never block agent execution.
        """
        consecutive_failures = 0

        while True:
            try:
                first = await self._publish_queue.get()
                batch: List[Tuple[str, Dict[str, Any]]] = [first]

                # Coalesce whatever else is already queued into one round trip.
                while len(batch) < DEFAULT_PUBLISH_BATCH:
                    try:
                        batch.append(self._publish_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                published = await self._publish_events_to_redis(batch)
                if published:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= DEFAULT_REDIS_FAILURE_THRESHOLD:
                        self._redis_disabled = True
                        self._redis_retry_at = time.monotonic() + DEFAULT_REDIS_RETRY_COOLDOWN
                        logger.warning(
                            "Redis pub/sub unavailable after %d consecutive failures; "
                            "pausing for %.0fs. Live streaming continues over in-memory "
                            "subscribers only.",
                            consecutive_failures,
                            DEFAULT_REDIS_RETRY_COOLDOWN,
                        )
                        # Drain the backlog so it cannot accumulate while paused.
                        while not self._publish_queue.empty():
                            try:
                                self._publish_queue.get_nowait()
                                self._publish_dropped_count += 1
                            except asyncio.QueueEmpty:
                                break
                        consecutive_failures = 0

            except asyncio.CancelledError:
                raise
            except Exception as loop_err:
                logger.debug("Redis publish loop error: %s", loop_err)
                await asyncio.sleep(0.25)

    async def _drain_publish_queue(self) -> None:
        """Publish everything still queued (bounded by the caller's timeout)."""
        while not self._publish_queue.empty():
            batch: List[Tuple[str, Dict[str, Any]]] = []
            while len(batch) < DEFAULT_PUBLISH_BATCH:
                try:
                    batch.append(self._publish_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                return
            await self._publish_events_to_redis(batch)

    async def _ensure_redis_client(self) -> Optional[Any]:
        """Lazily construct the async Redis client."""
        if not self.redis_url:
            return None
        if self._redis_client is None:
            import redis.asyncio as aioredis

            self._redis_client = aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis_client

    async def _publish_events_to_redis(
        self, batch: List[Tuple[str, Dict[str, Any]]]
    ) -> bool:
        """Publish a batch of events and extend each task's cross-process replay buffer.

        Returns:
            True if the batch was published, False if Redis was unavailable.
        """
        try:
            client = await self._ensure_redis_client()
        except Exception as client_err:
            # Missing `redis` package or a malformed URL: let the caller's failure
            # counter disable pub/sub instead of spinning on every event.
            logger.debug("Redis client initialization failed: %s", client_err)
            self._publish_dropped_count += len(batch)
            return False
        if client is None:
            return False

        serialized: List[Tuple[str, str]] = [
            (task_id, json.dumps(event, default=str)) for task_id, event in batch
        ]

        try:
            for task_id, raw_json in serialized:
                await client.publish(f"forge:events:{task_id}", raw_json)

            # Extend the Redis replay buffer that stream.py reads on reconnect.
            per_task: Dict[str, List[str]] = {}
            for task_id, raw_json in serialized:
                per_task.setdefault(task_id, []).append(raw_json)

            for task_id, raw_events in per_task.items():
                buffer_key = f"forge:buffer:{task_id}"
                if hasattr(client, "rpush"):
                    await client.rpush(buffer_key, *raw_events)
                    if hasattr(client, "ltrim"):
                        await client.ltrim(buffer_key, -DEFAULT_REDIS_BUFFER_LIMIT, -1)
                    if hasattr(client, "expire"):
                        await client.expire(buffer_key, DEFAULT_REDIS_BUFFER_TTL)

            self._published_count += len(batch)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as redis_err:
            self._publish_dropped_count += len(batch)
            logger.debug("Redis live publish or buffer failed: %s", redis_err)
            return False

    # --------------------------------------------------------------------------
    # PRIMARY EMIT & RECORD API
    # --------------------------------------------------------------------------

    async def emit(
        self,
        task_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        immediate: bool = True,
    ) -> bool:
        """Emit a structured execution log, node state transition, or test outcome immediately.

        Hot-path guarantees: the event is appended to the task's replay ring and fanned
        out to every live subscriber queue without a single ``await`` on I/O. Redis
        publication is handed to a background task, so a stalled broker degrades
        cross-process streaming without adding latency to this call.

        Args:
            task_id: Target task UUID string.
            event_type: Node event identifier (e.g., 'node_entry', 'node_exit', 'test_outcome', 'log').
            payload: Node metadata, state payload, or test execution outcomes.
            message: Convenience human-readable summary, folded into ``payload['message']``.
            metadata: Additional metadata merged into ``payload`` (takes precedence over it).
            immediate: If True, flushes event immediately to live subscribers and sink buffers.

        Returns:
            True if event was processed successfully.
        """
        data = dict(payload) if isinstance(payload, dict) else (payload or {})
        if isinstance(data, dict):
            if metadata:
                data.update(metadata)
            if message is not None:
                data["message"] = message
        timestamp = datetime.now(timezone.utc).isoformat()

        event: Dict[str, Any] = {
            "task_id": str(task_id),
            "event_type": str(event_type),
            "event": str(event_type),
            "payload": data,
            "data": data,
            "timestamp": timestamp,
        }

        # Flatten payload fields for immediate UI consumption and property access
        if isinstance(data, dict):
            for k, v in data.items():
                if k not in event:
                    event[k] = v

        # 1. Replay ring first, so a client connecting the instant after this emit
        #    still receives the event. Steps 1 and 2 contain no awaits, so they are
        #    atomic with respect to other coroutines on this event loop.
        self._record_history(task_id, event)

        # 2. Immediate live broadcast to WebSocket / SSE subscribers (no I/O)
        await self._broadcast_to_subscribers(task_id, event)

        # 3. Thread-safe buffering for the background persistence sinks
        await self.record(event)

        # 4. If immediate, trigger flush signal for background persistence sinks
        if immediate:
            self._flush_event.set()

        return True

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
            # Keep the replay ring consistent for off-loop producers too (e.g. Celery).
            buffered_task_id = p.get("task_id")
            if buffered_task_id:
                self._record_history(str(buffered_task_id), p)
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

    # --------------------------------------------------------------------------
    # BACKGROUND FLUSH LOOP & RETRY SINK EMISSION
    # --------------------------------------------------------------------------

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


def setup_dispatcher_lifecycle(
    app: Any, dispatcher: Optional[TelemetryDispatcher] = None
) -> TelemetryDispatcher:
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


__all__ = [
    "DEFAULT_BUFFER_CAPACITY",
    "DEFAULT_FLUSH_INTERVAL",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_MAX_BATCH",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_TRACKED_TASKS",
    "DEFAULT_PUBLISH_BATCH",
    "DEFAULT_PUBLISH_QUEUE_CAPACITY",
    "DEFAULT_REDIS_FAILURE_THRESHOLD",
    "DEFAULT_REDIS_RETRY_COOLDOWN",
    "DEFAULT_RETRY_BACKOFF",
    "DEFAULT_SUBSCRIBER_QUEUE_CAPACITY",
    "TelemetryDispatcher",
    "TelemetrySink",
    "get_telemetry_dispatcher",
    "set_telemetry_dispatcher",
    "setup_dispatcher_lifecycle",
]
