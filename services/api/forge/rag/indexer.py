"""Deep Recursive Code Intelligence and Tree-Sitter AST Indexer for FORGE Graph-RAG.

Performs recursive directory traversal across multi-language codebases (.py, .ts, .js, .rs, .go),
extracts granular AST symbol definitions (classes, methods, functions) and dependency graph edges
(imports, calls, enclosed_by) via Tree-Sitter, applies semantic text chunking with overlap for
large files, and batch inserts vector embeddings into PostgreSQL/pgvector with strict transaction safety.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
from typing import Any, List, Optional, Set, Tuple
import uuid

from openai import AsyncOpenAI
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
import tree_sitter
import tree_sitter_python as tspython

from services.api.forge.security.sanitizer import PromptSanitizer
from services.api.models.code import CodeChunk, CodeEdge

logger = logging.getLogger("forge.rag.indexer")


# ------------------------------------------------------------------------------
# 1. CONFIGURATION & DATA STRUCTURES
# ------------------------------------------------------------------------------


@dataclass
class CodeIndexerConfig:
    """Configuration options for repository traversal, AST parsing, and chunking."""

    supported_extensions: Set[str] = field(
        default_factory=lambda: {
            # Python
            ".py",
            # JavaScript / TypeScript
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            # Systems languages
            ".rs",
            ".go",
            # Other source & docs
            ".java",
            ".c",
            ".cpp",
            ".h",
            ".hpp",
            ".cs",
            ".rb",
            ".php",
            ".md",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".sql",
        }
    )

    ignored_directories: Set[str] = field(
        default_factory=lambda: {
            ".git",
            ".svn",
            ".hg",
            "node_modules",
            "vendor",
            "venv",
            ".venv",
            "env",
            ".env",
            "dist",
            "build",
            "out",
            "target",
            "bin",
            "obj",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".next",
            ".nuxt",
            ".turbo",
            ".cargo",
            ".vscode",
            ".idea",
        }
    )

    max_file_size_bytes: int = 1024 * 1024  # 1 MB threshold to skip giant or minified files
    max_chunk_chars: int = 1200  # Target characters per semantic code chunk (~35-40 lines)
    chunk_overlap_chars: int = 200  # Overlap between consecutive chunks (~6-8 lines)
    embedding_batch_size: int = 64  # Batch size for OpenAI embedding API calls
    db_batch_size: int = 100  # Batch size for database bulk operations

    @property
    def chunk_size(self) -> int:
        return self.max_chunk_chars

    @chunk_size.setter
    def chunk_size(self, val: int) -> None:
        self.max_chunk_chars = val

    @property
    def chunk_overlap(self) -> int:
        return self.chunk_overlap_chars

    @chunk_overlap.setter
    def chunk_overlap(self, val: int) -> None:
        self.chunk_overlap_chars = val


@dataclass
class CodeSymbol:
    """Granular AST symbol extracted from source code."""

    file_path: str
    symbol_name: str
    symbol_type: str  # 'function', 'method', 'class', 'interface', 'struct', 'module', 'chunk'
    parent_symbol: Optional[str]
    start_line: int
    end_line: int
    content: str


@dataclass
class CodeDependencyEdge:
    """Dependency relationship between two AST symbols or files."""

    source_symbol: str
    target_symbol: str
    edge_type: str  # 'imports', 'calls', 'enclosed_by'
    file_path: str


# ------------------------------------------------------------------------------
# 2. SEMANTIC TEXT CHUNKING WITH OVERLAP
# ------------------------------------------------------------------------------


def semantic_chunk_content(
    content: str,
    file_path: str,
    max_chunk_chars: int = 1200,
    overlap_chars: int = 200,
    base_symbol_name: Optional[str] = None,
    base_symbol_type: str = "chunk",
    base_parent_symbol: Optional[str] = None,
    start_line_offset: int = 1,
    *,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    symbol_name: Optional[str] = None,
    symbol_type: Optional[str] = None,
    parent_symbol: Optional[str] = None,
) -> List[CodeSymbol]:
    """Split source code or document text into semantic, overlapping chunks at newline boundaries.

    Ensures that large files, scripts, or documentation produce comprehensive,
    multi-symbol coverage for Graph-RAG rather than being truncated or represented
    by a single monolithic symbol.
    """
    if chunk_size is not None:
        max_chunk_chars = chunk_size
    if chunk_overlap is not None:
        overlap_chars = chunk_overlap
    if symbol_name is not None:
        base_symbol_name = symbol_name
    if symbol_type is not None:
        base_symbol_type = symbol_type
    if parent_symbol is not None:
        base_parent_symbol = parent_symbol
    if not content or not content.strip():
        return []

    lines = content.splitlines(keepends=True)
    if not lines:
        return []

    # If the entire content fits within the max chunk size, return a single symbol
    if len(content) <= max_chunk_chars:
        stem = base_symbol_name or Path(file_path).stem
        return [
            CodeSymbol(
                file_path=file_path,
                symbol_name=stem,
                symbol_type=base_symbol_type,
                parent_symbol=base_parent_symbol,
                start_line=start_line_offset,
                end_line=start_line_offset + len(lines) - 1,
                content=content.strip(),
            )
        ]

    chunks: List[CodeSymbol] = []
    start_idx = 0
    chunk_num = 1
    stem = base_symbol_name or Path(file_path).stem

    while start_idx < len(lines):
        current_lines: List[str] = []
        current_len = 0
        end_idx = start_idx

        while end_idx < len(lines) and (current_len < max_chunk_chars or not current_lines):
            line = lines[end_idx]
            current_lines.append(line)
            current_len += len(line)
            end_idx += 1

        chunk_text = "".join(current_lines).strip()
        if chunk_text:
            s_line = start_line_offset + start_idx
            e_line = start_line_offset + end_idx - 1
            sym_name = f"{stem}#p{chunk_num}" if chunk_num > 1 else stem
            chunks.append(
                CodeSymbol(
                    file_path=file_path,
                    symbol_name=sym_name,
                    symbol_type=base_symbol_type,
                    parent_symbol=base_parent_symbol,
                    start_line=s_line,
                    end_line=max(s_line, e_line),
                    content=chunk_text,
                )
            )
            chunk_num += 1

        if end_idx >= len(lines):
            break

        # Calculate overlap step-back in line count
        overlap_len = 0
        step_back = 0
        while step_back < len(current_lines) and overlap_len < overlap_chars:
            step_back += 1
            overlap_len += len(current_lines[-step_back])

        next_start = end_idx - step_back
        if next_start <= start_idx:
            next_start = start_idx + 1
        start_idx = next_start

    return chunks


# ------------------------------------------------------------------------------
# 3. MULTI-LANGUAGE AST PARSER (TREE-SITTER + REGEX PATTERN ENGINES)
# ------------------------------------------------------------------------------


class TreeSitterASTExtractor:
    """Deep AST parser extracting symbols and dependency edges across multiple languages."""

    def __init__(self) -> None:
        """Initialize Tree-sitter Python language and parser."""
        self.py_language = tree_sitter.Language(tspython.language())
        self.py_parser = tree_sitter.Parser(self.py_language)

    def parse_python(
        self, file_path: str, content: str
    ) -> Tuple[List[CodeSymbol], List[CodeDependencyEdge]]:
        """Parse Python source code with Tree-sitter, extracting all functions, classes, and edges."""
        encoded = content.encode("utf8")
        tree = self.py_parser.parse(encoded)

        symbols: List[CodeSymbol] = []
        edges: List[CodeDependencyEdge] = []
        seen_edges: Set[Tuple[str, str, str]] = set()
        scope_stack: List[str] = []

        def extract_callee(call_node: Any) -> Optional[str]:
            func_node = call_node.child_by_field_name("function") or (
                call_node.children[0] if call_node.children else None
            )
            if not func_node:
                return None
            target = func_node
            while target.type == "parenthesized_expression" and target.named_child_count > 0:
                target = target.named_children[0]
            if target.type == "identifier":
                return encoded[target.start_byte : target.end_byte].decode("utf8", errors="replace")
            elif target.type == "attribute":
                attr_child = target.child_by_field_name("attribute")
                if attr_child and attr_child.type == "identifier":
                    return encoded[attr_child.start_byte : attr_child.end_byte].decode(
                        "utf8", errors="replace"
                    )
            return None

        def traverse(node: Any) -> None:
            # 1. Import statements
            if node.type == "import_statement":
                for child in node.children:
                    if child.type == "dotted_name":
                        mod = child.text.decode("utf8")
                        key = (file_path, mod, "imports")
                        if key not in seen_edges:
                            seen_edges.add(key)
                            edges.append(
                                CodeDependencyEdge(
                                    source_symbol=file_path,
                                    target_symbol=mod,
                                    edge_type="imports",
                                    file_path=file_path,
                                )
                            )
                    elif child.type == "aliased_import":
                        orig = child.children[0].text.decode("utf8")
                        key = (file_path, orig, "imports")
                        if key not in seen_edges:
                            seen_edges.add(key)
                            edges.append(
                                CodeDependencyEdge(
                                    source_symbol=file_path,
                                    target_symbol=orig,
                                    edge_type="imports",
                                    file_path=file_path,
                                )
                            )

            elif node.type == "import_from_statement":
                mod_node = next(
                    (c for c in node.children if c.type in {"dotted_name", "relative_import"}),
                    None,
                )
                mod = mod_node.text.decode("utf8") if mod_node else ""
                if mod:
                    key = (file_path, mod, "imports")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            CodeDependencyEdge(
                                source_symbol=file_path,
                                target_symbol=mod,
                                edge_type="imports",
                                file_path=file_path,
                            )
                        )

                past_import = False
                for child in node.children:
                    if child.type == "import":
                        past_import = True
                        continue
                    if past_import:
                        if child.type == "dotted_name":
                            sym_name = child.text.decode("utf8")
                            full_target = f"{mod}.{sym_name}" if mod else sym_name
                            key = (file_path, full_target, "imports")
                            if key not in seen_edges:
                                seen_edges.add(key)
                                edges.append(
                                    CodeDependencyEdge(
                                        source_symbol=file_path,
                                        target_symbol=full_target,
                                        edge_type="imports",
                                        file_path=file_path,
                                    )
                                )
                        elif child.type == "aliased_import":
                            orig_node = child.children[0] if child.children else None
                            if orig_node:
                                sym_name = orig_node.text.decode("utf8")
                                full_target = f"{mod}.{sym_name}" if mod else sym_name
                                key = (file_path, full_target, "imports")
                                if key not in seen_edges:
                                    seen_edges.add(key)
                                    edges.append(
                                        CodeDependencyEdge(
                                            source_symbol=file_path,
                                            target_symbol=full_target,
                                            edge_type="imports",
                                            file_path=file_path,
                                        )
                                    )

            # 2. Class definition
            elif node.type == "class_definition":
                name_node = next((c for c in node.children if c.type == "identifier"), None)
                sym_name = name_node.text.decode("utf8") if name_node else "AnonymousClass"
                parent_sym = scope_stack[-1] if scope_stack else None
                snippet = encoded[node.start_byte : node.end_byte].decode("utf8", errors="replace")

                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=sym_name,
                        symbol_type="class",
                        parent_symbol=parent_sym,
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        content=snippet,
                    )
                )

                if parent_sym:
                    key = (f"{file_path}::{sym_name}", f"{file_path}::{parent_sym}", "enclosed_by")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            CodeDependencyEdge(
                                source_symbol=f"{file_path}::{sym_name}",
                                target_symbol=f"{file_path}::{parent_sym}",
                                edge_type="enclosed_by",
                                file_path=file_path,
                            )
                        )

                scope_stack.append(sym_name)
                for child in node.children:
                    traverse(child)
                scope_stack.pop()
                return

            # 3. Function / Method definition
            elif node.type == "function_definition":
                name_node = next((c for c in node.children if c.type == "identifier"), None)
                sym_name = name_node.text.decode("utf8") if name_node else "anonymous_function"
                parent_sym = scope_stack[-1] if scope_stack else None
                sym_type = "method" if parent_sym else "function"
                snippet = encoded[node.start_byte : node.end_byte].decode("utf8", errors="replace")

                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=sym_name,
                        symbol_type=sym_type,
                        parent_symbol=parent_sym,
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        content=snippet,
                    )
                )

                source_qual = f"{file_path}::{sym_name}"
                if parent_sym:
                    key = (source_qual, f"{file_path}::{parent_sym}", "enclosed_by")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            CodeDependencyEdge(
                                source_symbol=source_qual,
                                target_symbol=f"{file_path}::{parent_sym}",
                                edge_type="enclosed_by",
                                file_path=file_path,
                            )
                        )

                # Scan function body for call invocations
                call_nodes: List[Any] = []

                def find_calls(curr: Any) -> None:
                    if curr.type == "call":
                        call_nodes.append(curr)
                    for ch in curr.children:
                        find_calls(ch)

                find_calls(node)
                for c in call_nodes:
                    callee = extract_callee(c)
                    if callee:
                        ckey = (source_qual, callee, "calls")
                        if ckey not in seen_edges:
                            seen_edges.add(ckey)
                            edges.append(
                                CodeDependencyEdge(
                                    source_symbol=source_qual,
                                    target_symbol=callee,
                                    edge_type="calls",
                                    file_path=file_path,
                                )
                            )

                scope_stack.append(sym_name)
                for child in node.children:
                    traverse(child)
                scope_stack.pop()
                return

            for child in node.children:
                traverse(child)

        traverse(tree.root_node)
        return symbols, edges

    def parse_javascript_typescript(
        self, file_path: str, content: str
    ) -> Tuple[List[CodeSymbol], List[CodeDependencyEdge]]:
        """Parse JavaScript and TypeScript files for classes, functions, interfaces, and imports."""
        symbols: List[CodeSymbol] = []
        edges: List[CodeDependencyEdge] = []
        seen_edges: Set[Tuple[str, str, str]] = set()

        lines = content.splitlines()

        # Extract import statements
        import_pattern = re.compile(
            r"""(?:import\s+(?:\{[^}]*\}|[\w*]+(?:\s*,\s*\{[^}]*\})?)\s+from\s+['"]([^'"]+)['"]|require\(['"]([^'"]+)['"]\))"""
        )
        for line in lines:
            m = import_pattern.search(line)
            if m:
                target_mod = m.group(1) or m.group(2)
                if target_mod:
                    key = (file_path, target_mod, "imports")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            CodeDependencyEdge(
                                source_symbol=file_path,
                                target_symbol=target_mod,
                                edge_type="imports",
                                file_path=file_path,
                            )
                        )

        # Class pattern: class ClassName
        class_pattern = re.compile(r"(?:export\s+)?(?:default\s+)?class\s+([A-Za-z0-9_$]+)")
        # Function pattern: function name(...) or const name = (...) =>
        fn_pattern = re.compile(
            r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)|(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_$]+)\s*(?::\s*[^=>]+)?\s*=>"
        )
        # Class method pattern: [public/private/protected] [static] [async] methodName(...)
        method_pattern = re.compile(
            r"^\s*(?:(?:public|private|protected|static|async|override|readonly|get|set)\s+)*([A-Za-z0-9_$]+)\s*\([^)]*\)"
        )
        # Interface / type pattern
        iface_pattern = re.compile(r"(?:export\s+)?(?:interface|type)\s+([A-Za-z0-9_$]+)")

        current_class: Optional[str] = None
        bracket_depth = 0
        class_bracket_depth = 0

        for idx, line in enumerate(lines, start=1):
            # Track bracket depth
            bracket_depth += line.count("{") - line.count("}")
            if current_class and bracket_depth < class_bracket_depth:
                current_class = None

            # Check class
            m_cls = class_pattern.search(line)
            if m_cls:
                c_name = m_cls.group(1)
                current_class = c_name
                class_bracket_depth = bracket_depth
                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=c_name,
                        symbol_type="class",
                        parent_symbol=None,
                        start_line=idx,
                        end_line=min(idx + 50, len(lines)),
                        content="\n".join(lines[idx - 1 : min(idx + 50, len(lines))]),
                    )
                )
                continue

            # Check interface
            m_iface = iface_pattern.search(line)
            if m_iface:
                i_name = m_iface.group(1)
                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=i_name,
                        symbol_type="interface",
                        parent_symbol=None,
                        start_line=idx,
                        end_line=min(idx + 25, len(lines)),
                        content="\n".join(lines[idx - 1 : min(idx + 25, len(lines))]),
                    )
                )
                continue

            # Check function
            m_fn = fn_pattern.search(line)
            if m_fn:
                f_name = m_fn.group(1) or m_fn.group(2)
                if f_name:
                    sym_type = "method" if current_class else "function"
                    symbols.append(
                        CodeSymbol(
                            file_path=file_path,
                            symbol_name=f_name,
                            symbol_type=sym_type,
                            parent_symbol=current_class,
                            start_line=idx,
                            end_line=min(idx + 35, len(lines)),
                            content="\n".join(lines[idx - 1 : min(idx + 35, len(lines))]),
                        )
                    )
                    if current_class:
                        edges.append(
                            CodeDependencyEdge(
                                source_symbol=f"{file_path}::{f_name}",
                                target_symbol=f"{file_path}::{current_class}",
                                edge_type="enclosed_by",
                                file_path=file_path,
                            )
                        )
                    continue

            # Check class method when inside class body
            if current_class:
                m_meth = method_pattern.search(line)
                if m_meth:
                    meth_name = m_meth.group(1)
                    if meth_name not in {
                        "constructor",
                        "if",
                        "for",
                        "while",
                        "switch",
                        "catch",
                        "function",
                        "return",
                    }:
                        symbols.append(
                            CodeSymbol(
                                file_path=file_path,
                                symbol_name=meth_name,
                                symbol_type="method",
                                parent_symbol=current_class,
                                start_line=idx,
                                end_line=min(idx + 35, len(lines)),
                                content="\n".join(lines[idx - 1 : min(idx + 35, len(lines))]),
                            )
                        )
                        edges.append(
                            CodeDependencyEdge(
                                source_symbol=f"{file_path}::{meth_name}",
                                target_symbol=f"{file_path}::{current_class}",
                                edge_type="enclosed_by",
                                file_path=file_path,
                            )
                        )
                        continue

        return symbols, edges

    def parse_go(
        self, file_path: str, content: str
    ) -> Tuple[List[CodeSymbol], List[CodeDependencyEdge]]:
        """Parse Go source files for functions, methods, structs, interfaces, and package imports."""
        symbols: List[CodeSymbol] = []
        edges: List[CodeDependencyEdge] = []
        lines = content.splitlines()

        # Imports (single line and parenthesized block)
        in_import_block = False
        import_single = re.compile(r'import\s+(?:[A-Za-z0-9_.]+\s+)?["\']([^"\']+)["\']')
        import_block_line = re.compile(r'(?:[A-Za-z0-9_.]+\s+)?["\']([^"\']+)["\']')

        for line in lines:
            s_line = line.strip()
            if s_line.startswith("import ("):
                in_import_block = True
                continue
            if in_import_block:
                if s_line == ")":
                    in_import_block = False
                    continue
                m_blk = import_block_line.search(s_line)
                if m_blk:
                    edges.append(
                        CodeDependencyEdge(
                            source_symbol=file_path,
                            target_symbol=m_blk.group(1),
                            edge_type="imports",
                            file_path=file_path,
                        )
                    )
                continue

            m = import_single.search(line)
            if m:
                edges.append(
                    CodeDependencyEdge(
                        source_symbol=file_path,
                        target_symbol=m.group(1),
                        edge_type="imports",
                        file_path=file_path,
                    )
                )

        # Functions & Methods: func (r *Receiver) Name(...) or func Name(...)
        fn_pattern = re.compile(
            r"func\s+(?:\((?:[A-Za-z0-9_*]+\s+)?\*?([A-Za-z0-9_]+)\)\s+)?([A-Za-z0-9_]+)\s*\("
        )
        type_pattern = re.compile(r"type\s+([A-Za-z0-9_]+)\s+(?:struct|interface)")

        for idx, line in enumerate(lines, start=1):
            m_type = type_pattern.search(line)
            if m_type:
                t_name = m_type.group(1)
                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=t_name,
                        symbol_type="struct",
                        parent_symbol=None,
                        start_line=idx,
                        end_line=min(idx + 30, len(lines)),
                        content="\n".join(lines[idx - 1 : min(idx + 30, len(lines))]),
                    )
                )
                continue

            m_fn = fn_pattern.search(line)
            if m_fn:
                receiver = m_fn.group(1)
                fn_name = m_fn.group(2)
                sym_type = "method" if receiver else "function"
                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=fn_name,
                        symbol_type=sym_type,
                        parent_symbol=receiver,
                        start_line=idx,
                        end_line=min(idx + 35, len(lines)),
                        content="\n".join(lines[idx - 1 : min(idx + 35, len(lines))]),
                    )
                )
                if receiver:
                    edges.append(
                        CodeDependencyEdge(
                            source_symbol=f"{file_path}::{fn_name}",
                            target_symbol=f"{file_path}::{receiver}",
                            edge_type="enclosed_by",
                            file_path=file_path,
                        )
                    )

        return symbols, edges

    def parse_rust(
        self, file_path: str, content: str
    ) -> Tuple[List[CodeSymbol], List[CodeDependencyEdge]]:
        """Parse Rust source files for functions, structs, traits, enums, and use modules."""
        symbols: List[CodeSymbol] = []
        edges: List[CodeDependencyEdge] = []
        lines = content.splitlines()

        use_pattern = re.compile(r"use\s+([A-Za-z0-9_:]+)")
        for line in lines:
            m = use_pattern.search(line)
            if m:
                edges.append(
                    CodeDependencyEdge(
                        source_symbol=file_path,
                        target_symbol=m.group(1),
                        edge_type="imports",
                        file_path=file_path,
                    )
                )

        struct_pattern = re.compile(
            r"(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z0-9_]+)"
        )
        fn_pattern = re.compile(r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)")
        impl_pattern = re.compile(
            r"impl(?:\s+<[^>]+>)?\s+(?:[A-Za-z0-9_]+\s+for\s+)?([A-Za-z0-9_]+)"
        )

        current_impl: Optional[str] = None
        bracket_depth = 0
        impl_bracket_depth = 0

        for idx, line in enumerate(lines, start=1):
            bracket_depth += line.count("{") - line.count("}")
            if current_impl and bracket_depth < impl_bracket_depth:
                current_impl = None

            m_impl = impl_pattern.search(line)
            if m_impl:
                current_impl = m_impl.group(1)
                impl_bracket_depth = bracket_depth

            m_struct = struct_pattern.search(line)
            if m_struct:
                s_name = m_struct.group(1)
                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=s_name,
                        symbol_type="struct",
                        parent_symbol=None,
                        start_line=idx,
                        end_line=min(idx + 30, len(lines)),
                        content="\n".join(lines[idx - 1 : min(idx + 30, len(lines))]),
                    )
                )
                continue

            m_fn = fn_pattern.search(line)
            if m_fn:
                fn_name = m_fn.group(1)
                sym_type = "method" if current_impl else "function"
                symbols.append(
                    CodeSymbol(
                        file_path=file_path,
                        symbol_name=fn_name,
                        symbol_type=sym_type,
                        parent_symbol=current_impl,
                        start_line=idx,
                        end_line=min(idx + 35, len(lines)),
                        content="\n".join(lines[idx - 1 : min(idx + 35, len(lines))]),
                    )
                )
                if current_impl:
                    edges.append(
                        CodeDependencyEdge(
                            source_symbol=f"{file_path}::{fn_name}",
                            target_symbol=f"{file_path}::{current_impl}",
                            edge_type="enclosed_by",
                            file_path=file_path,
                        )
                    )

        return symbols, edges

    def extract_symbols_and_edges(
        self, file_path: str, content: str
    ) -> Tuple[List[CodeSymbol], List[CodeDependencyEdge]]:
        """Extract code symbols and dependency edges from source content based on language.

        Dispatches to Tree-sitter for Python, regex AST extractors for TS/JS, Go, and Rust,
        or semantic line-boundary chunking for generic documentation/scripts.
        """
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".py":
            return self.parse_python(file_path, content)
        elif ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            return self.parse_javascript_typescript(file_path, content)
        elif ext == ".go":
            return self.parse_go(file_path, content)
        elif ext == ".rs":
            return self.parse_rust(file_path, content)
        else:
            stem = Path(file_path).stem
            chunks = semantic_chunk_content(
                content=content,
                file_path=file_path,
                base_symbol_name=stem,
                base_symbol_type="document",
            )
            return chunks, []


# ------------------------------------------------------------------------------
# 4. REPOSITORY INDEXER & PGVECTOR TRANSACTION-SAFE STORAGE
# ------------------------------------------------------------------------------


class RepositoryIndexer:
    """Deep, recursive code intelligence indexer and pgvector persistence engine."""

    def __init__(
        self,
        db_session: AsyncSession,
        config: Optional[CodeIndexerConfig] = None,
    ) -> None:
        """Initialize repository indexer with database session and configuration."""
        self.session = db_session
        self.config = config or CodeIndexerConfig()
        self.ast_extractor = TreeSitterASTExtractor()

    def traverse_directory(self, root_dir: str | Path) -> List[Path]:
        """Recursively scan directory tree and return all valid source files.

        Automatically prunes VCS, package managers, virtual environments, and build folders.
        """
        base_path = Path(root_dir)
        if not base_path.is_dir():
            logger.warning("Target directory does not exist: %s", root_dir)
            return []

        matched_files: List[Path] = []

        for root, dirs, files in os.walk(base_path):
            # Prune ignored and hidden directories in-place
            dirs[:] = [
                d
                for d in dirs
                if d not in self.config.ignored_directories and not d.startswith(".")
            ]

            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in self.config.supported_extensions:
                    full_p = Path(root) / file
                    try:
                        # Check file size limit
                        if full_p.stat().st_size <= self.config.max_file_size_bytes:
                            matched_files.append(full_p)
                        else:
                            logger.debug("Skipping file exceeding size limit: %s", full_p)
                    except OSError:
                        pass

        matched_files.sort()
        logger.info(
            "Recursive traversal discovered %d source files in '%s'",
            len(matched_files),
            root_dir,
        )
        return matched_files

    def parse_file(
        self, file_path: str, content: str
    ) -> Tuple[List[CodeSymbol], List[CodeDependencyEdge]]:
        """Parse source content into multiple granular symbols and dependency edges.

        If a file yields no distinct AST symbols or is large, semantic text chunking with
        overlap is automatically applied to guarantee deep, multi-chunk searchability.
        """
        ext = os.path.splitext(file_path)[1].lower()
        symbols: List[CodeSymbol] = []
        edges: List[CodeDependencyEdge] = []

        try:
            if ext == ".py":
                symbols, edges = self.ast_extractor.parse_python(file_path, content)
            elif ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                symbols, edges = self.ast_extractor.parse_javascript_typescript(file_path, content)
            elif ext == ".go":
                symbols, edges = self.ast_extractor.parse_go(file_path, content)
            elif ext == ".rs":
                symbols, edges = self.ast_extractor.parse_rust(file_path, content)
        except Exception as parse_err:
            logger.warning("AST parsing failed on '%s': %s", file_path, parse_err)

        # If zero AST symbols were found (e.g. documentation, scripts, JSON, SQL, or flat modules):
        # Apply semantic chunking with overlap across the entire file
        if not symbols:
            symbols = semantic_chunk_content(
                content=content,
                file_path=file_path,
                max_chunk_chars=self.config.max_chunk_chars,
                overlap_chars=self.config.chunk_overlap_chars,
                base_symbol_name=Path(file_path).stem,
                base_symbol_type="file",
            )
        else:
            # If any individual extracted symbol is massive (> 2500 chars), chunk it with overlap
            refined_symbols: List[CodeSymbol] = []
            for sym in symbols:
                if len(sym.content) > self.config.max_chunk_chars * 2:
                    sub_chunks = semantic_chunk_content(
                        content=sym.content,
                        file_path=file_path,
                        max_chunk_chars=self.config.max_chunk_chars,
                        overlap_chars=self.config.chunk_overlap_chars,
                        base_symbol_name=sym.symbol_name,
                        base_symbol_type=sym.symbol_type,
                        base_parent_symbol=sym.parent_symbol,
                        start_line_offset=sym.start_line,
                    )
                    refined_symbols.extend(sub_chunks if sub_chunks else [sym])
                else:
                    refined_symbols.append(sym)
            symbols = refined_symbols

        return symbols, edges

    async def _generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate 1536-dimensional vector embeddings in concurrent batches with zero-vector fallback."""
        if not texts:
            return []

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.debug("OPENAI_API_KEY unset. Emitting zero-vector embeddings.")
            return [[0.0] * 1536 for _ in texts]

        embeddings: List[List[float]] = []
        client = AsyncOpenAI(api_key=api_key)
        batch_size = self.config.embedding_batch_size
        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
        sem = asyncio.Semaphore(5)

        async def _embed_batch(batch: List[str]) -> List[List[float]]:
            clean_batch = [t[:8000] for t in batch]
            async with sem:
                try:
                    res = await client.embeddings.create(
                        model="text-embedding-3-small",
                        input=clean_batch,
                        timeout=20.0,
                    )
                    return [item.embedding for item in res.data]
                except Exception as exc:
                    logger.warning("Embedding API request failed: %s. Using zero-vectors.", exc)
                    return [[0.0] * 1536 for _ in batch]

        batch_results = await asyncio.gather(*(_embed_batch(b) for b in batches))
        for res in batch_results:
            embeddings.extend(res)

        return embeddings

    async def index_directory(
        self,
        root_dir: str | Path,
        repo_id: uuid.UUID | str,
        commit_sha: str,
        auto_commit: bool = True,
    ) -> Tuple[int, int]:
        """Deeply index all code symbols and edges in root_dir into PostgreSQL/pgvector.

        Ensures full transaction safety: writes are atomic and rolled back if insertion panics.

        Args:
            root_dir: Absolute path of repository or directory to index.
            repo_id: Repository UUID.
            commit_sha: Commit SHA string.
            auto_commit: Whether to commit the transaction immediately (default: True).

        Returns:
            Tuple of (total_symbols_indexed, total_edges_indexed).
        """
        base_dir = Path(root_dir)
        files = self.traverse_directory(base_dir)

        parsed_symbols: List[CodeSymbol] = []
        parsed_edges: List[CodeDependencyEdge] = []

        parsed_repo_id = uuid.UUID(str(repo_id))

        # 1. Parse all discovered files
        for fpath in files:
            rel_path = str(fpath.relative_to(base_dir)).replace("\\", "/")
            try:
                raw_text = fpath.read_text(encoding="utf-8", errors="replace")
                sanitized_text = PromptSanitizer.sanitize(raw_text)

                file_symbols, file_edges = self.parse_file(rel_path, sanitized_text)
                parsed_symbols.extend(file_symbols)
                parsed_edges.extend(file_edges)
            except Exception as read_err:
                logger.warning("Failed to read and parse '%s': %s", rel_path, read_err)

        logger.info(
            "Extracted %d granular symbols and %d dependency edges for repo %s (commit: %s)",
            len(parsed_symbols),
            len(parsed_edges),
            repo_id,
            commit_sha,
        )

        if not parsed_symbols:
            logger.warning("No symbols extracted from repository directory: %s", root_dir)
            if auto_commit:
                await self.session.commit()
            return 0, 0

        # 2. Compute vector embeddings in batches
        chunk_texts = [s.content for s in parsed_symbols]
        embeddings = await self._generate_embeddings(chunk_texts)

        # 3. Assemble CodeChunk model entities
        chunk_entities: List[CodeChunk] = []
        for sym, emb in zip(parsed_symbols, embeddings):
            chunk_record = CodeChunk(
                id=uuid.uuid4(),
                repo_id=parsed_repo_id,
                commit_sha=commit_sha,
                file_path=sym.file_path,
                symbol_name=sym.symbol_name,
                symbol_type=sym.symbol_type,
                parent_symbol=sym.parent_symbol,
                content=sym.content,
                embedding=emb,
                tsv_content=func.to_tsvector("english", sym.content),
            )
            chunk_entities.append(chunk_record)

        # 4. Assemble CodeEdge model entities
        edge_entities: List[CodeEdge] = []
        for edge in parsed_edges:
            edge_record = CodeEdge(
                id=uuid.uuid4(),
                repo_id=parsed_repo_id,
                commit_sha=commit_sha,
                source_symbol=edge.source_symbol,
                target_symbol=edge.target_symbol,
                edge_type=edge.edge_type,
            )
            edge_entities.append(edge_record)

        # 5. Transaction-safe batch database insertion
        try:
            # Batch insert code chunks
            for i in range(0, len(chunk_entities), self.config.db_batch_size):
                batch = chunk_entities[i : i + self.config.db_batch_size]
                self.session.add_all(batch)

            # Batch insert code edges
            for i in range(0, len(edge_entities), self.config.db_batch_size):
                ebatch = edge_entities[i : i + self.config.db_batch_size]
                self.session.add_all(ebatch)

            if auto_commit:
                await self.session.commit()
                logger.info(
                    "Transaction committed: successfully stored %d pgvector chunks and %d edges",
                    len(chunk_entities),
                    len(edge_entities),
                )
            else:
                await self.session.flush()
                logger.info(
                    "Flushed %d pgvector chunks and %d edges to active session",
                    len(chunk_entities),
                    len(edge_entities),
                )
            return len(chunk_entities), len(edge_entities)

        except Exception as db_err:
            logger.exception("Database transaction failed during code indexing: %s", db_err)
            if auto_commit:
                await self.session.rollback()
            raise db_err


__all__ = [
    "CodeDependencyEdge",
    "CodeIndexerConfig",
    "CodeSymbol",
    "RepositoryIndexer",
    "TreeSitterASTExtractor",
    "semantic_chunk_content",
]
