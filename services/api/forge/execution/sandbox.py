"""Docker execution sandbox for secure and isolated command evaluation.

This module provides a hardened DockerSandbox class that strictly isolates test
and lint execution, enforces binary allowlists via shlex, prevents shell injection,
and applies hard timeouts to kill runaway execution and prevent resource exhaustion.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import docker
from docker.errors import DockerException, ImageNotFound
import requests

from services.api.forge.execution.models import ExecutionResult

logger = logging.getLogger(__name__)


class TestRunResult(dict):
    """Dictionary representing test execution metrics with attribute access compatibility."""

    __test__ = False

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'TestRunResult' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class DockerSandbox:
    """Hardened Docker sandbox for secure, isolated command execution."""

    # Strict binary allowlist: args[0] must exactly match one of these binaries
    ALLOWLISTED_BINARIES: FrozenSet[str] = frozenset(
        {
            "pytest",
            "npm",
            "cargo",
            "ruff",
            "mypy",
        }
    )

    # Shell metacharacters strictly rejected to prevent injection or escape attempts
    FORBIDDEN_OPERATORS: Tuple[str, ...] = ("|", ">", "<", "&", ";", "`", "$", "\n", "\r")

    def __init__(self, client: Optional[docker.DockerClient] = None) -> None:
        """Initialize the Docker sandbox with an active Docker client.

        Args:
            client: Optional preconfigured DockerClient instance. If omitted,
                connects to the local daemon via docker.from_env().

        Raises:
            RuntimeError: If Docker daemon connection fails.
        """
        try:
            self.client = client if client is not None else docker.from_env()
        except DockerException as exc:
            logger.error("Failed to connect to Docker daemon: %s", exc)
            raise RuntimeError(
                f"Cannot connect to Docker daemon. Is Docker Desktop running? Error: {exc}"
            ) from exc

    def validate_command(self, command: str) -> List[str]:
        """Validate command string and parse securely into argument list.

        Enforces:
        1. Non-empty command string.
        2. Absolute prohibition of shell redirection/chaining metacharacters.
        3. Parses command via shlex.split to bypass shell execution.
        4. Exact match of args[0] against ALLOWLISTED_BINARIES (no prefix matching).

        Args:
            command: Raw command string to validate.

        Returns:
            List of parsed argument strings for direct process execution.

        Raises:
            ValueError: If command is empty, contains forbidden characters, or args[0]
                is not an allowlisted binary.
        """
        if not command or not command.strip():
            raise ValueError("Command cannot be empty.")

        clean_command = command.strip()

        # Reject shell operators explicitly before splitting
        for forbidden in self.FORBIDDEN_OPERATORS:
            if forbidden in clean_command:
                raise ValueError(
                    f"Security violation: Shell operator '{forbidden}' is forbidden in sandbox."
                )

        try:
            args = shlex.split(clean_command)
        except ValueError as parse_err:
            raise ValueError(f"Malformed command syntax: {parse_err}") from parse_err

        if not args:
            raise ValueError("Command resolved to an empty argument list.")

        binary = args[0]
        # Strict exact binary match: do not use .startswith()
        if binary not in self.ALLOWLISTED_BINARIES:
            raise ValueError(
                f"Security violation: Binary '{binary}' is not allowlisted. "
                f"Allowed binaries: {sorted(self.ALLOWLISTED_BINARIES)}"
            )

        return args

    def run_command(
        self,
        worktree_path: str,
        command: str,
        image: str = "forge-runner",
        timeout_seconds: int = 45,
        mem_limit: str = "512m",
        nano_cpus: int = 1000000000,
    ) -> ExecutionResult:
        """Execute an allowlisted command inside an isolated Docker container with strict timeouts.

        Security constraints enforced:
        - shlex.split command list execution (no shell chaining).
        - args[0] exact allowlist match.
        - network_mode="none" (complete network isolation; zero data exfiltration).
        - mem_limit="512m" (512MB memory ceiling; prevents host crash on leaks).
        - nano_cpus=1000000000 (1 CPU core).
        - volumes: Only mounts isolated worktree to /workspace as read-write.
        - Strict timeout mechanism: container.wait(timeout=timeout_seconds) with fallback killer;
          calls container.stop() and container.remove() on timeout.

        Args:
            worktree_path: Host filesystem path to the Git worktree.
            command: Command string to execute (e.g., 'pytest tests/').
            image: Docker image name. Defaults to 'forge-runner'.
            timeout_seconds: Timeout threshold in seconds. Defaults to 45.
            mem_limit: Memory limit string. Defaults to '512m'.
            nano_cpus: CPU allocation in nanoCPUs. Defaults to 1000000000 (1 core).

        Returns:
            ExecutionResult(exit_code: int, stdout: str, stderr: str, timeout: bool)
        """
        # 1. Command validation & shlex parsing
        try:
            parsed_args = self.validate_command(command)
        except ValueError as val_err:
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr=str(val_err),
                timeout=False,
            )

        # 2. Path validation: ensure worktree directory exists on host
        worktree_abs = Path(worktree_path).resolve()
        if not worktree_abs.exists() or not worktree_abs.is_dir():
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"Error: Worktree path does not exist on host: {worktree_path}",
                timeout=False,
            )

        container = None
        timed_out = threading.Event()
        stdout_str = ""
        stderr_str = ""
        exit_code = -1

        def _timeout_handler() -> None:
            """Background watchdog thread that forcefully terminates container on timeout."""
            timed_out.set()
            logger.warning(
                "Sandbox execution timed out after %ds. Stopping container...",
                timeout_seconds,
            )
            if container is not None:
                try:
                    container.stop(timeout=2)
                except Exception:
                    try:
                        container.kill()
                    except Exception:
                        pass

        watchdog = threading.Timer(float(timeout_seconds), _timeout_handler)
        watchdog.daemon = True

        try:
            logger.info(
                "Creating detached container '%s' to run %s in '%s'",
                image,
                parsed_args,
                worktree_abs,
            )

            # 3. Create container with detached execution and resource constraints
            try:
                container = self.client.containers.create(
                    image=image,
                    command=parsed_args,  # Pass parsed list directly to avoid shell
                    volumes={
                        str(worktree_abs): {
                            "bind": "/workspace",
                            "mode": "rw",
                        }
                    },
                    working_dir="/workspace",
                    network_mode="none",
                    mem_limit=mem_limit,
                    nano_cpus=nano_cpus,
                    detach=True,
                )
            except ImageNotFound:
                fallback_image = "python:3.11-slim"
                logger.warning(
                    "Image '%s' not found locally. Falling back to '%s'",
                    image,
                    fallback_image,
                )
                try:
                    container = self.client.containers.create(
                        image=fallback_image,
                        command=parsed_args,
                        volumes={
                            str(worktree_abs): {
                                "bind": "/workspace",
                                "mode": "rw",
                            }
                        },
                        working_dir="/workspace",
                        network_mode="none",
                        mem_limit=mem_limit,
                        nano_cpus=nano_cpus,
                        detach=True,
                    )
                except Exception as fb_err:
                    return ExecutionResult(
                        exit_code=1,
                        stdout="",
                        stderr=f"Docker image error: '{image}' not found locally. Fallback failed: {fb_err}",
                        timeout=False,
                    )

            container.start()
            watchdog.start()

            # 4. Wait for container with strict timeout limit
            try:
                wait_result = container.wait(timeout=timeout_seconds)
                if isinstance(wait_result, dict):
                    exit_code = int(wait_result.get("StatusCode", -1))
                else:
                    exit_code = int(wait_result)

            except (
                requests.exceptions.ReadTimeout,
                requests.exceptions.Timeout,
                TimeoutError,
                DockerException,
            ) as timeout_exc:
                timed_out.set()
                logger.warning("Container wait timed out (%s). Stopping container...", timeout_exc)
                try:
                    container.stop(timeout=2)
                except Exception:
                    try:
                        container.kill()
                    except Exception:
                        pass
                try:
                    container.remove(force=True)
                except Exception:
                    pass

                timeout_err = f"Execution timed out after {timeout_seconds}s. Possible infinite loop detected."
                return ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr=timeout_err,
                    timeout=True,
                )

            # Check if background watchdog triggered first
            if timed_out.is_set():
                exit_code = -1
                timeout_msg = f"Execution timed out after {timeout_seconds}s. Possible infinite loop detected."
                stderr_str = timeout_msg

            # 5. Capture standard output and error streams
            try:
                stdout_raw = container.logs(stdout=True, stderr=False)
                stderr_raw = container.logs(stdout=False, stderr=True)
                stdout_str = stdout_raw.decode("utf-8", errors="replace")
                captured_err = stderr_raw.decode("utf-8", errors="replace")

                if stderr_str:
                    stderr_str = (
                        f"{captured_err}\n{stderr_str}".strip() if captured_err else stderr_str
                    )
                else:
                    stderr_str = captured_err
            except Exception as log_err:
                logger.warning("Failed to retrieve container logs: %s", log_err)
                if not stderr_str:
                    stderr_str = f"Log retrieval error: {log_err}"

        except DockerException as d_err:
            logger.exception("Docker execution error: %s", d_err)
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"Docker execution error: {d_err}",
                timeout=False,
            )

        except Exception as exc:
            logger.exception("Unexpected error in DockerSandbox: %s", exc)
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"Unexpected sandbox error: {exc}",
                timeout=False,
            )

        finally:
            watchdog.cancel()
            if container is not None:
                try:
                    container.remove(force=True)
                    logger.debug("Sandbox container removed successfully.")
                except Exception as rm_err:
                    logger.warning("Failed to remove sandbox container: %s", rm_err)

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            timeout=timed_out.is_set(),
        )

    def run_tests(
        self,
        worktree_path: str,
        command: str = "pytest",
        timeout_seconds: int = 45,
        image: str = "forge-runner",
        test_command: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute sandboxed pytest run with hard boundaries and output verification.

        1. Container Constraints:
           - mem_limit="512m"
           - nano_cpus=1000000000 (1 core)
           - network_mode="none" (no external egress)
           - Mounts worktree_path as volume.
        2. Execution Timeout:
           - timeout=timeout_seconds (default 45s).
           - Forcefully kills and removes container on timeout.
           - Returns: {"exit_code": -1, "status": "timed_out", "stdout": "", "stderr": f"Execution timed out after {timeout_seconds}s. Possible infinite loop detected."}
        3. Output Verification:
           - Regex parses counts for: passed, failed, errors, skipped.
           - Rule: Only PASSED if exit_code == 0 AND failed == 0 AND errors == 0 AND passed > 0.
           - If exit_code == 0 but passed == 0:
             {"exit_code": 5, "error": "No tests were executed. Ensure test files and target functions exist."}
           - Returns structured dict: {"exit_code": int, "passed_count": int, "failed_count": int, "raw_output": str, ...}
        """
        effective_cmd = test_command if test_command is not None else command
        exec_res = self.run_command(
            worktree_path=worktree_path,
            command=effective_cmd,
            image=image,
            timeout_seconds=timeout_seconds,
            mem_limit="512m",
            nano_cpus=1000000000,
        )

        # 2. Execution Timeout Check
        if exec_res.timeout or exec_res.exit_code == -1 or exec_res.exit_code == 124:
            timeout_msg = (
                f"Execution timed out after {timeout_seconds}s. Possible infinite loop detected."
            )
            return TestRunResult(
                {
                    "exit_code": -1,
                    "status": "timed_out",
                    "stdout": "",
                    "stderr": timeout_msg,
                    "passed_count": 0,
                    "failed_count": 0,
                    "errors_count": 0,
                    "skipped_count": 0,
                    "raw_output": timeout_msg,
                    "error": timeout_msg,
                }
            )

        # 3. Output Verification
        raw_output = f"{exec_res.stdout}\n{exec_res.stderr}".strip()
        passed_count = 0
        failed_count = 0
        errors_count = 0
        skipped_count = 0

        passed_m = re.search(r"\b(\d+)\s+passed\b", raw_output)
        if passed_m:
            passed_count = int(passed_m.group(1))

        failed_m = re.search(r"\b(\d+)\s+failed\b", raw_output)
        if failed_m:
            failed_count = int(failed_m.group(1))

        errors_m = re.search(r"\b(\d+)\s+error(?:s)?\b", raw_output)
        if errors_m:
            errors_count = int(errors_m.group(1))

        skipped_m = re.search(r"\b(\d+)\s+skipped\b", raw_output)
        if skipped_m:
            skipped_count = int(skipped_m.group(1))

        exit_code = exec_res.exit_code
        status = "failed"
        err_msg: Optional[str] = None

        if exit_code == 0:
            if passed_count > 0 and failed_count == 0 and errors_count == 0:
                status = "passed"
            elif passed_count == 0:
                exit_code = 5
                status = "no_tests"
                err_msg = "No tests were executed. Ensure test files and target functions exist."
            else:
                exit_code = 1
                status = "failed"
                err_msg = "Non-zero failures or errors reported despite exit code 0."
        else:
            status = "failed"
            err_msg = exec_res.stderr or "Tests failed."

        return TestRunResult(
            {
                "exit_code": exit_code,
                "passed_count": passed_count,
                "failed_count": failed_count,
                "errors_count": errors_count,
                "skipped_count": skipped_count,
                "raw_output": raw_output,
                "stdout": exec_res.stdout,
                "stderr": exec_res.stderr,
                "status": status,
                "error": err_msg,
            }
        )
