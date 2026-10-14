"""Unit and integration tests for Telemetry Event Stream Router and Redis buffer replay.

Verifies:
1. buffer_telemetry_event buffers events in Redis under 'forge:buffer:{task_id}'.
2. get_buffered_events replays historical logs on reconnect so clients never get a blank screen.
3. SSE endpoint yields 15-second heartbeat pings when stream is idle.
4. WebSocket endpoint sends 15-second heartbeat pings and connects cleanly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from services.api.forge.routers.stream import (
    BUFFER_KEY_PREFIX,
    HEARTBEAT_INTERVAL_SECONDS,
    buffer_telemetry_event,
    get_buffered_events,
)
from services.api.main import app
from tests.conftest import FakeAioRedisBroker, FakeAioRedisClient


@pytest.mark.asyncio
async def test_buffer_telemetry_event_and_replay():
    """Verify telemetry events are buffered in Redis and replayed on mid-task reconnect."""
    broker = FakeAioRedisBroker()
    fake_redis = FakeAioRedisClient(broker)
    task_id = f"test-buffering-{uuid.uuid4().hex[:8]}"

    # Event 1: Planner
    event_1 = {
        "task_id": task_id,
        "event_type": "node_exit",
        "node": "planner_node",
        "step": 1,
        "status": "planning",
        "message": "Formulated execution plan",
    }
    # Event 2: Implementer
    event_2 = {
        "task_id": task_id,
        "event_type": "node_exit",
        "node": "implementer_node",
        "step": 3,
        "status": "implemented",
        "message": "Applied surgical patch",
    }

    with patch(
        "services.api.forge.routers.stream.get_redis_client",
        return_value=fake_redis,
    ):
        # Buffer events
        b1 = await buffer_telemetry_event(task_id, event_1)
        b2 = await buffer_telemetry_event(task_id, event_2)
        assert b1 is True
        assert b2 is True

        # Reconnect mid-task and retrieve buffered events
        buffered = await get_buffered_events(task_id)
        assert len(buffered) == 2
        assert buffered[0]["node"] == "planner_node"
        assert buffered[1]["node"] == "implementer_node"


@pytest.mark.asyncio
async def test_stream_router_sse_instant_buffer_replay():
    """Verify GET /api/tasks/{task_id}/events instantly yields historical events from Redis buffer."""
    broker = FakeAioRedisBroker()
    fake_redis = FakeAioRedisClient(broker)
    task_id = f"test-sse-replay-{uuid.uuid4().hex[:8]}"

    # Seed Redis buffer with historical logs before client connects
    buffer_key = f"{BUFFER_KEY_PREFIX}{task_id}"
    history_events = [
        {"node": "planner_node", "step": 1, "status": "planning", "message": "Historical plan"},
        {"node": "implementer_node", "step": 3, "status": "implemented", "message": "Historical patch"},
        {"node": "verifier_node", "step": 4, "status": "completed", "message": "Verification completed"},
    ]
    for ev in history_events:
        await fake_redis.rpush(buffer_key, json.dumps(ev))

    transport = httpx.ASGITransport(app=app)
    received_events: List[Dict[str, Any]] = []

    with (
        patch("services.api.forge.routers.stream.get_redis_client", return_value=fake_redis),
        patch("services.api.routers.tasks.aioredis.from_url", return_value=fake_redis),
        patch("redis.asyncio.from_url", return_value=fake_redis),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream("GET", f"/api/tasks/{task_id}/events") as response:
                assert response.status_code == 200
                assert "text/event-stream" in response.headers.get("content-type", "")

                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        raw_data = line[len("data:") :].strip()
                        if raw_data:
                            parsed = json.loads(raw_data)
                            received_events.append(parsed)
                            if parsed.get("status") in ("completed", "failed"):
                                break

    # Assert client received all historical events immediately upon connecting
    assert len(received_events) >= 3
    nodes = [e.get("node") for e in received_events if "node" in e]
    assert "planner_node" in nodes
    assert "implementer_node" in nodes
    assert "verifier_node" in nodes


@pytest.mark.asyncio
async def test_stream_router_heartbeat_interval():
    """Verify SSE stream emits a ping heartbeat when interval elapses."""
    task_id = f"test-heartbeat-{uuid.uuid4().hex[:8]}"
    broker = FakeAioRedisBroker()
    fake_redis = FakeAioRedisClient(broker)

    req = AsyncMock()
    req.is_disconnected = AsyncMock(return_value=False)

    # Patch HEARTBEAT_INTERVAL_SECONDS to a short window (0.05s) for testing
    with (
        patch("services.api.forge.routers.stream.HEARTBEAT_INTERVAL_SECONDS", 0.05),
        patch("services.api.forge.routers.stream.get_redis_client", return_value=fake_redis),
        patch("services.api.routers.tasks.aioredis.from_url", return_value=fake_redis),
        patch("redis.asyncio.from_url", return_value=fake_redis),
    ):
        from services.api.forge.routers.stream import stream_task_events_sse

        resp = await stream_task_events_sse(task_id, req)
        heartbeat_received = False

        async for chunk in resp.body_iterator:
            if isinstance(chunk, dict) and chunk.get("event") == "ping":
                raw_payload = chunk.get("data", "{}")
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                if payload.get("type") == "heartbeat" and payload.get("task_id") == task_id:
                    heartbeat_received = True
                    break

        assert heartbeat_received is True


def test_stream_router_websocket_connection_and_heartbeat():
    """Verify WebSocket endpoint connects, replays buffer, and handles heartbeats."""
    from starlette.testclient import TestClient

    task_id = f"test-ws-{uuid.uuid4().hex[:8]}"
    broker = FakeAioRedisBroker()
    fake_redis = FakeAioRedisClient(broker)

    # Pre-seed Redis buffer synchronously via broker
    buffer_key = f"{BUFFER_KEY_PREFIX}{task_id}"
    asyncio.run(fake_redis.rpush(buffer_key, json.dumps({"node": "planner_node", "status": "planning"})))

    with (
        patch("services.api.forge.routers.stream.HEARTBEAT_INTERVAL_SECONDS", 0.05),
        patch("services.api.forge.routers.stream.get_redis_client", return_value=fake_redis),
        patch("services.api.routers.tasks.aioredis.from_url", return_value=fake_redis),
        patch("redis.asyncio.from_url", return_value=fake_redis),
    ):
        with TestClient(app) as client:
            with client.websocket_connect(f"/api/tasks/{task_id}/ws") as ws:
                # 1. First frame should be historical event
                event_1 = ws.receive_json()
                assert event_1.get("node") == "planner_node"

                # 2. Next frame should be heartbeat ping when idle
                heartbeat_frame = ws.receive_json()
                assert heartbeat_frame.get("type") == "ping"
                assert heartbeat_frame.get("event") == "heartbeat"
                assert heartbeat_frame.get("task_id") == task_id

                # 3. Client can send text ping
                ws.send_text("ping")
                pong_resp = ws.receive_text()
                assert pong_resp == "pong"
