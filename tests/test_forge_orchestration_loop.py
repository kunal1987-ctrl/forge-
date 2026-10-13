"""Comprehensive test suite for FORGE End-to-End Orchestration Loop:
1. Strict AgentState Contract & Edge Routing (graph.py, state.py)
2. Surgical Patch Application & Repo Validation (patch_applier.py)
3. Verifiable PR Generation & Git CLI Verification (pr_generator.py)
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from services.api.forge.exceptions import PatchApplicationError, PRCreationError
from services.api.forge.execution.patch_applier import SurgicalPatchApplier
from services.api.forge.execution.pr_generator import PRGenerator, pr_generator_node
from services.api.forge.orchestration.graph import (
    create_initial_agent_state,
    verify_router,
    workflow,
)
from services.api.forge.orchestration.state import (
    AgentState,
    validate_agent_state_contract,
)


# ===========================================================================
# PILLAR 1: STRICT STATE CONTRACT & EDGE ROUTING
# ===========================================================================


def test_agent_state_mandatory_contract_keys():
    """Verify AgentState contract defines and validates all mandatory keys."""
    # 1. create_initial_agent_state initializes all 8 mandatory keys
    state = create_initial_agent_state(
        task_id="task-orch-1",
        prompt="Fix division by zero",
        worktree_path="/tmp/test_wt",
    )
    assert "repo_path" in state
    assert "task_id" in state
    assert "issue_description" in state
    assert "patch" in state
    assert "test_results" in state
    assert "retry_count" in state
    assert "error_classification" in state
    assert "pr_url" in state

    assert state["repo_path"] == "/tmp/test_wt"
    assert state["task_id"] == "task-orch-1"
    assert state["issue_description"] == "Fix division by zero"
    assert state["patch"] == ""
    assert state["test_results"] == {}
    assert state["retry_count"] == 0
    assert state["error_classification"] == ""
    assert state["pr_url"] == ""

    # 2. validate_agent_state_contract succeeds on compliant state
    validate_agent_state_contract(state)

    # 3. validate_agent_state_contract raises TypeError on invalid types
    bad_state = dict(state)
    bad_state["retry_count"] = "not-an-int"  # type: ignore[assignment]
    with pytest.raises(TypeError, match="AgentState contract violation: key 'retry_count'"):
        validate_agent_state_contract(bad_state)  # type: ignore[arg-type]


def test_verify_router_success_routes_to_pr_generator():
    """Verify verify_router routes to pr_generator when exit_code == 0."""
    state: AgentState = {
        "test_results": {"exit_code": 0, "passed_count": 4},
        "retry_count": 0,
    }
    dest = verify_router(state)
    assert dest == "pr_generator"
    # Backward compatibility alias
    assert dest == "reviewer_node"


def test_verify_router_failure_under_budget_increments_and_routes_to_implementer():
    """Verify verify_router increments retry_count and routes to implementer with error classification."""
    state: AgentState = {
        "test_results": {
            "exit_code": 1,
            "passed_count": 0,
            "raw_output": "AssertionError: assert 1 == 2",
        },
        "retry_count": 1,
    }
    dest = verify_router(state)
    assert dest == "implementer"
    # Backward compatibility alias
    assert dest == "prune_state_node"
    assert state["retry_count"] == 2
    assert state.get("error_classification") == "ASSERTION_FAILURE"
    assert state.get("error_payload", {}).get("type") == "ASSERTION_FAILURE"


def test_verify_router_failure_over_budget_routes_to_failed():
    """Verify verify_router gracefully terminates by routing to failed when retry_count >= 3."""
    state: AgentState = {
        "test_results": {
            "exit_code": 1,
            "passed_count": 0,
            "raw_output": "SyntaxError: invalid syntax",
        },
        "retry_count": 3,
    }
    dest = verify_router(state)
    assert dest == "failed"
    # Backward compatibility alias
    assert dest == "cleanup_node"
    assert state.get("error_classification") == "SYNTAX_ERROR"


def test_graph_has_pr_generator_node():
    """Verify pr_generator node is registered in the compiled workflow."""
    nodes = workflow.nodes
    assert "pr_generator" in nodes
    assert "verifier_node" in nodes
    assert "pr_creation_node" in nodes


# ===========================================================================
# PILLAR 2: SURGICAL PATCH APPLICATION & REPO VALIDATION
# ===========================================================================


def test_patch_applier_validates_repo_file_and_search_block_existence(tmp_path):
    """Verify validate_patch checks target file and search block existence before writing to disk."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sample_file = repo_dir / "calculator.py"
    sample_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    applier = SurgicalPatchApplier(repo_root=repo_dir)

    # 1. Valid patch
    valid_patch = (
        "File: calculator.py\n"
        "<<<<<<< SEARCH\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "=======\n"
        "def add(a, b):\n"
        "    return int(a) + int(b)\n"
        ">>>>>>> REPLACE\n"
    )
    is_valid, err = applier.validate_patch(valid_patch)
    assert is_valid is True
    assert err is None

    # 2. Target file missing in repository
    missing_file_patch = (
        "File: nonexistent.py\n"
        "<<<<<<< SEARCH\n"
        "def foo(): pass\n"
        "=======\n"
        "def foo(): return 1\n"
        ">>>>>>> REPLACE\n"
    )
    is_valid_mf, err_mf = applier.validate_patch(missing_file_patch)
    assert is_valid_mf is False
    assert isinstance(err_mf, PatchApplicationError)
    assert "Target file does not exist in repository" in str(err_mf)

    # 3. Search block does not exist verbatim or fuzzy
    bad_search_patch = (
        "File: calculator.py\n"
        "<<<<<<< SEARCH\n"
        "def subtract(x, y):\n"
        "    return x - y\n"
        "=======\n"
        "def subtract(x, y):\n"
        "    return int(x) - int(y)\n"
        ">>>>>>> REPLACE\n"
    )
    is_valid_bs, err_bs = applier.validate_patch(bad_search_patch)
    assert is_valid_bs is False
    assert isinstance(err_bs, PatchApplicationError)
    assert "Search block does not exist in target file" in str(err_bs)


def test_patch_applier_apply_rejects_malformed_patch_without_hallucinating(tmp_path):
    """Verify apply() returns PatchApplicationError and does not touch repository on failure."""
    repo_dir = tmp_path / "repo_apply"
    repo_dir.mkdir()
    sample_file = repo_dir / "core.py"
    initial_content = "def run():\n    return 42\n"
    sample_file.write_text(initial_content, encoding="utf-8")

    applier = SurgicalPatchApplier(repo_root=repo_dir)

    malformed_patch = (
        "File: core.py\n"
        "<<<<<<< SEARCH\n"
        "def non_existent_function():\n"
        "    pass\n"
        "=======\n"
        "def run(): return 100\n"
        ">>>>>>> REPLACE\n"
    )

    res = applier.apply(malformed_patch)
    assert res.success is False
    assert res.exception is not None
    assert isinstance(res.exception, PatchApplicationError)

    # Ensure repository file was NOT modified
    assert sample_file.read_text(encoding="utf-8") == initial_content


# ===========================================================================
# PILLAR 3: VERIFIABLE PR GENERATION
# ===========================================================================


@pytest.mark.asyncio
async def test_pr_generator_node_rejects_execution_if_verifier_failed():
    """Verify pr_generator_node rejects execution if exit_code != 0."""
    state: AgentState = {
        "task_id": "task-pr-fail",
        "test_results": {"exit_code": 1, "passed_count": 0, "stderr": "Tests failed"},
        "retry_count": 1,
    }

    # By default, sets status to failed without raising
    res = await pr_generator_node(state, raise_on_error=False)
    assert res["status"] == "failed"
    assert state["error_payload"]["type"] == "PRCreationError"
    assert "Cannot generate PR: test suite did not pass" in res["error_message"]

    # When raise_on_error=True, raises typed PRCreationError
    with pytest.raises(PRCreationError, match="Cannot generate PR: test suite did not pass"):
        await pr_generator_node(state, raise_on_error=True)


@pytest.mark.asyncio
async def test_pr_generator_missing_github_token_raises_typed_error(tmp_path):
    """Verify PRGenerator raises PRCreationError if GitHub API token is missing."""
    worktree = tmp_path / "wt_pr"
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
    (worktree / "file.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(worktree), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True, check=True
    )

    generator = PRGenerator(github_token="")

    with pytest.raises(PRCreationError, match="GitHub API token.*is missing"):
        generator.create_pr(
            worktree_path=str(worktree),
            task_id="task-no-token",
            summary="Test fix",
        )


@pytest.mark.asyncio
async def test_pr_generator_node_successful_run(tmp_path):
    """Verify pr_generator_node runs git operations and returns verified pr_url to state."""
    worktree = tmp_path / "wt_pr_success"
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
    (worktree / "file.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(worktree), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True, check=True
    )

    state: AgentState = {
        "task_id": "task-pr-good",
        "repo_path": str(worktree),
        "worktree_path": str(worktree),
        "issue_description": "Fix memory leak in buffer",
        "test_results": {"exit_code": 0, "passed_count": 5},
        "retry_count": 0,
        "summary": "Fix memory leak in buffer",
    }

    mock_pr_url = "https://github.com/kunal1987-ctrl/forge-/pull/42"
    with patch.object(PRGenerator, "create_pr", return_value=mock_pr_url):
        res = await pr_generator_node(state)

    assert res["status"] == "pr_created"
    assert res["pr_url"] == mock_pr_url
