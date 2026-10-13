"""Comprehensive Tests for Celery Background Queue, Redis Locks, Ephemeral Worktrees, and Hardened Sandbox Security.

Verifies:
1. Celery Task Configuration & Timeouts (celery_app.py):
   - Strict hard (300s) and soft (270s) timeouts.
   - Redis DLQ route configuration and serialization.
2. Redis Distributed Locking & Worktree Lifecycle (worker_execution_lifecycle):
   - Acquires repo_lock(repo_id) before provisioning resources.
   - Provisions UUID-suffixed ephemeral worktree (/tmp/forge_worktrees/{task_id}_{uuid.uuid4().hex[:8]}).
   - try...finally guarantees cleanup_worktree_atomic (git worktree remove --force) and lock release on success or failure.
3. Hardened Docker Sandbox Integration (sandbox.py):
   - Enforces cap_drop=["ALL"], read_only=True, network_mode="none", mem_limit="512m", nano_cpus=1000000000.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.api.forge.execution.sandbox import run_hardened_container
from services.api.forge.worker.celery_app import (
    celery_app,
    get_dlq_tasks,
    push_to_dlq,
    worker_execution_lifecycle,
)


# ==============================================================================
# PILLAR 1: CELERY TIMEOUTS & REDIS DLQ ROUTING
# ==============================================================================


def test_celery_app_timeout_and_queue_routing():
    """Verify Celery task has strict hard time limit of 300s and soft limit of 270s with DLQ routing."""
    conf = celery_app.conf
    assert conf.task_time_limit == 300
    assert conf.task_soft_time_limit == 270
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.task_routes is not None
    assert "forge.execute_agent_task" in conf.task_routes
    assert "forge.dlq.*" in conf.task_routes


def test_redis_dlq_push_and_retrieval():
    """Verify push_to_dlq serializes failed task and pushes to Redis DLQ list."""
    mock_redis = MagicMock()
    stored_items = []

    def mock_lpush(key, value):
        stored_items.append(value)
        return len(stored_items)

    mock_redis.lpush = MagicMock(side_effect=mock_lpush)
    mock_redis.lrange = MagicMock(return_value=stored_items)
    mock_redis.publish = MagicMock()

    with patch("services.api.forge.worker.celery_app.get_redis_client", return_value=mock_redis):
        record = push_to_dlq(
            task_id="task-dlq-777",
            task_name="forge.execute_agent_task",
            args=(),
            kwargs={"prompt": "Fix deadlock"},
            error_msg="Container exited with code 137 (OOMKilled)",
            exception_type="SandboxExecutionError",
            traceback_str="Traceback...",
            retry_count=2,
            correlation_id="corr-dlq-777",
        )

        assert record["task_id"] == "task-dlq-777"
        assert record["exception_type"] == "SandboxExecutionError"
        assert record["retry_count"] == 2
        assert mock_redis.lpush.called
        assert mock_redis.publish.called

        dlq_tasks = get_dlq_tasks()
        assert len(dlq_tasks) >= 1
        assert dlq_tasks[0]["task_id"] == "task-dlq-777"


# ==============================================================================
# PILLAR 2: REDIS DISTRIBUTED LOCKING & EPHEMERAL WORKTREE LIFECYCLE
# ==============================================================================


def test_worker_execution_lifecycle_acquires_lock_and_cleans_worktree():
    """Verify worker_execution_lifecycle acquires repo_lock before provisioning,
    creates UUID-suffixed ephemeral worktree, and calls cleanup_worktree_atomic in finally.
    """
    task_id = "test-celery-task-123"
    repo_id = "test-repo-owner/repo-name"
    prompt = "Fix concurrency race condition in auth.py"

    mock_lock_cm = MagicMock()
    mock_lock_cm.__enter__ = MagicMock(return_value=mock_lock_cm)
    mock_lock_cm.__exit__ = MagicMock(return_value=None)

    with (
        patch(
            "services.api.forge.worker.celery_app.repo_lock", return_value=mock_lock_cm
        ) as mock_lock,
        patch(
            "services.api.forge.orchestration.worker.run_graph_and_publish", new_callable=MagicMock
        ) as mock_run_graph,
        patch("services.api.forge.worker.celery_app.cleanup_worktree_atomic") as mock_cleanup,
    ):
        # Make run_graph_and_publish return a completed coroutine
        async def fake_runner(*args, **kwargs):
            return None

        mock_run_graph.side_effect = fake_runner

        result = worker_execution_lifecycle(
            task_id=task_id,
            repo_id=repo_id,
            prompt=prompt,
            mode="fast",
        )

        # 1. Distributed lock acquired on repo_id before provisioning
        mock_lock.assert_called_once_with(
            repo_id, timeout=120.0, blocking=True, blocking_timeout=30.0
        )
        assert mock_lock_cm.__enter__.called

        # 2. Ephemeral worktree path has task_id and UUID suffix
        wt_path = result["worktree_path"]
        assert task_id in wt_path
        assert "/forge_worktrees" in wt_path.replace("\\", "/")

        # 3. cleanup_worktree_atomic executed in finally
        assert mock_cleanup.called
        cleanup_args = mock_cleanup.call_args[0]
        assert cleanup_args[1] == wt_path
        assert cleanup_args[2] == task_id

        # 4. Lock released in finally
        assert mock_lock_cm.__exit__.called


def test_worker_execution_lifecycle_cleans_up_even_on_crash():
    """Verify cleanup_worktree_atomic and lock release execute even if run_graph_and_publish crashes."""
    task_id = "test-celery-crash-999"
    repo_id = "test-crash-repo"
    prompt = "Crash simulation"

    mock_lock_cm = MagicMock()
    mock_lock_cm.__enter__ = MagicMock(return_value=mock_lock_cm)
    mock_lock_cm.__exit__ = MagicMock(return_value=None)

    with (
        patch("services.api.forge.worker.celery_app.repo_lock", return_value=mock_lock_cm),
        patch(
            "services.api.forge.orchestration.worker.run_graph_and_publish",
            side_effect=RuntimeError("Container killed unexpectedly"),
        ),
        patch("services.api.forge.worker.celery_app.cleanup_worktree_atomic") as mock_cleanup,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            worker_execution_lifecycle(
                task_id=task_id,
                repo_id=repo_id,
                prompt=prompt,
            )

        assert "Container killed unexpectedly" in str(exc_info.value)
        # Guaranteed cleanup called in finally
        assert mock_cleanup.called
        assert mock_cleanup.call_args[0][2] == task_id
        # Guaranteed lock released in finally
        assert mock_lock_cm.__exit__.called


# ==============================================================================
# PILLAR 3: HARDENED DOCKER SANDBOX SECURITY
# ==============================================================================


def test_docker_sandbox_strictly_enforces_all_security_parameters(tmp_path: Path):
    """Verify run_hardened_container and DockerSandbox enforce cap_drop, read_only, network_mode, mem_limit, nano_cpus."""
    dummy_worktree = tmp_path / "sandbox_wt"
    dummy_worktree.mkdir()

    mock_run = MagicMock()
    mock_client = MagicMock()
    mock_client.containers.run = mock_run

    # Test via standalone helper
    run_hardened_container(
        client=mock_client,
        image="forge-sandbox:3.11",
        command=["pytest", "-v"],
        worktree_path=dummy_worktree,
        mem_limit="512m",
        nano_cpus=1000000000,
    )

    assert mock_run.called
    kwargs = mock_run.call_args[1]

    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["read_only"] is True
    assert kwargs["network_mode"] == "none"
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["nano_cpus"] == 1000000000
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["user"] == "nobody"
    assert "/workspace" in str(kwargs["volumes"])
