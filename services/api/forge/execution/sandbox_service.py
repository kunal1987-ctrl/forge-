"""Internal API for executing repository commands in hardened Docker containers."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Dict, Optional

import docker
from docker.errors import DockerException
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.api.forge.execution.sandbox import (
    DockerSandbox,
    _active_sandbox_containers,
    _active_sandbox_lock,
)

logger = logging.getLogger(__name__)
app = FastAPI(title="FORGE Sandbox Worker", docs_url=None, redoc_url=None)
_docker_client: Optional[docker.DockerClient] = None
_sandbox: Optional[DockerSandbox] = None
_client_lock = threading.Lock()
_sandbox_lock = threading.Lock()


class SandboxCommandRequest(BaseModel):
    worktree_path: str = Field(min_length=1, max_length=4096)
    command: str = Field(min_length=1, max_length=4096)
    image: str = Field(default="forge-runner", max_length=128)
    timeout_seconds: int = Field(default=45, ge=1, le=300)
    environment: Dict[str, str] = Field(default_factory=dict)
    task_id: Optional[str] = Field(default=None, max_length=128)


def _get_docker_client() -> docker.DockerClient:
    global _docker_client
    with _client_lock:
        if _docker_client is None:
            _docker_client = docker.from_env()
    return _docker_client


def _get_sandbox() -> DockerSandbox:
    global _sandbox
    with _sandbox_lock:
        if _sandbox is None:
            _sandbox = DockerSandbox(client=_get_docker_client())
    return _sandbox


def _allowed_images() -> set[str]:
    runner_image = os.getenv("FORGE_RUNNER_IMAGE", "forge-runner")
    return {runner_image, "python:3.11-slim"}


@app.get("/health")
async def health() -> Dict[str, str]:
    try:
        await asyncio.to_thread(lambda: _get_docker_client().ping())
    except (DockerException, OSError) as exc:
        logger.error("Sandbox worker cannot reach Docker: %s", exc)
        raise HTTPException(status_code=503, detail="Docker daemon unavailable") from exc
    return {"status": "ok"}


@app.post("/run-command")
async def run_command(request: SandboxCommandRequest) -> Dict[str, object]:
    if request.image not in _allowed_images():
        raise HTTPException(status_code=400, detail="Sandbox image is not allowed")

    try:
        sandbox = _get_sandbox()
        result = await asyncio.to_thread(
            sandbox.run_command,
            worktree_path=request.worktree_path,
            command=request.command,
            image=request.image,
            timeout_seconds=request.timeout_seconds,
            environment=request.environment,
            task_id=request.task_id,
        )
    except (DockerException, OSError, RuntimeError) as exc:
        logger.exception("Sandbox worker failed to start a command")
        raise HTTPException(status_code=503, detail="Sandbox execution unavailable") from exc
    return result.model_dump()


@app.post("/cancel/{task_id}")
async def cancel_task(task_id: str) -> Dict[str, int]:
    with _active_sandbox_lock:
        container_ids = tuple(_active_sandbox_containers.get(task_id, ()))

    stopped = 0
    client = _get_docker_client()
    for container_id in container_ids:
        try:
            container = client.containers.get(container_id)
            await asyncio.to_thread(container.kill)
            stopped += 1
        except docker.errors.NotFound:
            continue
        except DockerException:
            logger.exception("Failed to stop sandbox container %s", container_id)
    return {"stopped": stopped}
