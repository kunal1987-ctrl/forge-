"""Static AST Anti-Swallowing Lint Guard for FORGE.

Scans Python files to detect exception-swallowing anti-patterns:
1. Bare except: handlers with no type specification.
2. Silent swallowing handlers (e.g. `except Exception: pass`).
3. Broad exception handlers catching `Exception` or `BaseException` without
   a subsequent `raise` statement or explicit assignment to the `state` object.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
from enum import Enum
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Union


class ViolationType(str, Enum):
    """Categorization of static AST exception handling anti-patterns."""

    BARE_EXCEPT = "BARE_EXCEPT"
    SILENT_SWALLOW = "SILENT_SWALLOW"
    UNPROPAGATED_EXCEPTION = "UNPROPAGATED_EXCEPTION"


@dataclass
class ExceptionViolation:
    """Detailed record of an exception handling violation detected by AST analysis."""

    file_path: str
    line_number: int
    col_offset: int
    violation_type: ViolationType
    handler_type: Optional[str]
    message: str
    code_snippet: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert violation to dictionary."""
        data = asdict(self)
        data["violation_type"] = self.violation_type.value
        return data


class ExceptionSwallowGuard(ast.NodeVisitor):
    """AST Visitor that audits try-except handlers for anti-patterns."""

    def __init__(self, filename: str, source_code: str) -> None:
        self.filename = filename
        self.source_lines = source_code.splitlines()
        self.violations: List[ExceptionViolation] = []

    def _get_snippet(self, lineno: int) -> str:
        """Extract line snippet safely."""
        if 1 <= lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()
        return ""

    def _is_broad_exception(self, node_type: Optional[ast.expr]) -> bool:
        """Determine if an exception type expression catches Exception or BaseException."""
        if node_type is None:
            return True  # Bare except is always broad

        type_repr = ast.unparse(node_type).strip()
        if type_repr in ("Exception", "BaseException"):
            return True

        if isinstance(node_type, ast.Tuple):
            for elt in node_type.elts:
                elt_repr = ast.unparse(elt).strip()
                if elt_repr in ("Exception", "BaseException"):
                    return True

        return False

    def _has_raise(self, body: List[ast.stmt]) -> bool:
        """Check if any statement in the handler body raises an exception."""
        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Raise):
                    return True
        return False

    def _has_state_assignment(self, body: List[ast.stmt]) -> bool:
        """Check if handler body assigns error metadata into a state dictionary or object."""
        for stmt in body:
            for sub in ast.walk(stmt):
                # Check regular assignments: state[...] = ... or state.xxx = ... or state = ...
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                            if target.value.id == "state":
                                return True
                        elif isinstance(target, ast.Attribute) and isinstance(
                            target.value, ast.Name
                        ):
                            if target.value.id == "state":
                                return True
                        elif isinstance(target, ast.Name) and target.id == "state":
                            return True
                # Check augmented assignments: state[...] += ...
                elif isinstance(sub, ast.AugAssign):
                    if isinstance(sub.target, ast.Subscript) and isinstance(
                        sub.target.value, ast.Name
                    ):
                        if sub.target.value.id == "state":
                            return True
                    elif isinstance(sub.target, ast.Attribute) and isinstance(
                        sub.target.value, ast.Name
                    ):
                        if sub.target.value.id == "state":
                            return True
                # Check method calls on state: state.update(...) or state.set(...)
                elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if isinstance(sub.func.value, ast.Name) and sub.func.value.id == "state":
                        if sub.func.attr in ("update", "setdefault", "set"):
                            return True
        return False

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        """Inspect every except handler."""
        snippet = self._get_snippet(node.lineno)

        # 1. Check for bare except:
        if node.type is None:
            self.violations.append(
                ExceptionViolation(
                    file_path=self.filename,
                    line_number=node.lineno,
                    col_offset=node.col_offset,
                    violation_type=ViolationType.BARE_EXCEPT,
                    handler_type=None,
                    message="Bare 'except:' handler without type specification is strictly forbidden.",
                    code_snippet=snippet,
                )
            )
            self.generic_visit(node)
            return

        handler_type = ast.unparse(node.type).strip()

        # 2. Check for broad Exception or BaseException
        if self._is_broad_exception(node.type):
            has_raise = self._has_raise(node.body)
            has_state = self._has_state_assignment(node.body)

            if not (has_raise or has_state):
                is_pure_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
                if is_pure_pass:
                    self.violations.append(
                        ExceptionViolation(
                            file_path=self.filename,
                            line_number=node.lineno,
                            col_offset=node.col_offset,
                            violation_type=ViolationType.SILENT_SWALLOW,
                            handler_type=handler_type,
                            message=f"Silent 'except {handler_type}: pass' completely swallows errors and is prohibited.",
                            code_snippet=snippet,
                        )
                    )
                else:
                    self.violations.append(
                        ExceptionViolation(
                            file_path=self.filename,
                            line_number=node.lineno,
                            col_offset=node.col_offset,
                            violation_type=ViolationType.UNPROPAGATED_EXCEPTION,
                            handler_type=handler_type,
                            message=(
                                f"Handler catches broad '{handler_type}' without re-raising or "
                                "assigning error metadata to the 'state' object."
                            ),
                            code_snippet=snippet,
                        )
                    )

        self.generic_visit(node)


def scan_code(code: str, filename: str = "<string>") -> List[ExceptionViolation]:
    """Scan Python source code string for exception-swallowing anti-patterns."""
    try:
        tree = ast.parse(code, filename=filename)
    except SyntaxError as e:
        return [
            ExceptionViolation(
                file_path=filename,
                line_number=e.lineno or 1,
                col_offset=e.offset or 0,
                violation_type=ViolationType.UNPROPAGATED_EXCEPTION,
                handler_type=None,
                message=f"SyntaxError while parsing file: {e}",
                code_snippet="",
            )
        ]

    guard = ExceptionSwallowGuard(filename=filename, source_code=code)
    guard.visit(tree)
    return guard.violations


def scan_file(file_path: Union[str, Path]) -> List[ExceptionViolation]:
    """Scan a single Python file for exception-swallowing anti-patterns."""
    path = Path(file_path)
    if not path.is_file() or not path.name.endswith(".py"):
        return []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [
            ExceptionViolation(
                file_path=str(path),
                line_number=1,
                col_offset=0,
                violation_type=ViolationType.UNPROPAGATED_EXCEPTION,
                handler_type=None,
                message=f"Failed to read file: {e}",
                code_snippet="",
            )
        ]

    return scan_code(content, filename=str(path))


def scan_directory(
    dir_path: Union[str, Path],
    recursive: bool = True,
    exclude_patterns: Optional[List[str]] = None,
) -> List[ExceptionViolation]:
    """Scan all Python files in a directory for exception-swallowing anti-patterns."""
    root = Path(dir_path)
    if not root.exists():
        return []

    pattern = "**/*.py" if recursive else "*.py"
    excludes = exclude_patterns or [
        ".venv",
        "venv",
        "__pycache__",
        ".git",
        ".pytest_cache",
        ".mypy_cache",
    ]

    violations: List[ExceptionViolation] = []
    for py_file in root.glob(pattern):
        if any(exc in py_file.parts for exc in excludes):
            continue
        violations.extend(scan_file(py_file))

    return violations


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    """CLI runner for exception guard checks during local development and CI."""
    parser = argparse.ArgumentParser(
        description="FORGE Static AST Anti-Swallowing Exception Lint Guard"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["services/api/forge/orchestration/nodes.py"],
        help="Files or directories to scan (default: services/api/forge/orchestration/nodes.py)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output violations as JSON array",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Exit on the first violation encountered",
    )

    args = parser.parse_args(argv)

    all_violations: List[ExceptionViolation] = []
    for p_str in args.paths:
        p = Path(p_str)
        if p.is_dir():
            v = scan_directory(p)
        elif p.is_file():
            v = scan_file(p)
        else:
            continue
        all_violations.extend(v)
        if args.fail_fast and all_violations:
            break

    if args.json:
        print(json.dumps([v.to_dict() for v in all_violations], indent=2))
        return 1 if all_violations else 0

    if not all_violations:
        print(
            f"[CLEAN] Exception Guard Clean: 0 anti-patterns detected across {len(args.paths)} target(s)."
        )
        return 0

    print(
        f"[FAIL] Exception Guard Failure: {len(all_violations)} exception-swallowing anti-pattern(s) detected!\n"
    )
    for violation in all_violations:
        print(
            f"[{violation.violation_type.value}] {violation.file_path}:{violation.line_number}:{violation.col_offset}"
        )
        print(f"  Message: {violation.message}")
        if violation.code_snippet:
            print(f"  Snippet: {violation.code_snippet}")
        print()

    return 1


if __name__ == "__main__":
    sys.exit(run_cli())
