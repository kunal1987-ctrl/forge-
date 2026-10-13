"""Unit and integration tests for FORGE Agent Evaluation & Execution Harness."""

from __future__ import annotations

import json
import shutil
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.api.forge.harness.evaluate import evaluate_benchmark, load_dataset
from services.api.forge.harness.runner import HarnessRunner
from services.api.forge.harness.schemas import BenchmarkReport, HarnessTask, TrialResult
from services.api.forge.orchestration.error_classifier import ErrorCategory


# ===========================================================================
# 1. HARNESS SCHEMAS TESTS
# ===========================================================================


def test_harness_task_schema_validation():
    """Verify HarnessTask schema parsing and default attributes."""
    task = HarnessTask(
        task_id="eval-task-001",
        repo_name="org/sample-repo",
        prompt="Fix division by zero error in calculator",
        ground_truth_patch="--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n- return a / b\n+ return a / b if b else 0",
        timeout_seconds=120,
    )

    assert task.task_id == "eval-task-001"
    assert task.repo_name == "org/sample-repo"
    assert task.timeout_seconds == 120
    assert task.test_command == "pytest"
    assert task.ground_truth_patch is not None


def test_trial_result_schema_validation():
    """Verify TrialResult model validation and serialization."""
    result = TrialResult(
        task_id="eval-task-001",
        success=True,
        execution_time_seconds=14.52,
        retries_used=1,
        error_category=None,
        generated_diff="### File: calc.py\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE",
        test_metrics={"exit_code": 0, "passed_count": 3, "failed_count": 0},
        node_transitions=["planner_node", "investigator_node", "implementer_node", "verifier_node"],
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["retries_used"] == 1
    assert len(data["node_transitions"]) == 4


def test_benchmark_report_aggregation_and_markdown():
    """Verify BenchmarkReport metrics calculations and Markdown export."""
    trials = [
        TrialResult(
            task_id="task-1",
            success=True,
            execution_time_seconds=10.0,
            retries_used=0,
            test_metrics={"passed_count": 5, "failed_count": 0},
        ),
        TrialResult(
            task_id="task-2",
            success=False,
            execution_time_seconds=15.0,
            retries_used=2,
            error_category=ErrorCategory.IMPORT_ERROR.value,
            test_metrics={"passed_count": 1, "failed_count": 1},
        ),
    ]

    report = BenchmarkReport(
        total_tasks=2,
        passed_tasks=1,
        success_rate=0.5,
        average_retries=1.0,
        average_execution_time_seconds=12.5,
        trials=trials,
    )

    assert report.total_tasks == 2
    assert report.passed_tasks == 1
    assert report.success_rate == 0.5

    md = report.to_markdown()
    assert "# FORGE Autonomous Agent Benchmark Report" in md
    assert "50.0%" in md
    assert "task-1" in md
    assert "task-2" in md
    assert "IMPORT_ERROR" in md


# ===========================================================================
# 2. HARNESS RUNNER ENVIRONMENT & GRADING TESTS
# ===========================================================================


def test_harness_runner_setup_environment(tmp_path):
    """Verify runner spins up isolated Git repository and worktree."""
    logs_dir = tmp_path / "logs"
    runner = HarnessRunner(logs_dir=logs_dir)

    task = HarnessTask(
        task_id="task-env-test",
        repo_name="synthetic-benchmark",
        prompt="Fix broken test assertion",
    )

    repo_path, worktree_path = runner.setup_environment(task)

    try:
        assert repo_path.exists()
        assert worktree_path.exists()
        assert (repo_path / "README.md").exists()
        assert (worktree_path / "README.md").exists()
        # Verify valid git repo
        assert (repo_path / ".git").exists()
    finally:
        shutil.rmtree(repo_path.parent, ignore_errors=True)


def test_harness_runner_grade_trial_success(tmp_path):
    """Verify grading detects clean test pass with positive test count."""
    worktree = tmp_path / "worktree_pass"
    worktree.mkdir()

    mock_sandbox = MagicMock()
    mock_sandbox.run_tests.return_value = {
        "exit_code": 0,
        "passed_count": 4,
        "failed_count": 0,
        "errors_count": 0,
        "raw_output": "4 passed in 0.25s",
    }

    runner = HarnessRunner(logs_dir=tmp_path / "logs", sandbox=mock_sandbox)
    task = HarnessTask(task_id="t-pass", repo_name="repo", prompt="Fix bug")

    is_success, metrics, err_cat = runner.grade_trial(
        task=task,
        worktree_path=worktree,
        final_state={"status": "verified"},
    )

    assert is_success is True
    assert metrics["passed_count"] == 4
    assert err_cat is None


def test_harness_runner_grade_trial_failure_and_classification(tmp_path):
    """Verify grading marks failure and classifies error category upon test failure."""
    worktree = tmp_path / "worktree_fail"
    worktree.mkdir()

    mock_sandbox = MagicMock()
    mock_sandbox.run_tests.return_value = {
        "exit_code": 1,
        "passed_count": 0,
        "failed_count": 1,
        "errors_count": 0,
        "raw_output": "ModuleNotFoundError: No module named 'scipy'",
    }

    runner = HarnessRunner(logs_dir=tmp_path / "logs", sandbox=mock_sandbox)
    task = HarnessTask(task_id="t-fail", repo_name="repo", prompt="Fix bug")

    is_success, metrics, err_cat = runner.grade_trial(
        task=task,
        worktree_path=worktree,
        final_state={"status": "failed_verification"},
    )

    assert is_success is False
    assert err_cat == ErrorCategory.IMPORT_ERROR.value


def test_harness_runner_log_trajectory(tmp_path):
    """Verify runner saves structured trajectory log JSON."""
    logs_dir = tmp_path / "logs"
    runner = HarnessRunner(logs_dir=logs_dir)

    task = HarnessTask(task_id="task-log-1", repo_name="repo-x", prompt="Fix bug X")
    trial = TrialResult(
        task_id="task-log-1",
        success=True,
        execution_time_seconds=5.2,
        retries_used=1,
    )

    log_path = runner.log_trajectory(
        task=task,
        trial=trial,
        final_state={
            "status": "completed",
            "retry_count": 1,
            "summary": "Repaired syntax",
            "generated_diff": "diff content",
        },
    )

    assert log_path.exists()
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert saved["task_id"] == "task-log-1"
    assert saved["trial_result"]["success"] is True
    assert saved["final_state"]["summary"] == "Repaired syntax"


# ===========================================================================
# 3. DATASET LOADING & BENCHMARK ORCHESTRATION TESTS
# ===========================================================================


def test_load_dataset_json_and_jsonl(tmp_path):
    """Verify dataset loading supports JSON array, dict-wrapped, and JSONL formats."""
    # 1. JSON Array
    json_path = tmp_path / "tasks.json"
    json_path.write_text(
        json.dumps(
            [
                {"task_id": "j-1", "repo_name": "repo1", "prompt": "Prompt 1"},
                {"task_id": "j-2", "repo_name": "repo2", "prompt": "Prompt 2"},
            ]
        ),
        encoding="utf-8",
    )
    loaded_json = load_dataset(json_path)
    assert len(loaded_json) == 2
    assert loaded_json[0].task_id == "j-1"

    # 2. JSONL
    jsonl_path = tmp_path / "tasks.jsonl"
    jsonl_path.write_text(
        '{"task_id": "jl-1", "repo_name": "repoA", "prompt": "Prompt A"}\n'
        '{"task_id": "jl-2", "repo_name": "repoB", "prompt": "Prompt B"}\n',
        encoding="utf-8",
    )
    loaded_jsonl = load_dataset(jsonl_path)
    assert len(loaded_jsonl) == 2
    assert loaded_jsonl[1].task_id == "jl-2"


@pytest.mark.asyncio
async def test_evaluate_benchmark_orchestration(tmp_path):
    """Verify evaluate_benchmark executes tasks, aggregates metrics, and generates reports."""
    tasks = [
        HarnessTask(task_id="bench-1", repo_name="r1", prompt="p1"),
        HarnessTask(task_id="bench-2", repo_name="r2", prompt="p2"),
    ]

    mock_runner = MagicMock()
    mock_runner.run_trial = AsyncMock(
        side_effect=[
            TrialResult(
                task_id="bench-1",
                success=True,
                execution_time_seconds=2.0,
                retries_used=0,
                test_metrics={"passed_count": 3, "failed_count": 0},
            ),
            TrialResult(
                task_id="bench-2",
                success=False,
                execution_time_seconds=4.0,
                retries_used=1,
                error_category=ErrorCategory.SYNTAX_ERROR.value,
                test_metrics={"passed_count": 0, "failed_count": 1},
            ),
        ]
    )

    out_dir = tmp_path / "reports"
    report = await evaluate_benchmark(
        dataset=tasks,
        concurrency=2,
        output_dir=out_dir,
        runner=mock_runner,
    )

    assert report.total_tasks == 2
    assert report.passed_tasks == 1
    assert report.success_rate == 0.5
    assert report.average_retries == 0.5
    assert report.average_execution_time_seconds == 3.0

    # Verify reports were written
    assert (out_dir / "latest_report.json").exists()
    assert (out_dir / "latest_report.md").exists()
