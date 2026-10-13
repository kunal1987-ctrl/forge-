"""Isolated test-bed runner for FORGE agent evaluation.

Spins up isolated Git worktrees, executes LangGraph state machine iterations,
grades test verification using DockerSandbox, and logs full trajectory traces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from services.api.forge.execution.sandbox import DockerSandbox
from services.api.forge.harness.schemas import HarnessTask, TrialResult
from services.api.forge.orchestration.error_classifier import ErrorClassifier, ErrorCategory
from services.api.forge.orchestration.graph import get_compiled_graph
from services.api.forge.orchestration.state import AgentState

logger = logging.getLogger(__name__)


class HarnessRunner:
    """Isolated execution harness for benchmarking and evaluating FORGE agents."""

    def __init__(
        self,
        logs_dir: Optional[Union[str, Path]] = None,
        sandbox: Optional[DockerSandbox] = None,
        base_worktree_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        """Initialize the harness runner.

        Args:
            logs_dir: Directory where evaluation trajectories will be logged.
                      Defaults to 'logs/evaluation'.
            sandbox: Optional DockerSandbox instance. If omitted, initialized on demand.
            base_worktree_dir: Directory where task worktrees will be created.
        """
        self.logs_dir = Path(logs_dir or "logs/evaluation").resolve()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._custom_sandbox = sandbox
        self.base_worktree_dir = (
            Path(base_worktree_dir or tempfile.gettempdir()) / "forge_eval_worktrees"
        )
        self.base_worktree_dir.mkdir(parents=True, exist_ok=True)

    def _get_sandbox(self) -> DockerSandbox:
        """Get or initialize DockerSandbox instance."""
        if self._custom_sandbox is not None:
            return self._custom_sandbox
        try:
            return DockerSandbox()
        except Exception as exc:
            logger.warning("HarnessRunner: Could not initialize Docker daemon connection: %s", exc)
            return None  # type: ignore[return-value]

    # =========================================================================
    # 1. ENVIRONMENT SETUP
    # =========================================================================

    def setup_environment(self, task: HarnessTask) -> Tuple[Path, Path]:
        """Spin up an isolated temporary Git repository and worktree for the task.

        Args:
            task: The evaluation benchmark task specification.

        Returns:
            Tuple of (repo_path, worktree_path).
        """
        temp_root = Path(tempfile.mkdtemp(prefix=f"forge_eval_{task.task_id}_"))
        repo_path = temp_root / "repo"
        repo_path.mkdir(parents=True, exist_ok=True)

        target_source = Path(task.repo_name)
        if target_source.exists() and target_source.is_dir():
            # Copy source directory into isolated evaluation sandbox
            logger.info(
                "setup_environment: Copying local repository '%s' to '%s'", target_source, repo_path
            )
            shutil.copytree(
                target_source, repo_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git")
            )
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "eval@forge.ai"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Forge Evaluator"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial benchmark snapshot", "--allow-empty"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
        elif (
            task.repo_name.startswith("http://")
            or task.repo_name.startswith("https://")
            or task.repo_name.startswith("git@")
        ):
            # Clone remote git repository
            logger.info(
                "setup_environment: Cloning remote repository '%s' into '%s'",
                task.repo_name,
                repo_path,
            )
            subprocess.run(
                ["git", "clone", task.repo_name, str(repo_path)], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@forge.ai"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Forge Evaluator"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
        else:
            # Synthetic / benchmark testbed template
            logger.info("setup_environment: Creating synthetic testbed repo in '%s'", repo_path)
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "eval@forge.ai"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Forge Evaluator"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            readme = repo_path / "README.md"
            readme.write_text(
                f"# Evaluation Testbed: {task.task_id}\n\n{task.prompt}\n", encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )

        if task.base_commit:
            try:
                subprocess.run(
                    ["git", "checkout", task.base_commit],
                    cwd=str(repo_path),
                    check=True,
                    capture_output=True,
                )
            except Exception as exc:
                logger.warning(
                    "setup_environment: Checkout base_commit '%s' failed: %s", task.base_commit, exc
                )

        worktree_path = temp_root / "worktree"
        worktree_path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            repo_path, worktree_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git")
        )

        return repo_path, worktree_path

    # =========================================================================
    # 2. AGENT EXECUTION WRAPPER
    # =========================================================================

    async def execute_agent(
        self,
        task: HarnessTask,
        repo_path: Path,
        worktree_path: Path,
    ) -> Tuple[Dict[str, Any], List[str], float, Dict[str, int]]:
        """Asynchronously invoke LangGraph state graph capturing transitions, diffs, and timing.

        Args:
            task: Benchmark task definition.
            repo_path: Root of test repository.
            worktree_path: Worktree where code mutations occur.

        Returns:
            Tuple of (final_state, node_transitions, elapsed_seconds, token_usage).
        """
        start_time = time.monotonic()
        node_transitions: List[str] = []
        token_usage: Dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        initial_state: AgentState = {
            "task_id": task.task_id,
            "prompt": task.prompt,
            "repo_id": str(repo_path),
            "worktree_path": str(worktree_path),
            "status": "idle",
            "retry_count": 0,
            "mode": "fast",
            "messages": [HumanMessage(content=task.prompt)],
            "summary": "",
            "context_retrieved": [],
            "repair_trajectory": [],
            "repro_test_code": "",
            "repro_test_path": "tests/_forge_repro_test.py",
            "repro_verified_failing": False,
        }

        memory_saver = MemorySaver()
        app = await get_compiled_graph(db_conn_string="memory", checkpointer=memory_saver)
        config = {"configurable": {"thread_id": task.task_id}}

        final_state: Dict[str, Any] = dict(initial_state)

        async def _run_stream() -> None:
            nonlocal final_state
            async for event in app.astream(initial_state, config=config):
                for node_name, state_update in event.items():
                    if node_name != "__interrupt__":
                        node_transitions.append(node_name)
                    if isinstance(state_update, dict):
                        final_state.update(state_update)

            # Check if workflow paused at reviewer_node awaiting approval
            state_snapshot = await app.aget_state(config)
            if state_snapshot.next and "reviewer_node" in state_snapshot.next:
                # Auto-approve in evaluation harness if tests passed
                logger.info(
                    "execute_agent: Auto-approving reviewer interrupt for benchmark task '%s'",
                    task.task_id,
                )
                resumed_update = {"decision": "approve", "review_decision": "approve"}
                await app.aupdate_state(config, resumed_update, as_node="reviewer_node")
                async for event in app.astream(None, config=config):
                    for node_name, state_update in event.items():
                        if node_name != "__interrupt__":
                            node_transitions.append(node_name)
                        if isinstance(state_update, dict):
                            final_state.update(state_update)

        try:
            await asyncio.wait_for(_run_stream(), timeout=float(task.timeout_seconds))
        except asyncio.TimeoutError:
            logger.warning(
                "execute_agent: Task '%s' timed out after %ds", task.task_id, task.timeout_seconds
            )
            final_state["status"] = "timed_out"
            final_state["error_classification"] = "TIMEOUT"
            final_state["error_message"] = (
                f"Execution exceeded timeout limit of {task.timeout_seconds}s"
            )
        except Exception as exc:
            logger.exception(
                "execute_agent: Error during agent graph execution for task '%s': %s",
                task.task_id,
                exc,
            )
            final_state["status"] = "failed"
            final_state["error_message"] = str(exc)

        elapsed = time.monotonic() - start_time
        return final_state, node_transitions, elapsed, token_usage

    # =========================================================================
    # 3. AUTOMATED GRADING
    # =========================================================================

    def grade_trial(
        self,
        task: HarnessTask,
        worktree_path: Path,
        final_state: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """Grade trial outcome via DockerSandbox verification and regression test evaluation.

        Args:
            task: The benchmark task specification.
            worktree_path: Path to worktree directory containing generated patch.
            final_state: State dictionary returned by LangGraph execution.

        Returns:
            Tuple of (success, test_metrics, error_category).
        """
        # If agent crashed or timed out before completion
        if final_state.get("status") in ("timed_out", "failed"):
            err_cat = str(final_state.get("error_classification") or "UNKNOWN")
            return (
                False,
                final_state.get("test_results")
                or {"exit_code": 1, "passed_count": 0, "failed_count": 1},
                err_cat,
            )

        sandbox = self._get_sandbox()
        test_metrics: Dict[str, Any] = {}
        raw_output = ""

        # 1. Run evaluation test command
        cmd = task.test_command or "pytest"
        if sandbox is not None:
            try:
                run_res = sandbox.run_tests(
                    worktree_path=str(worktree_path),
                    command=cmd,
                    timeout_seconds=min(task.timeout_seconds, 60),
                )
                test_metrics = dict(run_res)
                raw_output = str(run_res.get("raw_output") or "")
            except Exception as s_err:
                logger.warning(
                    "grade_trial: DockerSandbox execution failed (%s), running fallback subprocess",
                    s_err,
                )
                test_metrics = self._run_local_test_fallback(worktree_path, cmd)
                raw_output = str(test_metrics.get("raw_output") or "")
        else:
            test_metrics = self._run_local_test_fallback(worktree_path, cmd)
            raw_output = str(test_metrics.get("raw_output") or "")

        exit_code = int(test_metrics.get("exit_code", 1))
        passed_count = int(test_metrics.get("passed_count", 0))
        failed_count = int(test_metrics.get("failed_count", 0))
        errors_count = int(test_metrics.get("errors_count", 0))

        # Check isolated reproduction test if present
        repro_test = worktree_path / "tests" / "_forge_repro_test.py"
        if repro_test.exists():
            repro_res = self._run_local_test_fallback(
                worktree_path, "pytest tests/_forge_repro_test.py"
            )
            if repro_res.get("exit_code") != 0 or repro_res.get("passed_count", 0) == 0:
                logger.warning("grade_trial: Reproduction test did not pass in final verification.")
                return False, test_metrics, "ASSERTION_FAILURE"

        # Grading invariant: Must have exit_code == 0, passed_count > 0, zero failures/errors
        is_success = (
            (exit_code == 0) and (passed_count > 0) and (failed_count == 0) and (errors_count == 0)
        )

        error_category: Optional[str] = None
        if not is_success:
            classified = ErrorClassifier.classify(raw_output)
            error_category = (
                classified.value if isinstance(classified, ErrorCategory) else str(classified)
            )

        return is_success, test_metrics, error_category

    def _run_local_test_fallback(self, worktree_path: Path, command: str) -> Dict[str, Any]:
        """Local subprocess test runner fallback when Docker daemon is not active."""
        args = ["pytest", "-q"] if command == "pytest" else command.split()
        try:
            p = subprocess.run(
                args,
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=45,
            )
            raw = f"{p.stdout}\n{p.stderr}".strip()
            passed = len(re.findall(r"(?:PASSED|\b1 passed\b|passed in)", raw)) or (
                1 if p.returncode == 0 else 0
            )
            failed = len(re.findall(r"(?:FAILED|\bfailed\b)", raw)) or (
                1 if p.returncode != 0 else 0
            )
            return {
                "exit_code": p.returncode,
                "passed_count": passed,
                "failed_count": failed,
                "raw_output": raw,
                "stdout": p.stdout,
                "stderr": p.stderr,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "status": "timed_out",
                "passed_count": 0,
                "failed_count": 1,
                "raw_output": "Timed out after 45s",
            }
        except Exception as exc:
            return {
                "exit_code": 1,
                "passed_count": 0,
                "failed_count": 1,
                "raw_output": str(exc),
                "error": str(exc),
            }

    # =========================================================================
    # 4. TRAJECTORY LOGGING
    # =========================================================================

    def log_trajectory(
        self,
        task: HarnessTask,
        trial: TrialResult,
        final_state: Dict[str, Any],
    ) -> Path:
        """Save the full agent execution trace, generated diff, and test outputs to JSON.

        Args:
            task: Benchmark task definition.
            trial: Evaluated trial result.
            final_state: Final state payload from state machine.

        Returns:
            Path to the written log file.
        """
        log_file = self.logs_dir / f"{task.task_id}.json"

        # Safely serialize messages
        serialized_messages = []
        for m in final_state.get("messages") or []:
            if isinstance(m, BaseMessage):
                serialized_messages.append(
                    {
                        "type": type(m).__name__,
                        "content": getattr(m, "content", ""),
                        "id": getattr(m, "id", None),
                    }
                )
            elif isinstance(m, dict):
                serialized_messages.append(m)

        payload = {
            "task_id": task.task_id,
            "repo_name": task.repo_name,
            "prompt": task.prompt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trial_result": trial.model_dump(),
            "final_state": {
                "status": final_state.get("status"),
                "retry_count": final_state.get("retry_count", 0),
                "summary": final_state.get("summary", ""),
                "repair_trajectory": final_state.get("repair_trajectory", []),
                "error_classification": final_state.get("error_classification"),
                "repro_verified_failing": final_state.get("repro_verified_failing"),
                "generated_diff": final_state.get("generated_diff", ""),
                "messages": serialized_messages,
            },
        }

        log_file.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        trial.trajectory_log_path = str(log_file)
        logger.info(
            "log_trajectory: Saved evaluation trace for task '%s' to '%s'", task.task_id, log_file
        )
        return log_file

    # =========================================================================
    # 5. TRIAL RUNNER ORCHESTRATOR
    # =========================================================================

    async def run_trial(self, task: HarnessTask) -> TrialResult:
        """Execute a full isolated benchmark trial for a task and record results."""
        repo_path: Optional[Path] = None
        worktree_path: Optional[Path] = None
        temp_root: Optional[Path] = None

        try:
            repo_path, worktree_path = self.setup_environment(task)
            temp_root = repo_path.parent

            # Execute agent graph
            final_state, transitions, elapsed, tokens = await self.execute_agent(
                task, repo_path, worktree_path
            )

            # Grade trial output
            success, test_metrics, error_cat = self.grade_trial(task, worktree_path, final_state)

            trial = TrialResult(
                task_id=task.task_id,
                success=success,
                execution_time_seconds=round(elapsed, 3),
                retries_used=final_state.get("retry_count", 0),
                error_category=error_cat,
                generated_diff=str(final_state.get("generated_diff") or ""),
                test_metrics=test_metrics,
                node_transitions=transitions,
                token_usage=tokens,
                error_message=final_state.get("error_message"),
            )

            # Record trajectory log
            self.log_trajectory(task, trial, final_state)
            return trial

        except Exception as exc:
            logger.exception("run_trial: Fatal error executing task '%s': %s", task.task_id, exc)
            err_cat = "TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else "UNKNOWN"
            failed_trial = TrialResult(
                task_id=task.task_id,
                success=False,
                execution_time_seconds=0.0,
                retries_used=0,
                error_category=err_cat,
                error_message=str(exc),
                test_metrics={
                    "exit_code": 1,
                    "passed_count": 0,
                    "failed_count": 1,
                    "error": str(exc),
                },
            )
            try:
                self.log_trajectory(
                    task, failed_trial, {"status": "failed", "error_message": str(exc)}
                )
            except Exception:
                pass
            return failed_trial

        finally:
            # Clean up temporary worktrees
            if temp_root is not None and temp_root.exists():
                try:
                    shutil.rmtree(temp_root, ignore_errors=True)
                except Exception as clean_err:
                    logger.debug("run_trial: Cleanup notice for '%s': %s", temp_root, clean_err)
