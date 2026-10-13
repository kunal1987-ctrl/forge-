"""Static AST Code Guardrails for FORGE.

Performs deterministic static analysis on generated patches and Python source code
using Python's built-in `ast` module to block unauthorized system-level operations,
shell execution, dangerous module imports, and code injection primitives before
writing to disk or executing in sandboxes.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Tuple


@dataclass
class SecurityViolation:
    """Represents a specific AST security violation."""

    rule: str
    symbol: str
    line_number: int
    message: str


@dataclass
class ASTSecurityReport:
    """Consolidated security analysis report for a code block or patch."""

    is_safe: bool
    violations: List[SecurityViolation]
    summary: str

    def get_actionable_error(self) -> Optional[str]:
        """Return a formatted error string if security violations were detected."""
        if self.is_safe:
            return None
        msgs = [f"Line {v.line_number}: [{v.rule}] {v.message}" for v in self.violations]
        return f"Static AST Security Guardrail Rejection: {'; '.join(msgs)}"


class SecurityVisitor(ast.NodeVisitor):
    """AST visitor traversing nodes to detect forbidden calls, imports, and attributes."""

    # Modules strictly prohibited from being imported
    FORBIDDEN_MODULES: FrozenSet[str] = frozenset(
        {
            "subprocess",
            "ctypes",
            "pty",
            "socket",
            "urllib",
            "http.client",
            "posix",
            "nt",
            "code",
            "pdb",
        }
    )

    # Sub-modules / functions from 'os' or 'sys' that invoke system execution
    FORBIDDEN_SYSTEM_ATTRIBUTES: FrozenSet[str] = frozenset(
        {
            "system",
            "popen",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            "fork",
            "forkpty",
            "kill",
            "killpg",
            "plock",
        }
    )

    # Builtin functions strictly prohibited
    FORBIDDEN_BUILTINS: FrozenSet[str] = frozenset(
        {
            "eval",
            "exec",
            "__import__",
            "compile",
            "breakpoint",
        }
    )

    # Dangerous attributes often used in Python sandbox escapes / reflection attacks
    FORBIDDEN_ATTRIBUTES: FrozenSet[str] = frozenset(
        {
            "__subclasses__",
            "__globals__",
            "__code__",
            "__builtins__",
        }
    )

    def __init__(self) -> None:
        self.violations: List[SecurityViolation] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            base_module = alias.name.split(".")[0]
            if base_module in self.FORBIDDEN_MODULES or alias.name in self.FORBIDDEN_MODULES:
                self.violations.append(
                    SecurityViolation(
                        rule="FORBIDDEN_IMPORT",
                        symbol=alias.name,
                        line_number=node.lineno,
                        message=f"Unauthorized import of prohibited module '{alias.name}'.",
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            base_module = node.module.split(".")[0]
            if base_module in self.FORBIDDEN_MODULES or node.module in self.FORBIDDEN_MODULES:
                self.violations.append(
                    SecurityViolation(
                        rule="FORBIDDEN_IMPORT",
                        symbol=node.module,
                        line_number=node.lineno,
                        message=f"Unauthorized 'from {node.module} import ...' of prohibited module.",
                    )
                )

            # Check if importing system-level execution functions from os
            if base_module == "os":
                for alias in node.names:
                    if alias.name in self.FORBIDDEN_SYSTEM_ATTRIBUTES:
                        self.violations.append(
                            SecurityViolation(
                                rule="FORBIDDEN_SYSTEM_IMPORT",
                                symbol=f"os.{alias.name}",
                                line_number=node.lineno,
                                message=f"Unauthorized import of system execution function '{alias.name}' from 'os'.",
                            )
                        )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check direct function calls: eval(), exec(), __import__()
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in self.FORBIDDEN_BUILTINS:
                self.violations.append(
                    SecurityViolation(
                        rule="FORBIDDEN_BUILTIN_CALL",
                        symbol=func_name,
                        line_number=node.lineno,
                        message=f"Invocation of prohibited builtin function '{func_name}()'.",
                    )
                )

        # Check attribute function calls: os.system(), os.popen(), importlib.import_module()
        elif isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if attr_name in self.FORBIDDEN_SYSTEM_ATTRIBUTES:
                # Check if called on 'os' or similar object
                owner = ""
                if isinstance(node.func.value, ast.Name):
                    owner = node.func.value.id
                symbol_name = f"{owner}.{attr_name}" if owner else attr_name
                self.violations.append(
                    SecurityViolation(
                        rule="FORBIDDEN_SYSTEM_CALL",
                        symbol=symbol_name,
                        line_number=node.lineno,
                        message=f"Invocation of prohibited system execution method '{symbol_name}()'.",
                    )
                )

            # Dynamic module loading via importlib
            if attr_name == "import_module":
                self.violations.append(
                    SecurityViolation(
                        rule="DYNAMIC_MODULE_LOAD",
                        symbol="import_module",
                        line_number=node.lineno,
                        message="Dynamic runtime module loading via import_module() is prohibited.",
                    )
                )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Check introspection/sandbox escape attributes
        if node.attr in self.FORBIDDEN_ATTRIBUTES:
            self.violations.append(
                SecurityViolation(
                    rule="DANGEROUS_INTROSPECTION",
                    symbol=node.attr,
                    line_number=node.lineno,
                    message=f"Access to dangerous internal attribute '{node.attr}' is prohibited.",
                )
            )
        self.generic_visit(node)


class ASTCodeGuard:
    """Static AST guardrail validator for Python source files and generated diffs."""

    @classmethod
    def scan_python_code(cls, code: str, filename: str = "<source>") -> ASTSecurityReport:
        """Parse Python source into AST and scan for forbidden constructs.

        Args:
            code: Python code string.
            filename: Source descriptor.

        Returns:
            ASTSecurityReport detailing any violations.
        """
        if not code or not code.strip():
            return ASTSecurityReport(is_safe=True, violations=[], summary="Empty code block.")

        try:
            tree = ast.parse(code, filename=filename)
        except SyntaxError as syn_err:
            # If code is a snippet or hunk that isn't standalone valid module, attempt indented block
            tree = None
            try:
                tree = ast.parse(
                    "def _forge_wrapper_func():\n"
                    + "\n".join(f"    {line}" for line in code.splitlines())
                )
            except SyntaxError:
                pass

            if tree is None:
                # Pre-filter for dangerous tokens even in malformed Python syntax
                suspicious_tokens = [
                    "subprocess",
                    "os.system",
                    "os.popen",
                    "eval(",
                    "exec(",
                    "__import__",
                    "ctypes",
                ]
                for token in suspicious_tokens:
                    if token in code:
                        return ASTSecurityReport(
                            is_safe=False,
                            violations=[
                                SecurityViolation(
                                    rule="MALFORMED_DANGEROUS_PAYLOAD",
                                    symbol=token,
                                    line_number=syn_err.lineno or 1,
                                    message=f"Detected prohibited security token '{token}' in unparseable/malformed code.",
                                )
                            ],
                            summary="Prohibited security token detected in malformed syntax.",
                        )
                # Not a parseable Python syntax (e.g. plain text, json, yaml)
                return ASTSecurityReport(
                    is_safe=True,
                    violations=[],
                    summary=f"Skipped AST scan: non-Python or partial syntax ({syn_err.msg}).",
                )

        if tree is None:
            return ASTSecurityReport(
                is_safe=True,
                violations=[],
                summary="Skipped AST scan: unparseable AST tree.",
            )

        visitor = SecurityVisitor()
        visitor.visit(tree)

        is_safe = len(visitor.violations) == 0
        summary = (
            "Code passed static AST security audit."
            if is_safe
            else f"Detected {len(visitor.violations)} security violation(s)."
        )

        return ASTSecurityReport(
            is_safe=is_safe,
            violations=visitor.violations,
            summary=summary,
        )

    @classmethod
    def scan_patch(cls, patch_text: str) -> ASTSecurityReport:
        """Extract added or modified lines from a patch and scan for security violations.

        Accepts both Unified Git diffs ('+ ' lines) and Surgical SEARCH/REPLACE blocks.

        Args:
            patch_text: Raw diff or surgical patch.

        Returns:
            ASTSecurityReport for the modified code sections.
        """
        if not patch_text or not patch_text.strip():
            return ASTSecurityReport(is_safe=True, violations=[], summary="Empty patch.")

        # Extract only newly introduced lines to evaluate intent
        added_lines: List[str] = []

        # Check surgical blocks first
        if ">>>>>>> REPLACE" in patch_text:
            replace_blocks = re.findall(
                r"=======\s*\n(.*?)\n>>>>>>> REPLACE", patch_text, re.DOTALL
            )
            for block in replace_blocks:
                added_lines.extend(block.splitlines())

        # Check unified diff added lines
        for line in patch_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:])

        # If no added lines found, check the full patch text directly
        code_to_check = "\n".join(added_lines) if added_lines else patch_text

        return cls.scan_python_code(code_to_check, filename="<patch_diff>")

    @classmethod
    def validate_patch(cls, patch_text: str) -> Tuple[bool, Optional[str]]:
        """Convenience validation function returning (is_safe, error_message)."""
        report = cls.scan_patch(patch_text)
        return report.is_safe, report.get_actionable_error()


# Parity alias
ASTGuard = ASTCodeGuard
