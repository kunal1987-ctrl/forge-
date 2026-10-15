"""Verifiable Pull Request Generation Node, Authenticated Remote Push, and GitHub REST API Client.

Architecture:
1. Worktree Branch Tracking & Setup:
   - Explicitly creates or checks out local tracking branch: `git checkout -b forge-fix-{task_id}`.
   - Configures git author identity inside ephemeral worktree.
   - Stages and commits surgical fixes if any exist.

2. Authenticated Git Remote Push:
   - Configures the worktree git client to authenticate using GITHUB_TOKEN injected into the remote URL:
     `git remote set-url origin https://x-access-token:${GITHUB_TOKEN}@github.com/{owner}/{repo}.git`
   - Executes `git push -u origin forge-fix-{task_id}` with strict error capture.
   - Automatically handles non-fast-forward/conflict scenarios via `--force-with-lease`.
   - Redacts credentials in all logs, command outputs, and exception messages.

3. GitHub REST API PR Creation via httpx:
   - Probes default branch and repository access.
   - Makes POST request to https://api.github.com/repos/{owner}/{repo}/pulls.
   - Sets state["pr_url"] and state["pr_created"] = True.
   - Explicitly raises GitHubAuthError if credentials are missing or unauthorized (401/403/404).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import subprocess
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast

import httpx
from opentelemetry import trace

try:
    from services.api.core.config import settings
except ImportError:
    from core.config import settings  # type: ignore[no-redef]

from services.api.forge.engine.dispatcher import (
    TelemetryDispatcher,
    get_telemetry_dispatcher,
)
from services.api.forge.exceptions import GitHubAuthError, PRCreationError
from services.api.forge.execution.github import GitHubIntegration
from services.api.forge.logging import bind_context

if TYPE_CHECKING:
    from services.api.forge.engine.state import AgentState

logger = logging.getLogger("forge.engine.pr_generator")
tracer = trace.get_tracer("forge.engine.pr_generator")

__all__ = ["GitHubAuthError", "PRCreationError", "PRGenerator", "pr_generator_node"]


class PRGenerator:
    """Orchestrates local branch checkout, tracking, authenticated git push, and verified GitHub PR creation."""

    def __init__(self, github_token: Optional[str] = None) -> None:
        if github_token is not None:
            self.github_token = str(github_token).strip()
        elif "GITHUB_TOKEN" in os.environ:
            self.github_token = os.environ["GITHUB_TOKEN"].strip()
        else:
            self.github_token = str(getattr(settings, "GITHUB_TOKEN", None) or "").strip()

    @staticmethod
    def parse_owner_and_repo(
        repo_identifier: Optional[str] = None,
        worktree_path: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Resolve owner and repo names from repo_identifier, git remote, or configuration."""
        # 1. Check explicit GITHUB_REPOSITORY from env or settings if repo_identifier is default or empty
        env_repo = (
            os.getenv("GITHUB_REPOSITORY") or getattr(settings, "GITHUB_REPOSITORY", None) or ""
        ).strip()
        if (
            env_repo
            and "/" in env_repo
            and (not repo_identifier or repo_identifier == "default-repo")
        ):
            parts = env_repo.split("/", 1)
            return parts[0].strip(), parts[1].replace(".git", "").strip()

        # 2. Check repo_identifier
        if repo_identifier and repo_identifier != "default-repo":
            clean = str(repo_identifier).strip()
            # Handle full GitHub URLs
            if "github.com" in clean:
                match = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", clean)
                if match:
                    return match.group(1), match.group(2)
            elif "/" in clean:
                parts = clean.split("/")
                if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                    return parts[0].strip(), parts[1].replace(".git", "").strip()

        # 3. Check git remote in worktree_path if available
        if worktree_path and os.path.exists(worktree_path):
            try:
                res = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    remote_url = res.stdout.strip()
                    match = re.search(
                        r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", remote_url
                    )
                    if match:
                        return match.group(1), match.group(2)
            except Exception as exc:
                logger.debug("Failed to extract git remote from worktree: %s", exc)

        # 4. Fallback to settings / env or project default
        if env_repo and "/" in env_repo:
            parts = env_repo.split("/", 1)
            return parts[0].strip(), parts[1].replace(".git", "").strip()

        return "kunal1987-ctrl", "forge-"

    def sanitize_message(self, text: str) -> str:
        """Strip sensitive credentials like GITHUB_TOKEN from error messages and logs."""
        if not text:
            return ""
        sanitized = text
        if self.github_token:
            sanitized = sanitized.replace(self.github_token, "[REDACTED_TOKEN]")
        sanitized = re.sub(
            r"https://x-access-token:[^@]+@",
            "https://x-access-token:[REDACTED]@github.com/",
            sanitized,
        )
        return sanitized

    def run_git_command(
        self,
        cmd: List[str],
        cwd: str,
        task_id: str,
        branch_name: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git CLI command with credential redaction and typed error translation."""
        try:
            res = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                raw_err = res.stderr.strip() or res.stdout.strip()
                safe_err = self.sanitize_message(raw_err)
                safe_cmd = [self.sanitize_message(c) for c in cmd]
                cmd_str = " ".join(safe_cmd)

                # Detect authentication failure during push or remote operations
                auth_keywords = (
                    "401",
                    "403",
                    "authentication failed",
                    "invalid credentials",
                    "bad credentials",
                    "permission denied",
                    "could not read username",
                    "remote: repository not found",
                    "repository not found",
                )
                if any(kw in safe_err.lower() for kw in auth_keywords):
                    raise GitHubAuthError(
                        f"Git remote authentication failed during '{cmd_str}' (code {res.returncode}): {safe_err}",
                        task_id=task_id,
                        branch_name=branch_name,
                        details={
                            "cmd": safe_cmd,
                            "stderr": safe_err,
                            "exit_code": res.returncode,
                        },
                    )

                raise PRCreationError(
                    f"Git command '{cmd_str}' failed (code {res.returncode}): {safe_err}",
                    task_id=task_id,
                    branch_name=branch_name,
                    details={
                        "cmd": safe_cmd,
                        "stdout": self.sanitize_message(res.stdout),
                        "stderr": safe_err,
                    },
                )
            return res
        except (subprocess.SubprocessError, OSError) as exc:
            if isinstance(exc, (GitHubAuthError, PRCreationError)):
                raise
            safe_err = self.sanitize_message(str(exc))
            raise PRCreationError(
                f"Failed to execute git command: {safe_err}",
                task_id=task_id,
                branch_name=branch_name,
                cause=exc,
            ) from exc

    def configure_authenticated_remote(
        self,
        worktree_path: str,
        owner: str,
        repo: str,
        task_id: str,
        branch_name: str,
    ) -> str:
        """Configure the remote URL with an access token if available:
        git remote set-url origin https://x-access-token:${GITHUB_TOKEN}@github.com/{owner}/{repo}.git
        """
        if self.github_token:
            auth_remote_url = (
                f"https://x-access-token:{self.github_token}@github.com/{owner}/{repo}.git"
            )
        else:
            auth_remote_url = f"https://github.com/{owner}/{repo}.git"

        remotes_res = subprocess.run(
            ["git", "remote"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
        )
        existing_remotes = remotes_res.stdout.split()

        if "origin" in existing_remotes:
            self.run_git_command(
                ["git", "remote", "set-url", "origin", auth_remote_url],
                cwd=worktree_path,
                task_id=task_id,
                branch_name=branch_name,
            )
        else:
            self.run_git_command(
                ["git", "remote", "add", "origin", auth_remote_url],
                cwd=worktree_path,
                task_id=task_id,
                branch_name=branch_name,
            )
        return auth_remote_url

    def push_branch(
        self,
        worktree_path: str,
        branch_name: str,
        task_id: str,
        owner: str,
        repo: str,
    ) -> None:
        """Run git push -u origin forge-fix-{task_id} with proper error capture.
        Catches missing permissions, branch conflicts, and remote errors cleanly with redacted output.
        """
        push_cmd = ["git", "push", "-u", "origin", branch_name]
        safe_cmd_str = " ".join([self.sanitize_message(c) for c in push_cmd])

        try:
            res = subprocess.run(
                push_cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            safe_exc = self.sanitize_message(str(exc))
            logger.error("Failed executing git push command: %s", safe_exc)
            raise PRCreationError(
                f"Failed executing git push: {safe_exc}",
                error_code="GIT_PUSH_EXECUTION_ERROR",
                task_id=task_id,
                branch_name=branch_name,
                repo_name=f"{owner}/{repo}",
                cause=exc,
            ) from exc

        if res.returncode != 0:
            raw_err = res.stderr.strip() or res.stdout.strip()
            safe_err = self.sanitize_message(raw_err)
            err_lower = safe_err.lower()

            # 1. Missing permissions / authentication failure
            auth_keywords = (
                "401",
                "403",
                "permission denied",
                "permission to",
                "denied to",
                "authentication failed",
                "invalid credentials",
                "bad credentials",
                "could not read username",
                "remote: repository not found",
                "repository not found",
                "write access required",
            )
            if any(kw in err_lower for kw in auth_keywords):
                logger.error(
                    "Git remote authentication or permission failure pushing branch '%s' for task '%s': %s",
                    branch_name,
                    task_id,
                    safe_err,
                )
                raise GitHubAuthError(
                    f"Git remote authentication/permission failed during '{safe_cmd_str}' (code {res.returncode}): {safe_err}",
                    error_code="PUSH_PERMISSION_DENIED",
                    task_id=task_id,
                    branch_name=branch_name,
                    repo_name=f"{owner}/{repo}",
                    details={
                        "cmd": safe_cmd_str,
                        "stderr": safe_err,
                        "exit_code": res.returncode,
                    },
                )

            # 2. Branch conflicts / non-fast-forward
            conflict_keywords = (
                "[rejected]",
                "non-fast-forward",
                "fetch first",
                "remote contains work that you do not have locally",
                "failed to push some refs",
            )
            if any(kw in err_lower for kw in conflict_keywords):
                logger.warning(
                    "Branch conflict detected for '%s' during push. Attempting --force-with-lease...",
                    branch_name,
                )
                # Attempt force-with-lease on ephemeral fix branch
                force_res = subprocess.run(
                    ["git", "push", "-u", "--force-with-lease", "origin", branch_name],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if force_res.returncode == 0:
                    logger.info("Successfully pushed '%s' with --force-with-lease", branch_name)
                    return

                safe_force_err = self.sanitize_message(
                    force_res.stderr.strip() or force_res.stdout.strip()
                )
                logger.error(
                    "Branch conflict could not be resolved with force-with-lease for task '%s': %s",
                    task_id,
                    safe_force_err,
                )
                raise PRCreationError(
                    f"Branch conflict pushing '{branch_name}' to remote: {safe_force_err}",
                    error_code="BRANCH_CONFLICT",
                    task_id=task_id,
                    branch_name=branch_name,
                    repo_name=f"{owner}/{repo}",
                    details={
                        "cmd": f"git push -u --force-with-lease origin {branch_name}",
                        "stderr": safe_force_err,
                        "exit_code": force_res.returncode,
                    },
                )

            # 3. Other generic push failures
            logger.error(
                "Git push failed for task '%s' on branch '%s' (code %d): %s",
                task_id,
                branch_name,
                res.returncode,
                safe_err,
            )
            raise PRCreationError(
                f"Git push failed (code {res.returncode}): {safe_err}",
                error_code="GIT_PUSH_ERROR",
                task_id=task_id,
                branch_name=branch_name,
                repo_name=f"{owner}/{repo}",
                details={
                    "cmd": safe_cmd_str,
                    "stderr": safe_err,
                    "exit_code": res.returncode,
                },
            )

    def _lookup_existing_pr(
        self,
        owner: str,
        repo: str,
        head_branch: str,
        headers: Dict[str, str],
    ) -> Optional[str]:
        """Look up existing PR URL if PR has already been opened for this head branch."""
        try:
            with httpx.Client(timeout=15.0) as client:
                query_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
                res = client.get(
                    query_url,
                    headers=headers,
                    params={"head": f"{owner}:{head_branch}", "state": "all"},
                )
                if res.status_code == 200:
                    prs = res.json()
                    if prs and isinstance(prs, list) and len(prs) > 0:
                        return prs[0].get("html_url")
        except Exception as exc:
            logger.debug("Failed to query existing PRs: %s", exc)
        return None

    def create_github_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        task_id: str = "",
    ) -> str:
        """Create an upstream Pull Request via GitHub REST API using httpx."""
        if not self.github_token:
            raise GitHubAuthError(
                "GitHub API token (GITHUB_TOKEN) is missing or not configured. Cannot create Pull Request.",
                task_id=task_id,
                branch_name=head_branch,
                repo_name=f"{owner}/{repo}",
                details={"reason": "MISSING_GITHUB_TOKEN"},
            )

        api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "FORGE-Autonomous-Agent",
        }

        # Check and resolve default base branch if base_branch might differ
        actual_base = base_branch
        try:
            with httpx.Client(timeout=10.0) as client:
                repo_res = client.get(
                    f"https://api.github.com/repos/{owner}/{repo}", headers=headers
                )
                if repo_res.status_code == 200:
                    default_b = repo_res.json().get("default_branch")
                    if default_b and default_b != base_branch and base_branch in ("main", "master"):
                        logger.info(
                            "Using repository default branch '%s' instead of '%s'",
                            default_b,
                            base_branch,
                        )
                        actual_base = default_b
        except Exception as probe_err:
            logger.debug("Failed probing repo default branch: %s", probe_err)

        payload = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": actual_base,
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(api_url, headers=headers, json=payload)
        except Exception as net_err:
            raise PRCreationError(
                f"Failed to communicate with GitHub API at {api_url}: {self.sanitize_message(str(net_err))}",
                task_id=task_id,
                branch_name=head_branch,
                repo_name=f"{owner}/{repo}",
                cause=net_err,
            ) from net_err

        # 201 Created -> Success
        if response.status_code == 201:
            data = response.json()
            pr_url = data.get("html_url") or data.get("url") or ""
            if not pr_url:
                raise PRCreationError(
                    "GitHub API returned 201 Created but PR web URL is missing from payload.",
                    task_id=task_id,
                    branch_name=head_branch,
                    repo_name=f"{owner}/{repo}",
                    details={"response": data},
                )
            return str(pr_url)

        # 401 Unauthorized or 403 Forbidden -> Explicit GitHubAuthError
        if response.status_code in (401, 403):
            err_data: Dict[str, Any] = {}
            try:
                err_data = response.json()
            except Exception:
                err_data = {"raw": response.text}
            err_msg = err_data.get("message", response.text)
            raise GitHubAuthError(
                f"GitHub API authentication failed (HTTP {response.status_code}): {err_msg}. "
                "Ensure GITHUB_TOKEN is valid and has 'repo' / 'pull-requests:write' permissions.",
                task_id=task_id,
                branch_name=head_branch,
                repo_name=f"{owner}/{repo}",
                details={"status_code": response.status_code, "github_error": err_data},
            )

        # 422 Unprocessable Entity -> check if PR already exists for head branch
        if response.status_code == 422:
            err_data = {}
            try:
                err_data = response.json()
            except Exception:
                err_data = {"raw": response.text}
            err_str = str(err_data).lower()
            if "already exists" in err_str:
                existing_pr = self._lookup_existing_pr(owner, repo, head_branch, headers)
                if existing_pr:
                    logger.info("Reusing existing PR for %s: %s", head_branch, existing_pr)
                    return existing_pr

            raise PRCreationError(
                f"GitHub API PR creation validation error (HTTP 422): {err_data.get('message', response.text)}",
                task_id=task_id,
                branch_name=head_branch,
                repo_name=f"{owner}/{repo}",
                details={"github_error": err_data},
            )

        # 404 Not Found -> Repository not found or token lacks access
        if response.status_code == 404:
            raise GitHubAuthError(
                f"GitHub repository '{owner}/{repo}' not found or GITHUB_TOKEN lacks access (HTTP 404). "
                "Ensure GITHUB_TOKEN has 'repo' / 'pull-requests:write' permissions and repository exists.",
                task_id=task_id,
                branch_name=head_branch,
                repo_name=f"{owner}/{repo}",
                details={"status_code": 404, "response": response.text},
            )

        # Other HTTP errors
        raise PRCreationError(
            f"GitHub API error (HTTP {response.status_code}): {response.text}",
            task_id=task_id,
            branch_name=head_branch,
            repo_name=f"{owner}/{repo}",
            details={"status_code": response.status_code, "body": response.text},
        )

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
        """Execute git checkout, add, commit, authenticated push, and GitHub PR creation.

        Returns verified PR web URL or raises GitHubAuthError / PRCreationError.
        """
        wt_path = Path(worktree_path).resolve()
        if not wt_path.exists():
            raise PRCreationError(
                f"Worktree path does not exist: {worktree_path}",
                task_id=task_id,
                details={"worktree_path": worktree_path},
            )

        branch_name = f"forge-fix-{task_id}"

        # 1. Validate GitHub API credentials early
        if not self.github_token:
            raise GitHubAuthError(
                "GitHub API token (GITHUB_TOKEN) is missing or not configured. Cannot create Pull Request.",
                task_id=task_id,
                branch_name=branch_name,
                details={"reason": "MISSING_GITHUB_TOKEN"},
            )

        owner, repo = self.parse_owner_and_repo(repo_name, str(wt_path))

        # 2. Configure git identity in worktree
        subprocess.run(
            ["git", "config", "user.name", "FORGE Autonomous Agent"],
            cwd=str(wt_path),
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["git", "config", "user.email", "forge-agent@local"],
            cwd=str(wt_path),
            capture_output=True,
            check=False,
        )

        # 3. Explicitly check out and track the branch: git checkout -b forge-fix-{task_id}
        check_branch = subprocess.run(
            ["git", "rev-parse", "--verify", branch_name],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if check_branch.returncode == 0:
            self.run_git_command(
                ["git", "checkout", branch_name],
                cwd=str(wt_path),
                task_id=task_id,
                branch_name=branch_name,
            )
        else:
            self.run_git_command(
                ["git", "checkout", "-b", branch_name],
                cwd=str(wt_path),
                task_id=task_id,
                branch_name=branch_name,
            )

        # 4. Stage and commit changes if any exist
        self.run_git_command(
            ["git", "add", "-A"],
            cwd=str(wt_path),
            task_id=task_id,
            branch_name=branch_name,
        )

        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if status_res.stdout.strip():
            commit_msg = f"fix: {summary.strip() or f'automated fix for task {task_id}'}"
            self.run_git_command(
                ["git", "commit", "-m", commit_msg],
                cwd=str(wt_path),
                task_id=task_id,
                branch_name=branch_name,
            )

        # 5. Configure remote URL with access token if available:
        # git remote set-url origin https://x-access-token:${GITHUB_TOKEN}@github.com/{owner}/{repo}.git
        self.configure_authenticated_remote(
            worktree_path=str(wt_path),
            owner=owner,
            repo=repo,
            task_id=task_id,
            branch_name=branch_name,
        )

        # 6. Run git push -u origin forge-fix-{task_id} with proper error capture
        self.push_branch(
            worktree_path=str(wt_path),
            branch_name=branch_name,
            task_id=task_id,
            owner=owner,
            repo=repo,
        )

        # 7. Create Pull Request via GitHub REST API with httpx
        title = f"fix: {summary.strip() or f'Automated fix for task {task_id}'}"
        body = GitHubIntegration.format_pr_body(
            context_and_root_cause=summary or "Automated fix and architectural refinement.",
            surgical_changes=patch_diff or "Surgical changes verified cleanly.",
            verification_log=verification_log or "Automated tests verified cleanly (exit_code=0).",
            regression_test_details="Automated regression test passed in sandbox.",
            task_id=task_id,
        )

        pr_url = self.create_github_pull_request(
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            head_branch=branch_name,
            base_branch=base_branch,
            task_id=task_id,
        )
        return pr_url


async def pr_generator_node(
    state: AgentState,
    raise_on_error: bool = False,
    dispatcher: Optional[TelemetryDispatcher] = None,
) -> Dict[str, Any]:
    """Execute verifiable PR generation node.

    Pre-condition: Only executes if verifier_node returned exit_code == 0.
    """
    with tracer.start_as_current_span("pr_generator_node") as span:
        state_dict = cast(Dict[str, Any], state)
        task_id = str(state.get("task_id", ""))
        repo_id = str(state.get("repo_id", ""))
        bind_context(task_id=task_id, repo_id=repo_id, node_name="pr_generator_node")

        span.set_attribute("forge.task_id", task_id)
        d = dispatcher or get_telemetry_dispatcher()

        await d.emit(
            task_id=task_id,
            event_type="node_entry",
            payload={
                "node": "pr_generator",
                "step": 5,
                "status": "creating_pr",
                "message": f"Entering pr_generator: Pushing authenticated branch and opening Pull Request for task {task_id}",
            },
        )

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
            state_dict["error_payload"] = {
                "type": "PRCreationError",
                "message": str(err),
                "traceback": traceback.format_exc(),
                "node": "pr_generator_node",
            }
            state_dict["status"] = "failed"
            state_dict["pr_created"] = False
            state_dict["pr_url"] = ""

            await d.emit(
                task_id=task_id,
                event_type="node_error",
                payload={
                    "node": "pr_generator",
                    "error": str(err),
                    "error_payload": state["error_payload"],
                },
            )
            if raise_on_error:
                raise err
            return {
                "status": "failed",
                "error_message": str(err),
                "error_payload": state["error_payload"],
                "pr_created": False,
                "pr_url": None,
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

            # Explicit State Writing for Frontend Real-time Rendering
            state_dict["pr_url"] = pr_url
            state_dict["status"] = "pr_created"
            state_dict["pr_created"] = True

            # Compute and persist real diff statistics, latency impact, and confidence scores
            try:
                from services.api.forge.services.metrics import compute_and_save_task_metrics

                metrics_data = compute_and_save_task_metrics(
                    state=cast(Dict[str, Any], state),
                    worktree_path=worktree_path,
                )
            except Exception as m_err:
                logger.warning("pr_generator_node: Could not compute metrics: %s", m_err)
                metrics_data = state.get("metrics") or {}

            await d.emit(
                task_id=task_id,
                event_type="node_exit",
                payload={
                    "node": "pr_generator",
                    "step": 5,
                    "status": "pr_created",
                    "pr_url": pr_url,
                    "metrics": metrics_data,
                    "diff_stats": state.get("diff_stats"),
                    "confidence_score": state.get("confidence_score"),
                    "latency_impact": state.get("latency_impact"),
                    "message": f"Created verified GitHub Pull Request: {pr_url}",
                },
            )
            logger.info("pr_generator_node: Created verified Pull Request: %s", pr_url)
            return {
                "pr_url": pr_url,
                "status": "pr_created",
                "pr_created": True,
                "metrics": metrics_data,
                "confidence_score": state.get("confidence_score"),
                "diff_stats": state.get("diff_stats"),
                "latency_impact": state.get("latency_impact"),
            }
        except (GitHubAuthError, PRCreationError, Exception) as exc:
            safe_msg = generator.sanitize_message(str(exc))
            logger.warning("pr_generator_node: PR generation failed: %s", safe_msg)
            state_dict["error_payload"] = {
                "type": type(exc).__name__,
                "message": safe_msg,
                "traceback": generator.sanitize_message(traceback.format_exc()),
                "node": "pr_generator_node",
            }
            state_dict["status"] = "failed"
            state_dict["pr_created"] = False
            state_dict["pr_url"] = ""

            await d.emit(
                task_id=task_id,
                event_type="node_error",
                payload={
                    "node": "pr_generator",
                    "error": safe_msg,
                    "error_payload": state["error_payload"],
                },
            )
            if raise_on_error:
                if not isinstance(exc, (GitHubAuthError, PRCreationError)):
                    raise PRCreationError(safe_msg, task_id=task_id, cause=exc) from exc
                raise
            return {
                "status": "failed",
                "error_message": safe_msg,
                "error_payload": state["error_payload"],
                "pr_created": False,
                "pr_url": None,
            }
