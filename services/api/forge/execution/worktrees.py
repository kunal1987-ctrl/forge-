"""Git Worktree Manager for isolated task mutations.

This module provides a WorktreeManager class to manage ephemeral Git worktrees,
enabling isolated code mutations and patch applications without modifying
the primary working directory.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorktreeManager:
    """Manages ephemeral Git worktrees for isolated task execution.

    Wraps the Git CLI via subprocess to create dedicated worktrees, apply patches,
    and cleanly prune workspaces and temporary branches once operations complete.
    """

    def __init__(
        self,
        repo_path: str,
        base_worktree_dir: str = "/tmp/forge/worktrees",
    ) -> None:
        """Initialize the WorktreeManager and validate the repository.

        Args:
            repo_path: Absolute or relative path to the base Git repository.
            base_worktree_dir: Base directory where task worktrees will be created.
                Defaults to '/tmp/forge/worktrees'.

        Raises:
            FileNotFoundError: If the specified repo_path does not exist or is not a directory.
            ValueError: If the directory is not a valid Git repository.
        """
        self.repo_path = Path(repo_path).resolve()
        self.base_worktree_dir = base_worktree_dir

        if not self.repo_path.exists() or not self.repo_path.is_dir():
            raise FileNotFoundError(
                f"Base repository path does not exist or is not a directory: {self.repo_path}"
            )

        # Validate that repo_path is inside a valid git repository
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

    def _get_worktree_path(self, task_id: str) -> str:
        """Construct the absolute path for a task worktree.

        Args:
            task_id: Unique task identifier.

        Returns:
            The normalized worktree directory path.
        """
        # Using normalized path ensures compatibility across OS environments
        target_path = Path(self.base_worktree_dir) / task_id
        return str(target_path)

    def _get_branch_name(self, task_id: str) -> str:
        """Construct the branch name for a task.

        Args:
            task_id: Unique task identifier.

        Returns:
            The branch name (e.g., 'forge/task-{task_id}').
        """
        return f"forge/task-{task_id}"

    def create_worktree(self, task_id: str, base_branch: str = "main") -> str:
        """Create a new Git branch and isolated worktree for a task.

        Creates branch named 'forge/task-{task_id}' and initializes a new
        worktree at '/tmp/forge/worktrees/{task_id}'.

        Args:
            task_id: Unique identifier for the current task.
            base_branch: Branch or commit reference to branch from. Defaults to 'main'.

        Returns:
            The filesystem path to the newly created worktree.

        Raises:
            RuntimeError: If the Git worktree creation command fails.
        """
        worktree_path = self._get_worktree_path(task_id)
        branch_name = self._get_branch_name(task_id)

        # Ensure parent directory exists
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "git",
            "worktree",
            "add",
            "-b",
            branch_name,
            worktree_path,
            base_branch,
        ]

        logger.info(
            "Creating worktree at '%s' for branch '%s' from '%s'",
            worktree_path,
            branch_name,
            base_branch,
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

        return worktree_path

    def apply_patch(self, worktree_path: str, patch_text: str) -> bool:
        """Write and apply a unified diff patch to the target worktree.

        Writes the patch to a temporary file inside the worktree and applies it
        using 'git apply'. Cleans up the temporary file upon completion.

        Args:
            worktree_path: Filesystem path to the target worktree.
            patch_text: Unified diff patch content to apply.

        Returns:
            True if the patch was applied cleanly, False otherwise.
        """
        if not os.path.exists(worktree_path):
            logger.error("Target worktree path does not exist: %s", worktree_path)
            return False

        temp_patch_path: Optional[str] = None
        try:
            # Create a temporary patch file within the worktree directory
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

            cmd = ["git", "apply", "--whitespace=nowarn", temp_patch_path]
            logger.info("Applying patch to worktree at '%s'", worktree_path)

            result = subprocess.run(
                cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.warning(
                    "git apply failed with exit code %d: %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return False

            return True

        except Exception as exc:
            logger.exception("Unexpected error while applying patch: %s", exc)
            return False

        finally:
            if temp_patch_path and os.path.exists(temp_patch_path):
                try:
                    os.remove(temp_patch_path)
                except OSError as rm_err:
                    logger.debug("Failed to remove temporary patch file '%s': %s", temp_patch_path, rm_err)

    def cleanup(self, task_id: str) -> None:
        """Remove the target worktree and delete the associated task branch.

        Runs 'git worktree remove --force' and deletes the temporary branch
        'forge/task-{task_id}'.

        Args:
            task_id: Unique identifier of the task being cleaned up.
        """
        worktree_path = self._get_worktree_path(task_id)
        branch_name = self._get_branch_name(task_id)

        logger.info("Cleaning up worktree and branch for task '%s'", task_id)

        # 1. Remove the worktree via Git
        remove_cmd = ["git", "worktree", "remove", "--force", worktree_path]
        remove_res = subprocess.run(
            remove_cmd,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )
        if remove_res.returncode != 0:
            logger.warning(
                "git worktree remove returned non-zero code (%d): %s",
                remove_res.returncode,
                remove_res.stderr.strip(),
            )

        # 2. Prune any stale worktree references
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )

        # 3. If worktree directory remains on disk, clean it up manually
        if os.path.exists(worktree_path):
            try:
                shutil.rmtree(worktree_path, ignore_errors=True)
            except Exception as rm_err:
                logger.warning("Failed to delete leftover directory '%s': %s", worktree_path, rm_err)

        # 4. Force delete the temporary task branch
        delete_branch_cmd = ["git", "branch", "-D", branch_name]
        branch_res = subprocess.run(
            delete_branch_cmd,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )
        if branch_res.returncode != 0:
            logger.warning(
                "git branch -D returned non-zero code (%d): %s",
                branch_res.returncode,
                branch_res.stderr.strip(),
            )
