"""Docker execution sandbox for secure and isolated command evaluation.

This module provides a DockerSandbox class that manages sandboxed execution of
test suites and linting tools inside strictly isolated, resource-constrained
Docker containers.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import docker
from docker.errors import APIError, DockerException
import requests

logger = logging.getLogger(__name__)


class DockerSandbox:
    """Manages secure, isolated test and lint execution within Docker containers."""

    # Allowed command prefixes to prevent arbitrary execution
    ALLOWED_COMMAND_PREFIXES: Tuple[str, ...] = (
        "pytest",
        "npm test",
        "mypy",
        "ruff",
    )

    # Shell characters strictly rejected to prevent injection
    FORBIDDEN_CHARACTERS: Tuple[str, ...] = ("|", ">", "&", ";", "`", "$")

    def __init__(self, client: Optional[docker.DockerClient] = None) -> None:
        """Initialize the Docker sandbox with an active Docker client.

        Args:
            client: Optional preconfigured DockerClient instance. If not provided,
                initializes from environment variables via docker.from_env().

        Raises:
            DockerException: If the Docker client fails to connect to the Docker daemon.
        """
        try:
            self.client = client if client is not None else docker.from_env()
        except DockerException as exc:
            logger.error("Failed to initialize Docker client from environment: %s", exc)
            raise

    def validate_command(self, command: str) -> None:
        """Validate command against strict allowlists and injection rules.

        Args:
            command: Command string to validate.

        Raises:
            ValueError: If command contains forbidden shell operators or does not
                start with an allowed test/lint prefix.
        """
        if not command or not command.strip():
            raise ValueError("Command cannot be empty.")

        clean_command = command.strip()

        # Reject any shell piping, redirection, or chaining characters
        for forbidden in self.FORBIDDEN_CHARACTERS:
            if forbidden in clean_command:
                raise ValueError(
                    f"Command contains forbidden shell operator '{forbidden}' "
                    f"to prevent injection: '{command}'"
                )

        # Ensure command begins with an approved tool
        is_allowed = any(
            clean_command == prefix or clean_command.startswith(f"{prefix} ")
            for prefix in self.ALLOWED_COMMAND_PREFIXES
        )

        if not is_allowed:
            raise ValueError(
                f"Command '{command}' is not in the allowlist. "
                f"Must start with one of: {', '.join(self.ALLOWED_COMMAND_PREFIXES)}"
            )

    def run_command(
        self,
        worktree_path: str,
        command: str,
        image: str = "python:3.11-slim",
        timeout_seconds: int = 120,
    ) -> Dict[str, Any]:
        """Execute an allowlisted command inside an isolated Docker container.

        Configures strict resource limits and full network isolation:
        - Network mode: 'none'
        - Memory limit: 2GB ('2g')
        - CPU limit: 2 CPUs (nano_cpus=2000000000)
        - Working directory: '/workspace'
        - Workspace mount: Read-Write to worktree_path

        Args:
            worktree_path: Filesystem path to the Git worktree to mount.
            command: Allowlisted command to execute (e.g. 'pytest -v', 'ruff check').
            image: Docker container image to execute against. Defaults to 'python:3.11-slim'.
            timeout_seconds: Maximum allowed runtime in seconds before force killing.
                Defaults to 120.

        Returns:
            Dictionary containing:
                - 'exit_code': int exit code of the process (-1 or 124 on timeout)
                - 'stdout': decoded standard output string
                - 'stderr': decoded standard error string

        Raises:
            ValueError: If command validation fails.
            FileNotFoundError: If the worktree_path does not exist.
            APIError: If Docker daemon errors prevent container creation.
        """
        self.validate_command(command)

        worktree_abs = Path(worktree_path).resolve()
        if not worktree_abs.exists() or not worktree_abs.is_dir():
            raise FileNotFoundError(
                f"Worktree path does not exist or is not a directory: {worktree_path}"
            )

        # Split command securely into argv list to bypass shell evaluation
        parsed_cmd = shlex.split(command.strip())

        container = None
        is_timeout = False
        stdout_str = ""
        stderr_str = ""
        exit_code = -1

        try:
            logger.info(
                "Spinning up container '%s' to run '%s' in '%s'",
                image,
                command,
                worktree_abs,
            )

            # Security-hardened container configuration
            container = self.client.containers.create(
                image=image,
                command=parsed_cmd,
                volumes={
                    str(worktree_abs): {
                        "bind": "/workspace",
                        "mode": "rw",
                    }
                },
                working_dir="/workspace",
                network_mode="none",
                mem_limit="2g",
                nano_cpus=2000000000,
                detach=True,
            )

            container.start()

            # Wait for container execution with timeout
            try:
                wait_result = container.wait(timeout=timeout_seconds)
                if isinstance(wait_result, dict):
                    exit_code = int(wait_result.get("StatusCode", -1))
                else:
                    exit_code = int(wait_result)
            except (requests.exceptions.Timeout, TimeoutError):
                logger.warning(
                    "Command timed out after %d seconds. Force-killing container...",
                    timeout_seconds,
                )
                is_timeout = True
                try:
                    container.kill()
                except Exception as kill_err:
                    logger.debug("Failed to kill container (may have already stopped): %s", kill_err)

            # Capture logs
            try:
                stdout_raw = container.logs(stdout=True, stderr=False)
                stderr_raw = container.logs(stdout=False, stderr=True)
                stdout_str = stdout_raw.decode("utf-8", errors="replace")
                stderr_str = stderr_raw.decode("utf-8", errors="replace")
            except Exception as log_err:
                logger.warning("Failed to retrieve container logs: %s", log_err)

            if is_timeout:
                exit_code = 124  # Standard timeout exit code
                timeout_msg = f"Execution timed out after {timeout_seconds} seconds."
                stderr_str = f"{stderr_str}\n{timeout_msg}".strip() if stderr_str else timeout_msg

        except Exception as exc:
            logger.exception("Error during sandboxed execution: %s", exc)
            if not is_timeout:
                stderr_str = f"Sandbox execution error: {exc}"
            raise

        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                    logger.debug("Removed sandbox container successfully.")
                except Exception as rm_err:
                    logger.warning("Failed to remove sandbox container: %s", rm_err)

        return {
            "exit_code": exit_code,
            "stdout": stdout_str,
            "stderr": stderr_str,
        }
