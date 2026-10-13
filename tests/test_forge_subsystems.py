"""Phase 2: Comprehensive Subsystem Isolation Unit Tests for FORGE.

Tests each core engine in isolation:
1. RAG & Graph-RAG Engine (parser.py & hybrid_search.py)
2. Patch & Anti-Truncation Gate (diff_validator.py)
3. Isolated Worktree Engine (worktrees.py)
4. Docker Sandbox Execution (sandbox.py)
5. State Pruner (pruner.py)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)

from services.api.forge.execution.diff_validator import (
    DiffValidator,
    validate_no_test_tampering,
)
from services.api.forge.execution.sandbox import DockerSandbox
from services.api.forge.execution.worktrees import WorktreeManager
from services.api.forge.orchestration.pruner import (
    compact_messages,
    deduplicate_and_cap_context,
    extract_error_details,
    sanitize_test_logs,
)
from services.api.forge.retrieval.hybrid_search import (
    ranker,
    truncate_dependency_code,
)
from services.api.forge.retrieval.parser import CodeParser


# ===========================================================================
# 1. RAG & GRAPH-RAG ENGINE TESTS
# ===========================================================================


def test_tree_sitter_ast_extraction():
    """Test Tree-sitter extraction of classes, functions, and call edges."""
    parser = CodeParser()
    sample_code = """
import os

class MathService:
    def helper(self, x):
        return x * 2

    def calculate(self, a, b):
        result = self.helper(a)
        return result + b

def standalone_func():
    service = MathService()
    return service.calculate(10, 20)
"""
    chunks, edges = parser.parse_file("services/math_service.py", sample_code)

    assert len(chunks) > 0
    symbols = {c["symbol_name"]: c for c in chunks}

    # Verify Class Chunk
    assert "MathService" in symbols
    assert symbols["MathService"]["symbol_type"] == "class"

    # Verify Method Chunk with Scope Hierarchy
    assert "helper" in symbols
    assert symbols["helper"]["symbol_type"] == "method"
    assert symbols["helper"]["parent_symbol"] == "MathService"

    assert "calculate" in symbols
    assert symbols["calculate"]["symbol_type"] == "method"
    assert symbols["calculate"]["parent_symbol"] == "MathService"

    # Verify Standalone Function
    assert "standalone_func" in symbols
    assert symbols["standalone_func"]["symbol_type"] == "function"
    assert symbols["standalone_func"]["parent_symbol"] is None

    # Verify Call & Enclosure Edges
    edge_tuples = {(e["source_symbol"], e["target_symbol"], e["edge_type"]) for e in edges}
    assert any(
        "helper" in src and "MathService" in tgt and etype == "enclosed_by"
        for src, tgt, etype in edge_tuples
    )
    assert any(
        "calculate" in src and "MathService" in tgt and etype == "enclosed_by"
        for src, tgt, etype in edge_tuples
    )
    assert any(etype == "calls" for _, _, etype in edge_tuples)


def test_flashrank_reranking():
    """Test FlashRank local CPU reranker ordering."""
    from flashrank import RerankRequest

    docs = [
        {"id": 1, "text": "Unrelated database migration for user tables."},
        {"id": 2, "text": "Function divide(a, b) divides two numbers and handles zero division."},
        {"id": 3, "text": "Logging configuration for backend server."},
    ]
    query = "How to divide numbers and avoid division by zero"
    req = RerankRequest(query=query, passages=docs)
    results = ranker.rerank(req)

    assert len(results) == 3
    # The divide function passage should be ranked top
    top_match = results[0]
    assert top_match["id"] == 2
    assert "divide" in top_match["text"]


def test_truncate_dependency_code():
    """Test dependency code truncation to preserve token budgets."""
    massive_function = """def heavy_utility(data: list[int]) -> int:
    \"\"\"Perform complex statistical calculations on dataset.
    This docstring explains in detail the algorithm used.
    \"\"\"
    # 50 lines of complex calculations
    total = sum(data)
    variance = total / len(data)
    log_variance = variance ** 0.5
    for item in data:
        print(f"Intermediate step {item}")
    return int(variance)
"""
    truncated = truncate_dependency_code(massive_function)
    assert "def heavy_utility(data: list[int]) -> int:" in truncated
    assert "Perform complex statistical calculations" in truncated
    assert "return int(variance)" in truncated
    assert "body truncated for context budget defense" in truncated


# ===========================================================================
# 2. PATCH & ANTI-TRUNCATION GATE TESTS
# ===========================================================================


def test_lazy_diff_markers_detection():
    """Verify detection and instant rejection of lazy diff markers."""
    lazy_diffs = [
        # Bare ellipses
        """--- a/main.py
+++ b/main.py
@@ -1,5 +1,5 @@
 def compute():
+    ...
""",
        # Commented ellipses
        """--- a/main.py
+++ b/main.py
@@ -1,5 +1,5 @@
 def compute():
+    # ...
""",
        # Natural language laziness
        """--- a/main.py
+++ b/main.py
@@ -1,5 +1,5 @@
 def compute():
+    # rest of code remains unchanged
""",
    ]

    for diff in lazy_diffs:
        val_result = DiffValidator.validate(diff)
        assert not val_result.is_valid
        assert val_result.error_type == "LAZY_TRUNCATION"
        assert "Diff rejected" in val_result.actionable_feedback


def test_test_tampering_detection_guard():
    """Verify test-tampering detection flags unauthorized test mutations."""
    # 1. Bugfix prompt + modifying test files -> REJECTED
    bugfix_prompt = "Fix NullPointerException in parse_json when string is empty"
    is_valid, msg = validate_no_test_tampering(
        ["services/parser.py", "tests/test_parser.py"], bugfix_prompt
    )
    assert not is_valid
    assert "REJECTED: You modified existing test files" in msg

    # 2. Explicit test prompt -> PERMITTED
    test_prompt = "Fix parser bug and update tests to cover empty input."
    is_valid, msg = validate_no_test_tampering(
        ["services/parser.py", "tests/test_parser.py"], test_prompt
    )
    assert is_valid
    assert msg is None

    # 3. Clean production code without tests -> PERMITTED
    is_valid, msg = validate_no_test_tampering(["services/parser.py"], bugfix_prompt)
    assert is_valid
    assert msg is None


def test_in_memory_ast_syntax_validation():
    """Verify in-memory AST validation catches broken syntax before touching disk."""
    broken_patch = """--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a + b
+    return a + (b *   # Missing closing bracket
"""
    val = DiffValidator.validate(broken_patch)
    assert not val.is_valid
    assert val.error_type == "SYNTAX_BREAKAGE"
    assert "syntax corruption" in val.actionable_feedback.lower()


# ===========================================================================
# 3. ISOLATED WORKTREE ENGINE TESTS
# ===========================================================================


def test_worktree_engine_lifecycle(tmp_path):
    """Test worktree creation, patch application, fallback, and atomic reset."""
    repo_dir = tmp_path / "lifecycle_repo"
    repo_dir.mkdir()
    (repo_dir / "calculator.py").write_text("def subtract(a, b):\n    return a - b\n")

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeTester"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True)

    wt_base = tmp_path / "worktrees"
    wm = WorktreeManager(str(repo_dir), base_worktree_dir=str(wt_base))

    # 1. Create worktree
    wt_path = wm.create_worktree("task-subsystem-1")
    assert os.path.exists(wt_path)

    # 2. Apply clean unified diff
    clean_diff = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,3 @@
 def subtract(a, b):
+    # Verified subtraction
     return a - b
"""
    res = wm.apply_patch(wt_path, clean_diff)
    assert res.success

    modified = wm.get_modified_files(wt_path)
    assert "calculator.py" in modified

    # 3. Test atomic reset_worktree()
    (Path(wt_path) / "untracked_file.py").write_text("debris\n")
    wm.reset_worktree(wt_path)

    # Verify untracked file is removed and modified file is restored to pristine HEAD
    assert not (Path(wt_path) / "untracked_file.py").exists()
    assert (
        Path(wt_path) / "calculator.py"
    ).read_text() == "def subtract(a, b):\n    return a - b\n"
    assert wm.get_modified_files(wt_path) == []

    # 4. Cleanup
    wm.cleanup("task-subsystem-1")
    assert not os.path.exists(wt_path)


# ===========================================================================
# 4. DOCKER SANDBOX EXECUTION TESTS
# ===========================================================================


def test_sandbox_pytest_exit_code_and_zero_tests_rejection():
    """Verify exit_code 0 with 0 tests passed is rejected as code 5 failure."""
    sandbox = object.__new__(DockerSandbox)

    # 1. 0 tests passed with returncode 0
    sandbox.run_command = MagicMock(
        return_value=MagicMock(
            exit_code=0,
            stdout="collected 0 items\n=== no tests ran in 0.01s ===",
            stderr="",
            timeout=False,
        )
    )
    res = sandbox.run_tests("/tmp/dummy", "pytest")
    assert res["exit_code"] == 5
    assert res["passed_count"] == 0
    assert res["status"] == "no_tests"
    assert "No tests were executed" in res["error"]

    # 2. Normal clean pass with 3 tests
    sandbox.run_command = MagicMock(
        return_value=MagicMock(
            exit_code=0,
            stdout="=== 3 passed in 0.15s ===",
            stderr="",
            timeout=False,
        )
    )
    res = sandbox.run_tests("/tmp/dummy", "pytest")
    assert res["exit_code"] == 0
    assert res["passed_count"] == 3
    assert res["failed_count"] == 0
    assert res["status"] == "passed"


def test_sandbox_timeout_termination():
    """Verify timeout terminates infinite execution and returns structured failure."""
    sandbox = object.__new__(DockerSandbox)
    sandbox.run_command = MagicMock(
        return_value=MagicMock(
            exit_code=-1,
            stdout="",
            stderr="Execution timed out after 45s. Possible infinite loop detected.",
            timeout=True,
        )
    )

    res = sandbox.run_tests("/tmp/dummy", "pytest", timeout_seconds=45)
    assert res["exit_code"] == -1
    assert res["status"] == "timed_out"
    assert "infinite loop detected" in res["stderr"]


# ===========================================================================
# 5. STATE PRUNER TESTS
# ===========================================================================


def test_pruner_sanitize_test_logs():
    """Verify ANSI code stripping and pytest trace extraction under 6,000 characters."""
    raw_pytest = """\x1b[31m=================================== FAILURES ===================================\x1b[0m
\x1b[31m__________________________________ test_divide _________________________________\x1b[0m

    def test_divide():
>       assert divide(10, 2) == 5
E       AssertionError: assert 20 == 5

calculator.py:2: AssertionError
=========================== short test summary info ===========================
FAILED test_calculator.py::test_divide - AssertionError: assert 20 == 5
============================== 1 failed in 0.05s ==============================
"""
    sanitized = sanitize_test_logs(raw_pytest)
    assert "\x1b[" not in sanitized
    assert "FAILURES" in sanitized
    assert "AssertionError: assert 20 == 5" in sanitized
    assert len(sanitized) <= 6000

    error_type, failed_file = extract_error_details(sanitized)
    assert error_type == "AssertionError"
    assert "calculator.py" in failed_file


def test_pruner_deduplicate_and_cap_context():
    """Verify deduplication by file_path::symbol_name and token capping."""
    chunk_1 = {"file_path": "a.py", "symbol_name": "fn1", "content": "x = 1\n", "is_primary": True}
    chunk_duplicate = {
        "file_path": "a.py",
        "symbol_name": "fn1",
        "content": "x = 1\n",
        "is_primary": True,
    }
    chunk_dep = {
        "file_path": "b.py",
        "symbol_name": "fn2",
        "content": "y = 2\n",
        "is_primary": False,
    }

    deduped = deduplicate_and_cap_context([chunk_1, chunk_duplicate, chunk_dep], max_tokens=100)
    assert len(deduped) == 2
    keys = [f"{c['file_path']}::{c['symbol_name']}" for c in deduped]
    assert keys == ["a.py::fn1", "b.py::fn2"]


def test_pruner_compact_messages_and_remove_message():
    """Verify obsolete assistant diff messages are converted to RemoveMessage."""
    system_msg = SystemMessage(content="You are an engineer.")
    human_msg = HumanMessage(content="Fix the bug.", id="msg-root")
    ai_old_diff = AIMessage(
        id="msg-ai-1",
        content="Here is the diff:\n```diff\n--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new\n```",
    )
    ai_new_msg = AIMessage(id="msg-ai-2", content="Thinking about repair...")

    messages: list[BaseMessage] = [system_msg, human_msg, ai_old_diff, ai_new_msg]
    trajectory = [
        {
            "attempt": 1,
            "failed_file": "main.py",
            "failure_type": "AssertionError",
            "summary": "assert 4 == 5",
        }
    ]

    commands = compact_messages(messages, repair_trajectory=trajectory)

    remove_ids = [cmd.id for cmd in commands if isinstance(cmd, RemoveMessage)]
    assert "msg-ai-1" in remove_ids
    assert any(
        isinstance(cmd, HumanMessage) and "STRUCTURED REPAIR TRAJECTORY" in cmd.content
        for cmd in commands
    )
