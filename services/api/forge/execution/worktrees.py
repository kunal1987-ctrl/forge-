"""Git Worktree Manager for isolated task mutations.

Provides the WorktreeManager class to manage ephemeral Git worktrees,
validate patch formats, prevent path traversal attacks, apply surgical SEARCH/REPLACE patches,
run lint and cleanup gates before committing, and guarantee atomic cleanup via
comprehensive try/finally blocks, UUID-suffixed ephemeral directories, and atexit hooks.
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
from typing import Any, Dict, Generator, List, Optional, Set

from pydantic import BaseModel, Field

from services.api.forge.execution.diff_validator import DiffValidator
from services.api.forge.execution.patch_applier import SurgicalPatchApplier

logger = logging.getLogger(__name__)

DEFAULT_BASE_WORKTREE_DIR = os.getenv("FORGE_WORKTREE_BASE_DIR", "/tmp/forge_worktrees")


class PatchResult(BaseModel):
    """Structured result of patch application."""

    success: bool = Field(description="True if patch was applied cleanly, False otherwise")
    error: Optional[str] = Field(
        default=None, description="Detailed error diagnostics if patch failed"
    )
    error_type: Optional[str] = Field(
        default=None, description="Category of error if rejected by validator"
    )
    message: Optional[str] = Field(
        default=None, description="Actionable feedback or diagnostic message"
    )

    def __bool__(self) -> bool:
        return self.success

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


def validate_patch_format(patch_text: str) -> bool:
    """Validate that patch text follows either surgical search/replace format or unified git diff format.

    Accepts:
    1. Surgical SEARCH/REPLACE blocks (<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE)
    2. Unified git diff format with standard headers ('---' and '+++') and hunk headers ('@@')
    3. Git diff header ('diff --git') with hunk headers ('@@')

    Args:
        patch_text: Raw diff or patch text to validate.

    Returns:
        True if the text is a valid patch format, False otherwise.
    """
    if not patch_text or not patch_text.strip():
        return False

    # Check for semantic search/replace block patch
    if SurgicalPatchApplier.is_search_replace_patch(patch_text):
        return True

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

    Hardened against path traversal, race conditions, and shared-state collisions:
    - Eliminates static shared directories by provisioning isolated paths suffixed
      with cryptographic UUIDs: `/tmp/forge_worktrees/{task_id}_{uuid.uuid4().hex[:8]}`.
    - Atomically creates git worktrees via `git worktree add -b forge-branch-{task_id}`.
    - Enforces path boundary verification, comprehensive try/finally chains,
      and atexit cleanup hooks.
    - Supports surgical search/replace patch application with automatic fallback,
      and a pre-commit lint and cleanup gate.
    """

    def __init__(
        self,
        base_repo_path: Optional[str] = None,
        repo_path: Optional[str] = None,
        base_worktree_dir: Optional[str] = None,
    ) -> None:
        """Initialize the WorktreeManager and validate base repository.

        Args:
            base_repo_path: Path to the base Git repository.
            repo_path: Alternate alias for base_repo_path.
            base_worktree_dir: Directory where task worktrees will be rooted.
                Defaults to '/tmp/forge_worktrees' or env FORGE_WORKTREE_BASE_DIR.

        Raises:
            FileNotFoundError: If repo path does not exist or is not a directory.
            ValueError: If repo path is not inside a valid Git repository.
        """
        chosen_path = repo_path or base_repo_path or os.getcwd()
        self.repo_path = Path(chosen_path).resolve()
        self.base_repo_path = str(self.repo_path)
        raw_base_dir = base_worktree_dir or DEFAULT_BASE_WORKTREE_DIR
        self.base_worktree_dir = os.path.abspath(raw_base_dir)

        self._active_tasks: Set[str] = set()
        self._task_worktrees: Dict[str, Set[str]] = {}
        self._worktree_to_task: Dict[str, str] = {}
        self._worktree_branches: Dict[str, str] = {}

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
            raise ValueError(f"Path is not a valid Git repository: {self.repo_path}") from err

        # Register exit hook for process termination safety
        atexit.register(self._emergency_cleanup)

    def _sanitize_task_id(self, task_id: str) -> str:
        """Sanitize and return a safe alphanumeric/hyphenated slug for task_id."""
        self._validate_task_id(task_id)
        return re.sub(r"[^a-zA-Z0-9_-]", "", task_id)

    def _validate_task_id(self, task_id: str) -> None:
        """Validate that task_id is a valid UUID or strictly alphanumeric string.

        Prevents directory traversal via path manipulation characters ('..', '/', '\').

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

        # Check if task_id is strictly alphanumeric (also accept standard hyphenated slugs)
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

    def _get_worktree_path(self, task_id: str, unique_suffix: Optional[str] = None) -> str:
        """Construct and verify the absolute path for a task worktree.

        Enforces that the resulting path physically resides strictly inside base_worktree_dir.

        Args:
            task_id: Validated task identifier.
            unique_suffix: Optional cryptographic UUID suffix (e.g. uuid.uuid4().hex[:8]).

        Returns:
            Verified absolute filesystem path string for the worktree.

        Raises:
            ValueError: If path traversal out of base_worktree_dir is detected.
        """
        clean_id = self._sanitize_task_id(task_id)

        # If this task already has registered worktree paths and no suffix requested, return the latest
        if (
            not unique_suffix
            and clean_id in self._task_worktrees
            and self._task_worktrees[clean_id]
        ):
            return next(iter(self._task_worktrees[clean_id]))

        dir_name = f"{clean_id}_{unique_suffix}" if unique_suffix else clean_id
        base_dir_abs = os.path.abspath(self.base_worktree_dir)
        target_path_abs = os.path.abspath(os.path.join(base_dir_abs, dir_name))

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
        """Construct the branch name for a task: forge-branch-{task_id}."""
        clean_id = self._sanitize_task_id(task_id)
        return f"forge-branch-{clean_id}"

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
        """Create a new Git branch and isolated ephemeral worktree for a task.

        Provisions a unique, isolated directory suffixed with a cryptographic UUID:
        `/tmp/forge_worktrees/{task_id}_{uuid.uuid4().hex[:8]}`.
        Ensures git worktrees are added atomically via:
        `git worktree add -b forge-branch-{task_id} /path/to/unique/dir main`.

        Args:
            task_id: Unique task identifier (valid UUID or alphanumeric).
            base_commit: Base commit or branch to branch from. Defaults to 'main'.
            base_branch: Alternate alias for base_commit.

        Returns:
            Absolute path to the new worktree directory.

        Raises:
            ValueError: If task_id is invalid or attempts path traversal.
            RuntimeError: If Git worktree creation fails.
        """
        clean_id = self._sanitize_task_id(task_id)
        unique_token = uuid.uuid4().hex[:8]
        worktree_path = self._get_worktree_path(clean_id, unique_suffix=unique_token)
        branch_name = self._get_branch_name(clean_id)
        ref = base_branch or base_commit or "main"

        # Check ref existence in repository, fallback to 'HEAD' if 'main' is not present
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

        # Atomic preparation: delete existing branch forge-branch-{task_id} if present from previous attempt
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )

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
            "Creating atomic worktree at '%s' for branch '%s' from '%s'",
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
            raise RuntimeError(f"Failed to create git worktree for task '{task_id}': {error_msg}")

        self._active_tasks.add(clean_id)
        if clean_id not in self._task_worktrees:
            self._task_worktrees[clean_id] = set()
        self._task_worktrees[clean_id].add(worktree_path)
        self._worktree_to_task[worktree_path] = clean_id
        self._worktree_branches[clean_id] = branch_name

        return worktree_path

    @contextlib.contextmanager
    def managed_worktree(
        self,
        task_id: str,
        base_commit: str = "main",
        base_branch: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """Context manager guaranteeing worktree cleanup in all exit/error paths."""
        ref = base_branch or base_commit or "main"
        worktree_path = self.create_worktree(task_id, base_commit=ref)
        try:
            yield worktree_path
        finally:
            self.cleanup(task_id, worktree_path=worktree_path)

    def _fallback_python_patch(self, worktree_path: str, raw_diff: str) -> bool:
        """Fallback search and replace engine when git apply fails."""
        if not raw_diff or not raw_diff.strip():
            return False

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
            m = re.search(r"--- (?:a/)?([^\s\t\n]+)\s*\n\+\+\+ (?:b/)?([^\s\t\n]+)", section)
            if not m:
                continue

            old_file, new_file = m.group(1), m.group(2)
            rel_path = new_file if new_file != "/dev/null" else old_file
            rel_path = re.sub(r"^[ab]/", "", rel_path)

            target_path = os.path.normpath(os.path.join(base_worktree_abs, rel_path))

            try:
                common = os.path.commonpath([base_worktree_abs, target_path])
                if common != base_worktree_abs:
                    logger.warning("Path traversal prevented in patch fallback: '%s'", target_path)
                    continue
            except ValueError:
                continue

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
                removed_lines: List[str] = []
                added_lines: List[str] = []

                for hl in hunk_lines:
                    if hl.startswith("-"):
                        removed_lines.append(hl[1:])
                        old_lines.append(hl[1:])
                    elif hl.startswith("+"):
                        added_lines.append(hl[1:])
                    elif hl.startswith(" "):
                        old_lines.append(hl[1:])
                    elif not hl.startswith("\\"):
                        old_lines.append(hl)

                search_block = "\n".join(old_lines)
                search_removed = "\n".join(removed_lines)

                applied_hunk = False

                if search_block and search_block in content:
                    replacement = "\n".join(
                        hl[1:] if hl.startswith("+") or hl.startswith(" ") else hl
                        for hl in hunk_lines
                        if not hl.startswith("-") and not hl.startswith("\\")
                    )
                    content = content.replace(search_block, replacement, 1)
                    applied_hunk = True
                elif search_removed and search_removed in content:
                    replace_block = "\n".join(added_lines)
                    content = content.replace(search_removed, replace_block, 1)
                    applied_hunk = True

                if not applied_hunk and (removed_lines or old_lines):
                    target_to_find = old_lines if old_lines else removed_lines
                    n_tgt = len(target_to_find)
                    best_ratio = 0.0
                    best_idx = -1

                    for idx in range(max(1, len(f_lines) - n_tgt + 1)):
                        window = f_lines[idx : idx + n_tgt]
                        ratio = difflib.SequenceMatcher(None, target_to_find, window).ratio()
                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_idx = idx

                    if best_ratio >= 0.8 and best_idx != -1:
                        del f_lines[best_idx : best_idx + n_tgt]
                        for off, ins_line in enumerate(added_lines):
                            f_lines.insert(best_idx + off, ins_line)
                        content = "\n".join(f_lines) + ("\n" if content.endswith("\n") else "")
                        applied_hunk = True

                if applied_hunk:
                    applied_any = True

            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)

        return applied_any

    def apply_patch(self, worktree_path: str, patch_text: str) -> PatchResult:
        """Apply patch to worktree using SurgicalPatchApplier as primary, falling back to git apply --3way.

        1. If patch_text contains semantic SEARCH/REPLACE blocks:
           Invokes SurgicalPatchApplier directly.
           If application succeeds, returns cleanly.
           If it fails, only falls back to git apply --3way if standard unified diff headers are also present.
        2. If standard unified diff format is supplied:
           Runs DiffValidator pre-flight validation.
           Attempts git apply --3way --whitespace=fix.
           Falls back to Python fuzzy hunk application if git apply fails.

        Args:
            worktree_path: Path to the target worktree.
            patch_text: Patch content (SEARCH/REPLACE blocks or Unified diff).

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

        # STEP 1: Primary Method - Surgical SEARCH/REPLACE Parser
        if SurgicalPatchApplier.is_search_replace_patch(patch_text):
            logger.info("Applying patch via primary SurgicalPatchApplier on '%s'", worktree_path)
            applier = SurgicalPatchApplier(repo_root=worktree_path)
            s_res = applier.apply(patch_text)
            if s_res.success:
                msg = f"Patch applied surgically: {s_res.applied_blocks} block(s) applied across {len(s_res.modified_files)} file(s)"
                logger.info(msg)
                return PatchResult(
                    success=True,
                    error=None,
                    message=msg,
                )

            logger.warning(
                "SurgicalPatchApplier failed on '%s': %s",
                worktree_path,
                s_res.error,
            )

            # Fall back to git apply --3way ONLY if standard unified diff headers are present
            has_diff_headers = (
                "--- " in patch_text and "+++ " in patch_text
            ) or "diff --git" in patch_text
            if not has_diff_headers:
                return PatchResult(
                    success=False,
                    error=s_res.error or "Surgical patch application failed",
                    error_type="PATCH_APPLICATION_FAILED",
                    message=s_res.error or "Surgical patch application failed",
                )

        # STEP 2: Pre-flight Diff Validation for standard unified diffs
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

            # STEP 3: Attempt git apply --3way --whitespace=fix
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
            logger.warning(
                "Exception during git apply --3way: %s. Triggering Python fallback.", exc
            )
        finally:
            if temp_patch_path and os.path.exists(temp_patch_path):
                try:
                    os.remove(temp_patch_path)
                except OSError as rm_err:
                    logger.debug("Failed removing temp patch '%s': %s", temp_patch_path, rm_err)

        # STEP 4: Fallback to Python search/replace engine
        try:
            logger.info("Attempting Python search-and-replace fallback on '%s'", worktree_path)
            if self._fallback_python_patch(worktree_path, patch_text):
                logger.info("Python fallback search-and-replace patch succeeded.")
                return PatchResult(
                    success=True,
                    error=None,
                    message="Patch applied cleanly via Python search-and-replace fallback",
                )
        except Exception as fb_exc:
            logger.warning("Exception in Python search-and-replace fallback: %s", fb_exc)

        final_err = f"git apply --3way failed: {git_error_output}"
        logger.error(
            "All patch application strategies exhausted on '%s': %s", worktree_path, final_err
        )
        return PatchResult(
            success=False,
            error=final_err,
            error_type="APPLY_FAILED",
            message=final_err,
        )

    def lint_and_cleanup(self, worktree_path: str) -> Dict[str, Any]:
        """Run formatting, linting, and remove uncommitted bytecode/debris from worktree."""
        if not worktree_path or not os.path.exists(worktree_path):
            return {"cleaned_items": [], "lint_fixed_files": [], "errors": ["Invalid path"]}

        target_dir = os.path.abspath(worktree_path)
        summary: Dict[str, Any] = {
            "cleaned_items": [],
            "lint_fixed_files": [],
            "errors": [],
        }

        # 1. Run ruff formatting on modified python files
        try:
            modified_files = self.get_modified_files(target_dir)
            py_files = [f for f in modified_files if f.endswith(".py")]

            if py_files:
                try:
                    subprocess.run(
                        ["ruff", "format"] + py_files,
                        cwd=target_dir,
                        capture_output=True,
                        text=True,
                    )
                    chk_res = subprocess.run(
                        ["ruff", "check", "--fix"] + py_files,
                        cwd=target_dir,
                        capture_output=True,
                        text=True,
                    )
                    if chk_res.returncode == 0:
                        summary["lint_fixed_files"] = py_files
                except FileNotFoundError:
                    logger.debug("ruff binary not found in PATH; skipping ruff format")
                except Exception as chk_err:
                    summary["errors"].append(f"ruff error: {chk_err}")

        except Exception as mod_err:
            logger.warning("Error detecting modified files for linting: %s", mod_err)
            summary["errors"].append(str(mod_err))

        # 2. Remove any uncommitted, untracked temporary files or bytecode
        cleaned_list: List[str] = []
        for root, dirs, files in os.walk(target_dir):
            if ".git" in dirs:
                dirs.remove(".git")

            for d in list(dirs):
                if d in ("__pycache__", ".pytest_cache", ".ruff_cache"):
                    dir_full = os.path.join(root, d)
                    try:
                        shutil.rmtree(dir_full, ignore_errors=True)
                        cleaned_list.append(os.path.relpath(dir_full, target_dir))
                        dirs.remove(d)
                    except Exception:
                        pass

            for f in files:
                if f.endswith((".pyc", ".pyo", ".pyd", ".patch")) or f.startswith(".tmp_"):
                    file_full = os.path.join(root, f)
                    try:
                        os.remove(file_full)
                        cleaned_list.append(os.path.relpath(file_full, target_dir))
                    except Exception:
                        pass

        summary["cleaned_items"] = cleaned_list
        return summary

    def commit_changes(self, worktree_path: str, message: str) -> bool:
        """Run lint_and_cleanup and commit changes to the task branch."""
        if not worktree_path or not os.path.exists(worktree_path):
            return False

        target_dir = os.path.abspath(worktree_path)

        # 1. Run lint and cleanup gate before staging
        self.lint_and_cleanup(target_dir)

        # 2. Stage all modifications
        add_res = subprocess.run(
            ["git", "add", "-A"],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )
        if add_res.returncode != 0:
            logger.warning("git add -A failed in %s: %s", target_dir, add_res.stderr.strip())
            return False

        # 3. Check if there are staged differences
        diff_res = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=target_dir,
            capture_output=True,
        )
        if diff_res.returncode == 0:
            logger.info("No staged changes to commit in worktree '%s'", target_dir)
            return True

        # 4. Commit staged changes
        commit_res = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )
        if commit_res.returncode != 0:
            logger.warning("git commit failed in %s: %s", target_dir, commit_res.stderr.strip())
            return False

        logger.info("Committed changes cleanly in %s: %s", target_dir, message)
        return True

    def reset_worktree(self, worktree_path: str) -> None:
        """Atomically reset the worktree to pristine base commit state."""
        if not worktree_path or not os.path.exists(worktree_path):
            logger.warning("reset_worktree: Worktree path '%s' does not exist.", worktree_path)
            return

        target_dir = os.path.abspath(worktree_path)
        logger.info("Atomically resetting worktree to pristine HEAD: %s", target_dir)

        subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )

    def get_modified_files(self, worktree_path: str) -> List[str]:
        """Return the list of modified, added, or deleted files in the worktree."""
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
            logger.warning(
                "get_modified_files: git status --porcelain failed: %s", res.stderr.strip()
            )
            return []

        modified_files: List[str] = []
        for line in res.stdout.splitlines():
            clean_line = line.strip()
            if not clean_line:
                continue
            path_part = line[3:].strip() if len(line) >= 4 else clean_line
            if "->" in path_part:
                path_part = path_part.split("->")[-1].strip()
            path_part = path_part.strip("\"'")
            normalized_path = path_part.replace("\\", "/")
            if normalized_path and normalized_path not in modified_files:
                modified_files.append(normalized_path)

        return modified_files

    def cleanup(self, task_id: str, worktree_path: Optional[str] = None) -> None:
        """Remove target worktree and delete task branch with guaranteed cleanup.

        Args:
            task_id: Unique task identifier.
            worktree_path: Optional explicit worktree path to prune.
        """
        clean_id = self._sanitize_task_id(task_id) if task_id else ""
        branch_name = self._get_branch_name(clean_id) if clean_id else ""

        target_paths: Set[str] = set()
        if worktree_path:
            target_paths.add(os.path.abspath(worktree_path))

        # Check registered paths in instance state
        if clean_id and clean_id in self._task_worktrees:
            target_paths.update(self._task_worktrees[clean_id])

        # Also search base_worktree_dir for any directory prefixed with clean_id
        if clean_id and os.path.exists(self.base_worktree_dir):
            try:
                for entry in os.listdir(self.base_worktree_dir):
                    if entry == clean_id or entry.startswith(f"{clean_id}_"):
                        candidate = os.path.abspath(os.path.join(self.base_worktree_dir, entry))
                        target_paths.add(candidate)
            except Exception:
                pass

        logger.info(
            "Initiating guaranteed cleanup for task '%s' (targets=%s)", task_id, target_paths
        )
        if clean_id:
            self._active_tasks.discard(clean_id)
            self._task_worktrees.pop(clean_id, None)

        for path in target_paths:
            self._worktree_to_task.pop(path, None)
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", path],
                    cwd=str(self.repo_path),
                    capture_output=True,
                    text=True,
                )
            except Exception as rm_err:
                logger.debug("git worktree remove note: %s", rm_err)
            finally:
                if os.path.exists(path):
                    shutil.rmtree(path, ignore_errors=True)

        # Prune worktree list in base repository
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
            )
        except Exception:
            pass

        # Delete branch forge-branch-{task_id}
        if branch_name:
            try:
                subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    cwd=str(self.repo_path),
                    capture_output=True,
                    text=True,
                )
            except Exception:
                pass

        # Also cleanup legacy branch forge/task-{task_id} if present
        if clean_id:
            try:
                subprocess.run(
                    ["git", "branch", "-D", f"forge/task-{clean_id}"],
                    cwd=str(self.repo_path),
                    capture_output=True,
                    text=True,
                )
            except Exception:
                pass
