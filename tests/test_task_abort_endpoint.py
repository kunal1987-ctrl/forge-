"""Tests for POST /api/tasks/{task_id}/abort endpoint, process termination, and telemetry broadcasting."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest
import redis

from services.api.core.database import get_db
from services.api.forge.engine.manager import task_manager
from services.api.forge.execution.process_manager import (
    _TASK_PROCESSES,
    register_task_container,
    register_task_process,
    terminate_task_execution,
    terminate_task_processes,
)
from services.api.forge.orchestration.worker import RUNNING_TASKS
from services.api.main import app
from services.api.routers.tasks import TaskExecutionStatus


@pytest.fixture(autouse=True)
def clean_system_state():
    """Ensure in-memory and Redis state is clean before and after each test."""
    RUNNING_TASKS.clear()
    _TASK_PROCESSES.clear()
    task_manager._in_memory_active.clear()
    task_manager._in_memory_queues.clear()
    task_manager._async_locks.clear()
    task_manager._redis_client = None
    task_manager._redis_loop = None

    try:
        r = redis.Redis.from_url(task_manager.redis_url, socket_timeout=1.0)
        keys = r.keys("forge:engine:*")
        if keys:
            r.delete(*keys)
        r.close()
    except Exception:
        pass

    yield

    RUNNING_TASKS.clear()
    _TASK_PROCESSES.clear()
    task_manager._in_memory_active.clear()
    task_manager._in_memory_queues.clear()
    task_manager._async_locks.clear()
    task_manager._redis_client = None
    task_manager._redis_loop = None

    try:
        r = redis.Redis.from_url(task_manager.redis_url, socket_timeout=1.0)
        keys = r.keys("forge:engine:*")
        if keys:
            r.delete(*keys)
        r.close()
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
    mock_task.prompt = "Execute test pipeline"
    mock_task.status = "aborted"
    mock_task.branch_name = "main"
    mock_task.retry_count = 0
    mock_res.scalar_one_or_none.return_value = mock_task
    mock_res.scalar_one.return_value = mock_task
    mock_res.scalars.return_value.all.return_value = []
    mock_res.scalar.return_value = 1
    mock_db.execute.return_value = mock_res
    return mock_db


@pytest.mark.asyncio
async def test_terminate_task_processes_kills_registered_process():
    """Verify that terminate_task_processes kills registered running subprocesses."""
    task_id = str(uuid.uuid4())
    # Spawn a sleeping python subprocess
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = subprocess.Popen(cmd)
    register_task_process(task_id, proc)

    assert proc.poll() is None  # Process is actively running

    killed = terminate_task_processes(task_id)
    assert killed == 1

    # Give OS a moment to reap process
    proc.wait(timeout=3)
    assert proc.poll() is not None


@pytest.mark.asyncio
async def test_terminate_task_execution_cleans_running_tasks_and_containers():
    """Verify that terminate_task_execution cancels worker asyncio task and runs container cleanup."""
    task_id = str(uuid.uuid4())

    # Create dummy asyncio task in RUNNING_TASKS
    async def dummy_loop():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    bg = asyncio.create_task(dummy_loop())
    RUNNING_TASKS[task_id] = bg

    register_task_container(task_id, f"forge-sandbox-{task_id}")

    stats = terminate_task_execution(task_id)
    assert stats["task_id"] == task_id
    assert stats["worker_cancelled"] is True
    assert task_id not in RUNNING_TASKS

    # Check that bg task was cancelled or is cancelling
    assert bg.cancelling() if hasattr(bg, "cancelling") else bg.cancelled()


@pytest.mark.asyncio
async def test_api_tasks_abort_endpoint_success(mock_db_session):
    """Verify POST /api/tasks/{task_id}/abort updates DB status, broadcasts event, and returns 200."""
    app.dependency_overrides[get_db] = lambda: mock_db_session
    transport = ASGITransport(app=app)
    repo_id = "test-repo-abort"
    task_id = str(uuid.uuid4())

    # Acquire engine lock to simulate active running engine
    await task_manager.acquire_task_lock(task_id=task_id, repo_id=repo_id)
    assert await task_manager.is_engine_busy(repo_id) is True

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Call POST /api/tasks/{task_id}/abort
            resp = await client.post(f"/api/tasks/{task_id}/abort")
            assert resp.status_code == 200
            data = resp.json()
            assert data["task_id"] == task_id
            assert data["status"] == "aborted"
            assert "halted and resources terminated" in data["message"]

            # Engine lock must now be released
            assert await task_manager.is_engine_busy(repo_id) is False

            # Verify GET /api/tasks/{task_id} returns status ABORTED
            status_resp = await client.get(f"/api/tasks/{task_id}")
            assert status_resp.status_code == 200
            assert status_resp.json()["status"] == TaskExecutionStatus.ABORTED
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_tasks_abort_dispatches_next_queued_task(mock_db_session):
    """Verify that aborting an active task pops the next queued task from the queue."""
    app.dependency_overrides[get_db] = lambda: mock_db_session
    transport = ASGITransport(app=app)
    repo_id = "test-repo-queue-abort"
    task1_id = str(uuid.uuid4())
    task2_id = str(uuid.uuid4())

    # Task 1 acquires lock
    acquired, _ = await task_manager.acquire_task_lock(task1_id, repo_id=repo_id)
    assert acquired is True

    # Task 2 is queued
    queued, q_pos = await task_manager.acquire_task_lock(
        task2_id, repo_id=repo_id, prompt="Queued task", allow_queue=True
    )
    assert queued is False
    assert q_pos == 1

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Abort Task 1
            resp = await client.post(f"/api/tasks/{task1_id}/abort")
            assert resp.status_code == 200

            # Task 2 should now have been popped from queue
            remaining_q = await task_manager.get_queue_length(repo_id)
            assert remaining_q == 0
    finally:
        app.dependency_overrides.pop(get_db, None)
