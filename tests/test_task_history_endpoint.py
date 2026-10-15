"""Tests for GET /api/tasks/history endpoint and task history normalization."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from services.api.core.database import get_db
from services.api.main import app
from services.api.models.repository import Repository
from services.api.models.task import Task
from services.api.routers.tasks import _task_runtime_is_stale, format_relative_time
from services.api.forge.orchestration.worker import RUNNING_TASKS


def test_format_relative_time():
    """Verify relative time formatting helper returns clean readable strings."""
    now = datetime.now(timezone.utc)
    assert format_relative_time(None) == "just now"
    assert format_relative_time(now - timedelta(seconds=20)) == "just now"
    assert format_relative_time(now - timedelta(minutes=5)) == "5m ago"
    assert format_relative_time(now - timedelta(hours=2)) == "2h ago"
    assert format_relative_time(now - timedelta(days=3)) == "3d ago"


def test_task_runtime_stale_after_worker_hard_timeout():
    now = datetime.now(timezone.utc)
    stale_task = MagicMock(spec=Task)
    stale_task.start_time = now - timedelta(minutes=6)
    stale_task.created_at = now - timedelta(minutes=6)
    fresh_task = MagicMock(spec=Task)
    fresh_task.start_time = now - timedelta(minutes=2)
    fresh_task.created_at = now - timedelta(minutes=2)

    assert _task_runtime_is_stale(stale_task, now) is True
    assert _task_runtime_is_stale(fresh_task, now) is False


@pytest.mark.asyncio
async def test_get_task_history_returns_ordered_normalized_list():
    """Verify GET /api/tasks/history queries database and returns normalized items ordered desc."""
    repo_id = uuid.uuid4()
    mock_repo = MagicMock(spec=Repository)
    mock_repo.id = repo_id
    mock_repo.name = "fastapi-demo"
    mock_repo.local_path = "https://github.com/demo/fastapi-demo.git"

    # Task 1: Merged task with PR URL
    t1_id = uuid.uuid4()
    t1 = MagicMock(spec=Task)
    t1.id = t1_id
    t1.repo_id = repo_id
    t1.prompt = "Fix memory leak in websocket loop\nDetailed extra instructions"
    t1.status = "completed"
    t1.branch_name = "forge/task-1"
    t1.retry_count = 0
    t1.created_at = datetime.now(timezone.utc) - timedelta(minutes=15)

    # Task 2: Running task
    t2_id = uuid.uuid4()
    t2 = MagicMock(spec=Task)
    t2.id = t2_id
    t2.repo_id = repo_id
    t2.prompt = "Add health probe metrics to gRPC client"
    t2.status = "running"
    t2.branch_name = "forge/task-2"
    t2.retry_count = 1
    t2.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    # Task 3: Failed task
    t3_id = uuid.uuid4()
    t3 = MagicMock(spec=Task)
    t3.id = t3_id
    t3.repo_id = repo_id
    t3.prompt = "Migrate database schemas to v2"
    t3.status = "failed"
    t3.branch_name = "forge/task-3"
    t3.retry_count = 3
    t3.created_at = datetime.now(timezone.utc) - timedelta(hours=1)

    mock_db = AsyncMock()

    # First execute returns the list of (Task, Repository) rows
    tasks_res = MagicMock()
    tasks_res.all.return_value = [
        (t2, mock_repo),
        (t1, mock_repo),
        (t3, mock_repo),
    ]

    # Second execute returns the events for extracting PR URLs and merge statuses
    events_res = MagicMock()
    events_res.all.return_value = [
        (t1_id, {"pr_url": "https://github.com/demo/fastapi-demo/pull/42", "merged": True}),
        (t2_id, {"node": "planner_node"}),
    ]

    mock_db.execute.side_effect = [tasks_res, events_res]

    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks/history?limit=10")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 3

            # Check Task 2 (latest)
            item_t2 = data[0]
            assert item_t2["task_id"] == str(t2_id)
            assert item_t2["title"] == "Add health probe metrics to gRPC client"
            assert item_t2["status"] == "RUNNING"
            assert item_t2["repo_url"] == "https://github.com/demo/fastapi-demo.git"

            # Check Task 1 (merged)
            item_t1 = data[1]
            assert item_t1["task_id"] == str(t1_id)
            assert item_t1["title"] == "Fix memory leak in websocket loop"
            assert item_t1["status"] == "MERGED"
            assert item_t1["pr_url"] == "https://github.com/demo/fastapi-demo/pull/42"

            # Check Task 3 (failed)
            item_t3 = data[2]
            assert item_t3["task_id"] == str(t3_id)
            assert item_t3["title"] == "Migrate database schemas to v2"
            assert item_t3["status"] == "FAILED"
            assert item_t3["pr_url"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_task_history_empty_returns_empty_list():
    """Verify GET /api/tasks/history returns [] when database has no tasks."""
    mock_db = AsyncMock()
    empty_res = MagicMock()
    empty_res.all.return_value = []
    mock_db.execute.return_value = empty_res

    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks/history")
            assert resp.status_code == 200
            data = resp.json()
            assert data == []
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_status", ["running", "pending"])
async def test_task_status_returns_prompt_plan_and_stops_stale_state(raw_status):
    task_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    task = MagicMock(spec=Task)
    task.id = task_id
    task.repo_id = uuid.uuid4()
    task.prompt = "Fix division by zero in calculator.py"
    task.status = raw_status
    task.branch_name = f"forge/task-{task_id}"
    task.retry_count = 0
    task.created_at = now - timedelta(hours=6)
    task.start_time = now - timedelta(hours=6)
    task.end_time = None
    task.duration_seconds = None

    steps = [{"step": 1, "title": "Fix division", "description": "Guard the divisor."}]
    legacy_plan = {
        "target_files": ["calculator.py"],
        "logic_steps": ["Guard the divisor."],
        "test_strategy": "pytest",
    }
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    event_result = MagicMock()
    event_result.scalars.return_value.all.return_value = [
        SimpleNamespace(
            payload={
                "node": "planner_node",
                "data": {"execution_plan": steps, "plan": legacy_plan},
            }
        )
    ]
    mock_db = AsyncMock()
    mock_db.execute.side_effect = [task_result, event_result]

    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/tasks/{task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "FAILED"
        assert data["prompt"] == task.prompt
        assert data["execution_plan"] == steps
        assert data["plan"] == legacy_plan
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_status", "with_local_worker", "expected_status"),
    [
        ("running", True, "ABORTED"),
        ("running", False, "FAILED"),
        ("pending", False, "FAILED"),
    ],
)
async def test_task_history_stops_stale_execution(
    monkeypatch,
    raw_status,
    with_local_worker,
    expected_status,
):
    task_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    task = MagicMock(spec=Task)
    task.id = task_id
    task.repo_id = uuid.uuid4()
    task.prompt = "Fix a stuck task"
    task.status = raw_status
    task.branch_name = f"forge/task-{task_id}"
    task.retry_count = 0
    task.created_at = now - timedelta(hours=6)
    task.start_time = now - timedelta(hours=6)
    task.end_time = None
    task.duration_seconds = None

    repo = MagicMock(spec=Repository)
    repo.name = "test-repo"
    repo.local_path = "https://github.com/example/test-repo.git"

    class StaleWorker:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    worker = StaleWorker() if with_local_worker else None
    task_result = MagicMock()
    task_result.all.return_value = [(task, repo)]
    events_result = MagicMock()
    events_result.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute.side_effect = [task_result, events_result, MagicMock()]
    if worker is not None:
        monkeypatch.setitem(RUNNING_TASKS, str(task_id), worker)
    revoke_task = AsyncMock()
    monkeypatch.setattr(
        "services.api.routers.tasks._revoke_celery_task",
        revoke_task,
    )
    monkeypatch.setattr(
        "services.api.forge.execution.process_manager.terminate_task_execution",
        lambda _: {"killed_processes": 0, "killed_containers": 0},
    )
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/tasks/history?limit=10")

        assert response.status_code == 200
        assert response.json()[0]["status"] == expected_status
        if worker is not None:
            assert worker.cancelled
        assert str(task_id) not in RUNNING_TASKS
        revoke_task.assert_awaited_once_with(str(task_id))
        mock_db.commit.assert_awaited_once()
        update_statement = mock_db.execute.await_args_list[-1].args[0]
        assert update_statement.get_execution_options()["synchronize_session"] is False
        assert task.status == expected_status.lower()
    finally:
        RUNNING_TASKS.pop(str(task_id), None)
        app.dependency_overrides.pop(get_db, None)
