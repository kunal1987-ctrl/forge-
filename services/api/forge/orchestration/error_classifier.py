"""Error classification engine for parsing Docker sandbox test outputs into distinct strategic types.

Polyglot-aware: the marker tables below cover pytest, npm (jest / vitest / mocha),
cargo, ``go test``, Maven Surefire and Gradle. The sandbox's unified parser also
emits a normalized projection (``FAILED <file>::<name>`` / ``E   <type>: <msg>``)
so failure locations survive even when a runner's native format is unfamiliar.

This module deliberately has **no imports outside the standard library** -- it is
consumed on error paths, so it must never drag in Docker or HTTP dependencies.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Optional, Tuple

# Source extensions recognised when scraping a failing file out of a traceback.
SOURCE_EXTENSION_ALTERNATION = (
    "py|pyi|go|rs|ts|tsx|js|jsx|mjs|cjs|java|kt|kts|rb|cs|cpp|cc|c|h|php|swift|scala"
)

# Compile-time / static error markers, normalized across languages.
POLYGLOT_SYNTAX_MARKERS: Tuple[str, ...] = (
    # Python
    "SyntaxError",
    "IndentationError",
    "TabError",
    # JavaScript / TypeScript
    "Unexpected token",
    "Unexpected end of",
    # Go
    "syntax error:",
    "expected ';'",
    "expected declaration",
    "expected type",
    # Java
    "';' expected",
    "class, interface, enum, or record expected",
    "reached end of file while parsing",
    # Rust
    "mismatched closing delimiter",
    "expected one of",
)

# Missing dependency / unresolved symbol markers.
POLYGLOT_IMPORT_MARKERS: Tuple[str, ...] = (
    # Python
    "ImportError",
    "ModuleNotFoundError",
    "No module named",
    "cannot import name",
    # Node
    "Cannot find module",
    "Module not found",
    "ERR_MODULE_NOT_FOUND",
    # Rust
    "unresolved import",
    "can't find crate",
    "E0432",
    "E0433",
    # Go
    "no required module provides package",
    "cannot find package",
    "undefined:",
    # Java
    "cannot find symbol",
    "package does not exist",
    "ClassNotFoundException",
    "NoClassDefFoundError",
)

# Failing-assertion markers, including the unified parser's projection.
POLYGLOT_ASSERTION_MARKERS: Tuple[str, ...] = (
    # Python
    "AssertionError",
    "assert ",
    # JavaScript / TypeScript
    "expect(",
    "toBe",
    # Go
    "--- FAIL",
    # Rust
    "panicked at",
    # Java
    "<<< FAILURE!",
    "<<< ERROR!",
    # pytest short summary and the sandbox's normalized projection
    "FAILED",
)


class ErrorCategory(str, Enum):
    """Distinct strategic error categories directing the repair loop."""

    SYNTAX_ERROR = "SYNTAX_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"

    @property
    def category(self) -> ErrorCategory:
        return self

    @property
    def failing_file(self) -> str:
        return getattr(self, "_failing_file", "unknown")

    @failing_file.setter
    def failing_file(self, val: str) -> None:
        object.__setattr__(self, "_failing_file", val)

    @property
    def mismatch_snippet(self) -> str:
        return getattr(self, "_mismatch_snippet", str(self.value))

    @mismatch_snippet.setter
    def mismatch_snippet(self, val: str) -> None:
        object.__setattr__(self, "_mismatch_snippet", val)

    @property
    def offending_symbol(self) -> str:
        return getattr(self, "_offending_symbol", "")

    @offending_symbol.setter
    def offending_symbol(self, val: str) -> None:
        object.__setattr__(self, "_offending_symbol", val)

    def model_dump(self) -> Dict[str, Any]:
        return {
            "category": self.value,
            "failing_file": self.failing_file,
            "mismatch_snippet": self.mismatch_snippet,
            "offending_symbol": self.offending_symbol,
        }


class ErrorClassificationResult:
    """Detailed error classification result with surgical error location and mismatch snippet."""

    def __init__(
        self,
        category: ErrorCategory,
        failing_file: str = "unknown",
        line_number: Optional[int] = None,
        mismatch_snippet: str = "",
        offending_symbol: str = "",
        raw_message: str = "",
        actionable_guidance: str = "",
    ):
        self.category = category
        self.failing_file = failing_file
        self.line_number = line_number
        self.mismatch_snippet = mismatch_snippet
        self.offending_symbol = offending_symbol
        self.raw_message = raw_message
        self.actionable_guidance = actionable_guidance

    def model_dump(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "failing_file": self.failing_file,
            "line_number": self.line_number,
            "mismatch_snippet": self.mismatch_snippet,
            "offending_symbol": self.offending_symbol,
            "raw_message": self.raw_message,
            "actionable_guidance": self.actionable_guidance,
        }


class ErrorClassifier:
    """Inspects raw Docker test logs and categorizes failures into distinct strategic types."""

    @staticmethod
    def classify(
        raw_output: str,
        stderr: str = "",
        error_payload: Optional[Dict[str, Any]] = None,
    ) -> ErrorCategory:
        """Categorize raw test output and structured error_payload into ErrorCategory enum."""
        combined = ((raw_output or "") + "\n" + (stderr or "")).strip()
        if error_payload:
            err_type = str(error_payload.get("type", ""))
            err_msg = str(error_payload.get("message", ""))
            combined = f"{combined}\n{err_type}: {err_msg}".strip()
        if not combined:
            return ErrorCategory.UNKNOWN

        failing_file = "unknown"
        m_file = re.search(
            rf"(?:FAILED|File)\s+['\"]?([^'\":\n]+\.(?:{SOURCE_EXTENSION_ALTERNATION}))\b",
            combined,
        )
        if m_file:
            failing_file = m_file.group(1).replace("\\", "/")

        mismatch_lines = []
        offending_symbol = ""
        for line in combined.splitlines():
            line_s = line.strip()
            if line_s.startswith("E   ") or line_s.startswith("E ") or "AssertionError" in line_s:
                mismatch_lines.append(line_s)
        mismatch_snippet = "\n".join(mismatch_lines) if mismatch_lines else ""
        m_sym = re.search(
            r"(?:cannot import name|No module named)\s+['\"]?([a-zA-Z0-9_\.]+)['\"]?", combined
        )
        if m_sym:
            offending_symbol = m_sym.group(1)

        result: ErrorCategory
        # Check error_payload direct exception hints
        if error_payload:
            err_t = str(error_payload.get("type", ""))
            if err_t in ("PatchApplicationError", "SyntaxError", "IndentationError"):
                result = ErrorCategory.SYNTAX_ERROR
                result.failing_file = error_payload.get("details", {}).get(
                    "target_file", failing_file
                )
                result.mismatch_snippet = error_payload.get("message", "")
                result.offending_symbol = offending_symbol
                return result
            elif err_t == "SandboxExecutionError" and error_payload.get("details", {}).get(
                "timed_out"
            ):
                result = ErrorCategory.TIMEOUT
                result.failing_file = failing_file
                result.mismatch_snippet = error_payload.get("message", "")
                result.offending_symbol = offending_symbol
                return result

        if "timed out" in combined.lower() or "timeout" in combined.lower():
            result = ErrorCategory.TIMEOUT
        elif any(marker in combined for marker in POLYGLOT_SYNTAX_MARKERS):
            result = ErrorCategory.SYNTAX_ERROR
        elif any(marker in combined for marker in POLYGLOT_IMPORT_MARKERS):
            result = ErrorCategory.IMPORT_ERROR
        elif any(marker in combined for marker in POLYGLOT_ASSERTION_MARKERS):
            result = ErrorCategory.ASSERTION_FAILURE
        else:
            result = ErrorCategory.UNKNOWN

        result.failing_file = failing_file
        result.mismatch_snippet = mismatch_snippet or str(result.value)
        result.offending_symbol = offending_symbol

        return result
