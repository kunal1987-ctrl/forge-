from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.api.forge.execution.models import ExecutionResult
from services.api.forge.execution.sandbox import DockerSandbox
from services.api.forge.execution.test_runners import detect_test_runner


@pytest.mark.parametrize(
    ("manifest", "contents", "install_command", "test_command"),
    [
        (
            "requirements.txt",
            "pytest==8.0\n",
            "pip install --no-index --disable-pip-version-check "
            "--target /workspace/.forge-python-deps -r requirements.txt",
            "pytest",
        ),
        (
            "package.json",
            json.dumps({"scripts": {"test": "jest"}}),
            "npm install --offline --package-lock=false",
            "npm test",
        ),
        ("Cargo.toml", "[package]\nname='sample'\nversion='0.1.0'\n", "cargo fetch --offline", "cargo test"),
        ("go.mod", "module example.com/sample\n\ngo 1.22\n", "go mod download", "go test ./..."),
    ],
)
def test_detected_runner_includes_offline_dependency_setup(
    tmp_path: Path,
    manifest: str,
    contents: str,
    install_command: str,
    test_command: str,
) -> None:
    (tmp_path / manifest).write_text(contents, encoding="utf-8")

    spec = detect_test_runner(tmp_path)

    assert spec is not None
    assert spec.install_command == install_command
    assert spec.command == test_command


def test_auto_detect_installs_dependencies_before_running_npm_tests(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}}),
        encoding="utf-8",
    )
    sandbox = object.__new__(DockerSandbox)
    sandbox.run_command = MagicMock(
        side_effect=[
            ExecutionResult(exit_code=0, stdout="", stderr="", timeout=False),
            ExecutionResult(
                exit_code=0,
                stdout="Tests: 1 passed, 1 total",
                stderr="",
                timeout=False,
            ),
        ]
    )

    result = sandbox.run_tests(str(tmp_path), auto_detect=True)

    calls = sandbox.run_command.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["command"] == "npm install --offline --package-lock=false"
    assert calls[1].kwargs["command"] == "npm test"
    assert calls[0].kwargs["environment"] == calls[1].kwargs["environment"]
    assert result["exit_code"] == 0
    assert result["dependencies_installed"] is True


def test_dependency_install_failure_stops_test_execution(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("missing-package\n", encoding="utf-8")
    sandbox = object.__new__(DockerSandbox)
    sandbox.run_command = MagicMock(
        return_value=ExecutionResult(
            exit_code=1,
            stdout="",
            stderr="No matching distribution found",
            timeout=False,
        )
    )

    result = sandbox.run_tests(str(tmp_path), auto_detect=True)

    sandbox.run_command.assert_called_once()
    assert result["exit_code"] == 1
    assert result["status"] == "failed"
    assert result["dependencies_installed"] is False
    assert "Offline dependency installation failed" in result["error"]
