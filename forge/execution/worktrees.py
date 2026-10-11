"""Git Worktree Manager for isolated task mutations.

Re-exports WorktreeManager from services.api.forge.execution.worktrees.
"""

from services.api.forge.execution.worktrees import (
    PatchResult,
    WorktreeManager,
    validate_patch_format,
)

__all__ = ["WorktreeManager", "PatchResult", "validate_patch_format"]
