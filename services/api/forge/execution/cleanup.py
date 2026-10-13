"""Atomic Resource Cleanup, Emergency Traps, and Lifecycle Management for FORGE.

Eliminates shared-state hazards, file collisions, and orphaned processes:
- Prunes ephemeral Git worktrees safely (`git worktree remove --force` & `git worktree prune`).
- Releases Redis distributed repository locks immediately in `finally` blocks.
- Cleans up dangling temporary Docker container volumes, sandbox containers, and orphaned test files.
- Registers global Python `atexit` traps to safeguard against crashes, timeout kills, or unhandled exceptions.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Any, AsyncGenerator, Callable, Dict, Generator, List, Optional

from services.api.forge.concurrency.locks import (
    AsyncDistributedRepoLock,
    DistributedRepoLock,
)
from services.api.forge.execution.worktrees import WorktreeManager

logger = logging.getLogger(__name__)

# Thread-safe registry for emergency process termination traps
_LOCK = threading.Lock()
_EMERGENCY_CLEANUP_REGISTRY: Dict[str, Callable[[], None]] = {}


def register_emergency_cleanup(cleanup_id: str, callback: Callable[[], None]) -> None:
    """Register an emergency cleanup callback to run on abrupt process termination."""
    with _LOCK:
        _EMERGENCY_CLEANUP_REGISTRY[cleanup_id] = callback


def unregister_emergency_cleanup(cleanup_id: str) -> None:
    """Unregister an emergency cleanup callback upon orderly task completion."""
    with _LOCK:
        _EMERGENCY_CLEANUP_REGISTRY.pop(cleanup_id, None)


def _global_atexit_handler() -> None:
    """Invoked automatically by Python interpreter on process shutdown or crash."""
    with _LOCK:
        callbacks = list(_EMERGENCY_CLEANUP_REGISTRY.values())
        _EMERGENCY_CLEANUP_REGISTRY.clear()

    for cb in callbacks:
        try:
            cb()
        except Exception as exc:
            logger.debug("Emergency atexit trap execution note: %s", exc)


atexit.register(_global_atexit_handler)


def cleanup_orphaned_files(directory_or_worktree: str) -> List[str]:
    """Find and remove uncommitted test artifacts, patch files, and cache debris.

    Cleans up:
    - Temporary reproduction tests: `tests/_forge_repro_test.py`
    - Cache directories: `__pycache__`, `.pytest_cache`, `.ruff_cache`
    - Temporary patch / diff files: `*.patch`, `.tmp_*`

    Args:
        directory_or_worktree: Target worktree or project directory.

    Returns:
        List of paths successfully cleaned up.
    """
    if not directory_or_worktree or not os.path.exists(directory_or_worktree):
        return []

    cleaned: List[str] = []
    base_dir = os.path.abspath(directory_or_worktree)

    # 1. Clean dedicated reproduction test file if present
    repro_path = os.path.join(base_dir, "tests", "_forge_repro_test.py")
    if os.path.exists(repro_path):
        try:
            os.remove(repro_path)
            cleaned.append(repro_path)
            logger.debug("Removed orphaned reproduction test: %s", repro_path)
        except Exception as e:
            logger.debug("Failed removing %s: %s", repro_path, e)

    # 2. Walk directory to remove debris
    try:
        for root, dirs, files in os.walk(base_dir):
            if ".git" in dirs:
                dirs.remove(".git")

            for d in list(dirs):
                if d in ("__pycache__", ".pytest_cache", ".ruff_cache"):
                    full_d = os.path.join(root, d)
                    try:
                        shutil.rmtree(full_d, ignore_errors=True)
                        cleaned.append(full_d)
                        dirs.remove(d)
                    except Exception:
                        pass

            for f in files:
                if (
                    f.endswith((".patch", ".pyc", ".pyo"))
                    or f.startswith(".tmp_")
                    or f == "_forge_repro_test.py"
                ):
                    full_f = os.path.join(root, f)
                    try:
                        os.remove(full_f)
                        cleaned.append(full_f)
                    except Exception:
                        pass
    except Exception as walk_err:
        logger.debug("cleanup_orphaned_files walk error: %s", walk_err)

    return cleaned


def cleanup_dangling_docker_resources(task_id: str) -> None:
    """Forcefully remove dangling Docker sandbox containers or volumes associated with task_id.

    Args:
        task_id: Unique task identifier.
    """
    if not task_id:
        return

    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(task_id))
    container_name = f"forge-sandbox-{clean_id}"

    # Try Docker Python SDK first
    try:
        import docker

        client = docker.from_env()
        try:
            container = client.containers.get(container_name)
            container.remove(force=True, v=True)
            logger.info("Removed dangling Docker container via SDK: %s", container_name)
        except Exception:
            pass

        # Also prune containers with matching label
        try:
            labeled = client.containers.list(
                all=True,
                filters={"label": f"forge.task_id={clean_id}"},
            )
            for c in labeled:
                try:
                    c.remove(force=True, v=True)
                    logger.info("Removed labeled sandbox container: %s", c.id)
                except Exception:
                    pass
        except Exception:
            pass

        return
    except Exception:
        pass

    # Fallback to docker CLI if docker python package is not configured
    try:
        subprocess.run(
            ["docker", "rm", "-f", "-v", container_name],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def cleanup_worktree_atomic(
    repo_path: str,
    worktree_path: str,
    task_id: str,
    branch_name: Optional[str] = None,
) -> None:
    """Atomically and forcefully remove a Git worktree, prune references, and delete branch.

    Args:
        repo_path: Base Git repository directory.
        worktree_path: Ephemeral worktree directory.
        task_id: Task identifier.
        branch_name: Optional explicit branch name (defaults to forge-branch-{task_id}).
    """
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(task_id))
    branch = branch_name or f"forge-branch-{clean_id}"
    base_repo = os.path.abspath(repo_path)
    target_wt = os.path.abspath(worktree_path) if worktree_path else ""

    logger.info(
        "Executing atomic worktree cleanup for task '%s' (worktree='%s', branch='%s')",
        clean_id,
        target_wt,
        branch,
    )

    # 1. Force removal via git worktree remove
    if target_wt and os.path.exists(target_wt) and target_wt != base_repo:
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", target_wt],
                cwd=base_repo,
                capture_output=True,
                text=True,
            )
        except Exception as rm_err:
            logger.debug("git worktree remove notice: %s", rm_err)

    # 2. Prune worktrees in base repo
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=base_repo,
            capture_output=True,
            text=True,
        )
    except Exception:
        pass

    # 3. Clean filesystem directory if still present
    if target_wt and os.path.exists(target_wt) and target_wt != base_repo:
        try:
            shutil.rmtree(target_wt, ignore_errors=True)
        except Exception:
            pass

    # 4. Force delete git task branch
    if branch:
        try:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=base_repo,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass


@contextlib.contextmanager
def managed_task_lifecycle(
    task_id: str,
    repo_path: str,
    repo_identifier: Optional[str] = None,
    base_commit: str = "main",
    redis_client: Optional[Any] = None,
    lock_timeout: float = 120.0,
    acquire_timeout: float = 30.0,
    blocking: bool = True,
) -> Generator[str, None, None]:
    """Strict lifecycle context manager wrapping worktree creation, lock acquisition, and cleanup.

    Guarantees that:
    1. Exclusive Redis mutex lock is acquired before any git branch/worktree mutations.
    2. Ephemeral UUID-suffixed worktree is provisioned atomically.
    3. Global emergency atexit traps are armed.
    4. IN ALL CASES (success, agent exception, Docker timeout, Celery abort):
       - Worktree is forcefully removed and pruned.
       - Orphaned temporary files and test debris are swept.
       - Dangling Docker sandbox containers are destroyed.
       - Redis distributed lock is released immediately.

    Args:
        task_id: Unique task identifier.
        repo_path: Base Git repository directory path.
        repo_identifier: Optional repository URL or ID used as the Redis lock key.
        base_commit: Git commit/branch to branch from.
        redis_client: Optional synchronous Redis client.
        lock_timeout: Lock lease duration in seconds (TTL).
        acquire_timeout: Maximum wait time to acquire lock in seconds.
        blocking: Whether to wait for lock or fail immediately.

    Yields:
        Absolute filesystem path to the provisioned ephemeral worktree.
    """
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(task_id))
    repo_id = repo_identifier or repo_path

    lock = DistributedRepoLock(
        repo_identifier=repo_id,
        redis_client=redis_client,
        timeout=lock_timeout,
        blocking=blocking,
        blocking_timeout=acquire_timeout,
    )

    # 1. Acquire exclusive lock
    lock.acquire()

    wm = WorktreeManager(repo_path=repo_path)
    worktree_path: Optional[str] = None
    trap_id = f"trap_{clean_id}"

    try:
        # 2. Atomically create ephemeral UUID-suffixed worktree
        worktree_path = wm.create_worktree(clean_id, base_commit=base_commit)

        # 3. Arm emergency atexit trap
        def _emergency_trap() -> None:
            if worktree_path:
                try:
                    wm.cleanup(clean_id, worktree_path=worktree_path)
                except Exception:
                    pass
                try:
                    cleanup_orphaned_files(worktree_path)
                except Exception:
                    pass
            try:
                cleanup_dangling_docker_resources(clean_id)
            except Exception:
                pass
            try:
                lock.release()
            except Exception:
                pass

        register_emergency_cleanup(trap_id, _emergency_trap)

        yield worktree_path

    finally:
        # Disarm emergency trap
        unregister_emergency_cleanup(trap_id)

        # 4. Guaranteed atomic cleanup sequence
        if worktree_path:
            try:
                cleanup_orphaned_files(worktree_path)
            except Exception as e:
                logger.debug("Orphaned files cleanup error: %s", e)

            try:
                wm.cleanup(clean_id, worktree_path=worktree_path)
            except Exception as e:
                logger.warning("Worktree cleanup error for task %s: %s", clean_id, e)

        try:
            cleanup_dangling_docker_resources(clean_id)
        except Exception as e:
            logger.debug("Docker resources cleanup error: %s", e)

        # 5. Release distributed Redis mutex lock immediately
        try:
            lock.release()
        except Exception as e:
            logger.warning("Error releasing repository lock in lifecycle: %s", e)


@contextlib.asynccontextmanager
async def async_managed_task_lifecycle(
    task_id: str,
    repo_path: str,
    repo_identifier: Optional[str] = None,
    base_commit: str = "main",
    redis_client: Optional[Any] = None,
    lock_timeout: float = 120.0,
    acquire_timeout: float = 30.0,
    blocking: bool = True,
) -> AsyncGenerator[str, None]:
    """Asynchronous version of managed_task_lifecycle for FastAPI and asyncio tasks."""
    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(task_id))
    repo_id = repo_identifier or repo_path

    lock = AsyncDistributedRepoLock(
        repo_identifier=repo_id,
        redis_client=redis_client,
        timeout=lock_timeout,
        blocking=blocking,
        blocking_timeout=acquire_timeout,
    )

    await lock.acquire()

    wm = WorktreeManager(repo_path=repo_path)
    worktree_path: Optional[str] = None
    trap_id = f"async_trap_{clean_id}"

    try:
        worktree_path = wm.create_worktree(clean_id, base_commit=base_commit)

        def _emergency_trap() -> None:
            if worktree_path:
                try:
                    wm.cleanup(clean_id, worktree_path=worktree_path)
                except Exception:
                    pass
                try:
                    cleanup_orphaned_files(worktree_path)
                except Exception:
                    pass
            try:
                cleanup_dangling_docker_resources(clean_id)
            except Exception:
                pass

        register_emergency_cleanup(trap_id, _emergency_trap)

        yield worktree_path

    finally:
        unregister_emergency_cleanup(trap_id)

        if worktree_path:
            try:
                cleanup_orphaned_files(worktree_path)
            except Exception:
                pass
            try:
                wm.cleanup(clean_id, worktree_path=worktree_path)
            except Exception as e:
                logger.warning("Async worktree cleanup error: %s", e)

        try:
            cleanup_dangling_docker_resources(clean_id)
        except Exception:
            pass

        try:
            await lock.release()
        except Exception as e:
            logger.warning("Error releasing async repository lock: %s", e)
