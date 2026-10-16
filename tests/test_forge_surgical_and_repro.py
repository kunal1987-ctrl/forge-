"""Comprehensive tests for FORGE Surgical Patch Parser, Reproduction Gate, Lint Cleanup, and Rich PR Generation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.api.forge.execution.github import GitHubIntegration
from services.api.forge.execution.patch_applier import (
    SurgicalPatchApplier,
)
from services.api.forge.execution.worktrees import WorktreeManager, validate_patch_format
from services.api.forge.orchestration.nodes import reproduction_gate_node, verifier_node
from services.api.forge.orchestration.state import AgentState


# ===========================================================================
# 1. STEP 1: SURGICAL SEARCH/REPLACE PARSER & FUZZY MATCHING TESTS
# ===========================================================================


def test_surgical_parser_blocks_extraction():
    """Verify parser extracts semantic <<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE blocks."""
    patch_text = """
### File: services/calc.py
<<<<<<< SEARCH
def compute(x):
    return x * 10
=======
def compute(x):
    return x * 20
>>>>>>> REPLACE

### File: services/helper.py
<<<<<<< SEARCH
VAL = 1
=======
VAL = 2
>>>>>>> REPLACE
"""
    assert SurgicalPatchApplier.is_search_replace_patch(patch_text)
    blocks = SurgicalPatchApplier.parse_blocks(patch_text)
    assert len(blocks) == 2
    assert blocks[0].target_file == "services/calc.py"
    assert "return x * 10" in blocks[0].search_content
    assert "return x * 20" in blocks[0].replace_content
    assert blocks[1].target_file == "services/helper.py"
    assert blocks[1].search_content == "VAL = 1"
    assert blocks[1].replace_content == "VAL = 2"


def test_surgical_fuzzy_whitespace_and_indentation_matching(tmp_path):
    """Verify fuzzy matching handles trailing whitespace, newline variance, and flexible whitespace."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    target_file = repo_dir / "service.py"

    # Original content has extra trailing spaces and slightly different indentation
    target_file.write_text(
        "class Worker:\n    def run(self):\n        step_one()  \n        step_two()\n        return True\n",
        encoding="utf-8",
    )

    patch_text = """
File: service.py
<<<<<<< SEARCH
    def run(self):
        step_one()
        step_two()
=======
    def run(self):
        step_one()
        step_two_fixed()
>>>>>>> REPLACE
"""
    applier = SurgicalPatchApplier(repo_root=repo_dir)
    result = applier.apply(patch_text)
    assert result.success
    assert result.applied_blocks == 1
    assert "service.py" in result.modified_files

    updated_code = target_file.read_text(encoding="utf-8")
    assert "step_two_fixed()" in updated_code


def test_worktree_manager_surgical_primary_with_fallback(tmp_path):
    """Verify WorktreeManager uses SurgicalPatchApplier as primary, falling back to git apply."""
    repo_dir = tmp_path / "wt_repo"
    repo_dir.mkdir()
    (repo_dir / "math_mod.py").write_text("def mul(a, b):\n    return a + b\n")

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeTester"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    wm = WorktreeManager(str(repo_dir), base_worktree_dir=str(tmp_path / "worktrees"))
    wt_path = wm.create_worktree("task-primary-1")

    # 1. Primary: Surgical SEARCH/REPLACE
    surgical_patch = """
File: math_mod.py
<<<<<<< SEARCH
def mul(a, b):
    return a + b
=======
def mul(a, b):
    return a * b
>>>>>>> REPLACE
"""
    assert validate_patch_format(surgical_patch)
    p_res = wm.apply_patch(wt_path, surgical_patch)
    assert p_res.success
    assert "surgical" in p_res.message.lower()
    assert (Path(wt_path) / "math_mod.py").read_text() == "def mul(a, b):\n    return a * b\n"

    # 2. Standard diff fallback
    standard_diff = """--- a/math_mod.py
+++ b/math_mod.py
@@ -1,2 +1,3 @@
 def mul(a, b):
+    # multi-line doc
     return a * b
"""
    assert validate_patch_format(standard_diff)
    d_res = wm.apply_patch(wt_path, standard_diff)
    assert d_res.success

    wm.cleanup("task-primary-1")


# ===========================================================================
# 2. STEP 2: REPRODUCTION TEST ENGINE TESTS
# ===========================================================================


@pytest.mark.asyncio
async def test_reproduction_gate_isolates_defect(tmp_path):
    """Verify reproduction_gate_node writes test, verifies failure, and marks verified."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    state: AgentState = {
        "task_id": "task-repro-1",
        "worktree_path": str(worktree),
        "prompt": "Fix zero division bug in divide()",
        "plan": {
            "reproduction_test_code": (
                "from calculator import divide\n\n"
                "def test_divide_returns_quotient():\n"
                "    assert divide(10, 2) == 5\n"
            )
        },
        "status": "investigating",
    }

    mock_run = MagicMock(
        return_value={
            "exit_code": 1,
            "passed_count": 0,
            "failed_count": 1,
            "stderr": "Failure confirmed",
        }
    )
    with patch("services.api.forge.execution.sandbox.DockerSandbox.run_tests", mock_run):
        res = await reproduction_gate_node(state)

    assert res["reproduction_verified"] is True
    assert res["status"] in ("repro_verified", "reproduction_verified")
    assert res["repro_verified_failing"] is True
    repro_file = worktree / "tests" / "_forge_repro_test.py"
    assert repro_file.exists()
    assert "test_divide_returns_quotient" in repro_file.read_text()


@pytest.mark.asyncio
async def test_verifier_node_runs_repro_and_suite(tmp_path):
    """Verify verifier_node requires both reproduction test and full suite to pass, saving verification_log."""
    worktree = tmp_path / "worktree_ver"
    worktree.mkdir()
    tests_dir = worktree / "tests"
    tests_dir.mkdir()
    (tests_dir / "_forge_repro_test.py").write_text("def test_repro(): pass\n")

    state: AgentState = {
        "task_id": "task-ver-1",
        "worktree_path": str(worktree),
        "retry_count": 0,
        "status": "implementing",
    }

    def mock_tests(wt, cmd, timeout_seconds=30, auto_detect=False, task_id=None):
        if "_forge_repro_test.py" in cmd:
            return {
                "exit_code": 0,
                "passed_count": 1,
                "failed_count": 0,
                "stdout": "tests/_forge_repro_test.py: 1 passed",
            }
        return {
            "exit_code": 0,
            "passed_count": 5,
            "failed_count": 0,
            "stdout": "pytest: 5 passed in 0.2s",
        }

    with patch(
        "services.api.forge.execution.sandbox.DockerSandbox.run_tests", side_effect=mock_tests
    ):
        res = await verifier_node(state)

    assert res["status"] == "verified"
    assert "verification_log" in res
    assert "REPRODUCTION TEST VERIFICATION" in res["verification_log"]
    assert "FULL TEST SUITE VERIFICATION" in res["verification_log"]
    assert res["retry_count"] == 0


# ===========================================================================
# 3. STEP 3: LINT & CLEANUP GATE TESTS
# ===========================================================================


def test_worktree_lint_and_cleanup_gate(tmp_path):
    """Verify lint_and_cleanup removes bytecode, *.patch, and caches."""
    repo_dir = tmp_path / "cleanup_repo"
    repo_dir.mkdir()
    (repo_dir / "mod.py").write_text("x = 1\n")

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeTester"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    wm = WorktreeManager(str(repo_dir), base_worktree_dir=str(tmp_path / "worktrees"))
    wt_path = wm.create_worktree("task-clean-1")

    # Create uncommitted bytecode and temp patch
    (Path(wt_path) / "temp.patch").write_text("dummy")
    pycache = Path(wt_path) / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-314.pyc").write_text("bytecode")

    clean_summary = wm.lint_and_cleanup(wt_path)
    assert any("temp.patch" in item for item in clean_summary["cleaned_items"])
    assert any("__pycache__" in item for item in clean_summary["cleaned_items"])
    assert not (Path(wt_path) / "temp.patch").exists()
    assert not pycache.exists()

    wm.cleanup("task-clean-1")


# ===========================================================================
# 4. STEP 4: RICH PR GENERATION TESTS
# ===========================================================================


def test_rich_pr_generation_formatting():
    """Verify GitHubIntegration.format_pr_body generates all required structured markdown sections."""
    body = GitHubIntegration.format_pr_body(
        context_and_root_cause="Fixes race condition in task worker where status was overwritten prematurely.",
        surgical_changes=[
            "Add database row-level locking with SELECT FOR UPDATE",
            "Ensure atomic transition",
        ],
        verification_log="pytest tests/_forge_repro_test.py: 1 passed\npytest: 24 passed in 1.1s",
        regression_test_details="def test_race_condition():\n    assert worker.acquire_lock() is True",
        task_id="task-999",
    )

    # 1. Context & Root Cause
    assert "## 📋 Context & Root Cause" in body
    assert "race condition in task worker" in body

    # 2. Surgical Changes Made
    assert "## ⚡ Surgical Changes Made" in body
    assert "SELECT FOR UPDATE" in body

    # 3. Proof of Verification
    assert "## 🧪 Proof of Verification" in body
    assert "pytest: 24 passed in 1.1s" in body

    # 4. Added Regression Test Details
    assert "## 🛡️ Added Regression Test Details" in body
    assert "def test_race_condition" in body
    assert "Task `task-999`" in body
