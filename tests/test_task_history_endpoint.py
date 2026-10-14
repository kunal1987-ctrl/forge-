"""Tests for GET /api/tasks/history endpoint and task history normalization."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import uuid
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from services.api.core.database import get_db
from services.api.main import app
from services.api.models.repository import Repository
from services.api.models.task import Task
from services.api.routers.tasks import format_relative_time


def test_format_relative_time():
    """Verify relative time formatting helper returns clean readable strings."""
    now = datetime.now(timezone.utc)
    assert format_relative_time(None) == "just now"
    assert format_relative_time(now - timedelta(seconds=20)) == "just now"
    assert format_relative_time(now - timedelta(minutes=5)) == "5m ago"
    assert format_relative_time(now - timedelta(hours=2)) == "2h ago"
    assert format_relative_time(now - timedelta(days=3)) == "3d ago"


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
