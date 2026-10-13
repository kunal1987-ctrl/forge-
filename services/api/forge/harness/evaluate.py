"""CLI benchmark script and evaluation orchestrator for FORGE agents.

Loads benchmark datasets, executes tasks concurrently using a worker pool,
aggregates evaluation metrics, and generates JSON and Markdown benchmark reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

from services.api.forge.harness.runner import HarnessRunner
from services.api.forge.harness.schemas import BenchmarkReport, HarnessTask, TrialResult

logger = logging.getLogger("forge.harness.evaluate")


def load_dataset(dataset_path: Union[str, Path]) -> List[HarnessTask]:
    """Load benchmark dataset from a JSON or JSONL file.

    Args:
        dataset_path: Path to dataset file.

    Returns:
        List of validated HarnessTask objects.

    Raises:
        FileNotFoundError: If dataset path does not exist.
        ValueError: If file content cannot be parsed as valid tasks.
    """
    path = Path(dataset_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Evaluation dataset file not found: '{path}'")

    raw_text = path.read_text(encoding="utf-8").strip()
    tasks: List[HarnessTask] = []

    if path.suffix.lower() == ".jsonl":
        for idx, line in enumerate(raw_text.splitlines(), start=1):
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            data = json.loads(line_str)
            tasks.append(HarnessTask.model_validate(data))
    else:
        # Standard JSON (array or dict with 'tasks' key)
        data = json.loads(raw_text)
        if isinstance(data, list):
            for item in data:
                tasks.append(HarnessTask.model_validate(item))
        elif isinstance(data, dict) and "tasks" in data:
            for item in data["tasks"]:
                tasks.append(HarnessTask.model_validate(item))
        elif isinstance(data, dict):
            # Single task
            tasks.append(HarnessTask.model_validate(data))

    if not tasks:
        raise ValueError(f"No valid HarnessTask definitions found in '{path}'.")

    logger.info("load_dataset: Loaded %d benchmark tasks from '%s'", len(tasks), path)
    return tasks


async def evaluate_benchmark(
    dataset: List[HarnessTask],
    concurrency: int = 2,
    output_dir: Optional[Union[str, Path]] = None,
    runner: Optional[HarnessRunner] = None,
) -> BenchmarkReport:
    """Execute evaluation benchmark across tasks with bounded concurrency.

    Args:
        dataset: List of HarnessTask benchmark instances.
        concurrency: Maximum number of concurrent tasks executed.
        output_dir: Directory where report artifacts are saved.
                    Defaults to 'logs/evaluation/reports'.
        runner: Optional preconfigured HarnessRunner instance.

    Returns:
        Aggregated BenchmarkReport instance.
    """
    out_dir = Path(output_dir or "logs/evaluation/reports").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    active_runner = runner or HarnessRunner(logs_dir=out_dir.parent)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    logger.info(
        "evaluate_benchmark: Commencing benchmark run for %d tasks with concurrency=%d",
        len(dataset),
        concurrency,
    )

    results: List[TrialResult] = []

    async def _run_guarded(task: HarnessTask, index: int) -> TrialResult:
        async with semaphore:
            logger.info(
                "[%d/%d] Starting task: %s (%s)", index, len(dataset), task.task_id, task.repo_name
            )
            res = await active_runner.run_trial(task)
            status_str = "PASSED" if res.success else f"FAILED ({res.error_category or 'UNKNOWN'})"
            logger.info(
                "[%d/%d] Finished task: %s -> %s (time: %.2fs, retries: %d)",
                index,
                len(dataset),
                task.task_id,
                status_str,
                res.execution_time_seconds,
                res.retries_used,
            )
            return res

    tasks_coroutines = [_run_guarded(t, i + 1) for i, t in enumerate(dataset)]
    results = await asyncio.gather(*tasks_coroutines)

    # Compute aggregate metrics
    total_tasks = len(results)
    passed_tasks = sum(1 for r in results if r.success)
    success_rate = (passed_tasks / total_tasks) if total_tasks > 0 else 0.0
    avg_retries = (sum(r.retries_used for r in results) / total_tasks) if total_tasks > 0 else 0.0
    avg_time = (
        (sum(r.execution_time_seconds for r in results) / total_tasks) if total_tasks > 0 else 0.0
    )

    report = BenchmarkReport(
        total_tasks=total_tasks,
        passed_tasks=passed_tasks,
        success_rate=round(success_rate, 4),
        average_retries=round(avg_retries, 2),
        timestamp=datetime.now(timezone.utc).isoformat(),
        average_execution_time_seconds=round(avg_time, 2),
        trials=list(results),
    )

    # Save reports to disk
    ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"benchmark_report_{ts_slug}.json"
    md_path = out_dir / f"benchmark_report_{ts_slug}.md"
    latest_json = out_dir / "latest_report.json"
    latest_md = out_dir / "latest_report.md"

    json_content = json.dumps(report.model_dump(), indent=2)
    md_content = report.to_markdown()

    json_path.write_text(json_content, encoding="utf-8")
    md_path.write_text(md_content, encoding="utf-8")
    latest_json.write_text(json_content, encoding="utf-8")
    latest_md.write_text(md_content, encoding="utf-8")

    logger.info(
        "evaluate_benchmark: Benchmark completed. Reports saved to '%s' and '%s'",
        json_path,
        md_path,
    )
    return report


def main() -> None:
    """CLI entry point for running the evaluation benchmark."""
    parser = argparse.ArgumentParser(
        description="FORGE Agent Evaluation & Benchmark CLI Harness",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=str,
        required=True,
        help="Path to JSON or JSONL file containing benchmark tasks.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Maximum concurrent evaluation trials (bounded to protect container resources).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="logs/evaluation/reports",
        help="Destination directory for output JSON and Markdown reports.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "both"],
        default="both",
        help="Report export format.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging output.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        dataset = load_dataset(args.dataset)
    except Exception as err:
        sys.stderr.write(f"Error loading dataset: {err}\n")
        sys.exit(1)

    try:
        report = asyncio.run(
            evaluate_benchmark(
                dataset=dataset,
                concurrency=args.concurrency,
                output_dir=args.output_dir,
            )
        )
    except KeyboardInterrupt:
        sys.stderr.write("Benchmark interrupted by user.\n")
        sys.exit(130)
    except Exception as err:
        sys.stderr.write(f"Benchmark execution failed: {err}\n")
        sys.exit(1)

    # Print summary output to stdout
    print("\n" + "=" * 60)
    print("           FORGE AGENT BENCHMARK COMPLETE")
    print("=" * 60)
    print(f"Total Tasks:     {report.total_tasks}")
    print(f"Passed:          {report.passed_tasks} ({report.success_rate * 100:.1f}%)")
    print(f"Failed:          {report.total_tasks - report.passed_tasks}")
    print(f"Average Retries: {report.average_retries:.2f}")
    print(f"Average Time:    {report.average_execution_time_seconds:.2f}s")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
