"""Polyglot test-runner detection and unified result parsing for the FORGE sandbox.

**Detection is host-side against the mounted worktree.** ``DockerSandbox`` runs a
single allowlisted command via ``shlex`` with zero shell execution (no pipes, no
``&&``, no command substitution), so there is no in-container shell to probe with.
Instead we inspect the worktree directory directly -- that directory is
bind-mounted read-write at ``/workspace`` inside the container, so the host sees
exactly the files the runner will see. The result matches in-container detection
without weakening the sandbox.

Detected Python, npm, Cargo, and Go projects carry a dependency setup command.
The sandbox runs these commands with networking disabled and offline/cache-only
package resolution before executing tests.

**Parsing normalises** pytest, npm (jest / vitest / mocha), cargo, ``go test``,
Maven Surefire and Gradle output into one :class:`UnifiedTestReport`, plus a
``normalized_output`` projection that speaks the vocabulary
:class:`~services.api.forge.orchestration.error_classifier.ErrorClassifier`
already matches (``FAILED <file>::<name>`` and ``E   <type>: <message>``). That
projection is what lets the Error Classifier and Verifier node route failures
from any language without language-specific branching of their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
import json
import logging
from pathlib import Path
import re

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# 1. VOCABULARY
# ------------------------------------------------------------------------------


class ProjectLanguage:
    """Language identifiers for detected test projects."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    RUST = "rust"
    GO = "go"
    JAVA = "java"


class TestFramework:
    """Test framework identifiers; each maps to exactly one parser."""

    PYTEST = "pytest"
    NPM = "npm"
    CARGO_TEST = "cargo-test"
    GO_TEST = "go-test"
    MAVEN_SUREFIRE = "maven-surefire"
    GRADLE_TEST = "gradle-test"
    UNKNOWN = "unknown"


class TestRunStatus:
    """Normalized run status. Values match the pre-existing sandbox contract."""

    PASSED = "passed"
    FAILED = "failed"
    NO_TESTS = "no_tests"
    TIMED_OUT = "timed_out"


class CountGranularity:
    """What one unit in ``passed_count`` represents for a given runner."""

    TEST = "test"
    PACKAGE = "package"


NO_TESTS_MESSAGE = "No tests were executed. Ensure test files and target functions exist."


@dataclass(frozen=True)
class TestRunnerSpec:
    """A detected test runner and the exact command that executes it."""

    language: str
    framework: str
    runner: str
    command: str
    manifest: str
    display_name: str
    count_granularity: str = CountGranularity.TEST
    install_command: Optional[str] = None
    environment: Tuple[Tuple[str, str], ...] = ()

    @property
    def runner_binary(self) -> str:
        """First token of the command; must satisfy the sandbox binary allowlist."""
        return self.command.split()[0] if self.command else ""


@dataclass
class UnifiedFailure:
    """One normalized test failure, language-agnostic."""

    test_name: str
    file_path: str = "unknown"
    line_number: Optional[int] = None
    error_type: str = "AssertionError"
    message: str = ""
    traceback: str = ""
    language: str = "unknown"
    framework: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_name": self.test_name,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "error_type": self.error_type,
            "message": self.message,
            "traceback": self.traceback,
            "language": self.language,
            "framework": self.framework,
        }


@dataclass
class UnifiedTestReport:
    """The unified schema every language-specific parser produces."""

    exit_code: int
    status: str
    passed_count: int = 0
    failed_count: int = 0
    errors_count: int = 0
    skipped_count: int = 0
    total_count: int = 0
    error: Optional[str] = None
    language: str = "unknown"
    framework: str = TestFramework.UNKNOWN
    runner: str = "unknown"
    command: str = ""
    count_granularity: str = CountGranularity.TEST
    tests_detected: bool = False
    failures: List[UnifiedFailure] = field(default_factory=list)
    normalized_output: str = ""

    @property
    def passed(self) -> bool:
        """Strict pass invariant, identical to the pre-polyglot sandbox rule."""
        return (
            self.exit_code == 0
            and self.failed_count == 0
            and self.errors_count == 0
            and self.passed_count > 0
        )

    def to_dict(self) -> Dict[str, Any]:
        """Flatten into the dict contract the Verifier node already consumes."""
        return {
            "exit_code": self.exit_code,
            "status": self.status,
            "passed": self.passed,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "errors_count": self.errors_count,
            "skipped_count": self.skipped_count,
            "total_count": self.total_count,
            "error": self.error,
            "language": self.language,
            "framework": self.framework,
            "runner": self.runner,
            "command": self.command,
            "count_granularity": self.count_granularity,
            "tests_detected": self.tests_detected,
            "failures": [failure.to_dict() for failure in self.failures],
            "normalized_output": self.normalized_output,
        }


# ------------------------------------------------------------------------------
# 2. DETECTION
# ------------------------------------------------------------------------------

PYTHON_MANIFESTS: Tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "pytest.ini",
    "tox.ini",
    "setup.py",
    "setup.cfg",
)

GRADLE_MANIFESTS: Tuple[str, ...] = ("build.gradle", "build.gradle.kts")


def _read_npm_test_script(worktree: Path) -> Optional[str]:
    """Return the ``scripts.test`` command from package.json, or None.

    A ``package.json`` without a test script is extremely common (frontends,
    libraries, tooling packages). Running ``npm test`` there exits non-zero with
    "Missing script: test", which would be misreported as a test failure -- so a
    missing script means "not a test project" and detection moves on.
    """
    manifest = worktree / "package.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as parse_err:
        logger.warning("Unreadable package.json at '%s': %s", manifest, parse_err)
        return None
    if not isinstance(payload, dict):
        return None
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return None
    test_script = str(scripts.get("test") or "").strip()
    # npm init writes this placeholder; it is not a real suite.
    if not test_script or "Error: no test specified" in test_script:
        return None
    return test_script


def detect_test_runner(worktree_path: str | Path) -> Optional[TestRunnerSpec]:
    """Detect the project's test runner from its manifests.

    Checks are ordered and the first match wins:
    package.json -> Cargo.toml -> go.mod -> Python manifests -> pom.xml -> Gradle.

    Args:
        worktree_path: Host path to the Git worktree mounted at /workspace.

    Returns:
        The detected :class:`TestRunnerSpec`, or None when the worktree holds no
        recognizable test project.
    """
    worktree = Path(worktree_path)
    if not worktree.is_dir():
        return None

    if _read_npm_test_script(worktree) is not None:
        install_command = (
            "npm ci --offline"
            if (worktree / "package-lock.json").is_file()
            or (worktree / "npm-shrinkwrap.json").is_file()
            else "npm install --offline --package-lock=false"
        )
        return TestRunnerSpec(
            language=ProjectLanguage.JAVASCRIPT,
            framework=TestFramework.NPM,
            runner="npm",
            command="npm test",
            manifest="package.json",
            display_name="npm test",
            install_command=install_command,
            environment=(("npm_config_cache", "/workspace/.forge-npm-cache"),),
        )

    if (worktree / "Cargo.toml").is_file():
        return TestRunnerSpec(
            language=ProjectLanguage.RUST,
            framework=TestFramework.CARGO_TEST,
            runner="cargo",
            command="cargo test",
            manifest="Cargo.toml",
            display_name="cargo test",
            install_command="cargo fetch --offline",
            environment=(
                ("CARGO_HOME", "/workspace/.forge-cargo"),
                ("CARGO_NET_OFFLINE", "true"),
            ),
        )

    if (worktree / "go.mod").is_file():
        return TestRunnerSpec(
            language=ProjectLanguage.GO,
            framework=TestFramework.GO_TEST,
            runner="go",
            command="go test ./...",
            manifest="go.mod",
            display_name="go test ./...",
            # `go test ./...` reports per-package verdicts unless -v is passed,
            # so a passing unit is a package, not an individual test.
            count_granularity=CountGranularity.PACKAGE,
            install_command="go mod download",
            environment=(
                ("GOPATH", "/workspace/.forge-go"),
                ("GOMODCACHE", "/workspace/.forge-go/pkg/mod"),
                ("GOCACHE", "/tmp/.forge-go-build"),
                ("GOPROXY", "off"),
                ("GOSUMDB", "off"),
            ),
        )

    for python_manifest in PYTHON_MANIFESTS:
        if (worktree / python_manifest).is_file():
            if python_manifest == "requirements.txt":
                python_install_command: Optional[str] = (
                    "pip install --no-index --disable-pip-version-check "
                    "--target /workspace/.forge-python-deps -r requirements.txt"
                )
            elif python_manifest in ("pyproject.toml", "setup.py", "setup.cfg"):
                python_install_command = (
                    "pip install --no-index --disable-pip-version-check "
                    "--target /workspace/.forge-python-deps ."
                )
            else:
                python_install_command = None
            return TestRunnerSpec(
                language=ProjectLanguage.PYTHON,
                framework=TestFramework.PYTEST,
                runner="pytest",
                command="pytest",
                manifest=python_manifest,
                display_name="pytest",
                install_command=python_install_command,
                environment=(("PYTHONPATH", "/workspace/.forge-python-deps"),),
            )

    if (worktree / "pom.xml").is_file():
        # -B (batch mode) suppresses ANSI and interactive download prompts,
        # keeping Surefire's "Tests run:" summary machine-parseable.
        return TestRunnerSpec(
            language=ProjectLanguage.JAVA,
            framework=TestFramework.MAVEN_SUREFIRE,
            runner="mvn",
            command="mvn -B test",
            manifest="pom.xml",
            display_name="mvn test",
        )

    for gradle_manifest in GRADLE_MANIFESTS:
        if (worktree / gradle_manifest).is_file():
            # Prefer the checked-in wrapper: the container has no Gradle install.
            runner = "./gradlew" if (worktree / "gradlew").is_file() else "gradle"
            # --console=plain strips ANSI/progress output so the summary parses.
            command = f"{runner} test --console=plain"
            return TestRunnerSpec(
                language=ProjectLanguage.JAVA,
                framework=TestFramework.GRADLE_TEST,
                runner=runner,
                command=command,
                manifest=gradle_manifest,
                display_name=command,
            )

    return None


_COMMAND_SIGNATURES: Dict[str, Tuple[str, str, str]] = {
    "pytest": (ProjectLanguage.PYTHON, TestFramework.PYTEST, "pytest"),
    "npm": (ProjectLanguage.JAVASCRIPT, TestFramework.NPM, "npm"),
    "cargo": (ProjectLanguage.RUST, TestFramework.CARGO_TEST, "cargo"),
    "go": (ProjectLanguage.GO, TestFramework.GO_TEST, "go"),
    "mvn": (ProjectLanguage.JAVA, TestFramework.MAVEN_SUREFIRE, "mvn"),
    "gradle": (ProjectLanguage.JAVA, TestFramework.GRADLE_TEST, "gradle"),
    "./gradlew": (ProjectLanguage.JAVA, TestFramework.GRADLE_TEST, "./gradlew"),
    "gradlew": (ProjectLanguage.JAVA, TestFramework.GRADLE_TEST, "gradlew"),
}


def infer_spec_from_command(command: str) -> Optional[TestRunnerSpec]:
    """Infer a runner spec from an explicitly supplied command.

    Lets callers that pass a hand-written command (e.g. the reproduction gate's
    ``pytest tests/_forge_repro_test.py``) still get the right parser without
    running manifest detection.
    """
    tokens = (command or "").split()
    if not tokens:
        return None
    signature = _COMMAND_SIGNATURES.get(tokens[0])
    if signature is None:
        return None
    language, framework, runner = signature
    return TestRunnerSpec(
        language=language,
        framework=framework,
        runner=runner,
        command=command,
        manifest="",
        display_name=command,
    )


# ------------------------------------------------------------------------------
# 3. SHARED PARSING HELPERS
# ------------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT100 escape sequences so summaries stay regex-parseable."""
    return _ANSI_ESCAPE_RE.sub("", text or "")


@dataclass
class ParsedCounts:
    """Raw counts scraped from a runner's summary output."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    total: int = 0
    detected: bool = False
    no_tests: bool = False


NO_TESTS_MARKERS: Dict[str, Tuple[str, ...]] = {
    TestFramework.PYTEST: ("no tests ran", "collected 0 items"),
    TestFramework.NPM: ("no tests found",),
    TestFramework.CARGO_TEST: (),
    TestFramework.GO_TEST: ("[no test files]",),
    TestFramework.MAVEN_SUREFIRE: ("no tests to run",),
    TestFramework.GRADLE_TEST: ("no-source", "0 tests completed"),
}


def _marker_hit(framework: str, lowered_text: str) -> bool:
    """True when the output explicitly advertises that no tests were collected."""
    return any(marker in lowered_text for marker in NO_TESTS_MARKERS.get(framework, ()))


# ------------------------------------------------------------------------------
# 4. LANGUAGE-SPECIFIC COUNT PARSERS
# ------------------------------------------------------------------------------


def _parse_pytest(text: str) -> ParsedCounts:
    """Parse pytest's `=== 3 passed, 1 failed in 0.15s ===` summary."""
    counts = ParsedCounts()
    passed = re.search(r"\b(\d+)\s+passed\b", text)
    failed = re.search(r"\b(\d+)\s+failed\b", text)
    errors = re.search(r"\b(\d+)\s+error(?:s)?\b", text)
    skipped = re.search(r"\b(\d+)\s+skipped\b", text)
    deselected = re.search(r"\b(\d+)\s+deselected\b", text)
    collected = re.search(r"collected\s+(\d+)\s+items?", text)

    if passed:
        counts.passed = int(passed.group(1))
    if failed:
        counts.failed = int(failed.group(1))
    if errors:
        counts.errors = int(errors.group(1))
    if skipped:
        counts.skipped = int(skipped.group(1))

    counts.total = (
        int(collected.group(1))
        if collected
        else counts.passed + counts.failed + counts.errors + counts.skipped
    )
    counts.detected = any((passed, failed, errors, skipped, collected, deselected))
    counts.no_tests = counts.detected and counts.total == 0
    return counts


def _parse_npm(text: str) -> ParsedCounts:
    """Parse jest/vitest `Tests:` summaries and mocha `N passing` lines."""
    counts = ParsedCounts()
    matched = False

    # jest / vitest: "Tests:       1 failed, 3 passed, 4 total"
    token_re = re.compile(
        r"(\d+)\s+(passed|failed|skipped|todo|pending|total|flaky)", re.IGNORECASE
    )
    for summary in re.finditer(r"^Tests:\s*(.+)$", text, re.MULTILINE):
        for count_text, label in token_re.findall(summary.group(1)):
            label_lower = label.lower()
            value = int(count_text)
            matched = True
            if label_lower == "passed":
                counts.passed += value
            elif label_lower == "failed":
                counts.failed += value
            elif label_lower in ("skipped", "todo", "pending"):
                counts.skipped += value
            elif label_lower == "total":
                counts.total += value

    # A suite that failed to load reports zero test totals but a failed suite count.
    suite_failures = re.search(r"^Test Suites:.*?(\d+)\s+failed", text, re.MULTILINE)
    if suite_failures and counts.failed == 0:
        suite_failed = int(suite_failures.group(1))
        if suite_failed:
            counts.errors += suite_failed
            matched = True

    # mocha: "  3 passing", "  1 failing", "  2 pending"
    mocha_passing = re.search(r"^\s*(\d+)\s+passing\b", text, re.MULTILINE)
    mocha_failing = re.search(r"^\s*(\d+)\s+failing\b", text, re.MULTILINE)
    mocha_pending = re.search(r"^\s*(\d+)\s+pending\b", text, re.MULTILINE)
    if mocha_passing or mocha_failing:
        matched = True
        if mocha_passing:
            counts.passed = int(mocha_passing.group(1))
        if mocha_failing:
            counts.failed = int(mocha_failing.group(1))
        if mocha_pending:
            counts.skipped = int(mocha_pending.group(1))

    counts.total = counts.total or (counts.passed + counts.failed + counts.skipped)
    counts.detected = matched
    counts.no_tests = matched and counts.total == 0
    if "no tests found" in text.lower():
        counts.detected = True
        counts.no_tests = True
    return counts


def _parse_cargo(text: str) -> ParsedCounts:
    """Sum cargo's per-test-binary `test result: ok. N passed; M failed; ...` lines."""
    counts = ParsedCounts()
    result_re = re.compile(
        r"test result:\s*(?P<verdict>\w+)\.\s*"
        r"(?P<passed>\d+)\s+passed;\s*"
        r"(?P<failed>\d+)\s+failed;\s*"
        r"(?P<ignored>\d+)\s+ignored"
        r"(?:;\s*(?P<measured>\d+)\s+measured)?",
        re.IGNORECASE,
    )
    any_match = False
    for match in result_re.finditer(text):
        any_match = True
        counts.passed += int(match.group("passed"))
        counts.failed += int(match.group("failed"))
        counts.skipped += int(match.group("ignored")) + int(match.group("measured") or 0)

    # Cargo aborts the run on a compile error and emits no test result line.
    if re.search(r"^error(?:\[E\d+\])?:", text, re.MULTILINE) and not any_match:
        counts.errors += 1
        counts.detected = True

    counts.total = counts.passed + counts.failed + counts.skipped
    counts.detected = counts.detected or any_match
    counts.no_tests = any_match and counts.total == 0
    return counts


def _parse_go(text: str) -> ParsedCounts:
    """Parse `go test ./...` output.

    Without ``-v``, Go reports per-package verdicts (``ok`` / ``FAIL`` /
    ``[no test files]``) plus individual ``--- FAIL:`` lines but no per-test pass
    lines. Passing units therefore fall back to package verdicts, which is why
    the detected spec records ``count_granularity='package'``.
    """
    counts = ParsedCounts()
    per_test_passes = re.findall(r"^\s*--- PASS:", text, re.MULTILINE)
    per_test_fails = re.findall(r"^\s*--- FAIL:", text, re.MULTILINE)
    package_oks = re.findall(r"^ok\s+\S+", text, re.MULTILINE)
    package_fails = re.findall(r"^FAIL\s+\S+", text, re.MULTILINE)
    no_test_files = re.findall(r"\[no test files\]", text)
    skips = re.findall(r"^\s*--- SKIP:", text, re.MULTILINE)
    build_errors = re.findall(r"^#\s+\S+", text, re.MULTILINE)

    counts.passed = len(per_test_passes) if per_test_passes else len(package_oks)
    counts.failed = len(per_test_fails) if per_test_fails else len(package_fails)
    counts.skipped = len(skips)
    if build_errors and counts.failed == 0:
        counts.errors = len(build_errors)

    counts.detected = bool(
        per_test_passes or per_test_fails or package_oks or package_fails or no_test_files
    )
    counts.total = counts.passed + counts.failed + counts.skipped
    counts.no_tests = counts.detected and counts.total == 0
    return counts


def _parse_maven(text: str) -> ParsedCounts:
    """Parse Surefire's `Tests run: N, Failures: N, Errors: N, Skipped: N` summaries.

    Surefire prints the summary twice: once per test class on a line that also
    carries ``Time elapsed:`` (and possibly ``<<< FAILURE! - in <class>``), and
    once as Maven's final per-module aggregate without ``Time elapsed``. Summing
    every occurrence would double-count, so module aggregates win when present;
    the per-class lines are only summed for older Surefire versions that emit no
    aggregate at all. Per-class lines are never counted *alongside* aggregates.
    """
    counts = ParsedCounts()
    summary_re = re.compile(
        r"Tests run:\s*(?P<run>\d+),\s*"
        r"Failures:\s*(?P<failures>\d+),\s*"
        r"Errors:\s*(?P<errors>\d+),\s*"
        r"Skipped:\s*(?P<skipped>\d+)"
    )

    aggregate = ParsedCounts()
    per_class = ParsedCounts()
    saw_aggregate = False
    saw_per_class = False

    for line in text.splitlines():
        match = summary_re.search(line)
        if match is None:
            continue
        run = int(match.group("run"))
        failures = int(match.group("failures"))
        errors = int(match.group("errors"))
        skipped = int(match.group("skipped"))
        # Surefire's "Tests run" total already includes failures/errors/skips.
        passed = max(0, run - failures - errors - skipped)

        target = per_class if "Time elapsed" in line else aggregate
        if target is per_class:
            saw_per_class = True
        else:
            saw_aggregate = True
        target.passed += passed
        target.failed += failures
        target.errors += errors
        target.skipped += skipped

    any_match = saw_aggregate or saw_per_class
    if saw_aggregate:
        counts = aggregate
    elif saw_per_class:
        counts = per_class

    if re.search(r"Failed to execute goal", text) and not any_match:
        counts.errors += 1
        counts.detected = True

    counts.total = counts.passed + counts.failed + counts.errors + counts.skipped
    counts.detected = counts.detected or any_match
    counts.no_tests = any_match and counts.total == 0
    return counts


def _parse_gradle(text: str) -> ParsedCounts:
    """Parse Gradle's `N tests completed, M failed, K skipped` summary."""
    counts = ParsedCounts()
    summary_re = re.compile(
        r"(?P<completed>\d+)\s+tests?\s+completed"
        r"(?:,\s*(?P<failed>\d+)\s+failed)?"
        r"(?:,\s*(?P<skipped>\d+)\s+skipped)?",
        re.IGNORECASE,
    )
    match = summary_re.search(text)
    if match:
        completed = int(match.group("completed"))
        failed = int(match.group("failed") or 0)
        skipped = int(match.group("skipped") or 0)
        counts.failed = failed
        counts.skipped = skipped
        counts.passed = max(0, completed - failed - skipped)
        counts.detected = True

    if re.search(r"^> Task :\S*test\S* FAILED", text, re.MULTILINE):
        if counts.failed == 0:
            counts.errors += 1
        counts.detected = True

    counts.total = counts.passed + counts.failed + counts.errors + counts.skipped
    counts.no_tests = counts.detected and counts.total == 0
    return counts


_COUNT_PARSERS = {
    TestFramework.PYTEST: _parse_pytest,
    TestFramework.NPM: _parse_npm,
    TestFramework.CARGO_TEST: _parse_cargo,
    TestFramework.GO_TEST: _parse_go,
    TestFramework.MAVEN_SUREFIRE: _parse_maven,
    TestFramework.GRADLE_TEST: _parse_gradle,
}


# ------------------------------------------------------------------------------
# 5. FAILURE / TRACEBACK EXTRACTION
# ------------------------------------------------------------------------------

_PARAMETERISATION_RE = re.compile(r"\[[^\[\]]*\]$")


def _normalize_test_name(name: str) -> str:
    """Reduce a runner's test identifier to a comparable leaf name.

    Strips pytest parametrisation (``test_x[3]``) and any ``module::`` prefix so
    a short-summary line can be matched against its traceback block header.
    """
    leaf = (name or "").strip().split("::")[-1]
    previous = None
    while previous != leaf:
        previous = leaf
        leaf = _PARAMETERISATION_RE.sub("", leaf).strip()
    return leaf


def _extract_pytest_failures(text: str, language: str, framework: str) -> List[UnifiedFailure]:
    """Extract failures from pytest's short summary, enriched by traceback blocks."""
    failures: List[UnifiedFailure] = []

    blocks: Dict[str, str] = {}
    for block in re.finditer(
        r"^_{5,}\s*(?P<name>.+?)\s*_{5,}$(?P<body>.*?)(?=^_{5,}\s*\S|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        blocks[_normalize_test_name(block.group("name"))] = block.group("body")

    for match in re.finditer(
        r"^FAILED\s+(?P<file>[^\s:]+)(?:::(?P<name>[^\s]+))?(?:\s+-\s+(?P<msg>.*))?$",
        text,
        re.MULTILINE,
    ):
        file_path = match.group("file").replace("\\", "/")
        raw_name = match.group("name") or file_path
        message = (match.group("msg") or "").strip()
        body = blocks.get(_normalize_test_name(raw_name), "")

        error_type = "AssertionError"
        line_number: Optional[int] = None
        location = re.search(
            r"^(?P<file>[^\s:]+?):(?P<line>\d+):\s*(?P<type>\S.*?)\s*$", body, re.MULTILINE
        )
        if location:
            file_path = location.group("file").replace("\\", "/")
            line_number = int(location.group("line"))
            error_type = location.group("type").strip() or error_type
        if not message:
            emitted = re.findall(r"^E\s+(.*)$", body, re.MULTILINE)
            message = emitted[-1].strip() if emitted else ""

        failures.append(
            UnifiedFailure(
                test_name=raw_name.strip(),
                file_path=file_path,
                line_number=line_number,
                error_type=error_type,
                message=message,
                traceback=body.strip(),
                language=language,
                framework=framework,
            )
        )
    return failures


def _extract_go_failures(text: str, language: str, framework: str) -> List[UnifiedFailure]:
    """Extract Go failures from `--- FAIL:` blocks and `file_test.go:12:` detail lines."""
    failures: List[UnifiedFailure] = []
    lines = text.splitlines()

    for index, line in enumerate(lines):
        fail_match = re.match(r"^\s*--- FAIL:\s*(?P<name>\S+)", line)
        if not fail_match:
            continue

        test_name = fail_match.group("name")
        detail_lines: List[str] = []
        file_path = "unknown"
        line_number: Optional[int] = None
        message = ""

        for follow in lines[index + 1 : index + 12]:
            if re.match(r"^\s*--- (?:PASS|FAIL|SKIP):", follow) or re.match(
                r"^(?:ok|FAIL|PASS|\?)\s", follow
            ):
                break
            detail_lines.append(follow)
            location = re.search(r"(?P<file>[\w./\-]+\.\w+):(?P<line>\d+):", follow)
            if location and line_number is None:
                file_path = location.group("file")
                line_number = int(location.group("line"))
                message = follow.split(":", 2)[-1].strip()
            elif follow.strip() and not message:
                message = follow.strip()

        failures.append(
            UnifiedFailure(
                test_name=test_name,
                file_path=file_path,
                line_number=line_number,
                error_type="AssertionError",
                message=message or f"go test failed: {test_name}",
                traceback="\n".join(detail_lines).strip(),
                language=language,
                framework=framework,
            )
        )

    # Build/compile failures never emit a `--- FAIL:` line.
    for build_error in re.finditer(
        r"^#\s+(?P<pkg>\S+)\n(?P<detail>(?:.*\n)*?)(?=^#\s|\Z)", text, re.MULTILINE
    ):
        detail = build_error.group("detail").strip()
        location = re.search(
            r"(?P<file>[\w./\-]+\.\w+):(?P<line>\d+):\d*:?\s*(?P<msg>.*)", detail
        )
        failures.append(
            UnifiedFailure(
                test_name=f"build:{build_error.group('pkg')}",
                file_path=location.group("file") if location else "unknown",
                line_number=int(location.group("line")) if location else None,
                error_type="SyntaxError",
                message=(location.group("msg") if location else detail).strip(),
                traceback=detail,
                language=language,
                framework=framework,
            )
        )
    return failures


def _extract_cargo_failures(text: str, language: str, framework: str) -> List[UnifiedFailure]:
    """Extract Rust failures from `---- name stdout ----` panic blocks or `error[E...]`."""
    failures: List[UnifiedFailure] = []

    for block in re.finditer(
        r"^----\s+(?P<name>.+?)\s+stdout\s+----$(?P<body>.*?)(?=^----\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        body = block.group("body").strip()
        panic = re.search(
            r"panicked at\s+(?P<file>[^\s:]+):(?P<line>\d+):\d+:\s*\n?(?P<msg>[\s\S]*)", body
        )
        failures.append(
            UnifiedFailure(
                test_name=block.group("name").strip(),
                file_path=panic.group("file").replace("\\", "/") if panic else "unknown",
                line_number=int(panic.group("line")) if panic else None,
                error_type="AssertionError" if panic else "RuntimeError",
                message=panic.group("msg").strip() if panic else body,
                traceback=body,
                language=language,
                framework=framework,
            )
        )

    # Compile errors abort the run before any test executes.
    for compile_error in re.finditer(
        r"^error(?:\[(?P<code>E\d+)\])?:\s*(?P<msg>.*)\n\s*-->\s*(?P<file>[^\s:]+):(?P<line>\d+):\d+",
        text,
        re.MULTILINE,
    ):
        failures.append(
            UnifiedFailure(
                test_name=f"compile:{compile_error.group('code') or 'error'}",
                file_path=compile_error.group("file").replace("\\", "/"),
                line_number=int(compile_error.group("line")),
                error_type="SyntaxError",
                message=compile_error.group("msg").strip(),
                traceback=compile_error.group(0),
                language=language,
                framework=framework,
            )
        )
    return failures


_JEST_LOCATION_RE = re.compile(
    r"at\s+(?:.*?\()?(?P<file>[^\s:()]+):(?P<line>\d+):(?P<col>\d+)\)?"
)


def _extract_npm_failures(text: str, language: str, framework: str) -> List[UnifiedFailure]:
    """Extract failures from jest/vitest `● suite › test` headers or mocha `N)` blocks."""
    failures: List[UnifiedFailure] = []

    for block in re.finditer(
        r"^\s*●\s+(?P<name>[^\n●]+?)\n(?P<body>(?:.*\n)*?)(?=^\s*●\s|^\s*Test Suites:|\Z)",
        text,
        re.MULTILINE,
    ):
        body = block.group("body").rstrip()
        location = _JEST_LOCATION_RE.search(body)
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        failures.append(
            UnifiedFailure(
                test_name=block.group("name").strip(),
                file_path=location.group("file").replace("\\", "/") if location else "unknown",
                line_number=int(location.group("line")) if location else None,
                error_type="AssertionError",
                message=first_line,
                traceback=body,
                language=language,
                framework=framework,
            )
        )

    for block in re.finditer(
        r"^\s*\d+\)\s+(?P<name>[^\n]+)\n(?P<body>(?:.*\n)*?)(?=^\s*\d+\)\s|\Z)",
        text,
        re.MULTILINE,
    ):
        body = block.group("body").rstrip()
        location = _JEST_LOCATION_RE.search(body)
        error_line = next(
            (line.strip() for line in body.splitlines() if re.search(r"(?:Error|Exception)\b:", line)),
            "",
        )
        failures.append(
            UnifiedFailure(
                test_name=block.group("name").strip(),
                file_path=location.group("file").replace("\\", "/") if location else "unknown",
                line_number=int(location.group("line")) if location else None,
                error_type="AssertionError",
                message=error_line or (body.splitlines()[0].strip() if body else ""),
                traceback=body,
                language=language,
                framework=framework,
            )
        )
    return failures


def _extract_maven_failures(text: str, language: str, framework: str) -> List[UnifiedFailure]:
    """Extract Surefire `testFoo(com.example.FooTest) ... <<< FAILURE!` blocks."""
    failures: List[UnifiedFailure] = []
    pattern = re.compile(
        r"^\[ERROR\]\s+(?P<name>\S+)\((?P<cls>[^)]+)\)\s+Time elapsed:.*<<<"
        r"\s*(?:FAILURE|ERROR)!"
        r"(?P<body>(?:.*\n)*?)"
        r"(?=^\[ERROR\]\s+\S+\(|^\s*\[INFO\]|^\s*\[ERROR\]\s+Tests run:|\Z)",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        body = match.group("body").strip()
        type_match = re.search(
            r"(?P<type>[\w.]*(?:Exception|Error))(?::\s*(?P<msg>.*))?", body
        )
        location = re.search(r"at\s+(?P<loc>[\w.$]+)\((?P<file>[^:)]+):(?P<line>\d+)\)", body)
        error_type = type_match.group("type") if type_match else "AssertionError"
        message = (
            f"{error_type}: {type_match.group('msg').strip()}"
            if type_match and type_match.group("msg")
            else (body.splitlines()[0].strip() if body else error_type)
        )
        failures.append(
            UnifiedFailure(
                test_name=f"{match.group('cls')}#{match.group('name')}",
                file_path=location.group("file").replace("\\", "/") if location else "unknown",
                line_number=int(location.group("line")) if location else None,
                error_type=error_type,
                message=message,
                traceback=body,
                language=language,
                framework=framework,
            )
        )
    return failures


def _extract_gradle_failures(text: str, language: str, framework: str) -> List[UnifiedFailure]:
    """Extract Gradle `com.example.FooTest > testName FAILED` blocks."""
    failures: List[UnifiedFailure] = []
    pattern = re.compile(
        r"^(?P<cls>[\w.$]+)\s+>\s+(?P<name>\S+)\s+FAILED\s*$"
        r"(?P<body>(?:.*\n)*?)"
        r"(?=^[\w.$]+\s+>\s+\S+\s+FAILED|^\S|\Z)",
        re.MULTILINE,
    )
    location_re = re.compile(r"at\s+(?P<file>[^:()\s]+)\(?(?:[^)]*)\)?:?(?P<line>\d+)")
    for match in pattern.finditer(text):
        body = match.group("body").strip()
        type_match = re.search(r"(?P<type>[\w.]*(?:Exception|Error))", body)
        location = location_re.search(body)
        failures.append(
            UnifiedFailure(
                test_name=f"{match.group('cls')}#{match.group('name')}",
                file_path=location.group("file").replace("\\", "/") if location else "unknown",
                line_number=int(location.group("line")) if location else None,
                error_type=type_match.group("type") if type_match else "AssertionError",
                message=body.splitlines()[0].strip() if body else f"{match.group('name')} FAILED",
                traceback=body,
                language=language,
                framework=framework,
            )
        )
    return failures


_FAILURE_EXTRACTORS = {
    TestFramework.PYTEST: _extract_pytest_failures,
    TestFramework.NPM: _extract_npm_failures,
    TestFramework.CARGO_TEST: _extract_cargo_failures,
    TestFramework.GO_TEST: _extract_go_failures,
    TestFramework.MAVEN_SUREFIRE: _extract_maven_failures,
    TestFramework.GRADLE_TEST: _extract_gradle_failures,
}


# ------------------------------------------------------------------------------
# 6. NORMALIZED OUTPUT PROJECTION
# ------------------------------------------------------------------------------


def build_normalized_output(
    failures: Sequence[UnifiedFailure],
    stdout: str,
    stderr: str,
) -> str:
    """Project heterogeneous failures into the Error Classifier's vocabulary.

    Emits ``FAILED <file>::<name>`` and ``E   <type>: <message>`` lines -- exactly
    the shape ``ErrorClassifier`` already matches for pytest. Native error type
    names are preserved (``unresolved import``, ``cannot find symbol``, ...) so
    the classifier's polyglot markers can route them precisely.
    """
    lines: List[str] = []
    for failure in failures:
        location = failure.file_path
        if failure.line_number is not None:
            location = f"{location}:{failure.line_number}"
        lines.append(f"FAILED {location}::{failure.test_name}")

        detail = failure.message or failure.error_type
        if failure.error_type and failure.error_type not in detail:
            detail = f"{failure.error_type}: {detail}"
        lines.append(f"E   {detail}")

        for trace_line in failure.traceback.splitlines()[:40]:
            lines.append(f"E   {trace_line.rstrip()}")
        lines.append("")

    # Retain the raw streams so nothing the parsers missed is lost downstream.
    body = "\n".join(lines).strip()
    raw = f"{stdout}\n{stderr}".strip()
    return f"{body}\n\n{raw}".strip() if body else raw


# ------------------------------------------------------------------------------
# 7. UNIFIED ENTRY POINT
# ------------------------------------------------------------------------------


def parse_test_output(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    spec: Optional[TestRunnerSpec] = None,
    timeout_seconds: Optional[int] = None,
) -> UnifiedTestReport:
    """Normalize any supported runner's output into a :class:`UnifiedTestReport`.

    Exit-code policy (identical to the pre-polyglot pytest-only contract):
      * ``exit_code == 0`` with tests run and zero failures/errors -> ``passed``
      * ``exit_code == 0`` with no tests reported -> ``exit_code = 5``, ``no_tests``
      * ``exit_code == 0`` with failures reported -> ``exit_code = 1``, ``failed``
      * non-zero -> ``failed``

    Args:
        command: The command that was executed.
        exit_code: Raw container exit status.
        stdout: Captured stdout.
        stderr: Captured stderr.
        spec: Detected runner. When None, a spec is inferred from the command's
            first token, falling back to pytest-style heuristics.
        timeout_seconds: Timeout applied, used to render the timeout message.

    Returns:
        A populated UnifiedTestReport.
    """
    resolved_spec = spec if spec is not None else infer_spec_from_command(command)
    framework = resolved_spec.framework if resolved_spec else TestFramework.UNKNOWN
    language = resolved_spec.language if resolved_spec else "unknown"
    runner = (
        resolved_spec.runner
        if resolved_spec
        else (command.split()[0] if command.split() else "unknown")
    )
    granularity = (
        resolved_spec.count_granularity if resolved_spec else CountGranularity.TEST
    )

    combined = strip_ansi(f"{stdout}\n{stderr}")

    count_parser = _COUNT_PARSERS.get(framework, _parse_pytest)
    counts = count_parser(combined)

    if _marker_hit(framework, combined.lower()):
        counts.detected = True
        counts.no_tests = True

    extractor = _FAILURE_EXTRACTORS.get(framework)
    failures: List[UnifiedFailure] = extractor(combined, language, framework) if extractor else []

    # Some runners only reveal their failure count through extracted tracebacks.
    if counts.failed == 0 and counts.errors == 0 and failures:
        counts.failed = len(failures)

    normalized_exit = exit_code
    status = TestRunStatus.FAILED
    error_message: Optional[str] = None

    if exit_code == 0:
        if counts.passed > 0 and counts.failed == 0 and counts.errors == 0:
            status = TestRunStatus.PASSED
        elif counts.passed == 0:
            normalized_exit = 5
            status = TestRunStatus.NO_TESTS
            error_message = NO_TESTS_MESSAGE
        else:
            normalized_exit = 1
            status = TestRunStatus.FAILED
            error_message = "Non-zero failures or errors reported despite exit code 0."
    elif exit_code in (-1, 124):
        status = TestRunStatus.TIMED_OUT
        error_message = (
            f"Execution timed out after {timeout_seconds}s. Possible infinite loop detected."
            if timeout_seconds is not None
            else "Execution timed out. Possible infinite loop detected."
        )
    else:
        status = TestRunStatus.FAILED
        error_message = (stderr or stdout or "Tests failed.").strip()[:2000] or "Tests failed."

    return UnifiedTestReport(
        exit_code=normalized_exit,
        status=status,
        passed_count=counts.passed,
        failed_count=counts.failed,
        errors_count=counts.errors,
        skipped_count=counts.skipped,
        total_count=counts.total,
        error=error_message,
        language=language,
        framework=framework,
        runner=runner,
        command=command,
        count_granularity=granularity,
        tests_detected=counts.detected,
        failures=failures,
        normalized_output=build_normalized_output(failures, stdout, stderr),
    )


def describe_supported_runners() -> Dict[str, str]:
    """Return the manifest -> command detection table (for docs, UI and tests)."""
    return {
        "package.json (with a scripts.test entry)": "npm test",
        "Cargo.toml": "cargo test",
        "go.mod": "go test ./...",
        "pyproject.toml | requirements.txt | pytest.ini | tox.ini | setup.py | setup.cfg": "pytest",
        "pom.xml": "mvn -B test",
        "build.gradle | build.gradle.kts": "./gradlew test --console=plain",
    }


__all__ = [
    "CountGranularity",
    "NO_TESTS_MESSAGE",
    "NO_TESTS_MARKERS",
    "PYTHON_MANIFESTS",
    "ParsedCounts",
    "ProjectLanguage",
    "TestFramework",
    "TestRunStatus",
    "TestRunnerSpec",
    "UnifiedFailure",
    "UnifiedTestReport",
    "build_normalized_output",
    "describe_supported_runners",
    "detect_test_runner",
    "infer_spec_from_command",
    "parse_test_output",
    "strip_ansi",
]
