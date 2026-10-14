"""Tests for Task Manager execution duration tracking, timestamps, and terminal state metrics."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
import pytest

from services.api.forge.engine.manager import TaskExecutionManager, task_manager
from services.api.routers.tasks import (
    TaskAbortResponse,
    TaskCreateResponse,
    TaskExecutionStatus,
    TaskHistoryItem,
    TaskRunResponse,
    TaskStatusResponse,
)


@pytest.fixture(autouse=True)
def reset_task_manager():
    """Ensure engine manager locks, timings, and queues are completely reset before and after every test."""
    task_manager._in_memory_active.clear()
    task_manager._in_memory_queues.clear()
    task_manager._async_locks.clear()
    task_manager._task_timings.clear()
    task_manager._redis_client = None
    task_manager._redis_loop = None
    yield
    task_manager._in_memory_active.clear()
    task_manager._in_memory_queues.clear()
    task_manager._async_locks.clear()
    task_manager._task_timings.clear()
    task_manager._redis_client = None
    task_manager._redis_loop = None


@pytest.mark.asyncio
async def test_record_task_start_and_end_completed():
    """Verify start_time and end_time are recorded and duration_seconds computed on COMPLETED."""
    mgr = TaskExecutionManager()
    task_id = str(uuid.uuid4())
    repo_id = "test-repo"

    t0 = datetime(2026, 10, 14, 10, 0, 0)
    t1 = datetime(2026, 10, 14, 10, 2, 5)  # 125 seconds later

    with patch.object(mgr, "_persist_db_task_start", new_callable=AsyncMock) as mock_db_start, \
         patch.object(mgr, "_persist_db_task_end", new_callable=AsyncMock) as mock_db_end:

        # 1. Record task start
        st = await mgr.record_task_start(task_id, repo_id=repo_id, start_time=t0)
        assert st == t0
        mock_db_start.assert_awaited_once_with(task_id, t0)

        # 2. Check in-progress timing
        timing_running = mgr.get_task_timing(task_id)
        assert timing_running["start_time"] == t0
        assert timing_running["end_time"] is None
        assert timing_running["status"] == "RUNNING"

        # 3. Record task completion
        et, dur = await mgr.record_task_end(task_id, status="COMPLETED", end_time=t1, repo_id=repo_id)
        assert et == t1
        assert dur == 125
        mock_db_end.assert_awaited_once_with(task_id, t1, 125, "COMPLETED")

        # 4. Check terminal timing
        timing_done = mgr.get_task_timing(task_id)
        assert timing_done["start_time"] == t0
        assert timing_done["end_time"] == t1
        assert timing_done["duration_seconds"] == 125
        assert timing_done["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_record_task_end_failed_and_aborted():
    """Verify duration tracking for FAILED and ABORTED terminal states."""
    mgr = TaskExecutionManager()

    # Test FAILED
    fail_id = str(uuid.uuid4())
    t0 = datetime(2026, 10, 14, 12, 0, 0)
    t_fail = datetime(2026, 10, 14, 12, 0, 42)  # 42 seconds

    with patch.object(mgr, "_persist_db_task_start", new_callable=AsyncMock), \
         patch.object(mgr, "_persist_db_task_end", new_callable=AsyncMock) as mock_db_fail:
        await mgr.record_task_start(fail_id, start_time=t0)
        et, dur = await mgr.record_task_end(fail_id, status="FAILED", end_time=t_fail)
        assert dur == 42
        mock_db_fail.assert_awaited_once_with(fail_id, t_fail, 42, "FAILED")

    # Test ABORTED
    abort_id = str(uuid.uuid4())
    t_start = datetime(2026, 10, 14, 14, 0, 0)
    t_abort = datetime(2026, 10, 14, 14, 1, 15)  # 75 seconds

    with patch.object(mgr, "_persist_db_task_start", new_callable=AsyncMock), \
         patch.object(mgr, "_persist_db_task_end", new_callable=AsyncMock) as mock_db_abort:
        await mgr.record_task_start(abort_id, start_time=t_start)
        et, dur = await mgr.record_task_end(abort_id, status="ABORTED", end_time=t_abort)
        assert dur == 75
        mock_db_abort.assert_awaited_once_with(abort_id, t_abort, 75, "ABORTED")


@pytest.mark.asyncio
async def test_acquire_and_release_lifecycle_timing():
    """Verify acquiring and releasing concurrency lock records start, end, and duration."""
    mgr = TaskExecutionManager()
    task_id = str(uuid.uuid4())
    repo_id = "test-repo"

    with patch.object(mgr, "_persist_db_task_start", new_callable=AsyncMock), \
         patch.object(mgr, "_persist_db_task_end", new_callable=AsyncMock):

        acquired, _ = await mgr.acquire_task_lock(task_id, repo_id=repo_id)
        assert acquired is True

        timing_mid = mgr.get_task_timing(task_id)
        assert timing_mid["start_time"] is not None
        assert timing_mid["end_time"] is None
        assert timing_mid["duration_seconds"] is not None  # Live duration >= 0

        await mgr.release_task_lock(task_id, repo_id=repo_id, status="COMPLETED")

        timing_final = mgr.get_task_timing(task_id)
        assert timing_final["start_time"] is not None
        assert timing_final["end_time"] is not None
        assert timing_final["duration_seconds"] >= 0
        assert timing_final["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_execution_guard_records_durations():
    """Verify execution_guard context manager sets COMPLETED or FAILED timestamps and duration."""
    mgr = TaskExecutionManager()
    task_id = str(uuid.uuid4())

    with patch.object(mgr, "_persist_db_task_start", new_callable=AsyncMock), \
         patch.object(mgr, "_persist_db_task_end", new_callable=AsyncMock):

        # Successful block
        async with mgr.execution_guard(task_id) as (acquired, _):
            assert acquired is True

        timing = mgr.get_task_timing(task_id)
        assert timing["status"] == "COMPLETED"
        assert timing["end_time"] is not None
        assert timing["duration_seconds"] is not None

        # Failing block
        fail_id = str(uuid.uuid4())
        try:
            async with mgr.execution_guard(fail_id) as (acq, _):
                raise ValueError("Intentional crash")
        except ValueError:
            pass

        timing_fail = mgr.get_task_timing(fail_id)
        assert timing_fail["status"] == "FAILED"
        assert timing_fail["end_time"] is not None
        assert timing_fail["duration_seconds"] is not None


def test_response_models_serialization():
    """Verify all Pydantic response models correctly serialize timestamps and duration_seconds."""
    t0 = datetime(2026, 10, 14, 10, 0, 0)
    t1 = datetime(2026, 10, 14, 10, 3, 20)
    dur = 200

    # 1. TaskStatusResponse
    status_resp = TaskStatusResponse(
        task_id="task-123",
        status=TaskExecutionStatus.COMPLETED,
        start_time=t0,
        end_time=t1,
        duration_seconds=dur,
    )
    data = status_resp.model_dump()
    assert data["start_time"] == t0
    assert data["end_time"] == t1
    assert data["duration_seconds"] == 200

    # 2. TaskCreateResponse
    create_resp = TaskCreateResponse(
        task_id="task-123",
        status="pending",
        start_time=t0,
        end_time=None,
        duration_seconds=0,
    )
    assert create_resp.duration_seconds == 0

    # 3. TaskRunResponse
    run_resp = TaskRunResponse(
        task_id="task-123",
        status="pending",
        start_time=t0,
        end_time=None,
        duration_seconds=0,
    )
    assert run_resp.start_time == t0

    # 4. TaskAbortResponse
    abort_resp = TaskAbortResponse(
        task_id="task-123",
        status="aborted",
        killed_processes=1,
        killed_containers=1,
        start_time=t0,
        end_time=t1,
        duration_seconds=dur,
    )
    assert abort_resp.duration_seconds == 200

    # 5. TaskHistoryItem
    hist_item = TaskHistoryItem(
        task_id="task-123",
        title="Test Task",
        repo_url="https://github.com/test/repo",
        status="MERGED",
        created_at=t0.isoformat(),
        start_time=t0.isoformat(),
        end_time=t1.isoformat(),
        duration_seconds=dur,
    )
    assert hist_item.duration_seconds == 200
