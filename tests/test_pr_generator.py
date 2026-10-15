"""Unit and integration tests for PRGenerator and pr_generator_node.

Verifies:
1. Authenticated Git Remote Push:
   - Remote origin URL is configured with https://x-access-token:${GITHUB_TOKEN}@github.com/{owner}/{repo}.git
   - git push -u origin forge-fix-{task_id} is executed inside ephemeral worktree
2. GitHub REST API PR Creation via httpx:
   - POST to https://api.github.com/repos/{owner}/{repo}/pulls with title, body, head, base
   - Capture PR web URL and save directly to state["pr_url"]
   - Raise explicit GitHubAuthError if token is missing or unauthorized (401/403)
3. Credential Redaction:
   - GITHUB_TOKEN is masked in all command strings, stdout, stderr, and exception messages
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.api.core.config import settings
from services.api.forge.exceptions import GitHubAuthError
from services.api.forge.execution.pr_generator import PRGenerator, pr_generator_node
from services.api.forge.orchestration.state import AgentState


def test_parse_owner_and_repo_various_formats(tmp_path):
    """Verify owner and repo extraction from various formats and git remote."""
    # 1. "owner/repo" format
    o, r = PRGenerator.parse_owner_and_repo("myorg/myrepo")
    assert o == "myorg"
    assert r == "myrepo"

    # 2. "owner/repo.git"
    o, r = PRGenerator.parse_owner_and_repo("myorg/myrepo.git")
    assert o == "myorg"
    assert r == "myrepo"

    # 3. Full HTTPS GitHub URL
    o, r = PRGenerator.parse_owner_and_repo("https://github.com/facebook/react.git")
    assert o == "facebook"
    assert r == "react"

    # 4. SSH GitHub URL
    o, r = PRGenerator.parse_owner_and_repo("git@github.com:torvalds/linux.git")
    assert o == "torvalds"
    assert r == "linux"

    # 5. Extract from git remote origin in worktree
    repo_dir = tmp_path / "repo_remote"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/awesome-lib.git"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )
    with patch.dict(os.environ, {"GITHUB_REPOSITORY": "kunal1987-ctrl/forge-"}):
        o, r = PRGenerator.parse_owner_and_repo(None, str(repo_dir))
    assert o == "acme"
    assert r == "awesome-lib"


def test_configure_authenticated_remote_sets_token_url(tmp_path):
    """Verify configure_authenticated_remote injects GITHUB_TOKEN into origin remote URL."""
    repo_dir = tmp_path / "repo_auth"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)

    generator = PRGenerator(github_token="ghp_SecretToken123456789")
    auth_url = generator.configure_authenticated_remote(
        worktree_path=str(repo_dir),
        owner="kunal1987-ctrl",
        repo="forge-",
        task_id="task-auth-test",
        branch_name="forge-fix-task-auth-test",
    )

    expected_url = (
        "https://x-access-token:ghp_SecretToken123456789@github.com/kunal1987-ctrl/forge-.git"
    )
    assert auth_url == expected_url

    # Check git remote get-url origin
    res = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == expected_url

    # Update existing remote
    generator2 = PRGenerator(github_token="ghp_NewToken987654321")
    auth_url2 = generator2.configure_authenticated_remote(
        worktree_path=str(repo_dir),
        owner="kunal1987-ctrl",
        repo="forge-",
        task_id="task-auth-test",
        branch_name="forge-fix-task-auth-test",
    )
    expected_url2 = (
        "https://x-access-token:ghp_NewToken987654321@github.com/kunal1987-ctrl/forge-.git"
    )
    assert auth_url2 == expected_url2


@pytest.mark.asyncio
async def test_create_github_pull_request_via_httpx_success():
    """Verify create_github_pull_request makes POST request with correct payload and returns html_url."""
    generator = PRGenerator(github_token="ghp_ValidTokenForPR")

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "id": 101,
        "number": 42,
        "html_url": "https://github.com/kunal1987-ctrl/forge-/pull/42",
        "state": "open",
    }

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=404))
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        pr_url = await generator.create_github_pull_request(
            owner="kunal1987-ctrl",
            repo="forge-",
            title="fix: resolve zero division in calculator",
            body="## Context\nBug fix.",
            head_branch="forge-fix-task-100",
            base_branch="main",
            task_id="task-100",
        )

        assert pr_url == "https://github.com/kunal1987-ctrl/forge-/pull/42"
        mock_client.post.assert_awaited_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == "https://api.github.com/repos/kunal1987-ctrl/forge-/pulls"
        assert kwargs["headers"]["Authorization"] == "Bearer " + generator.github_token
        assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
        assert kwargs["json"]["head"] == "forge-fix-task-100"
        assert kwargs["json"]["base"] == "main"
        assert kwargs["json"]["title"] == "fix: resolve zero division in calculator"


@pytest.mark.asyncio
async def test_create_github_pull_request_unauthorized_raises_github_auth_error():
    """Verify HTTP 401/403 from GitHub REST API raises actionable GitHubAuthError."""
    generator = PRGenerator(github_token="ghp_BadOrExpiredToken")

    mock_resp_401 = MagicMock()
    mock_resp_401.status_code = 401
    mock_resp_401.text = "Bad credentials"
    mock_resp_401.json.return_value = {"message": "Bad credentials"}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=404))
    mock_client.post = AsyncMock(return_value=mock_resp_401)
    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GitHubAuthError) as exc_info:
            await generator.create_github_pull_request(
                owner="kunal1987-ctrl",
                repo="forge-",
                title="fix: test",
                body="test body",
                head_branch="forge-fix-123",
                base_branch="main",
            )

        assert "authentication failed (HTTP 401)" in str(exc_info.value)
        assert exc_info.value.error_code == "GITHUB_AUTH_ERROR"

    # Test HTTP 403 Forbidden
    mock_resp_403 = MagicMock()
    mock_resp_403.status_code = 403
    mock_resp_403.text = "Resource not accessible by integration"
    mock_resp_403.json.return_value = {"message": "Resource not accessible by integration"}

    mock_client.post.return_value = mock_resp_403
    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GitHubAuthError) as exc_info_403:
            await generator.create_github_pull_request(
                owner="kunal1987-ctrl",
                repo="forge-",
                title="fix: test",
                body="test body",
                head_branch="forge-fix-123",
                base_branch="main",
            )

        assert "authentication failed (HTTP 403)" in str(exc_info_403.value)
        assert exc_info_403.value.error_code == "GITHUB_AUTH_ERROR"


@pytest.mark.asyncio
async def test_missing_github_token_raises_github_auth_error():
    """Verify empty or missing GITHUB_TOKEN raises GitHubAuthError."""
    generator = PRGenerator(github_token="")

    with pytest.raises(GitHubAuthError, match="GitHub API token.*is missing"):
        await generator.create_github_pull_request(
            owner="kunal1987-ctrl",
            repo="forge-",
            title="fix: test",
            body="test body",
            head_branch="forge-fix-123",
            base_branch="main",
        )


@pytest.mark.asyncio
async def test_create_pr_checks_out_branch_commits_stages_and_pushes(tmp_path):
    worktree = tmp_path / "wt_pr_commands"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Tester"], cwd=str(worktree), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@forge.local"],
        cwd=str(worktree),
        capture_output=True,
        check=True,
    )
    (worktree / "existing.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(worktree), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(worktree),
        capture_output=True,
        check=True,
    )
    (worktree / "existing.py").write_text("print('fixed')\n", encoding="utf-8")

    generator = PRGenerator(github_token="ghp_ValidTokenForPR")
    pr_url = "https://github.com/acme/project/pull/1"
    with (
        patch.object(generator, "run_git_command", wraps=generator.run_git_command) as git_command,
        patch.object(generator, "push_branch") as push_branch,
        patch.object(
            generator,
            "create_github_pull_request",
            new_callable=AsyncMock,
            return_value=pr_url,
        ) as create_request,
    ):
        result = await generator.create_pr(
            worktree_path=str(worktree),
            task_id="task-pr-branch",
            summary="repair parser",
            repo_name="acme/project",
        )

    commands = [entry.args[0] for entry in git_command.call_args_list]
    assert ["git", "checkout", "-b", "forge-fix-task-pr-branch"] in commands
    assert ["git", "add", "."] in commands
    assert ["git", "commit", "-m", "fix: repair parser"] in commands
    push_branch.assert_called_once()
    assert push_branch.call_args.kwargs["branch_name"] == "forge-fix-task-pr-branch"
    create_request.assert_awaited_once()
    assert result == pr_url


def test_credential_redaction_in_git_command_errors(tmp_path):
    """Verify GITHUB_TOKEN is redacted if git commands output errors."""
    token = "ghp_SuperSecretCredential9999"
    generator = PRGenerator(github_token=token)

    raw_err = f"fatal: unable to access 'https://x-access-token:{token}@github.com/org/repo.git/': 403 Forbidden"
    sanitized = generator.sanitize_message(raw_err)
    assert token not in sanitized
    assert "[REDACTED]" in sanitized


@pytest.mark.asyncio
async def test_pr_generator_node_sets_state_pr_url(tmp_path):
    """Verify pr_generator_node sets state['pr_url'] directly and returns pr_url."""
    worktree = tmp_path / "wt_node_success"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Tester"], cwd=str(worktree), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "tester@forge.local"],
        cwd=str(worktree),
        capture_output=True,
        check=True,
    )
    (worktree / "test.py").write_text("# initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(worktree), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True, check=True
    )

    state: AgentState = {
        "task_id": "task-test-pr-state",
        "repo_path": str(worktree),
        "worktree_path": str(worktree),
        "issue_description": "Fix memory leak",
        "test_results": {"exit_code": 0, "passed_count": 3},
        "retry_count": 0,
        "summary": "Fix memory leak",
        "pr_url": "",
    }

    mock_pr_url = "https://github.com/kunal1987-ctrl/forge-/pull/777"
    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_TestTokenForNode"}),
        patch.object(PRGenerator, "create_pr", return_value=mock_pr_url),
    ):
        result = await pr_generator_node(state)

    assert result["status"] == "pr_created"
    assert result["pr_url"] == mock_pr_url
    # REQUIREMENT 2: verify PR URL is saved directly into state["pr_url"]
    assert state["pr_url"] == mock_pr_url


@pytest.mark.asyncio
async def test_pr_generator_node_missing_token_raises_or_records_github_auth_error(tmp_path):
    """Verify pr_generator_node raises or records GitHubAuthError if GITHUB_TOKEN is missing."""
    worktree = tmp_path / "wt_node_no_token"
    worktree.mkdir()

    state: AgentState = {
        "task_id": "task-no-token-node",
        "repo_path": str(worktree),
        "worktree_path": str(worktree),
        "test_results": {"exit_code": 0, "passed_count": 1},
        "retry_count": 0,
        "pr_url": "",
    }

    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        patch.object(settings, "GITHUB_TOKEN", "", create=True),
    ):
        # 1. Non-raising mode (returns failed and populates error_payload)
        res = await pr_generator_node(state, raise_on_error=False)
        assert res["status"] == "failed"
        assert state["error_payload"]["type"] == "GitHubAuthError"
        assert "GITHUB_TOKEN" in state["error_payload"]["message"]

        # 2. Raising mode (raises GitHubAuthError)
        with pytest.raises(GitHubAuthError):
            await pr_generator_node(state, raise_on_error=True)
