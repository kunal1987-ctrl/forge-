"""Distributed Redis Mutex Locks for Concurrency Control in FORGE.

Provides distributed synchronization across Celery workers and FastAPI processes
to prevent race conditions, file collisions, and concurrent Git mutations on
shared repositories.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
from typing import Any, AsyncGenerator, Generator, Optional

import redis
import redis.asyncio as aioredis
from redis.exceptions import LockError, RedisError

logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


class ConcurrencyLockError(Exception):
    """Raised when acquiring an exclusive distributed repository lock fails or times out."""

    def __init__(
        self,
        repo_identifier: str,
        lock_key: str,
        message: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.repo_identifier = repo_identifier
        self.lock_key = lock_key
        self.timeout = timeout
        detail = message or (
            f"Failed to acquire exclusive distributed lock for repository '{repo_identifier}' "
            f"(key='{lock_key}') within {timeout or 0:.1f}s: Repository is currently locked by another concurrent task."
        )
        super().__init__(detail)


def compute_repo_lock_key(repo_identifier: str) -> str:
    """Generate a deterministic Redis lock key for a repository URL or project ID.

    Normalizes the identifier and computes a SHA-256 hash to ensure a clean,
    bounded Redis key: lock:repo:{repo_hash}.

    Args:
        repo_identifier: Repository URL, project ID, or filesystem path.

    Returns:
        Formatted Redis key string, e.g. 'lock:repo:a1b2c3d4e5f67890'.
    """
    clean_identifier = str(repo_identifier).strip()
    if clean_identifier.startswith("lock:repo:"):
        return clean_identifier
    # Normalize Windows backslashes if identifier is a directory path
    normalized = clean_identifier.lower().replace("\\", "/")
    repo_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"lock:repo:{repo_hash}"


class DistributedRepoLock:
    """Synchronous Distributed Redis Mutex Lock using redis-py's redis.lock.Lock.

    Provides exclusive concurrency protection for Git operations (clone, fetch, checkout,
    worktree creation, and commit).

    Features:
    - Automatic lease timeout (TTL) to prevent permanent deadlocks if a worker crashes.
    - Configurable blocking timeout or instant non-blocking fail-fast.
    - Clean ConcurrencyLockError raised on failure.
    - Python context manager support (`with DistributedRepoLock(...):`).
    """

    def __init__(
        self,
        repo_identifier: str,
        redis_client: Optional[redis.Redis] = None,
        redis_url: Optional[str] = None,
        timeout: float = 60.0,
        blocking: bool = True,
        blocking_timeout: Optional[float] = 10.0,
        sleep: float = 0.1,
    ) -> None:
        """Initialize the DistributedRepoLock.

        Args:
            repo_identifier: Unique identifier for the repository (URL, ID, or path).
            redis_client: Optional existing synchronous redis.Redis client.
            redis_url: Optional Redis connection string if redis_client is None.
            timeout: Maximum lease duration in seconds before auto-expiration (default 60s).
            blocking: If True, waits up to blocking_timeout to acquire. If False, returns immediately.
            blocking_timeout: Maximum seconds to wait when blocking is True (default 10s).
            sleep: Polling interval in seconds between acquisition attempts.
        """
        self.repo_identifier = str(repo_identifier)
        self.lock_key = compute_repo_lock_key(self.repo_identifier)
        self.timeout = float(timeout)
        self.blocking = bool(blocking)
        self.blocking_timeout = float(blocking_timeout) if blocking_timeout is not None else None
        self.sleep = float(sleep)
        self._owned_client = False

        if redis_client is not None:
            self.client = redis_client
        else:
            url = redis_url or DEFAULT_REDIS_URL
            self.client = redis.Redis.from_url(url, decode_responses=True)
            self._owned_client = True

        self._lock: redis.lock.Lock = self.client.lock(
            name=self.lock_key,
            timeout=self.timeout,
            sleep=self.sleep,
            blocking=self.blocking,
            blocking_timeout=self.blocking_timeout,
            thread_local=True,
        )
        self._acquired = False

    def acquire(
        self,
        blocking: Optional[bool] = None,
        blocking_timeout: Optional[float] = None,
    ) -> bool:
        """Acquire the distributed mutex lock.

        Args:
            blocking: Override default blocking setting.
            blocking_timeout: Override default blocking timeout.

        Returns:
            True if lock was successfully acquired.

        Raises:
            ConcurrencyLockError: If the lock cannot be acquired or timed out.
        """
        b = self.blocking if blocking is None else bool(blocking)
        bt = self.blocking_timeout if blocking_timeout is None else blocking_timeout

        try:
            acquired = self._lock.acquire(blocking=b, blocking_timeout=bt)
        except RedisError as redis_err:
            logger.error(
                "Redis error during repository lock acquisition: %s (key=%s)",
                redis_err,
                self.lock_key,
            )
            raise ConcurrencyLockError(
                repo_identifier=self.repo_identifier,
                lock_key=self.lock_key,
                message=f"Redis connection error during lock acquisition: {redis_err}",
                timeout=bt,
            ) from redis_err

        if not acquired:
            mode_desc = (
                f"within {bt:.1f}s timeout" if (b and bt is not None) else "in non-blocking mode"
            )
            logger.warning(
                "Failed to acquire distributed lock for repository '%s' (key=%s) %s",
                self.repo_identifier,
                self.lock_key,
                mode_desc,
            )
            raise ConcurrencyLockError(
                repo_identifier=self.repo_identifier,
                lock_key=self.lock_key,
                message=(
                    f"Concurrency conflict: Could not acquire exclusive lock for repository "
                    f"'{self.repo_identifier}' (key='{self.lock_key}') {mode_desc}. Another task is mutating this repository."
                ),
                timeout=bt,
            )

        self._acquired = True
        logger.info(
            "Acquired exclusive distributed lock for repo '%s' (key=%s, ttl=%.1fs)",
            self.repo_identifier,
            self.lock_key,
            self.timeout,
        )
        return True

    def release(self) -> None:
        """Release the distributed mutex lock safely."""
        if not self._acquired:
            return

        try:
            if self._lock.owned():
                self._lock.release()
                logger.info(
                    "Released distributed lock for repo '%s' (key=%s)",
                    self.repo_identifier,
                    self.lock_key,
                )
            else:
                logger.debug(
                    "Lock for key '%s' was already released or expired in Redis",
                    self.lock_key,
                )
        except LockError as lock_err:
            logger.debug(
                "LockError releasing repository lock '%s': %s",
                self.lock_key,
                lock_err,
            )
        except Exception as exc:
            logger.warning(
                "Unexpected error releasing repository lock '%s': %s",
                self.lock_key,
                exc,
            )
        finally:
            self._acquired = False

    def is_locked(self) -> bool:
        """Check whether the lock key is currently held in Redis."""
        try:
            return bool(self._lock.locked())
        except Exception:
            return False

    def owned(self) -> bool:
        """Check whether this specific instance currently holds the lock."""
        if not self._acquired:
            return False
        try:
            return bool(self._lock.owned())
        except Exception:
            return False

    def __enter__(self) -> DistributedRepoLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()
        if self._owned_client:
            try:
                self.client.close()
            except Exception:
                pass


@contextlib.contextmanager
def repo_lock(
    repo_identifier: str,
    redis_client: Optional[redis.Redis] = None,
    redis_url: Optional[str] = None,
    timeout: float = 60.0,
    blocking: bool = True,
    blocking_timeout: Optional[float] = 10.0,
) -> Generator[DistributedRepoLock, None, None]:
    """Context manager for acquiring an exclusive distributed repository lock.

    Example:
        with repo_lock("https://github.com/org/repo.git", timeout=60.0):
            worktree = wm.create_worktree(task_id)

    Args:
        repo_identifier: Repository URL, project ID, or path.
        redis_client: Optional existing synchronous redis.Redis client.
        redis_url: Optional Redis connection string.
        timeout: Expiration TTL in seconds (default 60s).
        blocking: Whether to wait for lock acquisition (default True).
        blocking_timeout: Max wait time in seconds (default 10s).

    Yields:
        The acquired DistributedRepoLock instance.
    """
    lock = DistributedRepoLock(
        repo_identifier=repo_identifier,
        redis_client=redis_client,
        redis_url=redis_url,
        timeout=timeout,
        blocking=blocking,
        blocking_timeout=blocking_timeout,
    )
    with lock:
        yield lock


class AsyncDistributedRepoLock:
    """Asynchronous Distributed Redis Mutex Lock for FastAPI / asyncio event loops."""

    def __init__(
        self,
        repo_identifier: str,
        redis_client: Optional[aioredis.Redis] = None,
        redis_url: Optional[str] = None,
        timeout: float = 60.0,
        blocking: bool = True,
        blocking_timeout: Optional[float] = 10.0,
        sleep: float = 0.1,
    ) -> None:
        self.repo_identifier = str(repo_identifier)
        self.lock_key = compute_repo_lock_key(self.repo_identifier)
        self.timeout = float(timeout)
        self.blocking = bool(blocking)
        self.blocking_timeout = float(blocking_timeout) if blocking_timeout is not None else None
        self.sleep = float(sleep)
        self._owned_client = False

        if redis_client is not None:
            self.client = redis_client
        else:
            url = redis_url or DEFAULT_REDIS_URL
            self.client = aioredis.from_url(url, decode_responses=True)
            self._owned_client = True

        self._lock = self.client.lock(
            name=self.lock_key,
            timeout=self.timeout,
            sleep=self.sleep,
            blocking=self.blocking,
            blocking_timeout=self.blocking_timeout,
            thread_local=True,
        )
        self._acquired = False

    async def acquire(
        self,
        blocking: Optional[bool] = None,
        blocking_timeout: Optional[float] = None,
    ) -> bool:
        b = self.blocking if blocking is None else bool(blocking)
        bt = self.blocking_timeout if blocking_timeout is None else blocking_timeout

        try:
            acquired = await self._lock.acquire(blocking=b, blocking_timeout=bt)
        except RedisError as redis_err:
            logger.error(
                "Redis error during async lock acquisition: %s (key=%s)",
                redis_err,
                self.lock_key,
            )
            raise ConcurrencyLockError(
                repo_identifier=self.repo_identifier,
                lock_key=self.lock_key,
                message=f"Redis connection error during async lock acquisition: {redis_err}",
                timeout=bt,
            ) from redis_err

        if not acquired:
            mode_desc = (
                f"within {bt:.1f}s timeout" if (b and bt is not None) else "in non-blocking mode"
            )
            raise ConcurrencyLockError(
                repo_identifier=self.repo_identifier,
                lock_key=self.lock_key,
                message=(
                    f"Concurrency conflict: Could not acquire exclusive lock for repository "
                    f"'{self.repo_identifier}' (key='{self.lock_key}') {mode_desc}. Another task is mutating this repository."
                ),
                timeout=bt,
            )

        self._acquired = True
        logger.info(
            "Acquired async distributed lock for repo '%s' (key=%s, ttl=%.1fs)",
            self.repo_identifier,
            self.lock_key,
            self.timeout,
        )
        return True

    async def release(self) -> None:
        if not self._acquired:
            return

        try:
            if await self._lock.owned():
                await self._lock.release()
                logger.info(
                    "Released async distributed lock for repo '%s' (key=%s)",
                    self.repo_identifier,
                    self.lock_key,
                )
        except LockError as lock_err:
            logger.debug(
                "LockError releasing async repository lock '%s': %s",
                self.lock_key,
                lock_err,
            )
        except Exception as exc:
            logger.warning(
                "Unexpected error releasing async repository lock '%s': %s",
                self.lock_key,
                exc,
            )
        finally:
            self._acquired = False

    async def is_locked(self) -> bool:
        try:
            return bool(await self._lock.locked())
        except Exception:
            return False

    async def owned(self) -> bool:
        if not self._acquired:
            return False
        try:
            return bool(await self._lock.owned())
        except Exception:
            return False

    async def __aenter__(self) -> AsyncDistributedRepoLock:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.release()
        if self._owned_client:
            try:
                await self.client.aclose()
            except Exception:
                pass


@contextlib.asynccontextmanager
async def async_repo_lock(
    repo_identifier: str,
    redis_client: Optional[aioredis.Redis] = None,
    redis_url: Optional[str] = None,
    timeout: float = 60.0,
    blocking: bool = True,
    blocking_timeout: Optional[float] = 10.0,
) -> AsyncGenerator[AsyncDistributedRepoLock, None]:
    """Asynchronous context manager for acquiring an exclusive distributed repository lock."""
    lock = AsyncDistributedRepoLock(
        repo_identifier=repo_identifier,
        redis_client=redis_client,
        redis_url=redis_url,
        timeout=timeout,
        blocking=blocking,
        blocking_timeout=blocking_timeout,
    )
    async with lock:
        yield lock
