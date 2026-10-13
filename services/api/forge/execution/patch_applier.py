"""Surgical Search/Replace patch parser and atomic fuzzy matching engine."""

from __future__ import annotations

import difflib
from services.api.forge.exceptions import PatchApplicationError
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


@dataclass
class SearchReplaceBlock:
    """A parsed search/replace block targeting a file."""

    target_file: str
    search_content: str
    replace_content: str
    raw_block: str = ""


class PatchApplyResult(BaseModel):
    """Structured result of Search/Replace block patch application."""

    success: bool = Field(description="True if all blocks applied cleanly, False otherwise")
    applied_blocks: int = Field(
        default=0, description="Total number of successfully applied blocks"
    )
    failed_blocks: int = Field(default=0, description="Total number of blocks that failed to apply")
    error_message: Optional[str] = Field(
        default=None, description="Diagnostic error message if any block failed"
    )
    modified_files: List[str] = Field(
        default_factory=list, description="List of modified file paths"
    )
    applied_diff: str = Field(default="", description="Unified diff of all applied changes")

    @property
    def error(self) -> Optional[str]:
        """Backward-compatibility alias for error_message."""
        return self.error_message

    @property
    def total_blocks(self) -> int:
        return self.applied_blocks + self.failed_blocks

    def __bool__(self) -> bool:
        return self.success


# Alias for backward compatibility
PatchApplicationResult = PatchApplyResult


class SurgicalPatchApplier:
    """Applies semantic SEARCH/REPLACE blocks surgically to repository files.

    Format expected:
        [File / Path header, e.g.: File: path/to/file.py]
        <<<<<<< SEARCH
        [exact or near-exact code to match]
        =======
        [replacement code]
        >>>>>>> REPLACE

    Implements tiered search & fuzzy matching:
    1. Line Ending Normalization (\r\n -> \n) across targets and blocks.
    2. Primary Match: Exact string match of SEARCH block inside target file.
    3. Fallback Fuzzy Match:
       - Line-by-line whitespace normalization (trailing spaces and blank line variance).
       - Relative indentation structural matching (maintaining indentation offsets).
       - Flexible whitespace token matching (\\s+ matching arbitrary whitespace sequences).
       - Sliding window SequenceMatcher similarity matching (threshold >= 0.88).
    4. Atomic disk writes using os.replace to prevent partial writes.
    """

    @classmethod
    def get_block_regex(cls) -> re.Pattern:
        pattern = r"<<<<<<< SEARCH[^\n]*\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE"
        return re.compile(pattern, re.DOTALL)

    @classmethod
    def get_file_header_regex(cls) -> re.Pattern:
        pattern = r"(?:^|\n)(?:###\s+File:?|File:?|Filename:?|Path:?|---\s+(?:[ab]/)?|\+\+\+\s+(?:[ab]/)?|diff\s+--git\s+(?:[ab]/)?)\s*([^\s\n\r]+)"
        return re.compile(pattern, re.IGNORECASE)

    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root)

    @classmethod
    def is_search_replace_patch(cls, text: str) -> bool:
        """Check if the input string contains SEARCH/REPLACE blocks."""
        if not text:
            return False
        return "<<<<<<< SEARCH" in text and "=======" in text and ">>>>>>> REPLACE" in text

    @classmethod
    def parse_blocks(
        cls, patch_text: str, default_file: Optional[str] = None
    ) -> List[SearchReplaceBlock]:
        """Parse patch text into SearchReplaceBlock objects, associating each block

        with its preceding file header or file indication.
        """
        blocks: List[SearchReplaceBlock] = []
        if not cls.is_search_replace_patch(patch_text):
            return blocks

        # Normalize line endings to \n
        normalized_patch = patch_text.replace("\r\n", "\n")
        block_re = cls.get_block_regex()
        header_re = cls.get_file_header_regex()

        matches = list(block_re.finditer(normalized_patch))
        if not matches:
            return blocks

        last_idx = 0
        current_file = default_file or ""

        for match in matches:
            preceding_text = normalized_patch[last_idx : match.start()]
            file_headers = header_re.findall(preceding_text)
            if file_headers:
                raw_file = file_headers[-1].strip().strip('"').strip("'")
                if raw_file.startswith("a/") or raw_file.startswith("b/"):
                    raw_file = raw_file[2:]
                current_file = raw_file

            search_content = match.group(1)
            replace_content = match.group(2)

            blocks.append(
                SearchReplaceBlock(
                    target_file=current_file,
                    search_content=search_content,
                    replace_content=replace_content,
                    raw_block=match.group(0),
                )
            )
            last_idx = match.end()

        return blocks

    def apply(self, patch_text: str, default_file: Optional[str] = None) -> PatchApplyResult:
        """Applies all SEARCH/REPLACE blocks in patch_text to files under repo_root atomically."""
        blocks = self.parse_blocks(patch_text, default_file=default_file)
        if not blocks:
            return PatchApplyResult(
                success=False,
                applied_blocks=0,
                failed_blocks=0,
                error_message="No valid <<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE blocks found.",
            )

        # Group blocks by target file
        file_blocks: dict[str, List[SearchReplaceBlock]] = {}
        for block in blocks:
            if not block.target_file:
                return PatchApplyResult(
                    success=False,
                    applied_blocks=0,
                    failed_blocks=len(blocks),
                    error_message=f"Block missing target file header before:\n{block.search_content[:100]}...",
                )
            file_blocks.setdefault(block.target_file, []).append(block)

        modified_files: List[str] = []
        applied_count = 0
        diff_accum: List[str] = []

        for rel_path, b_list in file_blocks.items():
            file_path = (self.repo_root / rel_path).resolve()
            if not file_path.exists():
                return PatchApplyResult(
                    success=False,
                    applied_blocks=applied_count,
                    failed_blocks=len(blocks) - applied_count,
                    error_message=f"Target file does not exist: {rel_path}",
                )

            try:
                original_text = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, IOError, PatchApplicationError) as e:
                patch_err = (
                    e
                    if isinstance(e, PatchApplicationError)
                    else PatchApplicationError(
                        f"Failed to read file {rel_path}: {e}", target_file=rel_path, cause=e
                    )
                )
                return PatchApplyResult(
                    success=False,
                    applied_blocks=applied_count,
                    failed_blocks=len(blocks) - applied_count,
                    error_message=str(patch_err),
                )

            current_text = original_text
            for block in b_list:
                new_text, success = self.apply_block_to_content(current_text, block)
                if not success:
                    return PatchApplyResult(
                        success=False,
                        applied_blocks=applied_count,
                        failed_blocks=len(blocks) - applied_count,
                        error_message=f"Failed to match search block in {rel_path}:\n{block.search_content}",
                    )
                current_text = new_text
                applied_count += 1

            # Atomic disk write: write to temp file then os.replace
            temp_file = file_path.with_name(f".tmp_{file_path.name}_{uuid.uuid4().hex[:8]}")
            try:
                temp_file.write_text(current_text, encoding="utf-8")
                os.replace(temp_file, file_path)
            except (OSError, IOError, UnicodeDecodeError, PatchApplicationError) as write_err:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except OSError:
                        pass
                patch_err = (
                    write_err
                    if isinstance(write_err, PatchApplicationError)
                    else PatchApplicationError(
                        f"Atomic write failed for {rel_path}: {write_err}",
                        target_file=rel_path,
                        cause=write_err,
                    )
                )
                return PatchApplyResult(
                    success=False,
                    applied_blocks=applied_count - len(b_list),
                    failed_blocks=len(blocks) - (applied_count - len(b_list)),
                    error_message=str(patch_err),
                )

            modified_files.append(rel_path)

            # Generate unified diff for diagnostics
            orig_lines = original_text.splitlines(keepends=True)
            new_lines = current_text.splitlines(keepends=True)
            file_diff = "".join(
                difflib.unified_diff(
                    orig_lines,
                    new_lines,
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                )
            )
            if file_diff:
                diff_accum.append(file_diff)

        return PatchApplyResult(
            success=True,
            applied_blocks=applied_count,
            failed_blocks=0,
            modified_files=modified_files,
            applied_diff="\n".join(diff_accum),
        )

    def apply_block_to_content(self, content: str, block: SearchReplaceBlock) -> Tuple[str, bool]:
        """Locate search_content in content using multi-tier fuzzy matching and splice with replace_content.

        Returns (new_content, success).
        """
        # Normalize line endings to \n
        search = block.search_content.replace("\r\n", "\n")
        replace = block.replace_content.replace("\r\n", "\n")
        norm_content = content.replace("\r\n", "\n")
        had_crlf = "\r\n" in content

        # Tier 1: Exact match
        if search in norm_content:
            count = norm_content.count(search)
            if count > 1:
                logger.warning("Multiple exact matches (%d) found in file for search block", count)
            res = norm_content.replace(search, replace, 1)
            if had_crlf:
                res = res.replace("\n", "\r\n")
            return res, True

        # Tier 2: Line-by-line whitespace normalization & relative indentation structure
        match_res = self._match_line_normalized(norm_content, search, replace)
        if match_res is not None:
            if had_crlf:
                match_res = match_res.replace("\n", "\r\n")
            return match_res, True

        # Tier 3: Flexible whitespace regex matching
        match_res = self._match_regex_flexible(norm_content, search, replace)
        if match_res is not None:
            if had_crlf:
                match_res = match_res.replace("\n", "\r\n")
            return match_res, True

        # Tier 4: Line-based fuzzy sliding window (Levenshtein / difflib SequenceMatcher)
        match_res = self._match_fuzzy_sliding_window(norm_content, search, replace)
        if match_res is not None:
            if had_crlf:
                match_res = match_res.replace("\n", "\r\n")
            return match_res, True

        return content, False

    def _match_line_normalized(self, content: str, search: str, replace: str) -> Optional[str]:
        """Match lines by normalizing whitespace while verifying relative indentation structure."""
        content_lines = content.splitlines(keepends=True)
        search_lines = search.splitlines(keepends=True)

        c_stripped = [line.strip() for line in content_lines]
        s_stripped = [line.strip() for line in search_lines]

        # Trim boundary empty lines in search block
        s_start = 0
        while s_start < len(s_stripped) and not s_stripped[s_start]:
            s_start += 1
        s_end = len(s_stripped)
        while s_end > s_start and not s_stripped[s_end - 1]:
            s_end -= 1

        if s_start >= s_end:
            return None

        active_s_stripped = s_stripped[s_start:s_end]
        active_s_lines = search_lines[s_start:s_end]
        search_len = len(active_s_stripped)

        # Calculate relative indentation of search block non-empty lines
        first_non_empty_indent = len(active_s_lines[0]) - len(active_s_lines[0].lstrip())
        s_rel_indents = [
            (len(line) - len(line.lstrip())) - first_non_empty_indent if line.strip() else 0
            for line in active_s_lines
        ]

        matched_indices: List[int] = []

        for i in range(len(c_stripped) - search_len + 1):
            window = c_stripped[i : i + search_len]
            if window == active_s_stripped:
                # Check relative indentation structure
                window_lines = content_lines[i : i + search_len]
                w_first_indent = len(window_lines[0]) - len(window_lines[0].lstrip())
                w_rel_indents = [
                    (len(line) - len(line.lstrip())) - w_first_indent if line.strip() else 0
                    for line in window_lines
                ]
                if w_rel_indents == s_rel_indents or all(
                    not active_s_stripped[k] or w_rel_indents[k] == s_rel_indents[k]
                    for k in range(search_len)
                ):
                    matched_indices.append(i)

        # If exact relative indentation didn't match, fallback to pure stripped line match
        if not matched_indices:
            for i in range(len(c_stripped) - search_len + 1):
                if c_stripped[i : i + search_len] == active_s_stripped:
                    matched_indices.append(i)

        if len(matched_indices) == 1:
            idx = matched_indices[0]
            prefix = "".join(content_lines[:idx])
            suffix = "".join(content_lines[idx + search_len :])

            # Re-indent replacement lines to match target base indentation if applicable
            target_first_indent = len(content_lines[idx]) - len(content_lines[idx].lstrip())
            search_first_indent = len(active_s_lines[0]) - len(active_s_lines[0].lstrip())
            indent_diff = target_first_indent - search_first_indent

            rep_lines = replace.splitlines(keepends=True)
            adjusted_rep_lines = []
            for r_line in rep_lines:
                if indent_diff > 0 and r_line.strip():
                    adjusted_rep_lines.append((" " * indent_diff) + r_line)
                elif indent_diff < 0 and r_line.startswith(" " * abs(indent_diff)):
                    adjusted_rep_lines.append(r_line[abs(indent_diff) :])
                else:
                    adjusted_rep_lines.append(r_line)

            rep_str = "".join(adjusted_rep_lines)
            if content.endswith("\n") and not rep_str.endswith("\n") and not suffix:
                rep_str += "\n"

            return prefix + rep_str + suffix

        return None

    def _match_regex_flexible(self, content: str, search: str, replace: str) -> Optional[str]:
        r"""Construct a regex where whitespace matches \s+ flexibly."""
        tokens = re.split(r"(\s+)", search)
        regex_parts = []
        for token in tokens:
            if not token:
                continue
            if re.match(r"^\s+$", token):
                regex_parts.append(r"\s+")
            else:
                regex_parts.append(re.escape(token))

        pattern_str = "".join(regex_parts)
        try:
            pattern = re.compile(pattern_str)
            matches = list(pattern.finditer(content))
            if len(matches) == 1:
                m = matches[0]
                return content[: m.start()] + replace + content[m.end() :]
        except Exception as e:
            logger.debug("Flexible regex failed: %s", e)

        return None

    def _match_fuzzy_sliding_window(
        self, content: str, search: str, replace: str, threshold: float = 0.88
    ) -> Optional[str]:
        """Slide a window over content lines to find a high-similarity block."""
        c_lines = content.splitlines()
        s_lines = search.splitlines()

        s_text = "\n".join([line.strip() for line in s_lines if line.strip()])
        if not s_text:
            return None

        window_size = max(1, len(s_lines))
        best_ratio = 0.0
        best_pos = -1

        for i in range(len(c_lines) - window_size + 1):
            sub_text = "\n".join(
                [line.strip() for line in c_lines[i : i + window_size] if line.strip()]
            )
            ratio = difflib.SequenceMatcher(None, s_text, sub_text).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_pos = i

        if best_ratio >= threshold and best_pos != -1:
            raw_lines = content.splitlines(keepends=True)
            prefix = "".join(raw_lines[:best_pos])
            suffix = "".join(raw_lines[best_pos + window_size :])
            rep = replace
            if content.endswith("\n") and not rep.endswith("\n"):
                rep += "\n"
            return prefix + rep + suffix

        return None
