"""Process and Docker Container Execution Manager for FORGE.

Tracks active subprocesses (git clones, test runners, linters) and Docker test containers
associated with a task_id, providing instant, forceful termination on abort requests.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("forge.execution.process_manager")

_LOCK = threading.Lock()
_TASK_PROCESSES: Dict[str, Set[subprocess.Popen[Any]]] = {}
_TASK_CONTAINERS: Dict[str, Set[str]] = {}


def register_task_process(task_id: str, proc: subprocess.Popen[Any]) -> None:
    """Register an active subprocess associated with a task_id."""
    if not task_id or not proc:
        return
    with _LOCK:
        if task_id not in _TASK_PROCESSES:
            _TASK_PROCESSES[task_id] = set()
        _TASK_PROCESSES[task_id].add(proc)
        logger.debug("Registered process PID %s for task '%s'", proc.pid, task_id)


def unregister_task_process(task_id: str, proc: subprocess.Popen[Any]) -> None:
    """Unregister a completed subprocess."""
    if not task_id or not proc:
        return
    with _LOCK:
        if task_id in _TASK_PROCESSES:
            _TASK_PROCESSES[task_id].discard(proc)
            if not _TASK_PROCESSES[task_id]:
                _TASK_PROCESSES.pop(task_id, None)
        logger.debug("Unregistered process PID %s for task '%s'", proc.pid, task_id)


def register_task_container(task_id: str, container_id_or_name: str) -> None:
    """Register an active Docker container name or ID associated with a task_id."""
    if not task_id or not container_id_or_name:
        return
    with _LOCK:
        if task_id not in _TASK_CONTAINERS:
            _TASK_CONTAINERS[task_id] = set()
        _TASK_CONTAINERS[task_id].add(container_id_or_name)
        logger.debug("Registered container '%s' for task '%s'", container_id_or_name, task_id)


def unregister_task_container(task_id: str, container_id_or_name: str) -> None:
    """Unregister a removed Docker container."""
    if not task_id or not container_id_or_name:
        return
    with _LOCK:
        if task_id in _TASK_CONTAINERS:
            _TASK_CONTAINERS[task_id].discard(container_id_or_name)
            if not _TASK_CONTAINERS[task_id]:
                _TASK_CONTAINERS.pop(task_id, None)


def run_tracked_subprocess(
    task_id: Optional[str],
    cmd: List[str],
    timeout: Optional[float] = None,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Execute a subprocess while tracking its PID under task_id for instant abort termination."""
    stdout_opt = subprocess.PIPE if capture_output else None
    stderr_opt = subprocess.PIPE if capture_output else None

    proc = subprocess.Popen(
        cmd,
        stdout=stdout_opt,
        stderr=stderr_opt,
        text=text,
        cwd=cwd,
        env=env,
        **kwargs,
    )

    if task_id:
        register_task_process(task_id, proc)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        retcode = proc.poll() or 0
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        timeout_sec: float = (
            float(exc.timeout) if exc.timeout is not None else float(timeout or 0.0)
        )
        raise subprocess.TimeoutExpired(
            cmd, timeout_sec, output=exc.output, stderr=exc.stderr
        ) from exc
    except Exception:
        proc.kill()
        proc.communicate()
        raise
    finally:
        if task_id:
            unregister_task_process(task_id, proc)

    if check and retcode != 0:
        raise subprocess.CalledProcessError(retcode, cmd, output=stdout, stderr=stderr)

    return subprocess.CompletedProcess(cmd, retcode, stdout, stderr)


def terminate_task_processes(task_id: str) -> int:
    """Forcefully terminate all registered OS subprocesses for task_id.

    Returns:
        Number of active processes terminated.
    """
    if not task_id:
        return 0

    with _LOCK:
        procs = list(_TASK_PROCESSES.pop(task_id, set()))

    killed = 0
    for proc in procs:
        try:
            if proc.poll() is None:
                logger.info("Terminating subprocess PID %s for task '%s'", proc.pid, task_id)
                # Terminate on Windows via taskkill tree kill
                if sys.platform == "win32" and proc.pid:
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True,
                            timeout=5,
                        )
                    except Exception:
                        pass
                try:
                    proc.kill()
                except Exception:
                    pass
                killed += 1
        except Exception as exc:
            logger.debug("Error killing process for task %s: %s", task_id, exc)

    return killed


def terminate_task_docker_containers(task_id: str) -> int:
    """Forcefully kill and remove all Docker sandbox containers associated with task_id.

    Returns:
        Number of containers forcefully killed/removed.
    """
    if not task_id:
        return 0

    clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(task_id))
    killed = 0

    with _LOCK:
        registered_containers = list(_TASK_CONTAINERS.pop(task_id, set()))

    # 1. Attempt Docker Python SDK
    try:
        import docker

        client = docker.from_env()

        # Remove specifically registered container IDs/names
        for c_id in registered_containers:
            try:
                c = client.containers.get(c_id)
                try:
                    c.kill()
                except Exception:
                    pass
                c.remove(force=True, v=True)
                killed += 1
                logger.info("Removed registered Docker container %s for task %s", c_id, task_id)
            except Exception:
                pass

        # Scan all containers for sandbox name, labels, or task_id matches
        try:
            containers = client.containers.list(all=True)
            for c in containers:
                c_name = getattr(c, "name", "") or ""
                c_labels = getattr(c, "labels", {}) or {}
                if (
                    f"forge-sandbox-{clean_id}" in c_name
                    or clean_id in c_name
                    or c_labels.get("forge.task_id") == clean_id
                    or c_labels.get("task_id") == clean_id
                ):
                    try:
                        c.kill()
                    except Exception:
                        pass
                    try:
                        c.remove(force=True, v=True)
                        killed += 1
                        logger.info(
                            "Forcefully killed Docker container %s for task %s", c_name, task_id
                        )
                    except Exception:
                        pass
        except Exception as list_err:
            logger.debug("Docker container search error: %s", list_err)

    except Exception as sdk_err:
        logger.debug("Docker SDK not available or error: %s", sdk_err)

    # 2. CLI fallback
    try:
        container_name = f"forge-sandbox-{clean_id}"
        cli_res = subprocess.run(
            ["docker", "rm", "-f", "-v", container_name],
            capture_output=True,
            timeout=5,
        )
        if cli_res.returncode == 0:
            killed += 1
    except Exception:
        pass

    return killed


def terminate_task_execution(task_id: str, repo_id: Optional[str] = None) -> Dict[str, Any]:
    """Instant master termination handler for a task.

    Halts active asyncio worker tasks, terminates active subprocesses/git clones,
    forcefully removes Docker sandbox test containers, and cleans ephemeral worktrees.

    Args:
        task_id: Unique task identifier.
        repo_id: Optional repository identifier.

    Returns:
        Dict summarizing terminated resources.
    """
    if not task_id:
        return {"task_id": "", "killed_processes": 0, "killed_containers": 0, "status": "aborted"}

    logger.info("Executing terminate_task_execution for task '%s'", task_id)

    # 1. Cancel and unregister asyncio worker task in RUNNING_TASKS
    worker_cancelled = False
    try:
        from services.api.forge.orchestration.worker import RUNNING_TASKS

        running_task = RUNNING_TASKS.get(task_id)
        if running_task and not running_task.done():
            running_task.cancel()
            worker_cancelled = True
            logger.info("Cancelled running asyncio worker task for '%s'", task_id)
        RUNNING_TASKS.pop(task_id, None)
    except Exception as worker_err:
        logger.debug("Worker cancel notice: %s", worker_err)

    # 2. Terminate active OS subprocesses (git clones, test runs, linters)
    killed_procs = terminate_task_processes(task_id)

    # 3. Forcefully kill Docker sandbox containers
    killed_containers = terminate_task_docker_containers(task_id)

    # 4. Revoke Celery task if Celery is running
    try:
        from services.api.forge.worker.celery_app import celery_app

        celery_app.control.revoke(task_id, terminate=True, signal="SIGKILL")
        logger.info("Revoked Celery worker task for '%s'", task_id)
    except Exception:
        pass

    # 5. Atomic worktree cleanup & dangling Docker resources
    try:
        from services.api.forge.execution.cleanup import (
            cleanup_dangling_docker_resources,
            cleanup_worktree_atomic,
        )

        cleanup_dangling_docker_resources(task_id)

        clean_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(task_id))
        base_repo = repo_id if (repo_id and os.path.exists(repo_id)) else os.getcwd()
        base_wt_dir = os.getenv("FORGE_WORKTREE_BASE_DIR", "/tmp/forge_worktrees")

        # Scan for ephemeral worktree directories
        candidates = [
            os.path.join(base_wt_dir, clean_id),
            os.path.join(base_repo, f".forge_wt_{clean_id}"),
        ]
        if os.path.exists(base_wt_dir):
            for entry in os.listdir(base_wt_dir):
                if entry.startswith(clean_id):
                    candidates.append(os.path.join(base_wt_dir, entry))

        for cand in set(candidates):
            if os.path.exists(cand):
                try:
                    cleanup_worktree_atomic(
                        repo_path=base_repo,
                        worktree_path=cand,
                        task_id=task_id,
                        branch_name=f"forge/task-{task_id}",
                    )
                except Exception as wt_err:
                    logger.debug("Worktree cleanup notice: %s", wt_err)
    except Exception as cleanup_err:
        logger.debug("Cleanup notice during task termination: %s", cleanup_err)

    return {
        "task_id": task_id,
        "worker_cancelled": worker_cancelled,
        "killed_processes": killed_procs,
        "killed_containers": killed_containers,
        "status": "aborted",
    }
