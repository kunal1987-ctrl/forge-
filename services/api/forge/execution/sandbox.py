"""Docker execution sandbox for secure and isolated command evaluation.

This module provides a hardened DockerSandbox class that strictly isolates test
and lint execution, enforces binary allowlists via shlex, prevents shell injection,
and applies hard timeouts to kill runaway execution and prevent resource exhaustion.

Test execution is polyglot: :func:`~services.api.forge.execution.test_runners.detect_test_runner`
maps a worktree's manifests to the right runner (pytest / npm / cargo / go /
Maven / Gradle) and the unified parser normalizes every runner's output into one
schema, so the Verifier node and Error Classifier stay language-agnostic.
"""

from __future__ import annotations

import logging
import shlex
import threading
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import docker
from docker.errors import DockerException, ImageNotFound
import requests

from services.api.forge.exceptions import SandboxExecutionError
from services.api.forge.execution.models import ExecutionResult
from services.api.forge.execution.test_runners import (
    NO_TESTS_MESSAGE,
    TestRunStatus,
    TestRunnerSpec,
    describe_supported_runners,
    detect_test_runner,
    infer_spec_from_command,
    parse_test_output,
)

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

    # Strict binary allowlist: args[0] must exactly match one of these binaries.
    #
    # The polyglot runners extend the original Python-only set. Note that
    # `npm test`, `./gradlew` and `mvn` all execute *repository-controlled*
    # build scripts by design -- that is inherent to running a project's own test
    # suite, and is contained by the rest of the sandbox profile (no network,
    # read-only rootfs, non-root user, dropped capabilities, pids/memory caps).
    ALLOWLISTED_BINARIES: FrozenSet[str] = frozenset(
        {
            # Python
            "pytest",
            "ruff",
            "mypy",
            # Node / JavaScript
            "npm",
            # Rust
            "cargo",
            # Go
            "go",
            # Java
            "mvn",
            "gradle",
            "gradlew",
            "./gradlew",
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

        # Enforce hardened security options across any client.containers.run execution
        if hasattr(self.client, "containers") and hasattr(self.client.containers, "run"):
            orig_run = self.client.containers.run

            def _hardened_containers_run(*args: Any, **kwargs: Any) -> Any:
                kwargs["cap_drop"] = ["ALL"]
                kwargs["read_only"] = True
                kwargs["network_mode"] = "none"
                if "mem_limit" not in kwargs or not kwargs["mem_limit"]:
                    kwargs["mem_limit"] = "512m"
                if "nano_cpus" not in kwargs or not kwargs["nano_cpus"]:
                    kwargs["nano_cpus"] = 1000000000
                return orig_run(*args, **kwargs)

            self.client.containers.run = _hardened_containers_run

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

    @classmethod
    def get_hardened_container_kwargs(
        cls,
        image: str,
        command: List[str],
        worktree_abs: Path,
        mem_limit: str = "512m",
        nano_cpus: int = 1000000000,
    ) -> Dict[str, Any]:
        """Return enterprise-grade hardened container creation options.

        Enforces:
        - cap_drop=['ALL']: Strip all Linux capabilities
        - user='nobody': Non-root execution
        - read_only=True: Immutable root filesystem
        - tmpfs={'/tmp': 'rw,noexec,nosuid,size=64m'}: Ephemeral tmpfs volume for /tmp
        - network_mode='none': Complete network isolation (no network egress)
        - mem_limit='512m': 512MB RAM ceiling
        - nano_cpus=1000000000: 1 CPU core allocation
        - pids_limit=64: Process limit preventing fork bombs
        - security_opt=['no-new-privileges:true']: Prevent privilege escalation
        - Zero shell execution: command passed strictly as parsed list
        """
        return {
            "image": image,
            "command": command,  # Strictly argument list, zero shell execution
            "volumes": {
                str(worktree_abs): {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            },
            "working_dir": "/workspace",
            "network_mode": "none",
            "mem_limit": mem_limit,
            "nano_cpus": nano_cpus,
            "pids_limit": 64,
            "cap_drop": ["ALL"],
            "user": "nobody",
            "read_only": True,
            "tmpfs": {
                "/tmp": "rw,noexec,nosuid,size=64m",
            },
            "security_opt": ["no-new-privileges:true"],
            "detach": True,
        }

    def run_container(
        self,
        image: str,
        command: List[str] | str,
        worktree_path: str | Path,
        mem_limit: str = "512m",
        nano_cpus: int = 1000000000,
        **kwargs: Any,
    ) -> Any:
        """Run a container via docker.client.containers.run strictly enforcing hardened security options.

        Enforces:
        - cap_drop=['ALL']: Strips all Linux capabilities
        - read_only=True: Immutable root filesystem
        - network_mode='none': Zero external network egress
        - mem_limit: Explicit memory ceiling (defaults to '512m')
        - nano_cpus: Explicit CPU core allocation (defaults to 1000000000)
        """
        worktree_abs = Path(worktree_path).resolve()
        if isinstance(command, str):
            parsed_command = self.validate_command(command)
        else:
            parsed_command = list(command)

        container_kwargs = self.get_hardened_container_kwargs(
            image=image,
            command=parsed_command,
            worktree_abs=worktree_abs,
            mem_limit=mem_limit,
            nano_cpus=nano_cpus,
        )
        container_kwargs.update(kwargs)
        container_kwargs["cap_drop"] = ["ALL"]
        container_kwargs["read_only"] = True
        container_kwargs["network_mode"] = "none"
        container_kwargs["mem_limit"] = mem_limit
        container_kwargs["nano_cpus"] = nano_cpus

        logger.info(
            "Executing hardened container run (image='%s', mem_limit='%s', network_mode='none')",
            image,
            mem_limit,
        )
        return self.client.containers.run(**container_kwargs)

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
                except (DockerException, requests.exceptions.RequestException):
                    try:
                        container.kill()
                    except (DockerException, requests.exceptions.RequestException):
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

            # 3. Create container with detached execution and ultra-hardened security constraints
            container_kwargs = self.get_hardened_container_kwargs(
                image=image,
                command=parsed_args,
                worktree_abs=worktree_abs,
                mem_limit=mem_limit,
                nano_cpus=nano_cpus,
            )

            try:
                container = self.client.containers.create(**container_kwargs)
            except ImageNotFound:
                fallback_image = "python:3.11-slim"
                logger.warning(
                    "Image '%s' not found locally. Falling back to '%s'",
                    image,
                    fallback_image,
                )
                try:
                    fb_kwargs = dict(container_kwargs)
                    fb_kwargs["image"] = fallback_image
                    container = self.client.containers.create(**fb_kwargs)
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
                except (DockerException, requests.exceptions.RequestException):
                    try:
                        container.kill()
                    except (DockerException, requests.exceptions.RequestException):
                        pass
                try:
                    container.remove(force=True)
                except (DockerException, requests.exceptions.RequestException):
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

        except (DockerException, SandboxExecutionError) as d_err:
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
                except (DockerException, requests.exceptions.RequestException) as rm_err:
                    logger.warning("Failed to remove sandbox container: %s", rm_err)

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            timeout=timed_out.is_set(),
        )

    def detect_test_runner(self, worktree_path: str | Path) -> Optional[TestRunnerSpec]:
        """Detect the worktree's test runner from its manifests.

        Returns the detected :class:`TestRunnerSpec`, or None when no supported
        project manifest is present.
        """
        return detect_test_runner(worktree_path)

    @staticmethod
    def supported_test_runners() -> Dict[str, str]:
        """Return the manifest -> command detection table (for docs, UI and tests)."""
        return describe_supported_runners()

    def run_tests(
        self,
        worktree_path: str,
        command: str = "pytest",
        timeout_seconds: int = 45,
        image: str = "forge-runner",
        test_command: Optional[str] = None,
        auto_detect: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a sandboxed test run with hard boundaries and unified output parsing.

        1. Runner Selection:
           - When ``auto_detect`` is True, the worktree's manifests pick the runner
             (package.json -> npm, Cargo.toml -> cargo, go.mod -> go, Python
             manifests -> pytest, pom.xml -> mvn, Gradle -> gradlew) and that
             runner's command is executed.
           - Otherwise the explicit ``test_command``/``command`` is used verbatim.
             Auto-detection is opt-in so callers that hand-write a command -- such
             as the reproduction gate's `pytest tests/_forge_repro_test.py` -- are
             never overridden by an unrelated manifest.
        2. Container Constraints:
           - mem_limit="512m", nano_cpus=1000000000 (1 core), network_mode="none".
           - Mounts worktree_path as volume.
        3. Execution Timeout:
           - timeout=timeout_seconds (default 45s); forceful kill and removal.
           - Returns status "timed_out" with exit_code -1.
        4. Unified Output Verification:
           - Output is normalized through
             :func:`~services.api.forge.execution.test_runners.parse_test_output`,
             producing passed/failed/errors/skipped counts, structured ``failures``
             with normalized tracebacks, and a ``normalized_output`` projection the
             Error Classifier can route in any language.
           - Rule: only PASSED if exit_code == 0 AND failed == 0 AND errors == 0 AND passed > 0.
           - If exit_code == 0 but zero tests ran: exit_code becomes 5 with
             "No tests were executed. Ensure test files and target functions exist."

        Args:
            worktree_path: Host filesystem path to the Git worktree.
            command: Command string to execute when not auto-detecting.
            timeout_seconds: Timeout threshold in seconds. Defaults to 45.
            image: Docker image name. Defaults to 'forge-runner'.
            test_command: Explicit override for ``command`` (legacy parameter).
            auto_detect: Detect the runner from the worktree's manifests.

        Returns:
            Dict with exit_code, status, passed_count, failed_count, errors_count,
            skipped_count, raw_output, stdout, stderr, error, language, framework,
            runner, command, count_granularity, failures and normalized_output.
        """
        effective_cmd = test_command if test_command is not None else command

        runner_spec: Optional[TestRunnerSpec] = None
        if auto_detect:
            runner_spec = detect_test_runner(worktree_path)
            if runner_spec is not None:
                effective_cmd = runner_spec.command
                logger.info(
                    "Auto-detected %s runner from '%s' (command: '%s')",
                    runner_spec.display_name,
                    runner_spec.manifest,
                    effective_cmd,
                )
            else:
                logger.info(
                    "No supported test manifest in '%s'; falling back to '%s'.",
                    worktree_path,
                    effective_cmd,
                )

        if runner_spec is None:
            # Infer the parser from an explicit command so hand-written
            # `pytest ...` invocations still get pytest failure extraction.
            runner_spec = infer_spec_from_command(effective_cmd)

        exec_res = self.run_command(
            worktree_path=worktree_path,
            command=effective_cmd,
            image=image,
            timeout_seconds=timeout_seconds,
            mem_limit="512m",
            nano_cpus=1000000000,
        )

        # 3. Execution Timeout Check
        if exec_res.timeout or exec_res.exit_code == -1 or exec_res.exit_code == 124:
            timeout_msg = (
                f"Execution timed out after {timeout_seconds}s. Possible infinite loop detected."
            )
            if kwargs.get("raise_on_error"):
                raise SandboxExecutionError(
                    timeout_msg, command=effective_cmd, timed_out=True, exit_code=-1
                )
            report = parse_test_output(
                command=effective_cmd,
                exit_code=-1,
                stdout="",
                stderr=timeout_msg,
                spec=runner_spec,
                timeout_seconds=timeout_seconds,
            )
            payload = report.to_dict()
            payload["status"] = TestRunStatus.TIMED_OUT
            payload["stdout"] = ""
            payload["stderr"] = timeout_msg
            payload["raw_output"] = timeout_msg
            payload["error"] = timeout_msg
            payload["passed_count"] = 0
            payload["failed_count"] = 0
            payload["errors_count"] = 0
            payload["skipped_count"] = 0
            return TestRunResult(payload)

        # 4. Unified normalization across every supported runner
        raw_output = f"{exec_res.stdout}\n{exec_res.stderr}".strip()
        report = parse_test_output(
            command=effective_cmd,
            exit_code=exec_res.exit_code,
            stdout=exec_res.stdout,
            stderr=exec_res.stderr,
            spec=runner_spec,
            timeout_seconds=timeout_seconds,
        )
        payload = report.to_dict()
        payload["raw_output"] = raw_output
        payload["stdout"] = exec_res.stdout
        payload["stderr"] = exec_res.stderr
        if payload["status"] == TestRunStatus.NO_TESTS and not payload.get("error"):
            payload["error"] = NO_TESTS_MESSAGE
        return TestRunResult(payload)

    def run_detected_tests(
        self,
        worktree_path: str,
        timeout_seconds: int = 45,
        image: str = "forge-runner",
        fallback_command: str = "pytest",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Detect the worktree's project type and run its native test suite.

        Convenience wrapper over :meth:`run_tests` with ``auto_detect=True``.
        Falls back to ``fallback_command`` when no manifest is recognized, so
        behaviour is unchanged for Python-only worktrees.

        Args:
            worktree_path: Host filesystem path to the Git worktree.
            timeout_seconds: Timeout threshold in seconds. Defaults to 45.
            image: Docker image name. Defaults to 'forge-runner'.
            fallback_command: Command used when detection finds nothing.

        Returns:
            The unified test result dict described in :meth:`run_tests`.
        """
        return self.run_tests(
            worktree_path=worktree_path,
            command=fallback_command,
            timeout_seconds=timeout_seconds,
            image=image,
            auto_detect=True,
            **kwargs,
        )


def run_hardened_container(
    client: Any,
    image: str,
    command: List[str] | str,
    worktree_path: str | Path,
    mem_limit: str = "512m",
    nano_cpus: int = 1000000000,
    **kwargs: Any,
) -> Any:
    """Run a container via docker.client.containers.run strictly enforcing enterprise security parameters:
    - cap_drop=['ALL']: Strips all Linux capabilities
    - read_only=True: Immutable root filesystem
    - network_mode='none': Zero external network egress
    - mem_limit='512m': Explicit memory ceiling
    - nano_cpus=1000000000: 1 CPU core allocation
    """
    sandbox = DockerSandbox(client=client)
    return sandbox.run_container(
        image=image,
        command=command,
        worktree_path=worktree_path,
        mem_limit=mem_limit,
        nano_cpus=nano_cpus,
        **kwargs,
    )


__all__ = [
    "DockerSandbox",
    "TestRunResult",
    "detect_test_runner",
    "describe_supported_runners",
    "infer_spec_from_command",
    "parse_test_output",
    "run_hardened_container",
]
