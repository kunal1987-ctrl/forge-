"""Comprehensive tests for TaskExecutionManager, Distributed Concurrency Locking, and Task Execution Queue.

Verifies:
1. Concurrency Lock & Busy State:
   - Only one task can execute at a time per engine/repository.
   - Overlapping runs return clean HTTP 409 Conflict with detail:
     "Engine is currently busy executing an active task".
2. Task Execution Queue:
   - When queue=True is requested, concurrent tasks are queued in FIFO order (HTTP 202).
   - Queue inspection returns accurate positions and task metadata.
3. Lock Release Lifecycle:
   - Cancellation (POST /api/tasks/{id}/cancel) releases the lock.
   - Approval (POST /api/tasks/{id}/approve) releases the lock.
   - Pipeline completion releases the lock and automatically pops the next queued task.
4. Repository Isolation:
   - Tasks executing on distinct repositories run independently without false conflicts.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
import pytest
from fastapi import status

from services.api.core.database import get_db
from services.api.forge.engine.manager import (
    EngineBusyError,
    TaskExecutionManager,
    task_manager,
)
from services.api.main import app


@pytest.fixture(autouse=True)
def reset_task_manager():
    """Ensure engine manager locks and queues are completely reset before and after every test."""
    import redis

    task_manager._in_memory_active.clear()
    task_manager._in_memory_queues.clear()
    task_manager._async_locks.clear()
    task_manager._redis_client = None
    task_manager._redis_loop = None

    try:
        sync_redis = redis.from_url(task_manager.redis_url, decode_responses=True)
        keys = sync_redis.keys("forge:engine:*")
        if keys:
            sync_redis.delete(*keys)
        sync_redis.close()
    except Exception:
        pass

    yield

    task_manager._in_memory_active.clear()
    task_manager._in_memory_queues.clear()
    task_manager._async_locks.clear()
    task_manager._redis_client = None
    task_manager._redis_loop = None

    try:
        sync_redis = redis.from_url(task_manager.redis_url, decode_responses=True)
        keys = sync_redis.keys("forge:engine:*")
        if keys:
            sync_redis.delete(*keys)
        sync_redis.close()
    except Exception:
        pass


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_task = MagicMock()
    mock_task.id = uuid.uuid4()
    mock_task.repo_id = uuid.uuid4()
    mock_task.prompt = "Fix race condition in manager.py"
    mock_task.status = "running"
    mock_task.branch_name = "main"
    mock_task.retry_count = 0
    mock_res.scalar_one_or_none.return_value = mock_task
    mock_res.scalar.return_value = 1
    mock_db.execute.return_value = mock_res
    return mock_db


# ==============================================================================
# UNIT TESTS: TaskExecutionManager Concurrency Lock & Queue
# ==============================================================================


@pytest.mark.asyncio
async def test_manager_single_task_lock_acquisition_and_release():
    """Verify single task acquires lock, marks engine busy, and releases cleanly."""
    mgr = TaskExecutionManager()
    repo_id = "test-repo-1"
    task_id = str(uuid.uuid4())

    assert await mgr.is_engine_busy(repo_id) is False

    # 1. Acquire lock
    acquired, queue_pos = await mgr.acquire_task_lock(
        task_id=task_id,
        repo_id=repo_id,
        prompt="Fix socket bug",
        mode="fast",
        allow_queue=False,
    )
    assert acquired is True
    assert queue_pos is None
    assert await mgr.is_engine_busy(repo_id) is True

    active = await mgr.get_active_task(repo_id)
    assert active is not None
    assert active["task_id"] == task_id

    # 2. Release lock
    next_task = await mgr.release_task_lock(task_id, repo_id=repo_id)
    assert next_task is None
    assert await mgr.is_engine_busy(repo_id) is False
    assert await mgr.get_active_task(repo_id) is None


@pytest.mark.asyncio
async def test_manager_concurrent_task_raises_engine_busy_error():
    """Verify second task on busy engine raises EngineBusyError with exact required message."""
    mgr = TaskExecutionManager()
    repo_id = "test-repo-concurrency"
    task_1 = str(uuid.uuid4())
    task_2 = str(uuid.uuid4())

    await mgr.acquire_task_lock(task_1, repo_id=repo_id, prompt="Task 1")

    # Second concurrent task without queueing must raise EngineBusyError
    with pytest.raises(EngineBusyError) as exc_info:
        await mgr.acquire_task_lock(
            task_2,
            repo_id=repo_id,
            prompt="Task 2",
            allow_queue=False,
        )

    assert "Engine is currently busy executing an active task" in str(exc_info.value)
    assert exc_info.value.active_task_id == task_1


@pytest.mark.asyncio
async def test_manager_queueing_on_busy_engine():
    """Verify concurrent task with allow_queue=True is placed in queue with correct position."""
    mgr = TaskExecutionManager()
    repo_id = "test-repo-queue"
    task_1 = str(uuid.uuid4())
    task_2 = str(uuid.uuid4())
    task_3 = str(uuid.uuid4())

    # Task 1 executes
    await mgr.acquire_task_lock(task_1, repo_id=repo_id, prompt="Active Task 1")

    # Task 2 and 3 are queued
    acq_2, pos_2 = await mgr.acquire_task_lock(
        task_2, repo_id=repo_id, prompt="Queued Task 2", allow_queue=True
    )
    assert acq_2 is False
    assert pos_2 == 1

    acq_3, pos_3 = await mgr.acquire_task_lock(
        task_3, repo_id=repo_id, prompt="Queued Task 3", allow_queue=True
    )
    assert acq_3 is False
    assert pos_3 == 2

    assert await mgr.get_queue_length(repo_id) == 2
    queued_list = await mgr.get_queue(repo_id)
    assert len(queued_list) == 2
    assert queued_list[0]["task_id"] == task_2
    assert queued_list[1]["task_id"] == task_3

    # Releasing Task 1 automatically pops Task 2
    next_task = await mgr.release_task_lock(task_1, repo_id=repo_id)
    assert next_task is not None
    assert next_task["task_id"] == task_2
    assert await mgr.get_queue_length(repo_id) == 1

    # Releasing Task 2 pops Task 3
    next_task_2 = await mgr.release_task_lock(task_2, repo_id=repo_id)
    assert next_task_2 is not None
    assert next_task_2["task_id"] == task_3
    assert await mgr.get_queue_length(repo_id) == 0


@pytest.mark.asyncio
async def test_manager_repository_isolation():
    """Verify that distinct repositories maintain independent locks and do not block each other."""
    mgr = TaskExecutionManager()
    repo_a = "repo-alpha"
    repo_b = "repo-beta"
    task_a = str(uuid.uuid4())
    task_b = str(uuid.uuid4())

    acq_a, _ = await mgr.acquire_task_lock(task_a, repo_id=repo_a, prompt="Task on Repo A")
    assert acq_a is True
    assert await mgr.is_engine_busy(repo_a) is True

    # Repo B is idle and can acquire immediately
    assert await mgr.is_engine_busy(repo_b) is False
    acq_b, _ = await mgr.acquire_task_lock(task_b, repo_id=repo_b, prompt="Task on Repo B")
    assert acq_b is True
    assert await mgr.is_engine_busy(repo_b) is True


@pytest.mark.asyncio
async def test_manager_execution_guard_context_manager():
    """Verify execution_guard context manager releases lock even when exception is raised."""
    mgr = TaskExecutionManager()
    repo_id = "test-repo-guard"
    task_id = str(uuid.uuid4())

    with pytest.raises(RuntimeError):
        async with mgr.execution_guard(task_id, repo_id=repo_id, prompt="Failing task"):
            assert await mgr.is_engine_busy(repo_id) is True
            raise RuntimeError("Simulated pipeline failure")

    # Lock must be released cleanly after context manager exit
    assert await mgr.is_engine_busy(repo_id) is False


# ==============================================================================
# INTEGRATION TESTS: HTTP POST /api/tasks/run CONCURRENCY LOCK & QUEUE
# ==============================================================================


@pytest.mark.asyncio
async def test_api_tasks_run_idle_success(mock_db_session):
    """Verify POST /api/tasks/run returns HTTP 202 when engine is idle."""
    app.dependency_overrides[get_db] = lambda: mock_db_session
    transport = ASGITransport(app=app)

    try:
        with patch("services.api.routers.tasks.run_graph_and_publish"):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/tasks/run",
                    json={"prompt": "Fix database connection leak", "mode": "fast"},
                )
                assert resp.status_code == status.HTTP_202_ACCEPTED
                data = resp.json()
                assert "task_id" in data
                assert data["status"] == "pending"
                assert data["queue_position"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_tasks_run_busy_returns_409_conflict(mock_db_session):
    """Verify POST /api/tasks/run returns HTTP 409 Conflict with exact required message when engine is busy."""
    app.dependency_overrides[get_db] = lambda: mock_db_session
    transport = ASGITransport(app=app)
    repo_id = "repo-concurrency-test"

    # Simulate an active task currently running on this repo
    active_task_id = str(uuid.uuid4())
    await task_manager.acquire_task_lock(
        task_id=active_task_id,
        repo_id=repo_id,
        prompt="Currently running task",
    )
    assert await task_manager.is_engine_busy(repo_id) is True

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/tasks/run",
                json={
                    "prompt": "Second overlapping task",
                    "repo_id": repo_id,
                    "mode": "fast",
                    "queue": False,
                },
            )
            assert resp.status_code == status.HTTP_409_CONFLICT
            data = resp.json()

            # Message or detail must match exact required phrasing
            msg = data.get("detail", "") or data.get("message", "")
            assert "Engine is currently busy executing an active task" in msg
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_tasks_run_busy_with_queue_returns_202_queued(mock_db_session):
    """Verify POST /api/tasks/run with queue=True enqueues task and returns 202 with queue_position."""
    app.dependency_overrides[get_db] = lambda: mock_db_session
    transport = ASGITransport(app=app)
    repo_id = "repo-queue-test"

    # Simulate active running task
    active_task_id = str(uuid.uuid4())
    await task_manager.acquire_task_lock(
        task_id=active_task_id,
        repo_id=repo_id,
        prompt="Running active task",
    )

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/tasks/run",
                json={
                    "prompt": "Queued task behind active execution",
                    "repo_id": repo_id,
                    "mode": "fast",
                    "queue": True,
                },
            )
            assert resp.status_code == status.HTTP_202_ACCEPTED
            data = resp.json()
            assert data["status"] == "queued"
            assert data["queue_position"] == 1
            assert "task_id" in data

            # Verify queue inspection endpoint
            queue_resp = await client.get(f"/api/tasks/queue?repo_id={repo_id}")
            assert queue_resp.status_code == status.HTTP_200_OK
            q_data = queue_resp.json()
            assert q_data["is_busy"] is True
            assert q_data["queue_length"] == 1
            assert q_data["queued_tasks"][0]["task_id"] == data["task_id"]
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_task_cancel_releases_concurrency_lock(mock_db_session):
    """Verify POST /api/tasks/{id}/cancel releases engine lock and allows subsequent task run."""
    app.dependency_overrides[get_db] = lambda: mock_db_session
    transport = ASGITransport(app=app)
    repo_id = "repo-cancel-lock-test"
    task_id = str(uuid.uuid4())

    await task_manager.acquire_task_lock(
        task_id=task_id,
        repo_id=repo_id,
        prompt="Task to be cancelled",
    )
    assert await task_manager.is_engine_busy(repo_id) is True

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Cancel running task
            cancel_resp = await client.post(f"/api/tasks/{task_id}/cancel")
            assert cancel_resp.status_code == status.HTTP_200_OK

            # Engine lock must now be released
            assert await task_manager.is_engine_busy(repo_id) is False

            # 2. Subsequent task can now run immediately without 409 Conflict
            with patch("services.api.routers.tasks.run_graph_and_publish"):
                resp2 = await client.post(
                    "/api/tasks/run",
                    json={"prompt": "Next task after cancellation", "repo_id": repo_id},
                )
                assert resp2.status_code == status.HTTP_202_ACCEPTED
                assert resp2.json()["status"] == "pending"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_task_approve_releases_concurrency_lock(mock_db_session):
    """Verify POST /api/tasks/{id}/approve releases engine lock and allows subsequent task run."""
    app.dependency_overrides[get_db] = lambda: mock_db_session
    mock_db_session.get.return_value = MagicMock(status="awaiting_approval")
    transport = ASGITransport(app=app)
    repo_id = "repo-approve-lock-test"
    task_id = str(uuid.uuid4())

    await task_manager.acquire_task_lock(
        task_id=task_id,
        repo_id=repo_id,
        prompt="Task awaiting approval",
    )
    assert await task_manager.is_engine_busy(repo_id) is True

    try:
        # Mock compiled graph to avoid invoking real LangGraph
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"status": "completed"}
        mock_graph.aupdate_state.return_value = {"configurable": {"thread_id": task_id}}
        mock_graph.aget_state.side_effect = [
            SimpleNamespace(next=("reviewer_node",), interrupts=(), tasks=()),
            SimpleNamespace(next=("reviewer_node",), interrupts=(), tasks=()),
            SimpleNamespace(next=(), interrupts=(), tasks=()),
        ]

        with patch(
            "services.api.routers.tasks.get_compiled_graph", AsyncMock(return_value=mock_graph)
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                approve_resp = await client.post(
                    f"/api/tasks/{task_id}/approve",
                    json={"decision": "approve", "feedback": "LGTM"},
                )
                assert approve_resp.status_code == status.HTTP_200_OK

                # Engine lock must now be released
                assert await task_manager.is_engine_busy(repo_id) is False

                # Subsequent task run succeeds immediately
                with patch("services.api.routers.tasks.run_graph_and_publish"):
                    run_resp = await client.post(
                        "/api/tasks/run",
                        json={"prompt": "Next task after approval", "repo_id": repo_id},
                    )
                    assert run_resp.status_code == status.HTTP_202_ACCEPTED
                    assert run_resp.json()["status"] == "pending"
    finally:
        app.dependency_overrides.pop(get_db, None)
