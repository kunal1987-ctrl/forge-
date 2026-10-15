from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from services.api.forge.execution.models import ExecutionResult
from services.api.forge.execution import sandbox_service


@pytest.mark.asyncio
async def test_sandbox_service_restricts_images_and_forwards_task_id(monkeypatch):
    docker_client = MagicMock()
    monkeypatch.setattr(sandbox_service, "_get_docker_client", lambda: docker_client)
    monkeypatch.setattr(sandbox_service, "_sandbox", None)
    result = ExecutionResult(exit_code=0, stdout="ok", stderr="", timeout=False)
    run_command = MagicMock(return_value=result)
    monkeypatch.setattr(sandbox_service.DockerSandbox, "run_command", run_command)

    transport = ASGITransport(app=sandbox_service.app)
    async with AsyncClient(transport=transport, base_url="http://sandbox") as client:
        rejected = await client.post(
            "/run-command",
            json={
                "worktree_path": "/tmp/forge_worktrees/task-1",
                "command": "pytest",
                "image": "untrusted-image",
            },
        )
        accepted = await client.post(
            "/run-command",
            json={
                "worktree_path": "/tmp/forge_worktrees/task-1",
                "command": "pytest",
                "image": "forge-runner",
                "task_id": "task-1",
            },
        )

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json()["exit_code"] == 0
    assert run_command.call_args.kwargs["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_sandbox_service_cancels_active_task_containers(monkeypatch):
    container = MagicMock()
    docker_client = MagicMock()
    docker_client.containers.get.return_value = container
    monkeypatch.setattr(sandbox_service, "_get_docker_client", lambda: docker_client)
    monkeypatch.setitem(
        sandbox_service._active_sandbox_containers,
        "task-2",
        {"container-id"},
    )

    try:
        result = await sandbox_service.cancel_task("task-2")
    finally:
        sandbox_service._active_sandbox_containers.pop("task-2", None)

    assert result == {"stopped": 1}
    container.kill.assert_called_once()
