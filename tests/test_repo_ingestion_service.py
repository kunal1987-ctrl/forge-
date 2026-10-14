"""Unit and integration tests for repository ingestion service.

Verifies:
1. Subprocess git clone wrapper with --single-branch and 60s timeout.
2. Automated branch discrepancy fallback probing (main -> master -> default).
3. Sanitization of Git stderr (stripping credentials and local filesystem paths).
4. Safe rmtree cleanup handling Windows read-only file locks.
5. Structured GitIngestionError translation to clean HTTP 400 responses.
6. Guaranteed try...finally cleanup of ephemeral worktrees on failure.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
import pytest

from services.api.main import app
from services.api.forge.services.ingestion import (
    GitIngestionError,
    IngestionPipeline,
    safe_git_clone,
    safe_rmtree,
    sanitize_git_error,
)


# ==============================================================================
# 1. ERROR SANITIZATION & WORKTREE CLEANUP TESTS
# ==============================================================================


def test_sanitize_git_error_strips_credentials_and_local_paths():
    """Verify sanitize_git_error strips sensitive tokens/passwords and local paths."""
    raw_err = (
        "fatal: repository 'https://username:ghp_supersecret1234567890@github.com/org/repo.git' not found\n"
        "fatal: unable to access 'C:\\Users\\admin\\AppData\\Local\\Temp\\forge_ingest_xyz123': Permission denied\n"
    )
    target_dir = "C:\\Users\\admin\\AppData\\Local\\Temp\\forge_ingest_xyz123"

    sanitized = sanitize_git_error(raw_err, target_dir)

    assert "ghp_supersecret1234567890" not in sanitized
    assert "https://github.com/org/repo.git" in sanitized
    assert "C:\\Users\\admin" not in sanitized
    assert "[ephemeral_worktree]" in sanitized


def test_safe_rmtree_removes_readonly_files():
    """Verify safe_rmtree removes directories containing read-only files (Windows git objects)."""
    temp_dir = tempfile.mkdtemp(prefix="test_rmtree_")
    test_file = os.path.join(temp_dir, "readonly.txt")

    with open(test_file, "w") as f:
        f.write("test content")

    # Mark file as read-only
    os.chmod(test_file, stat.S_IREAD)

    assert os.path.exists(temp_dir)
    safe_rmtree(temp_dir)
    assert not os.path.exists(temp_dir)


# ==============================================================================
# 2. ROBUST SUBPROCESS GIT CLONE WRAPPER & BRANCH PROBING
# ==============================================================================


def test_safe_git_clone_success_on_requested_branch():
    """Verify safe_git_clone succeeds when requested branch exists."""
    target_dir = tempfile.mkdtemp(prefix="test_clone_")
    try:
        def mock_subprocess_run(cmd, *args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "rev-parse" in cmd:
                m.stdout = "abc1234567890\n"
            return m

        with patch("subprocess.run", side_effect=mock_subprocess_run) as mock_run:
            sha, branch = safe_git_clone("https://github.com/org/repo.git", target_dir, branch="dev")

            assert sha == "abc1234567890"
            assert branch == "dev"
            # Verify single-branch and timeout arguments
            first_call_cmd = mock_run.call_args_list[0][0][0]
            assert "--single-branch" in first_call_cmd
            assert "-b" in first_call_cmd
            assert "dev" in first_call_cmd
            assert mock_run.call_args_list[0][1]["timeout"] == 60
            assert mock_run.call_args_list[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    finally:
        safe_rmtree(target_dir)


def test_safe_git_clone_falls_back_to_main_then_master():
    """Verify safe_git_clone falls back to 'main' then 'master' when requested branch fails."""
    target_dir = tempfile.mkdtemp(prefix="test_clone_fallback_")
    try:
        attempts: list[str] = []

        def mock_subprocess_run(cmd, *args, **kwargs):
            m = MagicMock()
            if "clone" in cmd:
                branch_idx = cmd.index("-b") + 1 if "-b" in cmd else None
                b = cmd[branch_idx] if branch_idx else "default"
                attempts.append(b)
                if b == "unknown-feature":
                    m.returncode = 128
                    m.stderr = "fatal: Remote branch unknown-feature not found in upstream origin\n"
                    return m
                if b == "main":
                    m.returncode = 128
                    m.stderr = "fatal: Remote branch main not found in upstream origin\n"
                    return m
                if b == "master":
                    m.returncode = 0
                    m.stderr = ""
                    return m
            if "rev-parse" in cmd:
                m.returncode = 0
                m.stdout = "master-sha-999\n"
                return m
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            sha, resolved_branch = safe_git_clone(
                "https://github.com/org/repo.git", target_dir, branch="unknown-feature"
            )

            assert attempts == ["unknown-feature", "main", "master"]
            assert sha == "master-sha-999"
            assert resolved_branch == "master"
    finally:
        safe_rmtree(target_dir)


def test_safe_git_clone_omitted_branch_probes_main_first():
    """Verify safe_git_clone probes 'main' then 'master' when branch is omitted."""
    target_dir = tempfile.mkdtemp(prefix="test_clone_omitted_")
    try:
        attempts: list[str] = []

        def mock_subprocess_run(cmd, *args, **kwargs):
            m = MagicMock()
            if "clone" in cmd:
                b = cmd[cmd.index("-b") + 1] if "-b" in cmd else "default"
                attempts.append(b)
                m.returncode = 0
                return m
            if "rev-parse" in cmd:
                m.returncode = 0
                m.stdout = "main-sha-111\n"
                return m
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            sha, resolved_branch = safe_git_clone("https://github.com/org/repo.git", target_dir, branch=None)

            assert attempts == ["main"]
            assert sha == "main-sha-111"
            assert resolved_branch == "main"
    finally:
        safe_rmtree(target_dir)


def test_safe_git_clone_all_attempts_exhausted_raises_gitingestionerror():
    """Verify GitIngestionError is raised with clean message when all branch attempts fail."""
    target_dir = tempfile.mkdtemp(prefix="test_clone_fail_")
    try:
        def mock_subprocess_run(cmd, *args, **kwargs):
            m = MagicMock()
            m.returncode = 128
            m.stderr = (
                "fatal: repository 'https://user:secret@github.com/org/private-repo.git' not found\n"
                "fatal: clone failed at C:\\Users\\user\\AppData\\Local\\Temp\\test_dir\n"
            )
            return m

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            with pytest.raises(GitIngestionError) as exc_info:
                safe_git_clone("https://github.com/org/private-repo.git", target_dir, branch="feature")

            err = exc_info.value
            assert err.default_code == "GIT_INGESTION_ERROR"
            assert "secret" not in str(err)
            assert "C:\\Users" not in str(err)
            assert "Failed to clone repository" in str(err)
    finally:
        safe_rmtree(target_dir)


def test_safe_git_clone_timeout_raises_gitingestionerror():
    """Verify GitIngestionError is raised when git clone exceeds 60s timeout."""
    target_dir = tempfile.mkdtemp(prefix="test_clone_timeout_")
    try:
        def mock_subprocess_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            with pytest.raises(GitIngestionError) as exc_info:
                safe_git_clone("https://github.com/org/repo.git", target_dir, branch="main", timeout=60)

            err = exc_info.value
            assert "timed out after 60 seconds" in str(err)
            assert err.error_code == "GIT_CLONE_TIMEOUT"
    finally:
        safe_rmtree(target_dir)


# ==============================================================================
# 3. EPHEMERAL WORKTREE TRY...FINALLY CLEANUP
# ==============================================================================


@pytest.mark.asyncio
async def test_ingest_repository_cleans_ephemeral_worktree_on_failure():
    """Verify ephemeral worktree directory is safely wiped via try...finally even if parsing crashes."""
    mock_session = AsyncMock()
    mock_session.flush = AsyncMock(side_effect=RuntimeError("Database connection lost"))
    pipeline = IngestionPipeline(mock_session)

    created_dirs: list[str] = []

    def mock_safe_clone(repo_url, target_dir, branch=None, timeout=60):
        created_dirs.append(str(target_dir))
        with open(os.path.join(target_dir, "dummy.py"), "w") as f:
            f.write("def crash(): pass\n")
        return "sha-12345", "main"

    with patch("services.api.forge.services.ingestion.safe_git_clone", side_effect=mock_safe_clone):
        with pytest.raises(RuntimeError) as exc_info:
            await pipeline.ingest_repository("https://github.com/org/repo.git", branch="main")

        assert "Database connection lost" in str(exc_info.value)
        # Verify the created ephemeral directory was wiped by the finally block
        assert len(created_dirs) == 1
        assert not os.path.exists(created_dirs[0])


# ==============================================================================
# 4. HTTP 400 TRANSLATION VIA REPOSITORIES ENDPOINT
# ==============================================================================


@pytest.mark.asyncio
async def test_api_ingest_repository_git_error_returns_clean_400():
    """Verify POST /api/repositories/ingest translates GitIngestionError into a clean HTTP 400 response."""
    transport = ASGITransport(app=app)
    req_id = "test-git-400-req"

    mock_lock_cm = AsyncMock()

    with (
        patch("services.api.routers.repositories.async_repo_lock", return_value=mock_lock_cm),
        patch(
            "services.api.forge.services.ingestion.safe_git_clone",
            side_effect=GitIngestionError(
                message="Failed to clone repository 'https://github.com/org/nonexistent.git': Repository not found.",
                repo_url="https://github.com/org/nonexistent.git",
                branch="main",
            ),
        ),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/repositories/ingest",
                json={"repo_url": "https://github.com/org/nonexistent.git", "branch": "main"},
                headers={"x-request-id": req_id},
            )

        assert resp.status_code == 400
        data = resp.json()
        assert data["status"] == 400
        assert data["error_code"] == "BAD_REQUEST"
        assert "Repository not found" in data["message"]
        assert data["request_id"] == req_id
        assert resp.headers.get("x-content-type-options") == "nosniff"
