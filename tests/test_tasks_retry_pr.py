"""Tests for the PR Retry endpoint POST /api/tasks/{task_id}/retry-pr (Pillar 3)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
import pytest

from services.api.core.database import get_db
from services.api.forge.exceptions import GitHubAuthError, PRCreationError
from services.api.main import app


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_task = MagicMock()
    mock_task.id = uuid.uuid4()
    mock_task.repo_id = uuid.uuid4()
    mock_task.prompt = "Fix async socket timeout and add exponential backoff"
    mock_task.status = "completed"
    mock_task.branch_name = "main"
    mock_task.retry_count = 0
    mock_res.scalar_one_or_none.return_value = mock_task
    mock_db.execute.return_value = mock_res
    return mock_db


@pytest.mark.asyncio
async def test_retry_pr_missing_github_token_returns_400(mock_db_session, monkeypatch):
    """Verify that omitting GITHUB_TOKEN immediately returns HTTP 400 with descriptive error."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("services.api.core.config.settings.GITHUB_TOKEN", "", raising=False)

    app.dependency_overrides[get_db] = lambda: mock_db_session
    task_id = str(uuid.uuid4())

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/tasks/{task_id}/retry-pr")
            assert resp.status_code == 400
            data = resp.json()
            error_message = data.get("detail", "") or data.get("message", "")
            assert "PR Creation Failed" in error_message
            assert "GITHUB_TOKEN" in error_message
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_retry_pr_success_recreates_pr(mock_db_session, monkeypatch):
    """Verify that with valid token and branch, POST /api/tasks/{task_id}/retry-pr returns 200 with new pr_url."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_valid_test_token_12345")

    app.dependency_overrides[get_db] = lambda: mock_db_session
    task_id = str(uuid.uuid4())
    expected_pr_url = f"https://github.com/kunal1987-ctrl/forge-/pull/{task_id[:6]}"

    with (
        patch("services.api.forge.execution.pr_generator.PRGenerator.create_pr") as mock_create_pr,
        patch("redis.asyncio.from_url") as mock_redis_from_url,
    ):
        mock_create_pr.return_value = expected_pr_url
        mock_redis = AsyncMock()
        mock_redis_from_url.return_value = mock_redis

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/tasks/{task_id}/retry-pr")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "pr_created"
            assert data["pr_url"] == expected_pr_url
            assert data["task_id"] == task_id
            assert "message" in data

        # Verify PRGenerator was called with task_id
        mock_create_pr.assert_called_once()
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_retry_pr_github_auth_error_returns_401(mock_db_session, monkeypatch):
    """Verify that a GitHub authentication error raises HTTP 401 with descriptive error message."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_invalid_token")

    app.dependency_overrides[get_db] = lambda: mock_db_session
    task_id = str(uuid.uuid4())

    with patch("services.api.forge.execution.pr_generator.PRGenerator.create_pr") as mock_create_pr:
        mock_create_pr.side_effect = GitHubAuthError(
            "GitHub API authentication failed (HTTP 401): Bad credentials",
            task_id=task_id,
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/tasks/{task_id}/retry-pr")
            assert resp.status_code == 401
            data = resp.json()
            error_message = data.get("detail", "") or data.get("message", "")
            assert "PR Creation Failed" in error_message
            assert "Bad credentials" in error_message

    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_retry_pr_git_creation_error_returns_400(mock_db_session, monkeypatch):
    """Verify that git command failure raises HTTP 400 with descriptive error message."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_valid_token")

    app.dependency_overrides[get_db] = lambda: mock_db_session
    task_id = str(uuid.uuid4())

    with patch("services.api.forge.execution.pr_generator.PRGenerator.create_pr") as mock_create_pr:
        mock_create_pr.side_effect = PRCreationError(
            "Remote rejected branch push: protected branch",
            task_id=task_id,
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/tasks/{task_id}/retry-pr")
            assert resp.status_code == 400
            data = resp.json()
            error_message = data.get("detail", "") or data.get("message", "")
            assert "PR Creation Failed" in error_message
            assert "protected branch" in error_message

    app.dependency_overrides.pop(get_db, None)
