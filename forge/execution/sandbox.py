import os
import shlex
import docker
from docker.errors import DockerException, ImageNotFound
import requests


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
        image: str = "forge-runner",
        timeout_seconds: int = 120,
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
            image: Docker base image (default: 'forge-runner').
            timeout_seconds: Timeout before terminating long-running tests.

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

        container = None
        exit_code = 1
        output_str = ""

        try:
            # 4. Create ephemeral container with strict resource and network boundaries
            container = self.client.containers.create(
                image=image,
                command=shlex.split(clean_command),  # Bypasses /bin/sh to prevent injection
                volumes={
                    abs_worktree: {
                        "bind": "/workspace",
                        "mode": "rw",
                    }
                },
                working_dir="/workspace",
                network_mode="none",      # Zero network ingress/egress (no exfiltration/payload downloads)
                mem_limit="2g",           # 2GB RAM cap (prevents host crashes)
                nano_cpus=2000000000,     # Max 2 CPU cores
            )

            container.start()

            # Wait for execution with timeout
            try:
                wait_result = container.wait(timeout=timeout_seconds)
                exit_code = int(wait_result.get("StatusCode", 0) if isinstance(wait_result, dict) else wait_result)
            except (requests.exceptions.Timeout, TimeoutError):
                try:
                    container.kill()
                except Exception:
                    pass
                exit_code = 124
                output_str = f"Execution timed out after {timeout_seconds} seconds.\n"

            # Fetch output logs (both stdout & stderr)
            try:
                logs_bytes = container.logs(stdout=True, stderr=True)
                output_str += logs_bytes.decode("utf-8", errors="replace")
            except Exception as log_err:
                output_str += f"[Log capture error: {log_err}]"

            return {
                "exit_code": exit_code,
                "output": output_str,
            }

        except ImageNotFound:
            return {
                "exit_code": 1,
                "output": f"Docker image '{image}' not found locally. Run `docker pull {image}` or build it first.",
            }

        except DockerException as e:
            return {
                "exit_code": 1,
                "output": f"Docker execution error: {e}",
            }

        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass