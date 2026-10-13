"""Pre-flight diff validator preventing file truncation and lazy diffs.

Provides DiffValidator to halt lazy truncation tokens, corrupted hunk headers,
and AST syntax breakages in-memory before filesystem or Docker container execution.
"""

from __future__ import annotations

import ast
import difflib
import json
import logging
import os
import re
import textwrap
from typing import Any, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ValidationResult(BaseModel):
    """Structured result of pre-flight diff validation."""

    is_valid: bool = Field(
        ...,
        description="True if diff passes all safety, structural, and AST checks, False otherwise",
    )
    error_type: Optional[str] = Field(
        default=None,
        description="Category of rejection: 'LAZY_TRUNCATION', 'CORRUPTED_HUNK', or 'SYNTAX_BREAKAGE'",
    )
    offending_line: Optional[str] = Field(
        default=None,
        description="The specific line, header, or error snippet that triggered rejection",
    )
    actionable_feedback: Optional[str] = Field(
        default=None,
        description="Detailed actionable feedback instructing the LLM on how to resolve the rejection",
    )

    def __bool__(self) -> bool:
        return self.is_valid

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


class DiffValidator:
    """Pre-flight diff validator that halts lazy diffs, verifies AST integrity, and fails fast."""

    # 1. Truncation Marker Detection Regular Expressions
    BARE_ELLIPSES_RE = re.compile(r"^\s*(\.\.\.|…)\s*$")
    COMMENTED_ELLIPSES_RE = re.compile(r"^\s*(//|#|/\*|--|<!--)\s*(\.\.\.|…)")
    LAZINESS_RE = re.compile(
        r"(?i)(rest of (the )?(code|file|function|class|method|implementation)|remains? unchanged|remain(s)? the same|existing code|omitted for brevity|content omitted)"
    )

    # 2. Strict Unified Diff Hunk Header Pattern: @@ -[0-9]+(,[0-9]+)? \+[0-9]+(,[0-9]+)? @@
    HUNK_HEADER_STRICT_RE = re.compile(r"^@@\s+-[0-9]+(?:,[0-9]+)?\s+\+[0-9]+(?:,[0-9]+)?\s+@@")

    @classmethod
    def check_truncation_markers(cls, raw_diff: str) -> Optional[Tuple[str, int, str]]:
        """Scan added (+) and context ( ) lines for truncation and laziness markers.

        Args:
            raw_diff: Raw unified diff string.

        Returns:
            Tuple of (offending_line, line_number, marker_description) if found, else None.
        """
        lines = raw_diff.splitlines()
        for idx, line in enumerate(lines, 1):
            # Skip file headers and hunk markers
            if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                continue

            # Check added lines (+) and context lines ( )
            if line.startswith("+") or line.startswith(" "):
                content = line[1:]
                if cls.BARE_ELLIPSES_RE.search(content):
                    return line, idx, "bare ellipses token"
                if cls.COMMENTED_ELLIPSES_RE.search(content):
                    return line, idx, "commented ellipses token"
                if cls.LAZINESS_RE.search(content):
                    return line, idx, "natural language laziness marker"

        return None

    @classmethod
    def apply_diff_in_memory(cls, original_content: str, patch_diff: str) -> Tuple[bool, str]:
        """Apply unified diff to original_content in-memory without touching disk.

        Args:
            original_content: Base file content as string.
            patch_diff: Single or multi-file unified diff string.

        Returns:
            Tuple of (success_bool, patched_text_string).
        """
        if not patch_diff or not patch_diff.strip():
            return False, original_content

        is_new_file = bool(re.search(r"--- (?:/dev/null|null)", patch_diff))
        hunk_matches = list(
            re.finditer(
                r"^@@\s+-[0-9]+(?:,[0-9]+)?\s+\+[0-9]+(?:,[0-9]+)?\s+@@.*$",
                patch_diff,
                re.MULTILINE,
            )
        )

        if not hunk_matches:
            return False, original_content

        # Case 1: Pure creation of a new file
        if is_new_file:
            new_file_lines = [
                line[1:]
                for line in patch_diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            ]
            return True, "\n".join(new_file_lines)

        # Case 2: In-memory simulation when target file is not present on disk
        if not original_content:
            reconstructed_lines = []
            for hl in patch_diff.splitlines():
                if hl.startswith("+++") or hl.startswith("---") or hl.startswith("@@"):
                    continue
                if hl.startswith(("+", " ")):
                    reconstructed_lines.append(hl[1:])
            return True, "\n".join(reconstructed_lines)

        # Case 3: Applying hunks to existing file content
        lines = original_content.splitlines()

        for i, match in enumerate(hunk_matches):
            start_idx = match.end()
            end_idx = hunk_matches[i + 1].start() if i + 1 < len(hunk_matches) else len(patch_diff)
            hunk_body = patch_diff[start_idx:end_idx]

            old_lines: list[str] = []
            new_lines: list[str] = []
            removed_lines: list[str] = []
            added_lines: list[str] = []

            for hl in hunk_body.splitlines():
                if hl.startswith("-"):
                    removed_lines.append(hl[1:])
                    old_lines.append(hl[1:])
                elif hl.startswith("+"):
                    added_lines.append(hl[1:])
                    new_lines.append(hl[1:])
                elif hl.startswith(" "):
                    old_lines.append(hl[1:])
                    new_lines.append(hl[1:])
                elif not hl.startswith("\\") and hl.strip():
                    old_lines.append(hl)
                    new_lines.append(hl)

            # Pure addition to empty or new file
            if not old_lines and added_lines:
                lines.extend(added_lines)
                continue

            old_block = "\n".join(old_lines)
            new_block = "\n".join(new_lines)
            curr_text = "\n".join(lines)

            # 1. Exact block match
            if old_block and old_block in curr_text:
                curr_text = curr_text.replace(old_block, new_block, 1)
                lines = curr_text.splitlines()
                continue

            # 2. Fuzzy sliding window (ignoring leading/trailing whitespace variations)
            k = len(old_lines)
            best_score = 0.0
            best_slice = None
            if k > 0 and len(lines) >= k:
                for idx in range(len(lines) - k + 1):
                    window = lines[idx : idx + k]
                    score = difflib.SequenceMatcher(
                        None,
                        [item.strip() for item in window],
                        [item.strip() for item in old_lines],
                    ).ratio()
                    if score > best_score:
                        best_score = score
                        best_slice = (idx, idx + k)

            if best_score >= 0.6 and best_slice:
                s, e = best_slice
                lines[s:e] = new_lines
                continue

            # 3. Match on removed lines specifically
            if removed_lines:
                rk = len(removed_lines)
                r_best_score = 0.0
                r_best_slice = None
                if len(lines) >= rk:
                    for idx in range(len(lines) - rk + 1):
                        r_window = lines[idx : idx + rk]
                        r_score = difflib.SequenceMatcher(
                            None,
                            [item.strip() for item in r_window],
                            [item.strip() for item in removed_lines],
                        ).ratio()
                        if r_score > r_best_score:
                            r_best_score = r_score
                            r_best_slice = (idx, idx + rk)

                if r_best_score >= 0.7 and r_best_slice:
                    s, e = r_best_slice
                    lines[s:e] = added_lines
                    continue

            return False, original_content

        return True, "\n".join(lines)

    @classmethod
    def _check_delimiters_balanced(cls, text: str) -> Optional[str]:
        """Check for balanced delimiters in C-like languages when grammar module is absent."""
        stack: list[tuple[str, int]] = []
        mapping = {")": "(", "}": "{", "]": "["}
        in_string = False
        quote_char = ""
        in_single_line_comment = False
        in_multi_line_comment = False

        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ""

            if in_single_line_comment:
                if ch == "\n":
                    in_single_line_comment = False
                i += 1
                continue

            if in_multi_line_comment:
                if ch == "*" and nxt == "/":
                    in_multi_line_comment = False
                    i += 2
                    continue
                i += 1
                continue

            if in_string:
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == quote_char:
                    in_string = False
                i += 1
                continue

            if ch == "/" and nxt == "/":
                in_single_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_multi_line_comment = True
                i += 2
                continue

            if ch in ('"', "'", "`"):
                in_string = True
                quote_char = ch
                i += 1
                continue

            if ch in ("(", "{", "["):
                stack.append((ch, i))
            elif ch in (")", "}", "]"):
                if not stack or stack[-1][0] != mapping[ch]:
                    return f"Unmatched closing delimiter '{ch}' at position {i}"
                stack.pop()

            i += 1

        if stack:
            unclosed_ch, unclosed_pos = stack[-1]
            return f"Unclosed opening delimiter '{unclosed_ch}' at position {unclosed_pos}"
        return None

    @classmethod
    def simulate_and_verify_ast(
        cls, original_content: str, patch_diff: str, file_path: str
    ) -> Tuple[bool, Optional[str]]:
        """Simulate applying patch in memory and verify AST integrity.

        Args:
            original_content: Base file content.
            patch_diff: Hunk or patch content.
            file_path: Relative or absolute path of the target file.

        Returns:
            Tuple of (is_valid_bool, error_message_or_None).
        """
        success, patched_text = cls.apply_diff_in_memory(original_content, patch_diff)
        if not success:
            return False, f"Failed to apply patch in-memory to '{file_path}'"

        ext = os.path.splitext(file_path)[1].lower()

        # 1. Python AST verification via ast.parse
        if ext == ".py":
            try:
                ast.parse(patched_text, filename=file_path)
            except (SyntaxError, IndentationError) as err:
                if not original_content:
                    try:
                        ast.parse(textwrap.dedent(patched_text), filename=file_path)
                        return True, None
                    except (SyntaxError, IndentationError):
                        pass
                return False, f"SyntaxError in '{file_path}': {err.msg} at line {err.lineno}"

        # 2. JavaScript / TypeScript / Go / Rust AST verification via tree_sitter
        elif ext in (".js", ".jsx", ".ts", ".tsx", ".go", ".rs"):
            tree_sitter_checked = False
            try:
                import tree_sitter

                ts_lang = None
                if ext in (".js", ".jsx"):
                    import tree_sitter_javascript as mod

                    ts_lang = tree_sitter.Language(mod.language())
                elif ext in (".ts", ".tsx"):
                    import tree_sitter_typescript as mod

                    fn = getattr(
                        mod, "language_tsx" if ext == ".tsx" else "language_typescript", None
                    ) or getattr(mod, "language")
                    ts_lang = tree_sitter.Language(fn())
                elif ext == ".go":
                    import tree_sitter_go as mod

                    ts_lang = tree_sitter.Language(mod.language())
                elif ext == ".rs":
                    import tree_sitter_rust as mod

                    ts_lang = tree_sitter.Language(mod.language())

                if ts_lang is not None:
                    parser = tree_sitter.Parser(ts_lang)
                    tree = parser.parse(patched_text.encode("utf-8"))
                    tree_sitter_checked = True

                    if tree.root_node.has_error:

                        def find_err(node: Any) -> Optional[Any]:
                            if node.type == "ERROR" or node.is_missing:
                                return node
                            for c in node.children:
                                r = find_err(c)
                                if r:
                                    return r
                            return None

                        err_node = find_err(tree.root_node)
                        row = (err_node.start_point[0] + 1) if err_node else 1
                        kind = err_node.type if err_node else "ERROR"
                        return (
                            False,
                            f"Tree-sitter syntax corruption ({kind}) in '{file_path}' at line {row}",
                        )
            except Exception:
                pass

            if not tree_sitter_checked:
                delim_err = cls._check_delimiters_balanced(patched_text)
                if delim_err:
                    return False, f"Syntax error in '{file_path}': {delim_err}"

        # 3. JSON verification
        elif ext == ".json":
            try:
                json.loads(patched_text)
            except json.JSONDecodeError as jerr:
                return (
                    False,
                    f"JSON syntax error in '{file_path}': {jerr.msg} at line {jerr.lineno}",
                )

        return True, None

    @classmethod
    def validate(cls, raw_diff: str, worktree_path: Optional[str] = None) -> ValidationResult:
        """Validate diff against lazy truncation, corrupted hunks, and AST breakages.

        Args:
            raw_diff: Raw unified diff string.
            worktree_path: Optional root directory of the worktree for loading existing files.

        Returns:
            ValidationResult instance.
        """
        if not raw_diff or not raw_diff.strip():
            return ValidationResult(
                is_valid=False,
                error_type="CORRUPTED_HUNK",
                offending_line=None,
                actionable_feedback="Diff rejected: Empty diff provided.",
            )

        # 1. Truncation Marker Detection
        truncation_hit = cls.check_truncation_markers(raw_diff)
        if truncation_hit:
            line, line_no, marker_desc = truncation_hit
            offending = line.strip()
            return ValidationResult(
                is_valid=False,
                error_type="LAZY_TRUNCATION",
                offending_line=offending,
                actionable_feedback=(
                    f"Diff rejected: Found truncation marker '{offending}' at line {line_no} ({marker_desc}). "
                    "You MUST output contiguous, verbatim code lines without placeholder comments."
                ),
            )

        # 2. Hunk & Structural Syntax Check
        file_sections = [
            s.strip()
            for s in re.split(r"(?=(?:^|\n)(?:--- [^\n]+\n\+\+\+ |diff --git ))", raw_diff)
            if s.strip()
        ]
        if not file_sections:
            return ValidationResult(
                is_valid=False,
                error_type="CORRUPTED_HUNK",
                offending_line=None,
                actionable_feedback="Diff rejected: Missing target file headers ('--- a/...' and '+++ b/...').",
            )

        for section in file_sections:
            m = re.search(r"--- (?:a/)?([^\s\t\n]+)\s*\n\+\+\+ (?:b/)?([^\s\t\n]+)", section)
            if not m:
                return ValidationResult(
                    is_valid=False,
                    error_type="CORRUPTED_HUNK",
                    offending_line=section.splitlines()[0] if section.splitlines() else None,
                    actionable_feedback="Diff rejected: Target file paths could not be parsed from headers.",
                )

            old_file, new_file = m.group(1), m.group(2)
            rel_path = new_file if new_file != "/dev/null" else old_file
            rel_path = re.sub(r"^[ab]/", "", rel_path)

            hunk_headers = [
                line.strip() for line in section.splitlines() if line.strip().startswith("@@")
            ]
            if not hunk_headers:
                return ValidationResult(
                    is_valid=False,
                    error_type="CORRUPTED_HUNK",
                    offending_line=None,
                    actionable_feedback="Diff rejected: No hunk headers ('@@ -line,count +line,count @@') found in diff.",
                )

            for hh in hunk_headers:
                if not cls.HUNK_HEADER_STRICT_RE.match(hh):
                    return ValidationResult(
                        is_valid=False,
                        error_type="CORRUPTED_HUNK",
                        offending_line=hh,
                        actionable_feedback=(
                            f"Diff rejected: Malformed hunk header '{hh}'. "
                            "Must match format '@@ -line,count +line,count @@'."
                        ),
                    )

            # Check for empty hunks (hunk header without any diff lines)
            hunk_split = re.split(r"(?=(?:^|\n)@@\s+-[0-9]+)", section)
            for h in hunk_split:
                h_str = h.strip()
                if h_str.startswith("@@"):
                    h_lines = [
                        line_item
                        for line_item in h_str.splitlines()[1:]
                        if line_item.startswith(("+", "-", " "))
                        and not line_item.startswith(("+++", "---"))
                    ]
                    if not h_lines:
                        first_line = h_str.splitlines()[0]
                        return ValidationResult(
                            is_valid=False,
                            error_type="CORRUPTED_HUNK",
                            offending_line=first_line,
                            actionable_feedback=(
                                f"Diff rejected: Empty hunk under header '{first_line}'. "
                                "Each hunk must contain added, removed, or context lines."
                            ),
                        )

            # 3. In-Memory AST Pre-Flight Simulation
            original_content = ""
            if worktree_path:
                full_path = os.path.normpath(os.path.join(worktree_path, rel_path))
                if os.path.exists(full_path):
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                            original_content = f.read()
                    except Exception:
                        original_content = ""

            ok, err = cls.simulate_and_verify_ast(original_content, section, rel_path)
            if not ok:
                return ValidationResult(
                    is_valid=False,
                    error_type="SYNTAX_BREAKAGE",
                    offending_line=err,
                    actionable_feedback=(
                        f"Diff rejected: In-memory AST pre-flight simulation detected syntax corruption: {err}. "
                        "Ensure the patch forms valid, complete code syntax."
                    ),
                )

        return ValidationResult(is_valid=True)

    @classmethod
    def validate_no_test_tampering(
        cls, modified_files: list[str], prompt: str
    ) -> tuple[bool, str | None]:
        """Class method alias for validate_no_test_tampering."""
        return validate_no_test_tampering(modified_files, prompt)


def is_test_file(file_path: str) -> bool:
    """Determine whether a file path represents a test file."""
    norm = file_path.replace("\\", "/").lower().strip()
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    filename = parts[-1]

    # Directory check: located in tests/ or test/ directory
    if any(p in ("tests", "test", "__tests__") for p in parts[:-1]):
        return True
    if (
        norm.startswith("tests/")
        or norm.startswith("test/")
        or "/tests/" in norm
        or "/test/" in norm
    ):
        return True

    # Filename pattern check
    if (
        filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.endswith(".test.py")
    ):
        return True
    if (
        filename.endswith(".test.ts")
        or filename.endswith(".test.js")
        or filename.endswith(".spec.ts")
        or filename.endswith(".spec.js")
    ):
        return True
    if filename.endswith("_test.go") or filename.endswith("_test.rs"):
        return True

    return False


def validate_no_test_tampering(modified_files: list[str], prompt: str) -> tuple[bool, str | None]:
    """Validate that modified files do not tamper with test suites unless explicitly requested.

    Args:
        modified_files: List of files modified, added, or deleted by the patch.
        prompt: Root user task prompt.

    Returns:
        Tuple of (is_valid, rejection_reason).
    """
    if not modified_files:
        return True, None

    # Identify any test files among the modified list
    modified_test_files = [
        f for f in modified_files if is_test_file(f) and not f.endswith("_forge_repro_test.py")
    ]
    if not modified_test_files:
        return True, None

    # Check if prompt explicitly requested test additions, modifications, or updates
    test_intent_patterns = [
        r"(?i)\bwrite\s+(?:a\s+|new\s+|the\s+)?tests?\b",
        r"(?i)\bupdate\s+(?:the\s+|all\s+|existing\s+)?tests?\b",
        r"(?i)\bcreate\s+(?:a\s+|new\s+|the\s+)?tests?\b",
        r"(?i)\badd\s+(?:a\s+|new\s+|the\s+)?tests?\b",
        r"(?i)\bmodify\s+(?:the\s+|existing\s+)?tests?\b",
        r"(?i)\bchange\s+(?:the\s+|existing\s+)?tests?\b",
        r"(?i)\binclude\s+(?:a\s+|new\s+|unit\s+)?tests?\b",
        r"(?i)\bwith\s+(?:a\s+|new\s+|unit\s+)?tests?\b",
        r"(?i)\bnew\s+tests?\b",
        r"(?i)\bunit\s+tests?\b",
    ]
    prompt_str = prompt or ""
    allows_test_changes = any(re.search(pat, prompt_str) for pat in test_intent_patterns)

    if allows_test_changes:
        logger.info(
            "validate_no_test_tampering: Test file modifications permitted by user prompt: %s",
            modified_test_files,
        )
        return True, None

    # Flag as TEST_TAMPERING rejection
    rejection_msg = (
        "REJECTED: You modified existing test files. Fix the production bug in the "
        "application source code; do not modify or weaken the tests."
    )
    logger.warning(
        "validate_no_test_tampering: REJECTED patch modifying test files (%s) without prompt permission.",
        modified_test_files,
    )
    return False, rejection_msg
