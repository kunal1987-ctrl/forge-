"""Data contracts and Pydantic schemas for the FORGE Evaluation Harness."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class HarnessTask(BaseModel):
    """Specification of an evaluation benchmark task for an autonomous software engineering agent."""

    task_id: str = Field(
        ...,
        description="Unique identifier for the benchmark task (e.g., 'eval-pytest-001').",
    )
    repo_name: str = Field(
        ...,
        description="Target repository name, path, or benchmark clone target.",
    )
    prompt: str = Field(
        ...,
        description="Problem statement or user prompt defining the bug repair objective.",
    )
    ground_truth_patch: Optional[str] = Field(
        default=None,
        description="Optional canonical reference patch for validation or diff comparison.",
    )
    timeout_seconds: int = Field(
        default=300,
        description="Maximum execution wall-clock time limit in seconds.",
    )
    test_command: str = Field(
        default="pytest",
        description="Command to execute for verifying tests (e.g. 'pytest', 'pytest tests/test_bug.py').",
    )
    base_commit: Optional[str] = Field(
        default=None,
        description="Optional Git commit SHA or branch to checkout before starting evaluation.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary additional metadata (benchmark source, difficulty, tags).",
    )


class TrialResult(BaseModel):
    """Execution trial result, diagnostics, and test metrics for a single evaluated task."""

    task_id: str = Field(
        ...,
        description="Identifier matching HarnessTask.task_id.",
    )
    success: bool = Field(
        ...,
        description="True if the agent fixed the defect, passed verification, and introduced no regressions.",
    )
    execution_time_seconds: float = Field(
        default=0.0,
        description="Total duration of the execution trial in seconds.",
    )
    retries_used: int = Field(
        default=0,
        description="Total repair loop retries consumed during execution.",
    )
    error_category: Optional[str] = Field(
        default=None,
        description="Categorized failure type if trial failed (e.g. SYNTAX_ERROR, IMPORT_ERROR, ASSERTION_FAILURE, TIMEOUT).",
    )
    generated_diff: str = Field(
        default="",
        description="The final surgical search/replace or unified git patch produced by the agent.",
    )
    test_metrics: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured test results including exit_code, passed_count, failed_count, and raw outputs.",
    )
    node_transitions: List[str] = Field(
        default_factory=list,
        description="Ordered sequence of LangGraph state machine node transitions executed.",
    )
    token_usage: Dict[str, int] = Field(
        default_factory=dict,
        description="Estimated token usage breakdown (prompt_tokens, completion_tokens, total_tokens).",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Diagnostic error message if the trial failed.",
    )
    trajectory_log_path: Optional[str] = Field(
        default=None,
        description="Filesystem path where the complete execution trajectory was saved.",
    )


class BenchmarkReport(BaseModel):
    """Aggregate evaluation report summarizing performance across a benchmark dataset."""

    total_tasks: int = Field(
        ...,
        description="Total number of tasks evaluated in this benchmark run.",
    )
    passed_tasks: int = Field(
        ...,
        description="Number of benchmark tasks that succeeded.",
    )
    success_rate: float = Field(
        ...,
        description="Percentage of passed tasks expressed as a float between 0.0 and 1.0.",
    )
    average_retries: float = Field(
        default=0.0,
        description="Average number of repair loop retries per task.",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 UTC timestamp of report generation.",
    )
    average_execution_time_seconds: float = Field(
        default=0.0,
        description="Average execution duration per task in seconds.",
    )
    trials: List[TrialResult] = Field(
        default_factory=list,
        description="Individual trial results for all tasks in the benchmark run.",
    )

    def to_markdown(self) -> str:
        """Render a formatted GitHub Flavored Markdown summary report."""
        success_pct = f"{self.success_rate * 100:.1f}%"
        lines = [
            "# FORGE Autonomous Agent Benchmark Report",
            "",
            f"**Execution Timestamp:** `{self.timestamp}`  ",
            f"**Overall Success Rate:** `{success_pct}` ({self.passed_tasks}/{self.total_tasks} passed)  ",
            f"**Average Retries:** `{self.average_retries:.2f}`  ",
            f"**Average Execution Time:** `{self.average_execution_time_seconds:.2f}s`  ",
            "",
            "## Summary Metrics",
            "",
            "| Metric | Value |",
            "| :--- | :--- |",
            f"| Total Benchmark Tasks | {self.total_tasks} |",
            f"| Passed Tasks | {self.passed_tasks} |",
            f"| Failed Tasks | {self.total_tasks - self.passed_tasks} |",
            f"| Success Rate | **{success_pct}** |",
            f"| Average Retries | {self.average_retries:.2f} |",
            f"| Average Duration | {self.average_execution_time_seconds:.2f}s |",
            "",
            "## Task Breakdown",
            "",
            "| Task ID | Status | Time (s) | Retries | Error Category | Tests (Pass/Fail) |",
            "| :--- | :---: | :---: | :---: | :--- | :---: |",
        ]

        for t in self.trials:
            status_icon = "PASS" if t.success else "FAIL"
            err_cat = t.error_category or ("None" if t.success else "UNKNOWN")
            metrics = t.test_metrics or {}
            pass_cnt = metrics.get("passed_count", 0)
            fail_cnt = metrics.get("failed_count", 0)
            tests_summary = f"{pass_cnt}p / {fail_cnt}f"
            lines.append(
                f"| `{t.task_id}` | {status_icon} | {t.execution_time_seconds:.2f} | {t.retries_used} | `{err_cat}` | {tests_summary} |"
            )

        lines.append("")
        return "\n".join(lines)
