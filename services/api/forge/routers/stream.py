"""Production-grade Telemetry Event Stream Router for FORGE.

Architecture:
1. Event Stream Heartbeat:
   - Asynchronous 15-second heartbeat ping interval for both SSE and WebSocket endpoints.
   - Prevents timeouts, proxy connection drops, and firewall termination on idle sockets.
2. Redis Event Buffering & Mid-Task Reconnect Replay:
   - Buffers recent execution events in Redis under 'forge:buffer:{task_id}'.
   - On mid-task reconnect, instantly flushes historical events so the client never gets a blank screen.
3. Dual-Protocol Support:
   - SSE: GET /api/tasks/{task_id}/events and GET /api/stream/{task_id}
   - WebSocket: /api/tasks/{task_id}/ws and /ws/tasks/{task_id}
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional
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
BUFFER_MAX_LEN: int = 500
BUFFER_TTL_SECONDS: int = 3600


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
        return buffered_events

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
                    .order_by(TaskEvent.sequence.asc())
                )
                res = await session.execute(stmt)
                past_events = res.scalars().all()
                for pe in past_events:
                    payload = pe.payload or {}
                    if isinstance(payload, dict):
                        buffered_events.append(payload)
    except Exception as db_exc:
        logger.debug(f"Postgres fallback event replay skipped: {db_exc}")

    return buffered_events


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
        dispatcher = get_telemetry_dispatcher()
        local_queue = dispatcher.subscribe(task_id)

        try:
            # 1. INSTANT RECONNECT BUFFER REPLAY
            # Immediately stream historical logs to client to eliminate blank screens
            historical_events = await get_buffered_events(task_id)
            for hist_event in historical_events:
                yield {
                    "event": "message",
                    "data": json.dumps(hist_event, default=str),
                }
                if hist_event.get("status") in ("completed", "failed"):
                    return

            # 2. Subscribe to Redis distributed Pub/Sub channel
            channel = f"{BUFFER_CHANNEL_PREFIX}{task_id}"
            try:
                redis_client = await _resolve_redis_client()
                pubsub = redis_client.pubsub()
                await pubsub.subscribe(channel)
            except Exception as r_err:
                logger.debug(f"Redis pubsub subscribe failed, using in-memory queue: {r_err}")
                pubsub = None

            # 3. Continuous Event Stream & 15-Second Heartbeat Loop
            last_activity = asyncio.get_event_loop().time()

            while True:
                if await request.is_disconnected():
                    logger.info(f"SSE client disconnected from task {task_id}")
                    break

                received_event = False

                # A. Check Redis Pub/Sub channel
                if pubsub:
                    try:
                        msg = await pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=0.1,
                        )
                        if msg and msg.get("type") == "message":
                            raw_data = msg.get("data")
                            if raw_data:
                                yield {"event": "message", "data": str(raw_data)}
                                last_activity = asyncio.get_event_loop().time()
                                received_event = True

                                # Terminal state detection
                                try:
                                    parsed = json.loads(raw_data)
                                    if parsed.get("status") in ("completed", "failed"):
                                        break
                                except Exception:
                                    pass
                    except Exception as msg_err:
                        logger.debug(f"Redis pubsub read error: {msg_err}")

                # B. Check in-memory local dispatcher subscriber queue
                if not local_queue.empty():
                    try:
                        local_event = local_queue.get_nowait()
                        raw_json = json.dumps(local_event, default=str)
                        yield {"event": "message", "data": raw_json}
                        last_activity = asyncio.get_event_loop().time()
                        received_event = True

                        if local_event.get("status") in ("completed", "failed"):
                            break
                    except asyncio.QueueEmpty:
                        pass

                # C. ASYNCHRONOUS 15-SECOND HEARTBEAT PING
                # Prevents proxies/firewalls from terminating the connection during idle periods
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

                if not received_event:
                    await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            logger.info(f"SSE connection cancelled for task {task_id}")
        finally:
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

    redis_client = None
    pubsub = None
    channel = f"{BUFFER_CHANNEL_PREFIX}{task_id}"

    # 1. Replay historical buffer immediately
    try:
        historical_events = await get_buffered_events(task_id)
        for hist_event in historical_events:
            await websocket.send_json(hist_event)
    except Exception as hist_err:
        logger.debug(f"WebSocket historical replay error: {hist_err}")

    # 2. Subscribe to Redis pubsub
    try:
        redis_client = await _resolve_redis_client()
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
    except Exception as r_err:
        logger.debug(f"Redis client unavailable for WebSocket, using local queue: {r_err}")
        pubsub = None

    # Helper task: Receive client pongs or messages
    async def client_listener() -> None:
        try:
            while True:
                msg = await websocket.receive_text()
                # Respond to client ping with pong if client initiates
                if msg.strip().lower() == "ping":
                    await websocket.send_text("pong")
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass

    listener_task = asyncio.create_task(client_listener())
    last_ping = asyncio.get_event_loop().time()

    try:
        while True:
            received_event = False

            # Check Redis pubsub
            if pubsub:
                try:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=0.1,
                    )
                    if msg and msg.get("type") == "message":
                        raw_data = msg.get("data")
                        if raw_data:
                            parsed = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                            await websocket.send_json(parsed)
                            received_event = True
                            if isinstance(parsed, dict) and parsed.get("status") in (
                                "completed",
                                "failed",
                            ):
                                break
                except Exception as read_err:
                    logger.debug(f"WebSocket Redis read error: {read_err}")

            # Check local in-memory queue
            if not local_queue.empty():
                try:
                    local_event = local_queue.get_nowait()
                    await websocket.send_json(local_event)
                    received_event = True
                    if local_event.get("status") in ("completed", "failed"):
                        break
                except asyncio.QueueEmpty:
                    pass

            # 15-SECOND ASYNC HEARTBEAT PING
            now = asyncio.get_event_loop().time()
            if (now - last_ping) >= HEARTBEAT_INTERVAL_SECONDS:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "event": "heartbeat",
                        "task_id": task_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                last_ping = now

            if not received_event:
                await asyncio.sleep(0.05)

    except (WebSocketDisconnect, asyncio.CancelledError):
        logger.info(f"WebSocket disconnected for task {task_id}")
    finally:
        listener_task.cancel()
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
