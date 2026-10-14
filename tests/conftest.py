"""Pytest fixtures for FORGE CI test isolation and offline fallback mocking.

Provides fixtures for:
- Isolated In-Memory Async Redis (FakePubSub, FakeAioRedisClient, FakeAioRedisBroker)
- Mocked SQLAlchemy AsyncSession
- Mocked Docker daemon client
- Mocked Celery task dispatchers
Ensures all tests execute deterministically without external Redis, PostgreSQL, or Docker dependencies.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest


class FakePubSub:
    """In-memory pubsub subscriber avoiding live Redis socket connections."""

    def __init__(self, broker: FakeAioRedisBroker) -> None:
        self.broker = broker
        self.channel: Optional[str] = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self.channel = channel
        self.broker.subscribe(channel, self.queue)

    async def unsubscribe(self, channel: str) -> None:
        self.broker.unsubscribe(channel, self.queue)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 1.0
    ) -> Optional[Dict[str, Any]]:
        try:
            msg = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            return {"type": "message", "data": msg}
        except (asyncio.TimeoutError, TimeoutError):
            return None

    async def aclose(self) -> None:
        if self.channel:
            self.broker.unsubscribe(self.channel, self.queue)


class FakeAioRedisBroker:
    """In-memory multi-channel pubsub message exchange."""

    def __init__(self) -> None:
        self.subscribers: Dict[str, List[asyncio.Queue[str]]] = {}

    def subscribe(self, channel: str, queue: asyncio.Queue[str]) -> None:
        self.subscribers.setdefault(channel, []).append(queue)

    def unsubscribe(self, channel: str, queue: asyncio.Queue[str]) -> None:
        if channel in self.subscribers:
            self.subscribers[channel] = [q for q in self.subscribers[channel] if q is not queue]

    async def publish(self, channel: str, message: str) -> int:
        queues = list(self.subscribers.get(channel, []))
        for q in queues:
            await q.put(message)
        return len(queues)


class FakeAioRedisClient:
    """Mock async Redis client supporting in-memory pubsub, key-value ops, and ping."""

    def __init__(self, broker: Optional[FakeAioRedisBroker] = None) -> None:
        self.broker = broker or FakeAioRedisBroker()
        self._store: Dict[str, Any] = {}

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self.broker)

    async def publish(self, channel: str, message: str) -> int:
        return await self.broker.publish(channel, message)

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def set(self, key: str, value: Any, **kwargs: Any) -> bool:
        self._store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                count += 1
    async def rpush(self, key: str, *values: Any) -> int:
        if key not in self._store or not isinstance(self._store[key], list):
            self._store[key] = []
        for v in values:
            self._store[key].append(v)
        return len(self._store[key])

    async def lrange(self, key: str, start: int, stop: int) -> List[Any]:
        items = self._store.get(key, [])
        if not isinstance(items, list):
            return []
        if stop == -1:
            return list(items[start:])
        return list(items[start : stop + 1])

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        items = self._store.get(key, [])
        if isinstance(items, list):
            if stop == -1:
                self._store[key] = items[start:]
            else:
                self._store[key] = items[start : stop + 1]
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def aclose(self) -> None:
        pass


@pytest.fixture
def fake_redis_broker() -> FakeAioRedisBroker:
    """Provide an isolated in-memory Redis pubsub broker for inter-task streaming."""
    return FakeAioRedisBroker()


@pytest.fixture
def fake_aioredis(fake_redis_broker: FakeAioRedisBroker) -> FakeAioRedisClient:
    """Provide a mock async Redis client connected to the in-memory broker."""
    return FakeAioRedisClient(fake_redis_broker)


@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Provide an isolated mock SQLAlchemy AsyncSession."""
    session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_res.scalar.return_value = None
    mock_res.scalars.return_value.all.return_value = []
    session.execute.return_value = mock_res
    session.commit.return_value = None
    session.rollback.return_value = None
    session.close.return_value = None
    return session


@pytest.fixture
def mock_docker_client() -> MagicMock:
    """Provide a mock Docker daemon client."""
    client = MagicMock()
    client.containers.run.return_value = b""
    client.containers.list.return_value = []
    client.ping.return_value = True
    return client
