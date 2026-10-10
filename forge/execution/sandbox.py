import os
import shlex
import docker
import requests
from typing import Dict, Any


class DockerSandbox:
    """Manages isolated test execution within Docker containers."""

    ALLOWED_PREFIXES = ("pytest", "npm test", "mypy", "ruff")
    FORBIDDEN_OPERATORS = ("|", ">", "&")

    def __init__(self):
        """Initializes the Docker client from the local environment."""
        self.client = docker.from_env()

    def run_command(
        self,
        worktree_path: str,
        command: str,
        image: str = "python:3.11-slim",
        timeout_seconds: int = 120,
    ) -> Dict[str, Any]:
        """Runs an allowlisted command in an isolated Docker container with resource constraints.

        Args:
            worktree_path: Host path to the worktree to mount into /workspace.
            command: Allowlisted command to execute (e.g. 'pytest').
            image: Docker image. Defaults to 'python:3.11-slim'.
            timeout_seconds: Timeout in seconds before force-killing container.

        Returns:
            Dictionary with 'exit_code', 'stdout', and 'stderr'.
        """
        # Security: Reject shell piping and chaining
        clean_command = command.strip()
        for forbidden in self.FORBIDDEN_OPERATORS:
            if forbidden in clean_command:
                raise ValueError(
                    f"Command contains forbidden character '{forbidden}' to prevent shell injection: {command}"
                )

        # Security: Validate allowlisted prefixes
        if not any(
            clean_command == prefix or clean_command.startswith(f"{prefix} ")
            for prefix in self.ALLOWED_PREFIXES
        ):
            raise ValueError(
                f"Command '{command}' is not in the allowlist. Must begin with: {self.ALLOWED_PREFIXES}"
            )

        container = None
        is_timeout = False
        exit_code = -1
        stdout_str = ""
        stderr_str = ""

        try:
            # Mount worktree read-write and set strict resource/network boundaries
            container = self.client.containers.create(
                image=image,
                command=shlex.split(clean_command),
                volumes={
                    os.path.abspath(worktree_path): {
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

            # Wait for execution with timeout
            try:
                wait_result = container.wait(timeout=timeout_seconds)
                exit_code = int(wait_result.get("StatusCode", 0) if isinstance(wait_result, dict) else wait_result)
            except (requests.exceptions.Timeout, TimeoutError):
                is_timeout = True
                try:
                    container.kill()
                except Exception:
                    pass

            # Fetch logs
            stdout_str = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr_str = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

            if is_timeout:
                exit_code = 124
                timeout_msg = f"Command timed out after {timeout_seconds} seconds."
                stderr_str = f"{stderr_str}\n{timeout_msg}".strip() if stderr_str else timeout_msg

        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        return {
            "exit_code": exit_code,
            "stdout": stdout_str,
            "stderr": stderr_str,
        }
