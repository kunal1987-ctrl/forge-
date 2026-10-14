"""Unit tests for the dynamic metrics calculation engine (services/api/forge/services/metrics.py)."""

import pytest
from services.api.forge.services.metrics import (
    DiffStats,
    LatencyImpact,
    MetricsEngine,
    calculate_confidence,
    calculate_latency_impact,
    compute_and_save_task_metrics,
    parse_diff_stats,
    parse_patch_string,
    parse_git_shortstat_output,
)


SAMPLE_PATCH = """diff --git a/services/api/main.py b/services/api/main.py
index 1234567..89abcdef 100644
--- a/services/api/main.py
+++ b/services/api/main.py
@@ -10,6 +10,12 @@ import logging
 
+def added_telemetry_handler(event: dict) -> None:
+    # Process event
+    logging.info(f"Handled: {event}")
+    return None
+
 def existing_function():
-    return False
+    return True
"""

SAMPLE_MULTI_FILE_PATCH = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,3 +1,4 @@
+import os
 line1
 line2
diff --git a/pkg/b.py b/pkg/b.py
--- a/pkg/b.py
+++ b/pkg/b.py
@@ -5,5 +5,3 @@
-old_func()
-deprecated_call()
+new_func()
"""


def test_parse_patch_string_single_file():
    stats = parse_patch_string(SAMPLE_PATCH)
    assert isinstance(stats, DiffStats)
    assert stats.lines_added == 6
    assert stats.lines_deleted == 1
    assert stats.files_changed == 1
    assert stats.files_list == ["services/api/main.py"]
    assert stats.formatted_summary == "+6 / -1"
    assert stats.total_lines_changed == 7


def test_parse_patch_string_multi_file():
    stats = parse_patch_string(SAMPLE_MULTI_FILE_PATCH)
    assert stats.lines_added == 2  # 1 in a.py, 1 in b.py
    assert stats.lines_deleted == 2  # 2 in b.py
    assert stats.files_changed == 2
    assert "pkg/a.py" in stats.files_list
    assert "pkg/b.py" in stats.files_list
    assert stats.formatted_summary == "+2 / -2"


def test_parse_patch_string_empty():
    stats = parse_patch_string("")
    assert stats.lines_added == 0
    assert stats.lines_deleted == 0
    assert stats.files_changed == 0
    assert stats.files_list == []
    assert stats.formatted_summary == "+0 / -0"


def test_parse_git_shortstat_output():
    stdout = " 3 files changed, 28 insertions(+), 7 deletions(-)"
    stats = parse_git_shortstat_output(stdout)
    assert stats is not None
    assert stats.files_changed == 3
    assert stats.lines_added == 28
    assert stats.lines_deleted == 7
    assert stats.formatted_summary == "+28 / -7"


def test_parse_git_shortstat_insertions_only():
    stdout = " 1 file changed, 12 insertions(+)"
    stats = parse_git_shortstat_output(stdout)
    assert stats is not None
    assert stats.files_changed == 1
    assert stats.lines_added == 12
    assert stats.lines_deleted == 0


def test_parse_git_shortstat_deletions_only():
    stdout = " 2 files changed, 4 deletions(-)"
    stats = parse_git_shortstat_output(stdout)
    assert stats is not None
    assert stats.files_changed == 2
    assert stats.lines_added == 0
    assert stats.lines_deleted == 4


def test_calculate_confidence_base_perfect():
    # 0 retries, passing test suite
    conf = calculate_confidence(
        retry_count=0,
        test_results={"exit_code": 0, "passed_count": 5, "failed_count": 0},
    )
    assert conf == 1.0


def test_calculate_confidence_retries_penalty():
    # 1 retry = -0.15 -> 0.85 (with bonus capped at 0.90 max, or 0.85 without bonus)
    conf_1 = calculate_confidence(
        retry_count=1,
        test_results={"exit_code": 0, "passed_count": 0, "failed_count": 0},
    )
    assert conf_1 == 0.85

    # 2 retries = -0.30 -> 0.70
    conf_2 = calculate_confidence(
        retry_count=2,
        test_results={"exit_code": 0, "passed_count": 0, "failed_count": 0},
    )
    assert conf_2 == 0.70

    # 3 retries = -0.45 -> 0.55
    conf_3 = calculate_confidence(
        retry_count=3,
        test_results={"exit_code": 0, "passed_count": 0, "failed_count": 0},
    )
    assert conf_3 == 0.55


def test_calculate_confidence_test_failure_penalty():
    # Failed test suite with exit_code != 0
    conf = calculate_confidence(
        retry_count=1,
        test_results={"exit_code": 1, "passed_count": 2, "failed_count": 2},
    )
    # Starts at 0.85, scaled by pass ratio (0.50) -> 0.425 -> 0.42 or 0.43
    assert conf < 0.60
    assert conf >= 0.20


def test_calculate_confidence_coverage_completeness():
    # Factor in coverage percentage (e.g. 90%)
    conf = calculate_confidence(
        retry_count=0,
        test_results={"exit_code": 0, "passed_count": 10, "failed_count": 0, "coverage": 90.0},
    )
    # 80% + 20% * 0.90 = 0.98 -> 0.98
    assert conf == 0.98


def test_calculate_latency_impact_direct_measurements():
    # Pre: 1.25s, Post: 1.50s -> +0.25s / +250.0ms (+20.0%)
    impact = calculate_latency_impact(
        pre_test_duration=1.25,
        post_test_duration=1.50,
    )
    assert isinstance(impact, LatencyImpact)
    assert impact.duration_delta_seconds == 0.25
    assert impact.duration_delta_ms == 250.0
    assert impact.percentage_change == 20.0
    assert impact.is_regression is True
    assert "+250.0ms" in impact.description


def test_calculate_latency_impact_improvement():
    # Pre: 2.0s, Post: 1.6s -> -0.4s / -400.0ms (-20.0%)
    impact = calculate_latency_impact(
        pre_test_duration=2.0,
        post_test_duration=1.6,
    )
    assert impact.duration_delta_seconds == -0.4
    assert impact.duration_delta_ms == -400.0
    assert impact.is_regression is False
    assert "-400.0ms" in impact.description


def test_calculate_latency_impact_estimated_from_complexity():
    # No direct test durations, estimate overhead based on patch complexity
    patch_with_loop = """
+for item in items:
+    process(item)
+for k, v in dict_items:
+    save(k, v)
"""
    impact = calculate_latency_impact(
        patch_code=patch_with_loop,
    )
    assert impact.duration_delta_ms > 0
    assert "estimated overhead" in impact.description


def test_compute_and_save_task_metrics_persists_to_agent_state():
    state: dict = {
        "task_id": "test-task-123",
        "patch": SAMPLE_PATCH,
        "retry_count": 1,
        "test_results": {
            "exit_code": 0,
            "passed_count": 12,
            "failed_count": 0,
            "pre_test_duration": 1.2,
            "post_test_duration": 1.35,
        },
    }

    metrics = compute_and_save_task_metrics(state)

    # 1. Returned metrics dict structure
    assert metrics["lines_added"] == 6
    assert metrics["lines_deleted"] == 1
    assert metrics["files_changed"] == 1
    assert metrics["formatted_diff"] == "+6 / -1"
    assert metrics["retry_count"] == 1
    assert metrics["tests_passed"] == 12
    assert "confidence" in metrics
    assert "latency_impact" in metrics

    # 2. Assert direct persistence to AgentState keys
    assert "metrics" in state
    assert state["metrics"] == metrics
    assert state["confidence_score"] == metrics["confidence"]
    assert state["diff_stats"] == metrics["diff_stats"]
    assert state["latency_impact"] == metrics["latency_impact"]


def test_metrics_engine_class_interface():
    engine = MetricsEngine()
    diff = engine.parse_diff(SAMPLE_PATCH)
    assert diff.lines_added == 6
    assert diff.lines_deleted == 1

    conf = engine.calculate_confidence(retry_count=0)
    assert conf == 1.0

    state: dict = {"patch": SAMPLE_PATCH, "retry_count": 0}
    res = engine.compute_all(state, pre_test_duration=0.5, post_test_duration=0.6)
    assert res["formatted_diff"] == "+6 / -1"
    assert "confidence" in res
    assert state["metrics"]["formatted_diff"] == "+6 / -1"
