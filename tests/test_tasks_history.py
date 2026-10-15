"""Unit and integration tests for GET /api/tasks/history (Task History endpoint)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from services.api.core.database import get_db
from services.api.main import app
from services.api.models.repository import Repository
from services.api.models.task import Task


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return AsyncMock()


@pytest.mark.asyncio
async def test_get_task_history_empty_db_returns_empty_array(mock_db_session):
    """When the database contains no tasks, return an empty array [] without 500 error."""
    mock_res = MagicMock()
    mock_res.all.return_value = []
    mock_db_session.execute.return_value = mock_res

    app.dependency_overrides[get_db] = lambda: mock_db_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks/history")
            assert resp.status_code == 200
            assert resp.json() == []
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_task_history_db_error_returns_empty_array(mock_db_session):
    """When database execution raises an exception, return [] with 200 OK instead of a 500 error."""
    mock_db_session.execute.side_effect = Exception("Simulated database connection failure")

    app.dependency_overrides[get_db] = lambda: mock_db_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks/history")
            assert resp.status_code == 200
            assert resp.json() == []
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_task_history_populated_returns_sorted_items(mock_db_session):
    """When database has tasks, return properly formatted and normalized TaskHistoryItems."""
    tid = uuid.uuid4()
    rid = uuid.uuid4()
    created_at = datetime.now(timezone.utc)

    mock_task = MagicMock(spec=Task)
    mock_task.id = tid
    mock_task.repo_id = rid
    mock_task.prompt = "Fix memory leak in redis connection pool\nDetailed explanation here"
    mock_task.status = "completed"
    mock_task.branch_name = "forge/fix-leak"
    mock_task.retry_count = 1
    mock_task.created_at = created_at
    mock_task.start_time = created_at
    mock_task.end_time = created_at
    mock_task.duration_seconds = 42

    mock_repo = MagicMock(spec=Repository)
    mock_repo.id = rid
    mock_repo.name = "forge-engine"
    mock_repo.local_path = "https://github.com/myorg/forge-engine.git"

    # 1st execute for Task/Repo rows; 2nd execute for TaskEvent PR mapping
    mock_rows_res = MagicMock()
    mock_rows_res.all.return_value = [(mock_task, mock_repo)]

    mock_evt_res = MagicMock()
    mock_evt_res.all.return_value = [
        (tid, {"pr_url": "https://github.com/myorg/forge-engine/pull/12", "merged": True})
    ]

    mock_db_session.execute.side_effect = [mock_rows_res, mock_evt_res]

    app.dependency_overrides[get_db] = lambda: mock_db_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks/history?limit=10")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 1
            item = data[0]
            assert item["task_id"] == str(tid)
            assert item["title"] == "Fix memory leak in redis connection pool"
            assert item["status"] == "MERGED"
            assert item["pr_url"] == "https://github.com/myorg/forge-engine/pull/12"
            assert item["repo_url"] == "https://github.com/myorg/forge-engine.git"
            assert item["duration_seconds"] == 42
    finally:
        app.dependency_overrides.pop(get_db, None)
