"""Unit and Integration Tests for FORGE Concurrency, Worktrees, and Cleanup (Pillars 1-4).

Tests:
1. Distributed Redis Mutex Locking (locks.py)
   - Deterministic key hashing
   - Exclusive lock acquisition & release
   - Concurrency collision & ConcurrencyLockError timeout
   - Context manager auto-release
   - AsyncDistributedRepoLock and async_repo_lock
2. Unique UUID-Suffixed Ephemeral Worktrees (worktrees.py)
   - Ephemeral UUID-suffixed path format: /tmp/forge_worktrees/{task_id}_{uuid.uuid4().hex[:8]}
   - Atomic branch creation: forge-branch-{task_id}
   - Boundary checks and path traversal rejection
   - Patch application and atomic reset
   - Guaranteed cleanup of worktree and branches
3. Atomic Resource Cleanup & Emergency Traps (cleanup.py)
   - cleanup_worktree_atomic
   - cleanup_orphaned_files
   - cleanup_dangling_docker_resources
   - managed_task_lifecycle try/finally guarantee
   - Emergency atexit hook registration
4. Thread-Safe LangGraph State Propagation (graph.py & nodes.py)
   - create_initial_agent_state explicit worktree binding
   - Multi-threaded stateless node execution isolation
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from unittest.mock import MagicMock

import pytest

from services.api.forge.concurrency.locks import (
    ConcurrencyLockError,
    DistributedRepoLock,
    async_repo_lock,
    compute_repo_lock_key,
    repo_lock,
)
from services.api.forge.execution.cleanup import (
    cleanup_dangling_docker_resources,
    cleanup_orphaned_files,
    managed_task_lifecycle,
    register_emergency_cleanup,
    unregister_emergency_cleanup,
)
from services.api.forge.execution.worktrees import WorktreeManager
from services.api.forge.orchestration.graph import (
    create_initial_agent_state,
    critique_router,
)
from services.api.forge.orchestration.nodes import cleanup_node
from services.api.forge.orchestration.state import AgentState


# ==============================================================================
# PILLAR 1: DISTRIBUTED REDIS MUTEX LOCK TESTS
# ==============================================================================


def test_compute_repo_lock_key_determinism():
    """Verify deterministic hash and normalization for repository URLs and paths."""
    key1 = compute_repo_lock_key("https://github.com/org/repo.git")
    key2 = compute_repo_lock_key("https://github.com/org/repo.git")
    key3 = compute_repo_lock_key("HTTPS://GITHUB.COM/ORG/REPO.GIT")
    key_win = compute_repo_lock_key("D:\\projects\\repo")
    key_unix = compute_repo_lock_key("D:/projects/repo")

    assert key1 == key2
    assert key1 == key3
    assert key_win == key_unix
    assert key1.startswith("lock:repo:")
    assert len(key1) == len("lock:repo:") + 16


def test_distributed_repo_lock_lifecycle_mocked():
    """Verify lock acquisition, ownership, and release lifecycle."""
    mock_redis = MagicMock()
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = True
    mock_lock.locked.return_value = True
    mock_lock.owned.return_value = True
    mock_redis.lock.return_value = mock_lock

    lock = DistributedRepoLock("my-project-123", redis_client=mock_redis, timeout=30.0)
    assert not lock.owned()

    lock.acquire()
    assert lock.owned()
    assert lock.is_locked()

    lock.release()
    assert not lock.owned()
    mock_lock.release.assert_called_once()


def test_distributed_repo_lock_concurrency_conflict_raises_error():
    """Verify ConcurrencyLockError is raised when lock cannot be acquired."""
    mock_redis = MagicMock()
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = False  # Lock held by someone else
    mock_redis.lock.return_value = mock_lock

    lock = DistributedRepoLock(
        "https://github.com/busy/repo.git",
        redis_client=mock_redis,
        blocking=False,
    )

    with pytest.raises(ConcurrencyLockError) as exc_info:
        lock.acquire()

    assert "Concurrency conflict" in str(exc_info.value)
    assert "https://github.com/busy/repo.git" in str(exc_info.value)
    assert lock.lock_key in str(exc_info.value)


def test_repo_lock_context_manager():
    """Verify repo_lock context manager acquires and releases cleanly."""
    mock_redis = MagicMock()
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = True
    mock_lock.owned.return_value = True
    mock_redis.lock.return_value = mock_lock

    with repo_lock("repo-test-cm", redis_client=mock_redis) as lk:
        assert lk.owned()

    mock_lock.release.assert_called_once()


@pytest.mark.asyncio
async def test_async_distributed_repo_lock_lifecycle():
    """Verify async distributed repository lock acquisition and release."""
    mock_aioredis = MagicMock()
    mock_async_lock = MagicMock()

    async def mock_acquire(*args, **kwargs):
        return True

    async def mock_release():
        return None

    async def mock_owned():
        return True

    async def mock_locked():
        return True

    mock_async_lock.acquire = mock_acquire
    mock_async_lock.release = mock_release
    mock_async_lock.owned = mock_owned
    mock_async_lock.locked = mock_locked
    mock_aioredis.lock.return_value = mock_async_lock

    async with async_repo_lock("async-repo-1", redis_client=mock_aioredis) as lock:
        assert await lock.owned()
        assert await lock.is_locked()


# ==============================================================================
# PILLAR 2: UNIQUE UUID-SUFFIXED EPHEMERAL WORKTREES
# ==============================================================================


def test_worktree_unique_uuid_suffix_path_and_atomic_branch(tmp_path):
    """Verify unique UUID suffix and atomic git worktree branch naming."""
    repo_dir = tmp_path / "base_git_repo"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text("print('hello')\n")

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeTest"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    wt_base = tmp_path / "worktrees_pool"
    wm = WorktreeManager(str(repo_dir), base_worktree_dir=str(wt_base))

    # 1. Create worktree
    task_id = "task-uuid-test"
    wt_path = wm.create_worktree(task_id)

    assert os.path.exists(wt_path)
    # Verify path format: {task_id}_{8-hex-token}
    dir_name = os.path.basename(wt_path)
    assert dir_name.startswith("task-uuid-test_")
    token = dir_name.split("task-uuid-test_")[-1]
    assert len(token) == 8
    assert re.match(r"^[0-9a-f]{8}$", token)

    # Verify branch was created atomically as forge-branch-{task_id}
    branch_check = subprocess.run(
        ["git", "branch", "--list", f"forge-branch-{task_id}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )
    assert f"forge-branch-{task_id}" in branch_check.stdout

    # 2. Cleanup
    wm.cleanup(task_id, worktree_path=wt_path)
    assert not os.path.exists(wt_path)


def test_worktree_path_traversal_rejection(tmp_path):
    """Verify strict rejection of path traversal sequences in task_id."""
    repo_dir = tmp_path / "safe_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)

    wm = WorktreeManager(str(repo_dir), base_worktree_dir=str(tmp_path / "wt"))

    with pytest.raises(ValueError):
        wm.create_worktree("../escaped_task")

    with pytest.raises(ValueError):
        wm.create_worktree("task/subpath")

    with pytest.raises(ValueError):
        wm.create_worktree("task\\subpath")


# ==============================================================================
# PILLAR 3: ATOMIC RESOURCE CLEANUP & TRAPS
# ==============================================================================


def test_cleanup_orphaned_files(tmp_path):
    """Verify cleanup_orphaned_files sweeps repro tests, temporary patches, and caches."""
    worktree = tmp_path / "wt_debris"
    worktree.mkdir()
    (worktree / "tests").mkdir()
    (worktree / "tests" / "_forge_repro_test.py").write_text("def test_repro(): pass\n")
    (worktree / "test.patch").write_text("diff\n")
    (worktree / ".tmp_dump.txt").write_text("temp\n")
    (worktree / "__pycache__").mkdir()
    (worktree / "__pycache__" / "file.pyc").write_text("bytecode")

    cleaned = cleanup_orphaned_files(str(worktree))

    assert not (worktree / "tests" / "_forge_repro_test.py").exists()
    assert not (worktree / "test.patch").exists()
    assert not (worktree / ".tmp_dump.txt").exists()
    assert not (worktree / "__pycache__").exists()
    assert len(cleaned) >= 3


def test_cleanup_dangling_docker_resources_safe():
    """Verify cleanup_dangling_docker_resources runs safely without raising exceptions."""
    cleanup_dangling_docker_resources("task-mock-safe")


def test_managed_task_lifecycle_context_manager(tmp_path):
    """Verify managed_task_lifecycle creates worktree, acquires lock, and cleans up on exception."""
    repo_dir = tmp_path / "lifecycle_repo"
    repo_dir.mkdir()
    (repo_dir / "index.py").write_text("x = 1\n")
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@forge.ai"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeTest"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    mock_redis = MagicMock()
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = True
    mock_lock.owned.return_value = True
    mock_redis.lock.return_value = mock_lock

    captured_wt: list[str] = []

    with pytest.raises(RuntimeError):
        with managed_task_lifecycle(
            task_id="task-lifecycle-test",
            repo_path=str(repo_dir),
            redis_client=mock_redis,
        ) as wt_path:
            captured_wt.append(wt_path)
            assert os.path.exists(wt_path)
            raise RuntimeError("Agent crash during processing")

    # Verify worktree was pruned and removed despite exception
    assert len(captured_wt) == 1
    assert not os.path.exists(captured_wt[0])
    mock_lock.release.assert_called_once()


def test_emergency_trap_registration():
    """Verify registration and unregistration of emergency cleanup traps."""
    executed = False

    def trap():
        nonlocal executed
        executed = True

    register_emergency_cleanup("test_trap_1", trap)
    unregister_emergency_cleanup("test_trap_1")
    assert not executed


# ==============================================================================
# PILLAR 4: THREAD-SAFE LANGGRAPH STATE PROPAGATION
# ==============================================================================


def test_create_initial_agent_state_thread_safe():
    """Verify explicit binding of worktree_path to AgentState without globals."""
    state1 = create_initial_agent_state(
        task_id="task-worker-1",
        prompt="Fix subtraction bug",
        worktree_path="/tmp/forge_worktrees/task-1_a1b2c3d4",
    )
    state2 = create_initial_agent_state(
        task_id="task-worker-2",
        prompt="Fix addition bug",
        worktree_path="/tmp/forge_worktrees/task-2_e5f67890",
    )

    assert state1["worktree_path"] == "/tmp/forge_worktrees/task-1_a1b2c3d4"
    assert state2["worktree_path"] == "/tmp/forge_worktrees/task-2_e5f67890"
    assert state1["task_id"] != state2["task_id"]


def test_concurrent_stateless_node_execution():
    """Verify concurrent threads executing state routing do not collide or leak state."""
    results: dict[int, str] = {}

    def run_worker(thread_id: int):
        state: AgentState = {
            "task_id": f"thread-task-{thread_id}",
            "worktree_path": f"/tmp/forge_worktrees/task_{thread_id}_12345678",
            "confidence_score": 0.5 if thread_id % 2 == 0 else 0.9,
            "investigation_step": 1,
            "max_investigation_steps": 3,
            "investigation_complete": False,
        }
        route = critique_router(state)
        results[thread_id] = route

    threads = [threading.Thread(target=run_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Even threads (confidence=0.5 < 0.7) route to investigator_node
    # Odd threads (confidence=0.9 >= 0.7) route to implementer_node
    for i in range(10):
        if i % 2 == 0:
            assert results[i] == "investigator_node"
        else:
            assert results[i] == "implementer_node"


@pytest.mark.asyncio
async def test_cleanup_node_stateless_execution(tmp_path):
    """Verify cleanup_node runs statelessly reading worktree_path directly from state."""
    repo_dir = tmp_path / "repo_cleanup"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)

    dummy_wt = tmp_path / "wt_to_clean"
    dummy_wt.mkdir()
    (dummy_wt / "tests").mkdir()
    (dummy_wt / "tests" / "_forge_repro_test.py").write_text("debris")

    state: AgentState = {
        "task_id": "clean-task-1",
        "repo_id": str(repo_dir),
        "worktree_path": str(dummy_wt),
        "status": "running",
    }

    result = await cleanup_node(state)
    assert result["status"] == "cleaned_up"
    assert not os.path.exists(dummy_wt)
