"""Dynamic Metrics Calculation Engine for FORGE Autonomous Engineering Platform.

Architecture:
1. Diff Stats Parser:
   - Executes `git diff --shortstat` inside the active worktree or parses the unified
     git patch string to extract exact lines added and deleted (+X / -Y) and files changed.
2. Dynamic Confidence & Latency Scoring:
   - Computes `confidence` dynamically: Starts at 1.0 (100%), subtracts 0.15 for each retry
     iteration required by the verifier, and factors in test coverage completeness.
   - Computes `latency_impact`: Measures test suite execution time delta
     (`post_test_duration - pre_test_duration`) or estimates function execution overhead.
3. State Contract Propagation:
   - Stores computed metrics into the `AgentState` object (`state["metrics"]`,
     `state["confidence_score"]`, `state["diff_stats"]`, and `state["latency_impact"]`)
     so the API returns verified real numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
import re
import subprocess
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

if TYPE_CHECKING:
    from services.api.forge.orchestration.state import AgentState

logger = logging.getLogger("forge.services.metrics")

__all__ = [
    "DiffStats",
    "LatencyImpact",
    "MetricsEngine",
    "parse_diff_stats",
    "calculate_confidence",
    "calculate_latency_impact",
    "compute_and_save_task_metrics",
]


# =============================================================================
# 1. DIFF STATS PARSER
# =============================================================================


@dataclass
class DiffStats:
    """Exact diff statistics extracted from git shortstat or surgical patch string."""

    lines_added: int = 0
    lines_deleted: int = 0
    files_changed: int = 0
    files_list: List[str] = field(default_factory=list)

    @property
    def total_lines_changed(self) -> int:
        """Total volume of code lines modified (+X and -Y)."""
        return self.lines_added + self.lines_deleted

    @property
    def formatted_summary(self) -> str:
        """Standard +X / -Y diff summary string."""
        return f"+{self.lines_added} / -{self.lines_deleted}"

    @property
    def description(self) -> str:
        """Human-readable diff summary."""
        file_suffix = "file" if self.files_changed == 1 else "files"
        return f"{self.formatted_summary} across {self.files_changed} {file_suffix}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert diff statistics to dictionary."""
        return {
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "files_changed": self.files_changed,
            "total_lines_changed": self.total_lines_changed,
            "formatted_summary": self.formatted_summary,
            "description": self.description,
            "files_list": list(self.files_list),
        }


def parse_patch_string(patch: str) -> DiffStats:
    """Parse a unified git diff / patch string to extract exact additions, deletions, and filenames.

    Matches diff headers:
      - diff --git a/path/to/file b/path/to/file
      - +++ b/path/to/file
      - --- a/path/to/file
      - Added lines: lines starting with '+' (excluding '+++')
      - Deleted lines: lines starting with '-' (excluding '---')
    """
    if not patch or not isinstance(patch, str):
        return DiffStats()

    lines_added = 0
    lines_deleted = 0
    files_set: set[str] = set()

    for line in patch.splitlines():
        # Match git diff file headers
        if line.startswith("diff --git "):
            match = re.search(r"diff --git a/(.*?)\s+b/(.*?)$", line)
            if match:
                files_set.add(match.group(2).strip())
            continue

        if line.startswith("+++ "):
            clean_path = line[4:].strip()
            if clean_path.startswith("b/"):
                clean_path = clean_path[2:]
            if clean_path and clean_path != "/dev/null":
                files_set.add(clean_path)
            continue

        if line.startswith("--- "):
            clean_path = line[4:].strip()
            if clean_path.startswith("a/"):
                clean_path = clean_path[2:]
            if clean_path and clean_path != "/dev/null":
                files_set.add(clean_path)
            continue

        # Count additions and deletions
        if line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_deleted += 1

    files_list = sorted(list(files_set))
    files_changed = len(files_list)

    return DiffStats(
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        files_changed=files_changed,
        files_list=files_list,
    )


def parse_git_shortstat_output(stdout: str) -> Optional[DiffStats]:
    """Parse output of `git diff --shortstat`.

    Examples:
      - " 2 files changed, 14 insertions(+), 3 deletions(-)"
      - " 1 file changed, 5 insertions(+)"
      - " 1 file changed, 2 deletions(-)"
    """
    if not stdout or not stdout.strip():
        return None

    clean = stdout.strip()
    files_match = re.search(r"(\d+)\s+file", clean)
    ins_match = re.search(r"(\d+)\s+insertion", clean)
    del_match = re.search(r"(\d+)\s+deletion", clean)

    if not files_match and not ins_match and not del_match:
        return None

    files_changed = int(files_match.group(1)) if files_match else 0
    lines_added = int(ins_match.group(1)) if ins_match else 0
    lines_deleted = int(del_match.group(1)) if del_match else 0

    return DiffStats(
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        files_changed=files_changed,
        files_list=[],
    )


def parse_diff_stats(
    patch: Optional[str] = None,
    worktree_path: Optional[str] = None,
) -> DiffStats:
    """Extract exact lines added and deleted (+X / -Y) from git diff or patch string.

    Priority:
    1. If `worktree_path` is a valid git repository, query git for accurate shortstat.
    2. If `patch` is provided, parse the unified diff string.
    3. Merges results to guarantee accurate file lists and line counts.
    """
    git_stats: Optional[DiffStats] = None

    if worktree_path and os.path.exists(worktree_path):
        try:
            # 1. Check unstaged / staged working tree changes
            res = subprocess.run(
                ["git", "diff", "--shortstat"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                git_stats = parse_git_shortstat_output(res.stdout)

            # 2. If working tree is clean, check against HEAD~1 (if a commit was already created)
            if not git_stats or git_stats.total_lines_changed == 0:
                res_head = subprocess.run(
                    ["git", "diff", "--shortstat", "HEAD~1", "HEAD"],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                if res_head.returncode == 0 and res_head.stdout.strip():
                    git_stats = parse_git_shortstat_output(res_head.stdout)

            # Query filenames if git diff succeeded
            if git_stats:
                res_names = subprocess.run(
                    ["git", "diff", "--name-only"],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                if res_names.returncode == 0 and res_names.stdout.strip():
                    files = [f.strip() for f in res_names.stdout.splitlines() if f.strip()]
                    git_stats.files_list = files
                    if not git_stats.files_changed:
                        git_stats.files_changed = len(files)
        except Exception as exc:
            logger.debug(
                "parse_diff_stats: git command failed (%s), falling back to patch parsing", exc
            )

    # Parse patch string directly
    patch_stats = parse_patch_string(patch or "")

    if git_stats and git_stats.total_lines_changed > 0:
        if patch_stats.files_list and not git_stats.files_list:
            git_stats.files_list = patch_stats.files_list
            git_stats.files_changed = max(git_stats.files_changed, patch_stats.files_changed)
        return git_stats

    if patch_stats.total_lines_changed > 0 or patch_stats.files_changed > 0:
        return patch_stats

    return git_stats or DiffStats()


# =============================================================================
# 2. CONFIDENCE & LATENCY SCORING
# =============================================================================


def calculate_confidence(
    retry_count: int = 0,
    test_results: Optional[Dict[str, Any]] = None,
    coverage_completeness: Optional[float] = None,
) -> float:
    """Calculate agent confidence score dynamically upon task completion.

    Rules:
    - Base confidence starts at 1.0 (100%).
    - Subtract 0.15 for each retry iteration required by the verifier loop.
    - Factor in test coverage completeness:
      - If test suite had failures or exit_code != 0, apply failure penalty.
      - If coverage ratio is provided, weight by (0.8 + 0.2 * coverage).
    - Confidence is bounded between [0.0, 1.0] and rounded to 2 decimal places.
    """
    confidence = 1.0

    # 1. Retry iteration penalty (-0.15 per retry)
    retries = max(0, int(retry_count or 0))
    confidence -= retries * 0.15

    # 2. Factor in test execution results
    tests = test_results or {}
    exit_code = tests.get("exit_code", 0)
    passed_count = int(tests.get("passed_count", 0))
    failed_count = int(tests.get("failed_count", 0))
    total_tests = passed_count + failed_count

    if exit_code != 0 or failed_count > 0:
        # Failure penalty: Scale by pass ratio or cap at 0.35 if all failed
        if total_tests > 0:
            pass_ratio = passed_count / total_tests
            confidence *= max(0.20, pass_ratio)
        else:
            confidence = min(confidence, 0.30)
    else:
        # Green phase bonus / stability confirmation
        if passed_count > 0:
            confidence = min(1.0, confidence + 0.05) if retries > 0 else confidence

    # 3. Factor in test coverage completeness
    cov_val = (
        coverage_completeness
        if coverage_completeness is not None
        else tests.get("coverage") or tests.get("coverage_percentage")
    )

    if cov_val is not None:
        try:
            cov_float = float(cov_val)
            # Normalize percentage (e.g. 85.0 -> 0.85)
            if cov_float > 1.0:
                cov_float /= 100.0
            cov_float = max(0.0, min(1.0, cov_float))
            # Weighted coverage factor: 80% baseline + 20% coverage completeness
            coverage_factor = 0.80 + (0.20 * cov_float)
            confidence *= coverage_factor
        except (ValueError, TypeError):
            pass

    # Bound strictly between 0.0 and 1.0
    confidence = max(0.0, min(1.0, confidence))
    return round(confidence, 2)


@dataclass
class LatencyImpact:
    """Computed latency impact and test suite execution time delta."""

    duration_delta_seconds: float
    duration_delta_ms: float
    percentage_change: float
    estimated_overhead_ms: float
    is_regression: bool
    description: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert latency metrics to dictionary."""
        return {
            "duration_delta_seconds": self.duration_delta_seconds,
            "duration_delta_ms": self.duration_delta_ms,
            "percentage_change": self.percentage_change,
            "estimated_overhead_ms": self.estimated_overhead_ms,
            "is_regression": self.is_regression,
            "description": self.description,
        }


def calculate_latency_impact(
    pre_test_duration: Optional[float] = None,
    post_test_duration: Optional[float] = None,
    diff_stats: Optional[DiffStats] = None,
    patch_code: Optional[str] = None,
) -> LatencyImpact:
    """Calculate latency impact and execution overhead.

    Methods:
    1. Direct measurement: Computes `post_test_duration - pre_test_duration`.
    2. Estimated overhead: If direct pre/post benchmarks are absent, estimates
       function execution overhead based on patch complexity and AST additions.
    """
    pre_dur = float(pre_test_duration) if pre_test_duration is not None else None
    post_dur = float(post_test_duration) if post_test_duration is not None else None

    # 1. Direct measurement delta
    if pre_dur is not None and post_dur is not None and pre_dur >= 0 and post_dur >= 0:
        delta_seconds = round(post_dur - pre_dur, 4)
        delta_ms = round(delta_seconds * 1000.0, 2)
        pct = round((delta_seconds / pre_dur * 100.0), 2) if pre_dur > 0 else 0.0
        is_reg = delta_seconds > 0.05  # More than 50ms degradation is flagged as regression

        sign = "+" if delta_ms >= 0 else ""
        desc = f"{sign}{delta_ms:.1f}ms ({sign}{pct:.1f}%)"

        return LatencyImpact(
            duration_delta_seconds=delta_seconds,
            duration_delta_ms=delta_ms,
            percentage_change=pct,
            estimated_overhead_ms=abs(delta_ms),
            is_regression=is_reg,
            description=desc,
        )

    # 2. Estimated complexity-based overhead
    patch = patch_code or ""
    lines_added = (
        diff_stats.lines_added
        if diff_stats
        else len(
            [
                line
                for line in patch.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            ]
        )
    )

    # Base estimate: ~0.05ms per added line
    overhead_ms = round(lines_added * 0.05, 3)

    # Factor in high-overhead patterns: loops, network/IO calls, regex
    loop_count = len(re.findall(r"\b(for\s+|while\s+|async\s+for\s+)", patch))
    overhead_ms += loop_count * 0.45

    io_count = len(re.findall(r"\b(requests\.|httpx\.|open\(|subprocess\.|os\.system)", patch))
    overhead_ms += io_count * 3.5

    delta_sec = round(overhead_ms / 1000.0, 5)
    sign = "+" if overhead_ms >= 0 else ""
    desc = f"~{sign}{overhead_ms:.2f}ms estimated overhead"

    return LatencyImpact(
        duration_delta_seconds=delta_sec,
        duration_delta_ms=overhead_ms,
        percentage_change=0.0,
        estimated_overhead_ms=overhead_ms,
        is_regression=overhead_ms > 25.0,
        description=desc,
    )


# =============================================================================
# 3. STATE CONTRACT PROPAGATION & COMPREHENSIVE ENGINE
# =============================================================================


def compute_and_save_task_metrics(
    state: Union[AgentState, Dict[str, Any]],
    worktree_path: Optional[str] = None,
    pre_test_duration: Optional[float] = None,
    post_test_duration: Optional[float] = None,
    coverage_completeness: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute real diff statistics, latency impact, and agent confidence, then persist to AgentState.

    Saves:
    - `state["metrics"]`: Full structured metrics dictionary.
    - `state["confidence_score"]`: Dynamic confidence score (0.0 to 1.0).
    - `state["diff_stats"]`: Lines added, lines deleted, and files changed.
    - `state["latency_impact"]`: Latency delta in seconds.

    Returns the computed metrics dictionary.
    """
    # 1. Extract patch and worktree
    patch = str(state.get("patch") or state.get("generated_diff") or state.get("diff") or "")
    worktree = str(worktree_path or state.get("worktree_path") or state.get("repo_path") or "")

    # 2. Extract test and verification data
    retry_count = int(state.get("retry_count", 0))
    test_results = state.get("test_results") or {}

    # Extract durations from state if not passed explicitly
    pre_dur = (
        pre_test_duration
        if pre_test_duration is not None
        else state.get("pre_test_duration") or test_results.get("pre_test_duration")
    )
    post_dur = (
        post_test_duration
        if post_test_duration is not None
        else state.get("post_test_duration")
        or test_results.get("post_test_duration")
        or test_results.get("duration")
    )

    # 3. Compute metrics
    diff_stats = parse_diff_stats(patch=patch, worktree_path=worktree)
    confidence = calculate_confidence(
        retry_count=retry_count,
        test_results=test_results,
        coverage_completeness=coverage_completeness,
    )
    latency = calculate_latency_impact(
        pre_test_duration=pre_dur,
        post_test_duration=post_dur,
        diff_stats=diff_stats,
        patch_code=patch,
    )

    # 4. Assemble comprehensive metrics dictionary
    metrics_payload: Dict[str, Any] = {
        "lines_added": diff_stats.lines_added,
        "lines_deleted": diff_stats.lines_deleted,
        "files_changed": diff_stats.files_changed,
        "files_list": diff_stats.files_list,
        "formatted_diff": diff_stats.formatted_summary,
        "diff_summary": diff_stats.description,
        "diff_stats": diff_stats.to_dict(),
        "confidence": confidence,
        "confidence_score": confidence,
        "latency_impact": latency.duration_delta_seconds,
        "latency_impact_ms": latency.duration_delta_ms,
        "latency_details": latency.to_dict(),
        "retry_count": retry_count,
        "tests_passed": int(test_results.get("passed_count", 0)),
        "tests_failed": int(test_results.get("failed_count", 0)),
        "test_exit_code": int(test_results.get("exit_code", 0)),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    # 5. Save computed metrics directly into AgentState
    state["metrics"] = metrics_payload
    state["confidence_score"] = confidence
    state["diff_stats"] = diff_stats.to_dict()
    state["latency_impact"] = latency.duration_delta_seconds

    logger.info(
        "compute_and_save_task_metrics: diff=%s, confidence=%.2f, latency=%s",
        diff_stats.formatted_summary,
        confidence,
        latency.description,
    )

    return metrics_payload


class MetricsEngine:
    """Dynamic metrics calculation service."""

    def __init__(self, worktree_path: Optional[str] = None) -> None:
        self.worktree_path = worktree_path

    def parse_diff(self, patch: Optional[str] = None) -> DiffStats:
        """Parse diff statistics from patch string or active worktree."""
        return parse_diff_stats(patch=patch, worktree_path=self.worktree_path)

    def calculate_confidence(
        self,
        retry_count: int = 0,
        test_results: Optional[Dict[str, Any]] = None,
        coverage: Optional[float] = None,
    ) -> float:
        """Calculate confidence score."""
        return calculate_confidence(
            retry_count=retry_count,
            test_results=test_results,
            coverage_completeness=coverage,
        )

    def calculate_latency(
        self,
        pre_duration: Optional[float] = None,
        post_duration: Optional[float] = None,
        diff_stats: Optional[DiffStats] = None,
        patch: Optional[str] = None,
    ) -> LatencyImpact:
        """Calculate latency delta or overhead estimate."""
        return calculate_latency_impact(
            pre_test_duration=pre_duration,
            post_test_duration=post_duration,
            diff_stats=diff_stats,
            patch_code=patch,
        )

    def compute_all(
        self,
        state: Union[AgentState, Dict[str, Any]],
        pre_test_duration: Optional[float] = None,
        post_test_duration: Optional[float] = None,
        coverage_completeness: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute all metrics and persist into state object."""
        return compute_and_save_task_metrics(
            state=state,
            worktree_path=self.worktree_path,
            pre_test_duration=pre_test_duration,
            post_test_duration=post_test_duration,
            coverage_completeness=coverage_completeness,
        )
