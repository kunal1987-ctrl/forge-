"""Engine Task Execution Manager, Distributed Concurrency Lock, and Execution Queue.

Enforces strict concurrency control per engine/repository so that only one autonomous
task runs at a time. Incoming execution requests (e.g. POST /api/tasks/run) are either
placed into a FIFO queue (backed by Redis with an in-memory async fallback) or rejected
with a clean HTTP 409 Conflict: "Engine is currently busy executing an active task".
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from services.api.forge.exceptions import EngineBusyError

logger = logging.getLogger("forge.engine.manager")

DEFAULT_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DEFAULT_LOCK_TTL = float(os.getenv("FORGE_ENGINE_LOCK_TTL", "600.0"))  # 10 minutes max lease


@dataclass
class QueuedTask:
    """Metadata describing a queued task waiting for engine execution."""

    task_id: str
    repo_id: str
    prompt: str
    mode: str = "fast"
    enqueued_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "queued"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> QueuedTask:
        """Construct QueuedTask from a dictionary."""
        return cls(
            task_id=str(data.get("task_id", "")),
            repo_id=str(data.get("repo_id", "default-repo")),
            prompt=str(data.get("prompt", "")),
            mode=str(data.get("mode", "fast")),
            enqueued_at=str(data.get("enqueued_at", datetime.now(timezone.utc).isoformat())),
            status=str(data.get("status", "queued")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class ActiveTask:
    """Metadata describing a currently executing engine task."""

    task_id: str
    repo_id: str
    prompt: str
    mode: str = "fast"
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: str = "running"
    metadata: Dict[str, Any] = field(default_factory=dict)
    start_time: Optional[str] = field(default_factory=lambda: datetime.utcnow().isoformat())
    end_time: Optional[str] = None
    duration_seconds: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ActiveTask:
        """Construct ActiveTask from a dictionary."""
        return cls(
            task_id=str(data.get("task_id", "")),
            repo_id=str(data.get("repo_id", "default-repo")),
            prompt=str(data.get("prompt", "")),
            mode=str(data.get("mode", "fast")),
            started_at=str(data.get("started_at", datetime.utcnow().isoformat())),
            status=str(data.get("status", "running")),
            metadata=dict(data.get("metadata", {})),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            duration_seconds=data.get("duration_seconds"),
        )


class TaskExecutionManager:
    """Manages strict concurrency locks and execution queues per engine/repository.

    Features:
    - Distributed Redis mutex locks and Redis FIFO task queues for multi-process scalability.
    - Resilient in-memory async fallback (asyncio.Lock & deque) when Redis is offline or in tests.
    - Precise start_time, end_time, and duration_seconds tracking with database persistence.
    - Rejection with HTTP 409 Conflict ("Engine is currently busy executing an active task")
      when a concurrent task is submitted without queueing enabled.
    - Automatic queue progression: when a task completes or aborts, the next queued task is popped.
    - Context manager `execution_guard` for automatic lock acquisition and cleanup.
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        lock_ttl: float = DEFAULT_LOCK_TTL,
    ) -> None:
        self.redis_url = redis_url or DEFAULT_REDIS_URL
        self.lock_ttl = lock_ttl

        # In-memory concurrency tracking (repo_key -> ActiveTask / Lock / Queue)
        self._in_memory_active: Dict[str, ActiveTask] = {}
        self._in_memory_queues: Dict[str, List[QueuedTask]] = {}
        self._async_locks: Dict[str, asyncio.Lock] = {}
        self._manager_lock = asyncio.Lock()

        # In-memory execution timing registry: task_id -> {"start_time": datetime, "end_time": datetime, "duration_seconds": int, "status": str}
        self._task_timings: Dict[str, Dict[str, Any]] = {}

        self._redis_client: Optional[aioredis.Redis] = None

    def _get_async_lock(self, repo_key: str) -> asyncio.Lock:
        """Return or create a dedicated asyncio.Lock for the repository."""
        if repo_key not in self._async_locks:
            self._async_locks[repo_key] = asyncio.Lock()
        return self._async_locks[repo_key]

    async def _get_redis(self) -> Optional[aioredis.Redis]:
        """Obtain or initialize the async Redis client safely for the current event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        if self._redis_client is None or getattr(self, "_redis_loop", None) is not current_loop:
            if self._redis_client is not None:
                try:
                    await self._redis_client.aclose()
                except Exception:
                    pass
            try:
                self._redis_client = aioredis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                )
                self._redis_loop = current_loop
            except Exception as exc:
                logger.debug("Async Redis client initialization skipped: %s", exc)
                return None
        return self._redis_client

    def normalize_repo_key(self, repo_id: Optional[str]) -> str:
        """Normalize repository or engine identifier to a deterministic key.

        Args:
            repo_id: Optional repository UUID, URL, or name.

        Returns:
            Normalized repository key string.
        """
        if not repo_id or not str(repo_id).strip():
            return "default-repo"
        clean = str(repo_id).strip().lower().replace("\\", "/")
        # Extract base repository name if it's a URL or path
        if "/" in clean:
            clean = clean.rstrip("/").split("/")[-1]
            if clean.endswith(".git"):
                clean = clean[:-4]
        # Sanitize safe key
        safe = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", clean)
        return safe or "default-repo"

    def _active_key(self, repo_key: str) -> str:
        return f"forge:engine:active:{repo_key}"

    def _queue_key(self, repo_key: str) -> str:
        return f"forge:engine:queue:{repo_key}"

    def _timing_key(self, task_id: str) -> str:
        return f"forge:engine:timing:{task_id}"

    async def record_task_start(
        self,
        task_id: str,
        repo_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
    ) -> datetime:
        """Record start_time = datetime.utcnow() when a task pipeline is dispatched and execution begins.

        Saves start_time to in-memory registry, Redis cache, and PostgreSQL database Task record.
        """
        st = start_time if start_time is not None else datetime.utcnow()
        repo_key = self.normalize_repo_key(repo_id)

        timing_entry = self._task_timings.setdefault(task_id, {})
        timing_entry["start_time"] = st
        timing_entry["end_time"] = None
        timing_entry["duration_seconds"] = None
        timing_entry["status"] = "RUNNING"
        timing_entry["repo_id"] = repo_key

        if repo_key in self._in_memory_active:
            active = self._in_memory_active[repo_key]
            if active.task_id == task_id:
                active.start_time = st.isoformat()
                active.status = "running"

        try:
            r = await self._get_redis()
            if r is not None:
                payload = json.dumps(
                    {
                        "task_id": task_id,
                        "repo_id": repo_key,
                        "start_time": st.isoformat(),
                        "end_time": None,
                        "duration_seconds": None,
                        "status": "RUNNING",
                    }
                )
                await r.set(self._timing_key(task_id), payload, ex=int(self.lock_ttl * 2))
        except Exception as r_err:
            logger.debug("Redis record_task_start notice: %s", r_err)

        await self._persist_db_task_start(task_id, st)
        logger.info("Recorded execution start_time=%s for task '%s'", st.isoformat(), task_id)
        return st

    async def record_task_end(
        self,
        task_id: str,
        status: str = "COMPLETED",
        end_time: Optional[datetime] = None,
        repo_id: Optional[str] = None,
    ) -> Tuple[datetime, int]:
        """Record end_time = datetime.utcnow() and compute duration_seconds the moment
        the workflow reaches a terminal state (COMPLETED, FAILED, or ABORTED).

        Saves duration_seconds and timestamps into database task record and cache.
        """
        et = end_time if end_time is not None else datetime.utcnow()
        repo_key = self.normalize_repo_key(repo_id)

        st: Optional[datetime] = None
        timing_entry = self._task_timings.get(task_id)
        if timing_entry and timing_entry.get("start_time"):
            st = timing_entry["start_time"]

        if st is None and repo_key in self._in_memory_active:
            active = self._in_memory_active[repo_key]
            if active.task_id == task_id and active.start_time:
                try:
                    st = datetime.fromisoformat(active.start_time)
                except Exception:
                    pass

        if st is None:
            try:
                r = await self._get_redis()
                if r is not None:
                    raw = await r.get(self._timing_key(task_id))
                    if raw:
                        data = json.loads(raw) if isinstance(raw, str) else raw
                        if data.get("start_time"):
                            st = datetime.fromisoformat(data["start_time"])
            except Exception as r_err:
                logger.debug("Redis timing fetch notice: %s", r_err)

        if st is None:
            st = await self._fetch_db_task_start_time(task_id)

        if st is None:
            st = et

        norm_et = et.replace(tzinfo=None) if et.tzinfo else et
        norm_st = st.replace(tzinfo=None) if st.tzinfo else st
        duration_seconds = max(0, int((norm_et - norm_st).total_seconds()))

        norm_status = status.upper().strip()
        timing_entry = self._task_timings.setdefault(task_id, {})
        timing_entry["start_time"] = st
        timing_entry["end_time"] = et
        timing_entry["duration_seconds"] = duration_seconds
        timing_entry["status"] = norm_status
        timing_entry["repo_id"] = repo_key

        try:
            r = await self._get_redis()
            if r is not None:
                payload = json.dumps(
                    {
                        "task_id": task_id,
                        "repo_id": repo_key,
                        "start_time": st.isoformat(),
                        "end_time": et.isoformat(),
                        "duration_seconds": duration_seconds,
                        "status": norm_status,
                    }
                )
                await r.set(self._timing_key(task_id), payload, ex=86400)
        except Exception as r_err:
            logger.debug("Redis record_task_end notice: %s", r_err)

        await self._persist_db_task_end(task_id, et, duration_seconds, norm_status)
        logger.info(
            "Recorded terminal state=%s, end_time=%s, duration_seconds=%d for task '%s'",
            norm_status,
            et.isoformat(),
            duration_seconds,
            task_id,
        )
        return et, duration_seconds

    def get_task_timing(self, task_id: str) -> Dict[str, Any]:
        """Retrieve start_time, end_time, and computed duration_seconds for a task."""
        timing = self._task_timings.get(task_id)
        if timing:
            st = timing.get("start_time")
            et = timing.get("end_time")
            dur = timing.get("duration_seconds")
            if dur is None and st is not None:
                norm_now = datetime.utcnow()
                norm_st = st.replace(tzinfo=None) if st.tzinfo else st
                dur = max(0, int((norm_now - norm_st).total_seconds()))
            return {
                "start_time": st,
                "end_time": et,
                "duration_seconds": dur,
                "status": timing.get("status", "RUNNING"),
            }
        return {"start_time": None, "end_time": None, "duration_seconds": None, "status": None}

    async def _persist_db_task_start(self, task_id: str, start_time: datetime) -> None:
        """Persist start_time in the PostgreSQL database Task record."""
        try:
            import uuid
            from sqlalchemy import update
            from services.api.core.database import async_session_factory
            from services.api.models.task import Task

            task_uuid = uuid.UUID(task_id) if isinstance(task_id, str) else task_id
            async with async_session_factory() as session:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_uuid)
                    .values(start_time=start_time, status="running")
                )
                await session.commit()
        except Exception as db_err:
            logger.debug("Database persist start_time skipped: %s", db_err)

    async def _persist_db_task_end(
        self,
        task_id: str,
        end_time: datetime,
        duration_seconds: int,
        status: str,
    ) -> None:
        """Persist end_time and duration_seconds in the PostgreSQL database Task record."""
        try:
            import uuid
            from sqlalchemy import update
            from services.api.core.database import async_session_factory
            from services.api.models.task import Task

            task_uuid = uuid.UUID(task_id) if isinstance(task_id, str) else task_id
            norm_status = status.lower()
            async with async_session_factory() as session:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_uuid)
                    .values(
                        end_time=end_time,
                        duration_seconds=duration_seconds,
                        status=norm_status,
                    )
                )
                await session.commit()
        except Exception as db_err:
            logger.debug("Database persist end_time skipped: %s", db_err)

    async def _fetch_db_task_start_time(self, task_id: str) -> Optional[datetime]:
        """Fetch start_time or created_at from database Task record."""
        try:
            import uuid
            from sqlalchemy import select
            from services.api.core.database import async_session_factory
            from services.api.models.task import Task

            task_uuid = uuid.UUID(task_id) if isinstance(task_id, str) else task_id
            async with async_session_factory() as session:
                res = await session.execute(
                    select(Task.start_time, Task.created_at).where(Task.id == task_uuid)
                )
                row = res.first()
                if row:
                    return row[0] or row[1]
        except Exception as db_err:
            logger.debug("Database fetch start_time skipped: %s", db_err)
        return None

    async def is_engine_busy(self, repo_id: Optional[str] = None) -> bool:
        """Check whether the engine/repository currently has an active task running.

        Args:
            repo_id: Optional repository identifier.

        Returns:
            True if an active task is running, False otherwise.
        """
        repo_key = self.normalize_repo_key(repo_id)

        # 1. Check in-memory active registry
        active = self._in_memory_active.get(repo_key)
        if active is not None:
            # Verify if still alive via RUNNING_TASKS registry
            try:
                from services.api.forge.orchestration.worker import RUNNING_TASKS

                bg_task = RUNNING_TASKS.get(active.task_id)
                if bg_task is not None and bg_task.done():
                    # Task completed in background; clean up stale active record
                    self._in_memory_active.pop(repo_key, None)
                    active = None
                else:
                    return True
            except ImportError:
                return True

        # 2. Check worker RUNNING_TASKS if any task is executing
        try:
            from services.api.forge.orchestration.worker import RUNNING_TASKS

            for tid, t in list(RUNNING_TASKS.items()):
                if not t.done():
                    # If this repo matches or if checking default/global
                    if active is not None and active.task_id == tid:
                        return True
                    # If any task in memory is actively executing for this repo
                    if repo_key in ("default-repo", "global") or not repo_id:
                        return True
        except ImportError:
            pass

        # 3. Check Redis for active task key
        try:
            r = await self._get_redis()
            if r is not None:
                raw_active = await r.get(self._active_key(repo_key))
                if raw_active:
                    data = json.loads(raw_active) if isinstance(raw_active, str) else raw_active
                    task_id = data.get("task_id")
                    # Check if local worker knows it is already done
                    try:
                        from services.api.forge.orchestration.worker import RUNNING_TASKS

                        if task_id in RUNNING_TASKS and RUNNING_TASKS[task_id].done():
                            await r.delete(self._active_key(repo_key))
                            return False
                    except ImportError:
                        pass
                    return True
        except (RedisError, ConnectionError, OSError) as r_err:
            logger.debug("Redis active check fallback to memory: %s", r_err)

        return False

    async def get_active_task(self, repo_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retrieve metadata for the currently executing task on this engine/repo.

        Args:
            repo_id: Optional repository identifier.

        Returns:
            Dictionary with active task details or None if idle.
        """
        repo_key = self.normalize_repo_key(repo_id)

        # 1. In-memory check
        active = self._in_memory_active.get(repo_key)
        if active:
            return active.to_dict()

        # 2. Redis check
        try:
            r = await self._get_redis()
            if r is not None:
                raw = await r.get(self._active_key(repo_key))
                if raw:
                    return json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception as exc:
            logger.debug("Redis get_active_task error: %s", exc)

        return None

    async def acquire_task_lock(
        self,
        task_id: str,
        repo_id: Optional[str] = None,
        prompt: str = "",
        mode: str = "fast",
        allow_queue: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[int]]:
        """Attempt to acquire execution concurrency lock for a task.

        If the engine is currently busy:
        - If `allow_queue` is True: pushes task to the queue and returns (False, queue_position).
        - If `allow_queue` is False: raises `EngineBusyError("Engine is currently busy executing an active task")`.

        Args:
            task_id: Unique task identifier.
            repo_id: Optional repository identifier.
            prompt: Goal and instruction prompt.
            mode: LLM execution mode ('fast' or 'deep').
            allow_queue: If True, enqueues on conflict instead of raising.
            metadata: Optional additional task metadata.

        Returns:
            Tuple of (acquired: bool, queue_position: Optional[int]).
            If acquired immediately: (True, None).
            If queued: (False, position) where position >= 1.

        Raises:
            EngineBusyError: If engine is busy and allow_queue is False.
        """
        repo_key = self.normalize_repo_key(repo_id)
        async_lock = self._get_async_lock(repo_key)

        async with async_lock:
            busy = await self.is_engine_busy(repo_id=repo_id)

            if busy:
                active_meta = await self.get_active_task(repo_id=repo_id)
                active_id = active_meta.get("task_id") if active_meta else None

                if allow_queue:
                    queue_pos = await self.enqueue_task(
                        task_id=task_id,
                        repo_id=repo_key,
                        prompt=prompt,
                        mode=mode,
                        metadata=metadata,
                    )
                    logger.info(
                        "Engine is busy with task '%s'; enqueued task '%s' at position %d for repo '%s'",
                        active_id,
                        task_id,
                        queue_pos,
                        repo_key,
                    )
                    return False, queue_pos

                logger.warning(
                    "Engine is busy with task '%s'; rejecting concurrent task '%s' for repo '%s'",
                    active_id,
                    task_id,
                    repo_key,
                )
                raise EngineBusyError(
                    message="Engine is currently busy executing an active task",
                    active_task_id=active_id,
                    repo_id=repo_key,
                    details={
                        "conflicting_task_id": active_id,
                        "attempted_task_id": task_id,
                        "repo_id": repo_key,
                    },
                )

            # Engine is idle: acquire lock and record active task
            st = datetime.utcnow()
            active_task = ActiveTask(
                task_id=task_id,
                repo_id=repo_key,
                prompt=prompt,
                mode=mode,
                metadata=metadata or {},
                start_time=st.isoformat(),
            )
            self._in_memory_active[repo_key] = active_task

            try:
                r = await self._get_redis()
                if r is not None:
                    payload = json.dumps(active_task.to_dict())
                    await r.set(self._active_key(repo_key), payload, ex=int(self.lock_ttl))
            except Exception as r_err:
                logger.debug("Redis active set error: %s", r_err)

            await self.record_task_start(task_id=task_id, repo_id=repo_key, start_time=st)

            logger.info(
                "Acquired engine concurrency lock for task '%s' on repo '%s'",
                task_id,
                repo_key,
            )
            return True, None

    async def mark_task_running(
        self,
        task_id: str,
        repo_id: Optional[str] = None,
        prompt: str = "",
        mode: str = "fast",
    ) -> None:
        """Explicitly register a task as actively running on the engine."""
        repo_key = self.normalize_repo_key(repo_id)
        st = datetime.utcnow()
        active_task = ActiveTask(
            task_id=task_id,
            repo_id=repo_key,
            prompt=prompt,
            mode=mode,
            start_time=st.isoformat(),
        )
        self._in_memory_active[repo_key] = active_task

        try:
            r = await self._get_redis()
            if r is not None:
                await r.set(
                    self._active_key(repo_key),
                    json.dumps(active_task.to_dict()),
                    ex=int(self.lock_ttl),
                )
        except Exception as r_err:
            logger.debug("Redis mark_task_running failed: %s", r_err)

        await self.record_task_start(task_id=task_id, repo_id=repo_key, start_time=st)

    async def release_task_lock(
        self,
        task_id: str,
        repo_id: Optional[str] = None,
        status: str = "COMPLETED",
    ) -> Optional[Dict[str, Any]]:
        """Release the engine execution lock for a task upon completion or abortion.

        Automatically computes duration_seconds if reaching terminal state and
        pops the next queued task for this repository if one is waiting.

        Args:
            task_id: Task identifier releasing the lock.
            repo_id: Optional repository identifier.
            status: Terminal execution status ('COMPLETED', 'FAILED', or 'ABORTED').

        Returns:
            Dictionary with next queued task details, or None if the queue is empty.
        """
        # Ensure end_time and duration_seconds are computed and persisted if not already recorded
        if task_id:
            timing = self.get_task_timing(task_id)
            if not timing.get("end_time"):
                await self.record_task_end(task_id=task_id, status=status, repo_id=repo_id)
        if repo_id:
            repo_keys = [self.normalize_repo_key(repo_id)]
        else:
            # Discover which repo holds this task_id, or default to all active repos
            discovered = [
                k
                for k, v in list(self._in_memory_active.items())
                if v.task_id == task_id or not task_id
            ]
            repo_keys = discovered if discovered else ["default-repo"]

        next_popped: Optional[Dict[str, Any]] = None

        for repo_key in repo_keys:
            async_lock = self._get_async_lock(repo_key)

            async with async_lock:
                # 1. Release in-memory active task if it matches or if general release
                curr_active = self._in_memory_active.get(repo_key)
                if curr_active is not None and (curr_active.task_id == task_id or not task_id):
                    self._in_memory_active.pop(repo_key, None)
                    logger.info(
                        "Released in-memory engine lock for task '%s' on repo '%s'",
                        task_id,
                        repo_key,
                    )

                # 2. Release in Redis
                try:
                    r = await self._get_redis()
                    if r is not None:
                        raw = await r.get(self._active_key(repo_key))
                        if raw:
                            data = json.loads(raw) if isinstance(raw, str) else raw
                            if not task_id or data.get("task_id") == task_id:
                                await r.delete(self._active_key(repo_key))
                                logger.info(
                                    "Released Redis engine lock for task '%s' on repo '%s'",
                                    task_id,
                                    repo_key,
                                )
                except Exception as r_err:
                    logger.debug("Redis release_task_lock error: %s", r_err)

                # 3. Check for next queued task
                next_task = await self.dequeue_next_task(repo_id=repo_key)
                if next_task:
                    logger.info(
                        "Popped next queued task '%s' for repo '%s'",
                        next_task.get("task_id"),
                        repo_key,
                    )
                    if next_popped is None:
                        next_popped = next_task

        return next_popped

    async def enqueue_task(
        self,
        task_id: str,
        repo_id: Optional[str] = None,
        prompt: str = "",
        mode: str = "fast",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Push a task to the repository's execution queue.

        Args:
            task_id: Unique task identifier.
            repo_id: Target repository identifier.
            prompt: Task instruction prompt.
            mode: LLM execution mode.
            metadata: Optional metadata.

        Returns:
            1-indexed position in the queue.
        """
        repo_key = self.normalize_repo_key(repo_id)
        item = QueuedTask(
            task_id=task_id,
            repo_id=repo_key,
            prompt=prompt,
            mode=mode,
            metadata=metadata or {},
        )

        # 1. Enqueue in-memory
        if repo_key not in self._in_memory_queues:
            self._in_memory_queues[repo_key] = []
        self._in_memory_queues[repo_key].append(item)
        mem_pos = len(self._in_memory_queues[repo_key])

        # 2. Enqueue in Redis
        try:
            r = await self._get_redis()
            if r is not None:
                payload = json.dumps(item.to_dict())
                redis_len = await r.rpush(self._queue_key(repo_key), payload)
                return int(redis_len)
        except Exception as r_err:
            logger.debug("Redis enqueue error, using in-memory position: %s", r_err)

        return mem_pos

    async def dequeue_next_task(self, repo_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Pop the next task from the FIFO execution queue.

        Args:
            repo_id: Target repository identifier.

        Returns:
            Dictionary with the queued task metadata or None if empty.
        """
        repo_key = self.normalize_repo_key(repo_id)

        # 1. Try Redis first
        try:
            r = await self._get_redis()
            if r is not None:
                raw = await r.lpop(self._queue_key(repo_key))
                if raw:
                    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
                    # Also pop matching from in-memory if present
                    if repo_key in self._in_memory_queues and self._in_memory_queues[repo_key]:
                        self._in_memory_queues[repo_key].pop(0)
                    return data
        except Exception as r_err:
            logger.debug("Redis dequeue error, falling back to in-memory: %s", r_err)

        # 2. In-memory queue fallback
        if repo_key in self._in_memory_queues and self._in_memory_queues[repo_key]:
            popped = self._in_memory_queues[repo_key].pop(0)
            return popped.to_dict()

        return None

    async def get_queue(self, repo_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all tasks currently waiting in the execution queue.

        Args:
            repo_id: Target repository identifier.

        Returns:
            List of queued task dictionaries.
        """
        repo_key = self.normalize_repo_key(repo_id)

        try:
            r = await self._get_redis()
            if r is not None:
                raw_items = await r.lrange(self._queue_key(repo_key), 0, -1)
                if raw_items:
                    parsed: List[Dict[str, Any]] = []
                    for item in raw_items:
                        parsed.append(json.loads(item) if isinstance(item, str) else item)
                    return parsed
        except Exception as r_err:
            logger.debug("Redis get_queue error: %s", r_err)

        # In-memory queue
        return [q.to_dict() for q in self._in_memory_queues.get(repo_key, [])]

    async def get_queue_length(self, repo_id: Optional[str] = None) -> int:
        """Return the number of tasks waiting in the execution queue.

        Args:
            repo_id: Target repository identifier.

        Returns:
            Integer queue length.
        """
        repo_key = self.normalize_repo_key(repo_id)

        try:
            r = await self._get_redis()
            if r is not None:
                llen = await r.llen(self._queue_key(repo_key))
                return int(llen)
        except Exception as r_err:
            logger.debug("Redis llen error: %s", r_err)

        return len(self._in_memory_queues.get(repo_key, []))

    async def remove_from_queue(self, task_id: str, repo_id: Optional[str] = None) -> bool:
        """Remove a specific task from the queue before it begins execution."""
        repo_key = self.normalize_repo_key(repo_id)
        removed = False

        # Remove from in-memory
        if repo_key in self._in_memory_queues:
            before_len = len(self._in_memory_queues[repo_key])
            self._in_memory_queues[repo_key] = [
                q for q in self._in_memory_queues[repo_key] if q.task_id != task_id
            ]
            if len(self._in_memory_queues[repo_key]) < before_len:
                removed = True

        # Remove from Redis
        try:
            r = await self._get_redis()
            if r is not None:
                raw_items = await r.lrange(self._queue_key(repo_key), 0, -1)
                for raw in raw_items:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                    if data.get("task_id") == task_id:
                        await r.lrem(self._queue_key(repo_key), 1, raw)
                        removed = True
        except Exception as r_err:
            logger.debug("Redis remove_from_queue error: %s", r_err)

        return removed

    async def clear_all(self, repo_id: Optional[str] = None) -> None:
        """Clear active locks and queues (primarily for testing and teardowns)."""
        if self._redis_client is not None:
            try:
                await self._redis_client.aclose()
            except Exception:
                pass
            self._redis_client = None
            self._redis_loop = None

        if repo_id:
            repo_key = self.normalize_repo_key(repo_id)
            self._in_memory_active.pop(repo_key, None)
            self._in_memory_queues.pop(repo_key, None)
            for tid, tval in list(self._task_timings.items()):
                if tval.get("repo_id") == repo_key:
                    self._task_timings.pop(tid, None)
            try:
                r = await self._get_redis()
                if r is not None:
                    await r.delete(self._active_key(repo_key))
                    await r.delete(self._queue_key(repo_key))
            except Exception:
                pass
        else:
            self._in_memory_active.clear()
            self._in_memory_queues.clear()
            self._async_locks.clear()
            self._task_timings.clear()
            try:
                r = await self._get_redis()
                if r is not None:
                    keys = await r.keys("forge:engine:*")
                    if keys:
                        await r.delete(*keys)
            except Exception:
                pass

    @contextlib.asynccontextmanager
    async def execution_guard(
        self,
        task_id: str,
        repo_id: Optional[str] = None,
        prompt: str = "",
        mode: str = "fast",
        allow_queue: bool = False,
    ) -> AsyncGenerator[Tuple[bool, Optional[int]], None]:
        """Asynchronous context manager enforcing task execution concurrency locking.

        Example:
            async with task_manager.execution_guard(task_id, repo_id) as (acquired, queue_pos):
                await run_graph_and_publish(...)
        """
        acquired, queue_pos = await self.acquire_task_lock(
            task_id=task_id,
            repo_id=repo_id,
            prompt=prompt,
            mode=mode,
            allow_queue=allow_queue,
        )
        terminal_status = "COMPLETED"
        try:
            yield (acquired, queue_pos)
        except asyncio.CancelledError:
            terminal_status = "ABORTED"
            raise
        except Exception:
            terminal_status = "FAILED"
            raise
        finally:
            if acquired:
                await self.release_task_lock(task_id, repo_id=repo_id, status=terminal_status)


# Global singleton execution manager instance
task_manager = TaskExecutionManager()


def get_task_manager() -> TaskExecutionManager:
    """Return the global TaskExecutionManager singleton."""
    return task_manager


__all__ = [
    "ActiveTask",
    "EngineBusyError",
    "QueuedTask",
    "TaskExecutionManager",
    "get_task_manager",
    "task_manager",
]
