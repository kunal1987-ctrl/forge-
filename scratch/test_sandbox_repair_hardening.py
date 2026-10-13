"""Comprehensive test suite for Sandbox Testing, Bounded Repair Loop, and PR Approval Gate hardening."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from services.api.forge.execution.diff_validator import (
    validate_no_test_tampering,
)
from services.api.forge.execution.sandbox import DockerSandbox, TestRunResult
from services.api.forge.execution.worktrees import WorktreeManager
from services.api.forge.orchestration.graph import (
    review_router,
    verify_router,
)
from services.api.forge.orchestration.nodes import (
    verifier_node,
)
from services.api.forge.orchestration.state import AgentState


# ---------------------------------------------------------------------------
# STEP 1: WorktreeManager reset and status tests
# ---------------------------------------------------------------------------


def test_worktree_manager_reset_and_status(tmp_path):
    """Verify reset_worktree restores clean state and get_modified_files detects changes."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text("print('hello')\n")

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    wm = WorktreeManager(str(repo_dir))

    # Verify no modified files initially
    assert wm.get_modified_files(str(repo_dir)) == []

    # Modify an existing file and create an untracked file
    (repo_dir / "app.py").write_text("print('modified')\n")
    (repo_dir / "extra.py").write_text("untracked\n")

    modified = wm.get_modified_files(str(repo_dir))
    assert "app.py" in modified
    assert "extra.py" in modified

    # Perform atomic reset
    wm.reset_worktree(str(repo_dir))

    # Verify pristine state restored
    assert (repo_dir / "app.py").read_text() == "print('hello')\n"
    assert not (repo_dir / "extra.py").exists()
    assert wm.get_modified_files(str(repo_dir)) == []


# ---------------------------------------------------------------------------
# STEP 2: Test Immutability & Anti-Tampering tests
# ---------------------------------------------------------------------------


def test_test_tampering_rejected_on_bugfix():
    """Verify patch modifying tests is rejected if prompt is a bug fix without test request."""
    prompt = "Fix ValueError when processing negative integers in calculate_interest"
    modified_files = ["services/calc.py", "tests/test_calc.py"]

    is_valid, msg = validate_no_test_tampering(modified_files, prompt)
    assert is_valid is False
    assert "REJECTED: You modified existing test files" in msg


def test_test_tampering_allowed_when_prompt_requests_tests():
    """Verify test file modifications are allowed if prompt asks to write/update tests."""
    prompts = [
        "Modify add() to also multiply by 2. Update the tests to reflect this.",
        "Add new feature and write a new test for authentication.",
        "Create tests for payment gateway integration.",
        "Refactor database module with new unit tests.",
    ]
    modified_files = ["services/auth.py", "tests/test_auth.py"]

    for prompt in prompts:
        is_valid, msg = validate_no_test_tampering(modified_files, prompt)
        assert is_valid is True
        assert msg is None


def test_test_tampering_clean_production_code():
    """Verify non-test modifications pass cleanly."""
    prompt = "Fix styling and optimize database indexes."
    modified_files = ["services/db.py", "models/user.py"]

    is_valid, msg = validate_no_test_tampering(modified_files, prompt)
    assert is_valid is True
    assert msg is None


# ---------------------------------------------------------------------------
# STEP 3: Hardened Docker execution & pytest parsing tests
# ---------------------------------------------------------------------------


def test_pytest_parsing_metrics():
    """Verify regex metric extraction for passed, failed, errors, skipped."""
    sandbox = object.__new__(DockerSandbox)

    # 1. Clean pass
    mock_run = MagicMock(
        return_value=MagicMock(
            exit_code=0,
            stdout="=== 5 passed in 0.12s ===",
            stderr="",
            timeout=False,
        )
    )
    sandbox.run_command = mock_run

    res = sandbox.run_tests("/tmp/wt", "pytest")
    assert res["exit_code"] == 0
    assert res["passed_count"] == 5
    assert res["failed_count"] == 0
    assert res["status"] == "passed"

    # 2. Zero tests executed despite exit_code 0 -> Flag as failure with code 5
    mock_run = MagicMock(
        return_value=MagicMock(
            exit_code=0,
            stdout="collected 0 items\n=== no tests ran in 0.01s ===",
            stderr="",
            timeout=False,
        )
    )
    sandbox.run_command = mock_run
    res = sandbox.run_tests("/tmp/wt", "pytest")
    assert res["exit_code"] == 5
    assert res["passed_count"] == 0
    assert res["status"] == "no_tests"
    assert "No tests were executed" in res["error"]

    # 3. Failures in suite
    mock_run = MagicMock(
        return_value=MagicMock(
            exit_code=1,
            stdout="=== 2 failed, 4 passed in 0.40s ===",
            stderr="",
            timeout=False,
        )
    )
    sandbox.run_command = mock_run
    res = sandbox.run_tests("/tmp/wt", "pytest")
    assert res["exit_code"] == 1
    assert res["passed_count"] == 4
    assert res["failed_count"] == 2
    assert res["status"] == "failed"


def test_docker_sandbox_timeout():
    """Verify container timeout returns structured -1 timed_out dict."""
    sandbox = object.__new__(DockerSandbox)

    mock_run = MagicMock(
        return_value=MagicMock(
            exit_code=-1,
            stdout="",
            stderr="Execution timed out after 45s. Possible infinite loop detected.",
            timeout=True,
        )
    )
    sandbox.run_command = mock_run

    res = sandbox.run_tests("/tmp/wt", "pytest", timeout_seconds=45)
    assert res["exit_code"] == -1
    assert res["status"] == "timed_out"
    assert "infinite loop detected" in res["stderr"]


# ---------------------------------------------------------------------------
# STEP 4: Router tests
# ---------------------------------------------------------------------------


def test_verify_router_logic():
    """Verify routing decisions based on exit_code, passed_count, and retry_count."""
    # 1. Passed with at least 1 passing test -> reviewer_node
    state_pass: AgentState = {
        "test_results": {"exit_code": 0, "passed_count": 3},
        "retry_count": 0,
    }
    assert verify_router(state_pass) == "reviewer_node"

    # 2. Exit code 0 but 0 tests passed -> treated as failed, routes to pruner if retries remain
    state_no_tests: AgentState = {
        "test_results": {"exit_code": 0, "passed_count": 0},
        "retry_count": 1,
    }
    assert verify_router(state_no_tests) == "prune_state_node"

    # 3. Failed tests with retries left -> prune_state_node
    state_fail: AgentState = {
        "test_results": {"exit_code": 1, "passed_count": 0},
        "retry_count": 2,
    }
    assert verify_router(state_fail) == "prune_state_node"

    # 4. Failed tests with retries exhausted (>= 3) -> cleanup_node
    state_exhausted: AgentState = {
        "test_results": {"exit_code": 1, "passed_count": 0},
        "retry_count": 3,
    }
    assert verify_router(state_exhausted) == "cleanup_node"


def test_review_router_decisions():
    """Verify human review routing for approve, reject, and cancel."""
    assert review_router({"decision": "approve"}) == "pr_creation_node"
    assert review_router({"decision": "reject"}) == "implementer_node"
    assert review_router({"decision": "cancel"}) == "cleanup_node"
    assert review_router({"decision": "unknown"}) == "cleanup_node"


# ---------------------------------------------------------------------------
# STEP 5: Verifier node execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_node_retry_count_increment():
    """Verify verifier_node records structured metrics and increments retry_count on test failure."""
    with patch("services.api.forge.orchestration.nodes.DockerSandbox") as mock_box_cls:
        mock_instance = MagicMock()
        mock_box_cls.return_value = mock_instance
        # Simulate failed test
        mock_instance.run_tests.return_value = TestRunResult(
            {
                "exit_code": 1,
                "passed_count": 2,
                "failed_count": 1,
                "errors_count": 0,
                "skipped_count": 0,
                "raw_output": "=== 1 failed, 2 passed ===",
                "stdout": "",
                "stderr": "AssertionError",
                "status": "failed",
            }
        )

        state: AgentState = {
            "task_id": "task-123",
            "worktree_path": "/fake/worktree",
            "retry_count": 0,
            "status": "verifying",
        }

        # Mock os.path.exists to True
        with patch("os.path.exists", return_value=True):
            res = await verifier_node(state)

        assert res["retry_count"] == 1
        assert res["status"] == "verifying"
        assert res["test_results"]["exit_code"] == 1
        assert res["test_results"]["passed_count"] == 2
        assert res["test_results"]["failed_count"] == 1
