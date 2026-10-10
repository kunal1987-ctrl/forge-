import os
import shlex
import docker
from docker.errors import ContainerError, ImageNotFound, DockerException


class DockerSandbox:
    """Secure, isolated test execution sandbox using the Docker Python SDK."""

    ALLOWED_COMMANDS = ("pytest", "npm test", "cargo test", "ruff check")
    FORBIDDEN_OPERATORS = ("|", ">", "<", "&", ";", "`", "$")

    def __init__(self):
        """Initializes Docker client connection to local Docker daemon/Docker Desktop."""
        try:
            self.client = docker.from_env()
        except DockerException as e:
            raise RuntimeError(
                f"Cannot connect to Docker daemon. Is Docker Desktop running? Error: {e}"
            )

    def run_tests(
        self,
        worktree_path: str,
        test_command: str,
        image: str = "python:3.11-slim",
    ) -> dict:
        """Executes test commands strictly inside an ephemeral Docker container.

        Enforces:
        - network_mode="none": Total network isolation; prevents outbound data exfiltration.
        - mem_limit="2g": 2GB memory ceiling; prevents host crashes from memory leaks.
        - volumes: Mounts ONLY the ephemeral worktree to /workspace; no host filesystem access.
        - nano_cpus: Limits CPU consumption to 2 cores maximum.

        Args:
            worktree_path: Host path to the ephemeral worktree to mount into /workspace.
            test_command: Command string to run (must match an allowlisted prefix).
            image: Docker base image (default: 'python:3.11-slim').

        Returns:
            Dict containing 'exit_code' (int) and 'output' (str).
        """
        clean_command = test_command.strip()

        # 1. Shell Injection Guard: Reject command chaining and piping
        for forbidden in self.FORBIDDEN_OPERATORS:
            if forbidden in clean_command:
                return {
                    "exit_code": 1,
                    "output": f"Security Violation: Shell operator '{forbidden}' is forbidden.",
                }

        # 2. Command Allowlist Check
        if not any(
            clean_command == cmd or clean_command.startswith(f"{cmd} ")
            for cmd in self.ALLOWED_COMMANDS
        ):
            return {
                "exit_code": 1,
                "output": (
                    f"Security Violation: Command '{test_command}' not in allowlist. "
                    f"Allowed prefixes: {self.ALLOWED_COMMANDS}"
                ),
            }

        # 3. Path Validation: Ensure isolated worktree exists
        abs_worktree = os.path.abspath(worktree_path)
        if not os.path.exists(abs_worktree) or not os.path.isdir(abs_worktree):
            return {
                "exit_code": 1,
                "output": f"Error: Worktree path does not exist on host: {worktree_path}",
            }

        try:
            # 4. Spin up ephemeral container with strict resource and network boundaries
            container_output = self.client.containers.run(
                image=image,
                command=shlex.split(clean_command),  # Directly invoked; bypasses /bin/sh shell injection
                volumes={
                    abs_worktree: {
                        "bind": "/workspace",
                        "mode": "rw",
                    }
                },
                working_dir="/workspace",
                network_mode="none",      # Zero network ingress/egress
                mem_limit="2g",           # 2GB RAM cap
                nano_cpus=2000000000,     # Max 2 CPU cores
                detach=False,
                remove=True,              # Auto-delete container when finished
            )
            return {
                "exit_code": 0,
                "output": container_output.decode("utf-8", errors="replace"),
            }

        except ContainerError as e:
            # Triggered when test command returns non-zero exit code
            output_bytes = getattr(e, "stderr", None) or getattr(e, "stdout", None) or b""
            output_str = output_bytes.decode("utf-8", errors="replace") if isinstance(output_bytes, bytes) else str(e)
            return {
                "exit_code": e.exit_status,
                "output": output_str,
            }

        except ImageNotFound:
            return {
                "exit_code": 1,
                "output": f"Docker image '{image}' not found locally. Run `docker pull {image}` first.",
            }

        except DockerException as e:
            return {
                "exit_code": 1,
                "output": f"Docker execution error: {e}",
            }