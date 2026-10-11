"""Git Worktree Manager for isolated task mutations.

Provides the WorktreeManager class to manage ephemeral Git worktrees,
validate patch formats, prevent path traversal attacks, and guarantee cleanup
via comprehensive try/finally blocks and context managers.
"""

from __future__ import annotations

import atexit
import contextlib
import difflib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Generator, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from services.api.forge.execution.diff_validator import DiffValidator

logger = logging.getLogger(__name__)


class PatchResult(BaseModel):
    """Structured result of unified diff application."""

    success: bool = Field(description="True if patch was applied cleanly, False otherwise")
    error: Optional[str] = Field(default=None, description="Detailed error diagnostics if patch failed")
    error_type: Optional[str] = Field(default=None, description="Category of error if rejected by validator")
    message: Optional[str] = Field(default=None, description="Actionable feedback or diagnostic message")

    def __bool__(self) -> bool:
        return self.success

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)



def validate_patch_format(patch_text: str) -> bool:
    """Validate that patch text follows the unified git diff format.

    Requires standard unified diff header markers ('---' and '+++')
    and hunk headers ('@@').

    Args:
        patch_text: Raw diff text to validate.

    Returns:
        True if the text is a valid unified diff, False otherwise.
    """
    if not patch_text or not patch_text.strip():
        return False

    clean_text = patch_text.strip()

    # Check for presence of unified diff headers
    has_minus = bool(re.search(r"^---\s+\S+", clean_text, re.MULTILINE))
    has_plus = bool(re.search(r"^\+\+\+\s+\S+", clean_text, re.MULTILINE))
    has_hunk = bool(re.search(r"^@@\s+-\d+.*?\+\d+.*?@@", clean_text, re.MULTILINE))

    # Also accept standard git diff header
    has_git_header = bool(re.search(r"^diff --git\s+\S+\s+\S+", clean_text, re.MULTILINE))

    if (has_minus and has_plus and has_hunk) or (has_git_header and has_hunk):
        return True

    return False


class WorktreeManager:
    """Manages ephemeral Git worktrees for isolated task execution.

    Hardened against path traversal and leakage with strict input validation,
    path boundary verification, comprehensive try/finally chains, and atexit hooks.
    """

    def __init__(
        self,
        base_repo_path: Optional[str] = None,
        repo_path: Optional[str] = None,
        base_worktree_dir: str = "/tmp/forge/worktrees",
    ) -> None:
        """Initialize the WorktreeManager and validate base repository.

        Args:
            base_repo_path: Path to the base Git repository.
            repo_path: Alternate alias for base_repo_path.
            base_worktree_dir: Directory where task worktrees will be rooted.
                Defaults to '/tmp/forge/worktrees'.

        Raises:
            FileNotFoundError: If repo path does not exist or is not a directory.
            ValueError: If repo path is not inside a valid Git repository.
        """
        chosen_path = repo_path or base_repo_path or os.getcwd()
        self.repo_path = Path(chosen_path).resolve()
        self.base_repo_path = str(self.repo_path)
        self.base_worktree_dir = os.path.abspath(base_worktree_dir)
        self._active_tasks: Set[str] = set()

        if not self.repo_path.exists() or not self.repo_path.is_dir():
            raise FileNotFoundError(
                f"Base repository path does not exist or is not a directory: {self.repo_path}"
            )

        # Validate that repo_path is inside a valid Git repository
        try:
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.repo_path),
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            raise ValueError(
                f"Path is not a valid Git repository: {self.repo_path}"
            ) from err

        # Register exit hook for process termination safety
        atexit.register(self._emergency_cleanup)

    def _validate_task_id(self, task_id: str) -> None:
        """Validate that task_id is a valid UUID or strictly alphanumeric string.

        Prevents directory traversal via path manipulation characters ('..', '/', '\\').

        Args:
            task_id: Identifier string to validate.

        Raises:
            ValueError: If task_id is empty, malformed, or contains non-alphanumeric characters.
        """
        if not task_id or not isinstance(task_id, str):
            raise ValueError(f"task_id must be a non-empty string, got: {task_id!r}")

        clean_id = task_id.strip()

        # Check if task_id is a valid UUID string
        is_uuid = False
        try:
            uuid.UUID(clean_id)
            is_uuid = True
        except (ValueError, AttributeError):
            pass

        # Check if task_id is strictly alphanumeric (also accept standard hyphenated slugs/alphanumerics)
        is_alnum = clean_id.isalnum() or bool(re.match(r"^[a-zA-Z0-9_-]+$", clean_id))

        if not (is_uuid or is_alnum):
            raise ValueError(
                f"Security violation: task_id '{task_id}' must be a valid UUID or strictly alphanumeric string."
            )

        # Explicit safety check against path traversal tokens
        if ".." in clean_id or "/" in clean_id or "\\" in clean_id:
            raise ValueError(
                f"Security violation: Directory traversal characters detected in task_id '{task_id}'."
            )

    def _get_worktree_path(self, task_id: str) -> str:
        """Construct and verify the absolute path for a task worktree.

        Enforces that the resulting path physically resides strictly inside base_worktree_dir.

        Args:
            task_id: Validated task identifier.

        Returns:
            Verified absolute filesystem path string for the worktree.

        Raises:
            ValueError: If path traversal out of base_worktree_dir is detected.
        """
        self._validate_task_id(task_id)

        base_dir_abs = os.path.abspath(self.base_worktree_dir)
        target_path_abs = os.path.abspath(os.path.join(base_dir_abs, task_id))

        # Strict directory traversal verification using commonpath boundary check
        try:
            common = os.path.commonpath([base_dir_abs, target_path_abs])
        except ValueError as err:
            raise ValueError(
                f"Path traversal detected: worktree path '{target_path_abs}' is outside base '{base_dir_abs}'"
            ) from err

        if common != base_dir_abs or target_path_abs == base_dir_abs:
            raise ValueError(
                f"Security violation: worktree path '{target_path_abs}' escapes base directory '{base_dir_abs}'"
            )

        return target_path_abs

    def _get_branch_name(self, task_id: str) -> str:
        """Construct the branch name for a task."""
        self._validate_task_id(task_id)
        return f"forge/task-{task_id}"

    def _emergency_cleanup(self) -> None:
        """Emergency atexit handler to ensure no active worktrees leak on crash."""
        for task_id in list(self._active_tasks):
            try:
                self.cleanup(task_id)
            except Exception:
                pass

    def create_worktree(
        self,
        task_id: str,
        base_commit: str = "main",
        base_branch: Optional[str] = None,
    ) -> str:
        """Create a new Git branch and isolated worktree for a task.

        Validates task_id format and guarantees the worktree path resides
        inside base_worktree_dir.

        Args:
            task_id: Unique task identifier (must be a valid UUID or alphanumeric).
            base_commit: Base commit or branch to branch from. Defaults to 'main'.
            base_branch: Alternate alias for base_commit.

        Returns:
            Absolute path to the new worktree directory.

        Raises:
            ValueError: If task_id is invalid or attempts path traversal.
            RuntimeError: If Git worktree creation fails.
        """
        # Validate task_id and verify path containment
        worktree_path = self._get_worktree_path(task_id)
        branch_name = self._get_branch_name(task_id)
        ref = base_branch or base_commit or "HEAD"

        # If ref does not resolve (e.g. repo uses 'master' instead of 'main'), fallback to 'HEAD'
        check_ref = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )
        if check_ref.returncode != 0:
            logger.info("Reference '%s' not found in repo. Falling back to 'HEAD'.", ref)
            ref = "HEAD"

        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)

        cmd = [
            "git",
            "worktree",
            "add",
            "-b",
            branch_name,
            worktree_path,
            ref,
        ]

        logger.info(
            "Creating worktree at '%s' for branch '%s' from '%s'",
            worktree_path,
            branch_name,
            ref,
        )

        result = subprocess.run(
            cmd,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error("Git worktree creation failed: %s", error_msg)
            raise RuntimeError(
                f"Failed to create git worktree for task '{task_id}': {error_msg}"
            )

        self._active_tasks.add(task_id)
        return worktree_path

    @contextlib.contextmanager
    def managed_worktree(
        self,
        task_id: str,
        base_commit: str = "main",
        base_branch: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """Context manager guaranteeing worktree cleanup in all exit/error paths.

        Usage:
            with wm.managed_worktree("task-123") as worktree_path:
                # Do work in worktree_path
                ...
            # Guaranteed cleanup after block exits or raises.

        Args:
            task_id: Unique task identifier.
            base_commit: Base commit/branch.
            base_branch: Alternate alias for base_commit.

        Yields:
            worktree_path: Path to the ephemeral worktree.
        """
        ref = base_branch or base_commit or "main"
        worktree_path = self.create_worktree(task_id, base_commit=ref)
        try:
            yield worktree_path
        finally:
            self.cleanup(task_id)

    def _fallback_python_patch(self, worktree_path: str, raw_diff: str) -> bool:
        """Fallback search and replace engine when git apply fails.

        Parses raw_diff to extract target file paths, header mappings, and hunk blocks.
        Finds the closest matching block of text in target files (ignoring whitespace)
        and performs in-place replacement.

        Args:
            worktree_path: Path to the target worktree root.
            raw_diff: Raw unified diff string.

        Returns:
            True if at least one hunk was successfully applied, False otherwise.
        """
        if not raw_diff or not raw_diff.strip():
            return False

        # Split raw_diff into file sections
        file_sections = [
            s.strip()
            for s in re.split(r"(?=(?:^|\n)(?:--- [^\n]+\n\+\+\+ |diff --git ))", raw_diff)
            if s.strip()
        ]
        if not file_sections:
            return False

        base_worktree_abs = os.path.abspath(worktree_path)
        applied_any = False

        for section in file_sections:
            # Extract target file path from --- and +++ headers
            m = re.search(r"--- (?:a/)?([^\s\t\n]+)\s*\n\+\+\+ (?:b/)?([^\s\t\n]+)", section)
            if not m:
                continue

            old_file, new_file = m.group(1), m.group(2)
            rel_path = new_file if new_file != "/dev/null" else old_file
            rel_path = re.sub(r"^[ab]/", "", rel_path)

            target_path = os.path.normpath(os.path.join(base_worktree_abs, rel_path))

            # Directory traversal boundary protection
            try:
                common = os.path.commonpath([base_worktree_abs, target_path])
                if common != base_worktree_abs:
                    logger.warning("Path traversal prevented in patch fallback: '%s'", target_path)
                    continue
            except ValueError:
                continue

            # Extract hunk blocks starting with @@
            hunk_blocks = [
                h.strip()
                for h in re.split(r"(?=(?:^|\n)@@\s+-\d+)", section)
                if h.strip() and h.strip().startswith("@@")
            ]
            if not hunk_blocks:
                continue

            if os.path.exists(target_path):
                with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            else:
                content = ""
                os.makedirs(os.path.dirname(target_path), exist_ok=True)

            f_lines = content.splitlines()

            for hunk in hunk_blocks:
                hunk_lines = hunk.splitlines()[1:]
                old_lines: List[str] = []
                new_lines: List[str] = []
                removed_lines: List[str] = []
                added_lines: List[str] = []

                for hl in hunk_lines:
                    if hl.startswith("-"):
                        removed_lines.append(hl[1:])
                        old_lines.append(hl[1:])
                    elif hl.startswith("+"):
                        added_lines.append(hl[1:])
                        new_lines.append(hl[1:])
                    elif hl.startswith(" "):
                        old_lines.append(hl[1:])
                        new_lines.append(hl[1:])
                    elif not hl.startswith("\\"):
                        # Context line where leading space was omitted
                        old_lines.append(hl)
                        new_lines.append(hl)

                # Pure addition to empty or new file
                if not old_lines and added_lines:
                    f_lines.extend(added_lines)
                    applied_any = True
                    continue

                # 1. Direct exact block search
                old_block = "\n".join(old_lines)
                new_block = "\n".join(new_lines)
                current_text = "\n".join(f_lines)
                if old_block and old_block in current_text:
                    current_text = current_text.replace(old_block, new_block, 1)
                    f_lines = current_text.splitlines()
                    applied_any = True
                    continue

                # 2. Fuzzy sliding window search (ignoring leading/trailing whitespace)
                k = len(old_lines)
                best_score = 0.0
                best_slice: Optional[Tuple[int, int]] = None

                if k > 0 and len(f_lines) >= k:
                    for i in range(len(f_lines) - k + 1):
                        window = f_lines[i : i + k]
                        score = difflib.SequenceMatcher(
                            None,
                            [line.strip() for line in window],
                            [line.strip() for line in old_lines],
                        ).ratio()
                        if score > best_score:
                            best_score = score
                            best_slice = (i, i + k)

                if best_score >= 0.6 and best_slice:
                    start, end = best_slice
                    f_lines[start:end] = new_lines
                    applied_any = True
                elif removed_lines:
                    # 3. Match specifically on lines to be removed
                    r_k = len(removed_lines)
                    r_best_score = 0.0
                    r_best_slice: Optional[Tuple[int, int]] = None
                    if len(f_lines) >= r_k:
                        for i in range(len(f_lines) - r_k + 1):
                            r_window = f_lines[i : i + r_k]
                            r_score = difflib.SequenceMatcher(
                                None,
                                [line.strip() for line in r_window],
                                [line.strip() for line in removed_lines],
                            ).ratio()
                            if r_score > r_best_score:
                                r_best_score = r_score
                                r_best_slice = (i, i + r_k)

                    if r_best_score >= 0.7 and r_best_slice:
                        start, end = r_best_slice
                        f_lines[start:end] = added_lines
                        applied_any = True

            trailing_newline = "\n" if content.endswith("\n") or not content else ""
            new_content = "\n".join(f_lines) + trailing_newline
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(new_content)

        return applied_any

    def apply_patch(self, worktree_path: str, patch_text: str) -> PatchResult:
        """Apply unified diff using DiffValidator pre-flight check, git apply --3way, and Python fallback.

        1. Runs DiffValidator.validate(raw_diff, worktree_path) before disk writes or git execution.
           If validation fails, instantly aborts without running git apply or Python fallback.
        2. If valid, attempts git apply --3way --whitespace=fix.
        3. If git apply fails, attempts Python fallback search/replace engine.

        Args:
            worktree_path: Path to the target worktree.
            patch_text: Unified diff content to apply.

        Returns:
            PatchResult: Structured result with success, error_type, message, and error.
        """
        if not os.path.exists(worktree_path):
            error_msg = f"Target worktree path does not exist: {worktree_path}"
            logger.error(error_msg)
            return PatchResult(success=False, error=error_msg, message=error_msg)

        if not patch_text or not patch_text.strip():
            error_msg = "Patch application failed: empty patch provided."
            logger.warning(error_msg)
            return PatchResult(
                success=False,
                error_type="CORRUPTED_HUNK",
                error=error_msg,
                message=error_msg,
            )

        # STEP 1: Pre-flight Diff Validation (In-memory AST & truncation check)
        validation = DiffValidator.validate(patch_text, worktree_path=worktree_path)
        if not validation.is_valid:
            logger.warning(
                "WorktreeManager: Pre-flight diff validation failed (%s): %s",
                validation.error_type,
                validation.actionable_feedback,
            )
            return PatchResult(
                success=False,
                error_type=validation.error_type,
                message=validation.actionable_feedback,
                error=validation.actionable_feedback,
            )

        temp_patch_path: Optional[str] = None
        git_error_output = ""

        try:
            with tempfile.NamedTemporaryFile(
                dir=worktree_path,
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
            ) as temp_patch:
                temp_patch.write(patch_text)
                temp_patch.flush()
                temp_patch_path = temp_patch.name

            # STEP 2: Attempt git apply --3way --whitespace=fix
            apply_cmd = ["git", "apply", "--3way", "--whitespace=fix", temp_patch_path]
            logger.info("Attempting git apply --3way --whitespace=fix on '%s'", worktree_path)
            apply_res = subprocess.run(
                apply_cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )

            if apply_res.returncode == 0:
                logger.info("git apply --3way succeeded on '%s'", worktree_path)
                return PatchResult(
                    success=True,
                    error=None,
                    message="Patch applied cleanly via git apply --3way",
                )

            git_error_output = apply_res.stderr.strip() or apply_res.stdout.strip()
            logger.warning(
                "git apply --3way failed (exit code %d): %s. Triggering Python fallback.",
                apply_res.returncode,
                git_error_output,
            )

        except Exception as exc:
            git_error_output = str(exc)
            logger.warning("Exception during git apply --3way: %s. Triggering Python fallback.", exc)
        finally:
            if temp_patch_path and os.path.exists(temp_patch_path):
                try:
                    os.remove(temp_patch_path)
                except OSError as rm_err:
                    logger.debug("Failed removing temp patch '%s': %s", temp_patch_path, rm_err)

        # STEP 3: Fallback to Python search/replace engine
        try:
            fallback_success = self._fallback_python_patch(worktree_path, patch_text)
            if fallback_success:
                logger.info("Python fallback patch applied successfully on '%s'", worktree_path)
                return PatchResult(
                    success=True,
                    error=None,
                    message="Patch applied successfully via Python fallback engine",
                )
        except Exception as py_err:
            logger.warning("Python fallback patch encountered exception: %s", py_err)

        # STEP 4: Both failed -> return structured error
        error_msg = (
            "Patch application failed. Ensure your unified diff context lines "
            f"exactly match the source file. Here is the Git error: {git_error_output}"
        )
        logger.error(error_msg)
        return PatchResult(success=False, error=error_msg, message=error_msg)

    def reset_worktree(self, worktree_path: str) -> None:
        """Atomically reset the worktree to pristine base commit state.

        Runs `git reset --hard HEAD` and `git clean -fd` inside worktree_path to wipe
        any debris or partial modifications from previous failed retries.

        Args:
            worktree_path: Absolute or relative filesystem path to the target worktree.
        """
        if not worktree_path or not os.path.exists(worktree_path):
            logger.warning("reset_worktree: Worktree path '%s' does not exist.", worktree_path)
            return

        target_dir = os.path.abspath(worktree_path)
        logger.info("Atomically resetting worktree to pristine HEAD: %s", target_dir)

        # 1. git reset --hard HEAD
        reset_res = subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )
        if reset_res.returncode != 0:
            logger.warning("reset_worktree: git reset --hard failed: %s", reset_res.stderr.strip())

        # 2. git clean -fd
        clean_res = subprocess.run(
            ["git", "clean", "-fd"],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )
        if clean_res.returncode != 0:
            logger.warning("reset_worktree: git clean -fd failed: %s", clean_res.stderr.strip())

    def get_modified_files(self, worktree_path: str) -> List[str]:
        """Return the list of modified, added, or deleted files in the worktree.

        Runs `git status --porcelain` inside worktree_path and parses modified relative paths.

        Args:
            worktree_path: Absolute or relative filesystem path to the target worktree.

        Returns:
            List of normalized relative file paths that have been modified, added, or deleted.
        """
        if not worktree_path or not os.path.exists(worktree_path):
            return []

        target_dir = os.path.abspath(worktree_path)
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            logger.warning("get_modified_files: git status --porcelain failed: %s", res.stderr.strip())
            return []

        modified_files: List[str] = []
        for line in res.stdout.splitlines():
            clean_line = line.strip()
            if not clean_line:
                continue
            # Porcelain format: XY PATH or XY ORIG -> PATH
            # Status codes take first 2 chars, then space(s), then path
            path_part = line[3:].strip() if len(line) >= 4 else clean_line
            if "->" in path_part:
                path_part = path_part.split("->")[-1].strip()
            path_part = path_part.strip('"\'')
            normalized_path = path_part.replace("\\", "/")
            if normalized_path and normalized_path not in modified_files:
                modified_files.append(normalized_path)

        return modified_files

    def cleanup(self, task_id: str) -> None:
        """Remove target worktree and delete task branch with guaranteed cleanup.

        Uses comprehensive try/finally chains to ensure:
        1. git worktree remove --force is ALWAYS executed.
        2. git worktree prune is ALWAYS executed.
        3. Residual directory deletion is ALWAYS attempted.
        4. Temporary task branch is ALWAYS deleted.

        Args:
            task_id: Unique task identifier to clean up.
        """
        try:
            worktree_path = self._get_worktree_path(task_id)
            branch_name = self._get_branch_name(task_id)
        except ValueError:
            # If task_id validation fails, construct safe path inside base_worktree_dir
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", task_id)
            worktree_path = os.path.join(self.base_worktree_dir, safe_id)
            branch_name = f"forge/task-{safe_id}"

        logger.info("Initiating guaranteed cleanup for task '%s'", task_id)
        self._active_tasks.discard(task_id)

        try:
            # 1. Force remove worktree via Git
            remove_cmd = ["git", "worktree", "remove", "--force", worktree_path]
            res = subprocess.run(
                remove_cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                logger.debug("git worktree remove result: %s", res.stderr.strip())
        finally:
            try:
                # 2. Prune any stale worktree metadata
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(self.repo_path),
                    capture_output=True,
                    text=True,
                )
            finally:
                try:
                    # 3. Force clean directory if still remaining on disk
                    if os.path.exists(worktree_path):
                        shutil.rmtree(worktree_path, ignore_errors=True)
                finally:
                    try:
                        # 4. Force delete the temporary task branch
                        subprocess.run(
                            ["git", "branch", "-D", branch_name],
                            cwd=str(self.repo_path),
                            capture_output=True,
                            text=True,
                        )
                    except Exception as branch_err:
                        logger.debug("Branch deletion notice: %s", branch_err)
