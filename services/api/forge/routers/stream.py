"""Production-grade Telemetry Event Stream Router for FORGE.

Architecture:
1. Event Stream Heartbeat:
   - Asynchronous 15-second heartbeat ping interval for both SSE and WebSocket endpoints.
   - Prevents timeouts, proxy connection drops, and firewall termination on idle sockets.
2. Redis Event Buffering & Mid-Task Reconnect Replay:
   - Buffers recent execution events in Redis under 'forge:buffer:{task_id}'.
   - On mid-task reconnect, instantly flushes historical events so the client never gets a blank screen.
   - Falls back to the dispatcher's in-memory replay ring (last 100 events) when Redis
     has no buffered history, so single-process deployments still replay.
3. Dual-Protocol Support:
   - SSE: GET /api/tasks/{task_id}/events and GET /api/stream/{task_id}
   - WebSocket: /api/tasks/{task_id}/ws and /ws/tasks/{task_id}
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, AsyncGenerator, Deque, Dict, List, Optional, Set
import uuid

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from opentelemetry import trace
import redis.asyncio as aioredis
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from services.api.core.database import async_session_factory
from services.api.forge.engine.dispatcher import (
    get_telemetry_dispatcher,
)
from services.api.models.task import TaskEvent

logger = logging.getLogger("forge.routers.stream")
tracer = trace.get_tracer("forge.routers.stream")

router = APIRouter(tags=["Stream"])

# Operational Constants
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
HEARTBEAT_INTERVAL_SECONDS: float = 15.0
BUFFER_KEY_PREFIX: str = "forge:buffer:"
BUFFER_CHANNEL_PREFIX: str = "forge:events:"
BUFFER_MAX_LEN: int = 100
BUFFER_TTL_SECONDS: int = 3600
# In-process replay depth used when Redis has no buffered history for a task.
REPLAY_LIMIT: int = 100
LIVE_QUEUE_CAPACITY: int = 256
EVENT_DEDUPE_LIMIT: int = 1_000
TERMINAL_EVENT_STATUSES = {
    "completed",
    "failed",
    "aborted",
    "cancelled",
    "cleaned_up",
    "pr_created",
}


def _event_key(event: Dict[str, Any]) -> str:
    """Return a stable identity for suppressing duplicates across local and Redis paths."""
    event_id = event.get("event_id")
    if event_id is not None:
        return str(event_id)
    return json.dumps(event, sort_keys=True, default=str, separators=(",", ":"))


def _is_terminal_event(event: Dict[str, Any]) -> bool:
    return str(event.get("status", "")).lower() in TERMINAL_EVENT_STATUSES


def _remember_event(
    event: Dict[str, Any],
    seen: Set[str],
    recent: Deque[str],
) -> bool:
    """Remember a bounded set of recent event identities; return True for duplicates."""
    key = _event_key(event)
    if key in seen:
        return True
    if len(recent) == recent.maxlen:
        seen.discard(recent[0])
    recent.append(key)
    seen.add(key)
    return False


async def _put_latest(
    queue: asyncio.Queue[Dict[str, Any]],
    event: Dict[str, Any],
) -> None:
    """Enqueue without back-pressuring producers, evicting the oldest queued item."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning("Live stream queue remained full after evicting an event.")


async def _pump_local_events(
    source: asyncio.Queue[Dict[str, Any]],
    destination: asyncio.Queue[Dict[str, Any]],
) -> None:
    while True:
        await _put_latest(destination, await source.get())


async def _pump_redis_events(
    pubsub: Any,
    destination: asyncio.Queue[Dict[str, Any]],
) -> None:
    while True:
        try:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if not message or message.get("type") != "message":
                continue
            raw_data = message.get("data")
            if not raw_data:
                continue
            event = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            if isinstance(event, dict):
                await _put_latest(destination, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Redis stream reader failed; retrying: %s", exc)
            await asyncio.sleep(0.25)


# -----------------------------------------------------------------------------
# 1. Redis Telemetry Event Buffer Helpers
# -----------------------------------------------------------------------------


def get_redis_client(redis_url: Optional[str] = None) -> Any:
    """Retrieve configured async Redis client with automatic fallback."""
    url = redis_url or REDIS_URL
    return aioredis.from_url(url, decode_responses=True)


async def _resolve_redis_client(redis_url: Optional[str] = None) -> Any:
    """Helper to resolve redis client whether returned synchronously or as coroutine."""
    res = get_redis_client(redis_url)
    if asyncio.iscoroutine(res):
        return await res
    return res


async def buffer_telemetry_event(
    task_id: str,
    event: Dict[str, Any],
    redis_url: Optional[str] = None,
) -> bool:
    """Buffer a telemetry event into Redis for instant mid-task reconnection replay.

    Args:
        task_id: Unique task identifier.
        event: Normalized telemetry event payload.
        redis_url: Optional override Redis URL.

    Returns:
        True if successfully buffered.
    """
    buffer_key = f"{BUFFER_KEY_PREFIX}{task_id}"
    raw_json = json.dumps(event, default=str)
    try:
        client = await _resolve_redis_client(redis_url)
        if hasattr(client, "rpush"):
            await client.rpush(buffer_key, raw_json)
            if hasattr(client, "ltrim"):
                await client.ltrim(buffer_key, -BUFFER_MAX_LEN, -1)
            if hasattr(client, "expire"):
                await client.expire(buffer_key, BUFFER_TTL_SECONDS)
            return True
    except Exception as exc:
        logger.debug(f"Failed to buffer telemetry event in Redis for {task_id}: {exc}")
    return False


async def get_buffered_events(
    task_id: str,
    redis_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve recent buffered telemetry events from Redis or PostgreSQL fallback.

    Args:
        task_id: Unique task identifier.
        redis_url: Optional override Redis URL.

    Returns:
        List of historical events in chronological order.
    """
    buffered_events: List[Dict[str, Any]] = []
    buffer_key = f"{BUFFER_KEY_PREFIX}{task_id}"

    # 1. Query Redis In-Memory Ring Buffer
    try:
        client = await _resolve_redis_client(redis_url)
        if hasattr(client, "lrange"):
            raw_items = await client.lrange(buffer_key, 0, -1)
            for item in raw_items:
                try:
                    buffered_events.append(json.loads(item) if isinstance(item, str) else item)
                except Exception:
                    pass
    except Exception as exc:
        logger.debug(f"Redis buffer retrieval skipped or failed for {task_id}: {exc}")

    if buffered_events:
        return buffered_events[-REPLAY_LIMIT:]

    # 2. Fallback to PostgreSQL TaskEvent persistence
    try:
        async with async_session_factory() as session:
            try:
                task_uuid = uuid.UUID(task_id)
            except ValueError:
                task_uuid = None

            if task_uuid:
                stmt = (
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_uuid)
                    .order_by(TaskEvent.sequence.desc())
                    .limit(REPLAY_LIMIT)
                )
                res = await session.execute(stmt)
                past_events = reversed(res.scalars().all())
                for pe in past_events:
                    payload = pe.payload or {}
                    if isinstance(payload, dict):
                        buffered_events.append(payload)
    except Exception as db_exc:
        logger.debug(f"Postgres fallback event replay skipped: {db_exc}")

    return buffered_events[-REPLAY_LIMIT:]


# -----------------------------------------------------------------------------
# 2. Server-Sent Events (SSE) Endpoint with 15s Heartbeat & Reconnect Buffer
# -----------------------------------------------------------------------------


@router.get(
    "/api/tasks/{task_id}/events",
    response_class=EventSourceResponse,
    summary="Task Execution Event Stream (SSE)",
)
@router.get(
    "/api/stream/{task_id}",
    response_class=EventSourceResponse,
    summary="Task Execution Event Stream (SSE Alias)",
)
async def stream_task_events_sse(
    task_id: str,
    request: Request,
) -> EventSourceResponse:
    """Stream live task execution events via Server-Sent Events (SSE).

    Features:
    - Instant replay of historical telemetry logs from Redis buffer upon reconnection.
    - 15-second heartbeat ping keeping the HTTP connection alive through firewalls.
    - Non-blocking Redis Pub/Sub subscription and in-memory queue fallback.
    """

    async def sse_event_generator() -> AsyncGenerator[Dict[str, str], None]:
        redis_client = None
        pubsub = None
        channel = f"{BUFFER_CHANNEL_PREFIX}{task_id}"
        dispatcher = get_telemetry_dispatcher()
        local_queue = dispatcher.subscribe(task_id)
        live_events: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=LIVE_QUEUE_CAPACITY)
        reader_tasks: List[asyncio.Task[None]] = []
        seen_events: Set[str] = set()
        recent_events: Deque[str] = deque(maxlen=EVENT_DEDUPE_LIMIT)

        try:
            # Register both live sources before reading history so events produced
            # during replay are queued instead of lost.
            try:
                redis_client = await _resolve_redis_client()
                pubsub = redis_client.pubsub()
                await pubsub.subscribe(channel)
            except Exception as r_err:
                logger.debug(f"Redis pubsub subscribe failed, using in-memory queue: {r_err}")
                pubsub = None

            reader_tasks.append(asyncio.create_task(_pump_local_events(local_queue, live_events)))
            if pubsub is not None:
                reader_tasks.append(asyncio.create_task(_pump_redis_events(pubsub, live_events)))

            historical_events = await get_buffered_events(task_id)
            if not historical_events:
                historical_events = dispatcher.get_recent_events(task_id, limit=REPLAY_LIMIT)
            for hist_event in historical_events[-REPLAY_LIMIT:]:
                if _remember_event(hist_event, seen_events, recent_events):
                    continue
                yield {"event": "message", "data": json.dumps(hist_event, default=str)}
                if _is_terminal_event(hist_event):
                    return

            last_activity = asyncio.get_event_loop().time()

            while True:
                if await request.is_disconnected():
                    logger.info(f"SSE client disconnected from task {task_id}")
                    break

                wait_seconds = min(
                    0.5,
                    max(
                        0.01,
                        HEARTBEAT_INTERVAL_SECONDS
                        - (asyncio.get_event_loop().time() - last_activity),
                    ),
                )
                try:
                    live_event = await asyncio.wait_for(
                        live_events.get(),
                        timeout=wait_seconds,
                    )
                except asyncio.TimeoutError:
                    live_event = None

                if live_event is not None:
                    if not _remember_event(live_event, seen_events, recent_events):
                        yield {
                            "event": "message",
                            "data": json.dumps(live_event, default=str),
                        }
                        last_activity = asyncio.get_event_loop().time()
                        if _is_terminal_event(live_event):
                            break

                now = asyncio.get_event_loop().time()
                if (now - last_activity) >= HEARTBEAT_INTERVAL_SECONDS:
                    heartbeat_payload = {
                        "type": "heartbeat",
                        "event": "ping",
                        "task_id": task_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    yield {
                        "event": "ping",
                        "data": json.dumps(heartbeat_payload),
                    }
                    last_activity = now

        except asyncio.CancelledError:
            logger.info(f"SSE connection cancelled for task {task_id}")
            raise
        finally:
            for reader_task in reader_tasks:
                reader_task.cancel()
            if reader_tasks:
                await asyncio.gather(*reader_tasks, return_exceptions=True)
            dispatcher.unsubscribe(local_queue, task_id)
            if pubsub:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.close()
                except Exception:
                    pass
            if redis_client and hasattr(redis_client, "aclose"):
                try:
                    await redis_client.aclose()
                except Exception:
                    pass

    return EventSourceResponse(
        sse_event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -----------------------------------------------------------------------------
# 3. WebSocket Endpoint with 15s Heartbeat Ping & Reconnect Buffer
# -----------------------------------------------------------------------------


@router.websocket("/api/tasks/{task_id}/ws")
@router.websocket("/ws/tasks/{task_id}")
async def websocket_task_events(
    websocket: WebSocket,
    task_id: str,
) -> None:
    """Bidirectional WebSocket endpoint streaming task execution telemetry.

    Features:
    - Immediate replay of historical Redis buffer logs upon connection.
    - Continuous async 15-second ping frame to maintain firewall and proxy keepalive.
    - Handles incoming pong acknowledgements and client disconnects gracefully.
    """
    await websocket.accept()
    dispatcher = get_telemetry_dispatcher()
    local_queue = dispatcher.subscribe(task_id)
    live_events: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=LIVE_QUEUE_CAPACITY)
    send_lock = asyncio.Lock()
    redis_client = None
    pubsub = None
    channel = f"{BUFFER_CHANNEL_PREFIX}{task_id}"
    reader_tasks: List[asyncio.Task[None]] = []
    listener_task: Optional[asyncio.Task[None]] = None
    pending_tasks: List[asyncio.Task[Any]] = []
    seen_events: Set[str] = set()
    recent_events: Deque[str] = deque(maxlen=EVENT_DEDUPE_LIMIT)

    try:
        # Establish live subscriptions before replay to close the reconnect race.
        try:
            redis_client = await _resolve_redis_client()
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(channel)
        except Exception as r_err:
            logger.debug(f"Redis client unavailable for WebSocket, using local queue: {r_err}")
            pubsub = None

        reader_tasks.append(asyncio.create_task(_pump_local_events(local_queue, live_events)))
        if pubsub is not None:
            reader_tasks.append(asyncio.create_task(_pump_redis_events(pubsub, live_events)))

        historical_events = await get_buffered_events(task_id)
        if not historical_events:
            historical_events = dispatcher.get_recent_events(task_id, limit=REPLAY_LIMIT)
        for hist_event in historical_events[-REPLAY_LIMIT:]:
            if _remember_event(hist_event, seen_events, recent_events):
                continue
            await websocket.send_json(hist_event)
            if _is_terminal_event(hist_event):
                return

        async def client_listener() -> None:
            try:
                while True:
                    message = await websocket.receive_text()
                    if message.strip().lower() == "ping":
                        async with send_lock:
                            await websocket.send_text("pong")
            except WebSocketDisconnect:
                return

        listener_task = asyncio.create_task(client_listener())
        next_event_task = asyncio.create_task(live_events.get())
        pending_tasks.append(next_event_task)
        last_ping = asyncio.get_event_loop().time()

        while True:
            now = asyncio.get_event_loop().time()
            timeout = max(0.0, HEARTBEAT_INTERVAL_SECONDS - (now - last_ping))
            try:
                event = await asyncio.wait_for(live_events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                event = None

            if event is not None:
                next_event_task = asyncio.create_task(live_events.get())
                pending_tasks[:] = [next_event_task]
                if not _remember_event(event, seen_events, recent_events):
                    async with send_lock:
                        await websocket.send_json(event)
                    if _is_terminal_event(event):
                        break

            now = asyncio.get_event_loop().time()
            if (now - last_ping) >= HEARTBEAT_INTERVAL_SECONDS:
                async with send_lock:
                    await websocket.send_json(
                        {
                            "type": "ping",
                            "event": "heartbeat",
                            "task_id": task_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                last_ping = now

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    except asyncio.CancelledError:
        logger.info(f"WebSocket stream cancelled for task {task_id}")
        raise
    except Exception as stream_err:
        logger.warning("WebSocket stream failed for task %s: %s", task_id, stream_err)
    finally:
        if listener_task is not None:
            listener_task.cancel()
            pending_tasks.append(listener_task)
        for reader_task in reader_tasks:
            reader_task.cancel()
        pending_tasks.extend(reader_tasks)
        for pending_task in pending_tasks:
            if not pending_task.done():
                pending_task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        dispatcher.unsubscribe(local_queue, task_id)
        if pubsub:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass
        if redis_client and hasattr(redis_client, "aclose"):
            try:
                await redis_client.aclose()
            except Exception:
                pass
