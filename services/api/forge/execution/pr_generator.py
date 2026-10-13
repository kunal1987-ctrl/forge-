"""Verifiable Pull Request Generation Node and Git Branch Operations.

Enforces:
1. Pre-condition: Only executes after the verifier returns a successful exit code (0).
2. Programmatically executes git commands in the ephemeral worktree:
   - git checkout -b forge-fix-{task_id}
   - git add .
   - git commit -m "fix: {summary}"
   - git push origin forge-fix-{task_id}
3. Calls GitHub API to open verified pull request, returning real pr_url.
4. Raises typed PRCreationError if GitHub API credentials are missing or git push fails.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opentelemetry import trace

try:
    from services.api.core.config import settings
except ImportError:
    from core.config import settings  # type: ignore[no-redef]

from services.api.forge.exceptions import PRCreationError
from services.api.forge.execution.github import GitHubIntegration
from services.api.forge.logging import bind_context

if TYPE_CHECKING:
    from services.api.forge.orchestration.state import AgentState

logger = logging.getLogger("forge.pr_generator")
tracer = trace.get_tracer("forge.pr_generator")


class PRGenerator:
    """Orchestrates git commit, push, and verified GitHub PR creation."""

    def __init__(self, github_token: Optional[str] = None) -> None:
        if github_token is not None:
            self.github_token = github_token
        else:
            self.github_token = (
                getattr(settings, "GITHUB_TOKEN", None) or os.getenv("GITHUB_TOKEN") or ""
            )

    def run_git_command(
        self, cmd: List[str], cwd: str, task_id: str, branch_name: str
    ) -> subprocess.CompletedProcess[str]:
        """Run a git CLI command in worktree cwd, raising PRCreationError on nonzero exit."""
        try:
            res = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                stderr = res.stderr.strip() or res.stdout.strip()
                raise PRCreationError(
                    f"Git command {' '.join(cmd)} failed (code {res.returncode}): {stderr}",
                    task_id=task_id,
                    branch_name=branch_name,
                    details={"cmd": cmd, "stdout": res.stdout, "stderr": res.stderr},
                )
            return res
        except (subprocess.SubprocessError, OSError) as exc:
            if isinstance(exc, PRCreationError):
                raise
            raise PRCreationError(
                f"Failed to execute git command {' '.join(cmd)}: {exc}",
                task_id=task_id,
                branch_name=branch_name,
                cause=exc,
            ) from exc

    def create_pr(
        self,
        worktree_path: str,
        task_id: str,
        summary: str,
        repo_name: Optional[str] = None,
        base_branch: str = "main",
        verification_log: str = "",
        patch_diff: str = "",
    ) -> str:
        """Execute git checkout, add, commit, push, and GitHub PR creation.

        Returns verified PR URL or raises PRCreationError.
        """
        wt_path = Path(worktree_path).resolve()
        if not wt_path.exists():
            raise PRCreationError(
                f"Worktree path does not exist: {worktree_path}",
                task_id=task_id,
                details={"worktree_path": worktree_path},
            )

        branch_name = f"forge-fix-{task_id}"

        # 1. Validate GitHub API credentials early before pushing changes
        if not self.github_token:
            raise PRCreationError(
                "GitHub API token (GITHUB_TOKEN) is missing or not configured. Cannot create Pull Request.",
                task_id=task_id,
                branch_name=branch_name,
                details={"reason": "MISSING_GITHUB_TOKEN"},
            )

        # 2. Programmatically run git checkout -b forge-fix-{task_id}
        check_branch = subprocess.run(
            ["git", "rev-parse", "--verify", branch_name],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        if check_branch.returncode == 0:
            self.run_git_command(
                ["git", "checkout", branch_name], str(wt_path), task_id, branch_name
            )
        else:
            self.run_git_command(
                ["git", "checkout", "-b", branch_name], str(wt_path), task_id, branch_name
            )

        # 2. git add .
        self.run_git_command(["git", "add", "."], str(wt_path), task_id, branch_name)

        # 3. Check if changes exist, then git commit -m "fix: {summary}"
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
        )
        if status_res.stdout.strip():
            commit_msg = f"fix: {summary.strip() or f'automated fix for task {task_id}'}"
            self.run_git_command(
                ["git", "commit", "-m", commit_msg],
                str(wt_path),
                task_id,
                branch_name,
            )

        # 4. git push origin forge-fix-{task_id}
        self.run_git_command(
            ["git", "push", "origin", branch_name],
            str(wt_path),
            task_id,
            branch_name,
        )

        # 5. Open verified PR via GitHub API
        effective_repo = (
            repo_name or getattr(settings, "GITHUB_REPOSITORY", None) or "kunal1987-ctrl/forge-"
        )
        gh = GitHubIntegration(token=self.github_token)
        try:
            pr_data = gh.create_pull_request(
                repo_name=effective_repo,
                title=f"fix: {summary.strip() or f'Automated fix for task {task_id}'}",
                head_branch=branch_name,
                base_branch=base_branch,
                context_and_root_cause=summary,
                surgical_changes=patch_diff or "Surgical changes verified cleanly.",
                verification_log=verification_log
                or "Automated tests verified cleanly (exit_code=0).",
                task_id=task_id,
            )
            pr_url = pr_data.get("url") or pr_data.get("pr_url") or ""
            if not pr_url:
                raise PRCreationError(
                    "GitHub API did not return a valid PR URL.",
                    task_id=task_id,
                    branch_name=branch_name,
                    repo_name=effective_repo,
                )
            return str(pr_url)
        except Exception as api_err:
            if isinstance(api_err, PRCreationError):
                raise
            raise PRCreationError(
                f"GitHub Pull Request creation failed: {api_err}",
                task_id=task_id,
                branch_name=branch_name,
                repo_name=effective_repo,
                cause=api_err,
            ) from api_err


async def pr_generator_node(state: AgentState, raise_on_error: bool = False) -> Dict[str, Any]:
    """Execute verifiable PR generation node.

    Pre-condition: Only executes if verifier_node returned exit_code == 0.
    """
    with tracer.start_as_current_span("pr_generator_node") as span:
        task_id = str(state.get("task_id", ""))
        repo_id = str(state.get("repo_id", ""))
        bind_context(task_id=task_id, repo_id=repo_id, node_name="pr_generator_node")

        span.set_attribute("forge.task_id", task_id)

        # 1. Verification pre-condition check
        test_results = state.get("test_results") or {}
        exit_code = test_results.get("exit_code", 1)
        passed_count = test_results.get("passed_count", 0)

        if exit_code != 0 or (passed_count == 0 and "exit_code" in test_results):
            err = PRCreationError(
                f"Cannot generate PR: test suite did not pass (exit_code={exit_code}, passed={passed_count}).",
                task_id=task_id,
                details={"test_results": test_results},
            )
            state["error_payload"] = {
                "type": "PRCreationError",
                "message": str(err),
                "traceback": traceback.format_exc(),
                "node": "pr_generator_node",
            }
            if raise_on_error:
                raise err
            return {
                "status": "failed",
                "error_message": str(err),
                "error_payload": state["error_payload"],
            }

        worktree_path = str(state.get("worktree_path") or state.get("repo_path") or "")
        summary = str(
            state.get("summary") or state.get("issue_description") or state.get("prompt") or ""
        )
        patch_diff = str(state.get("patch") or state.get("generated_diff") or "")
        verification_log = str(state.get("verification_log") or "")

        generator = PRGenerator()
        try:
            pr_url = generator.create_pr(
                worktree_path=worktree_path,
                task_id=task_id,
                summary=summary,
                repo_name=repo_id if "/" in repo_id else None,
                verification_log=verification_log,
                patch_diff=patch_diff,
            )
            logger.info("pr_generator_node: Created verified Pull Request: %s", pr_url)
            return {
                "pr_url": pr_url,
                "status": "pr_created",
            }
        except (PRCreationError, Exception) as exc:
            logger.warning("pr_generator_node: PR generation failed: %s", exc)
            state["error_payload"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "node": "pr_generator_node",
            }
            if raise_on_error:
                if not isinstance(exc, PRCreationError):
                    raise PRCreationError(str(exc), task_id=task_id, cause=exc) from exc
                raise
            return {
                "status": "failed",
                "error_message": str(exc),
                "error_payload": state["error_payload"],
            }
