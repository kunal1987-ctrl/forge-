from __future__ import annotations

import logging
from typing import Any, Optional
import tree_sitter
import tree_sitter_python as tspython

logger = logging.getLogger(__name__)

# Maximum file size threshold for AST parsing (500 KB)
MAX_FILE_SIZE_BYTES: int = 500 * 1024


class CodeParser:
    """AST-based code parser using Tree-Sitter for code intelligence and Graph-RAG extraction.

    Hardened against resource exhaustion from minified or giant generated files
    with strict byte size thresholds.
    """

    def __init__(self) -> None:
        """Initialize the Tree-Sitter parser with Python grammar."""
        self.language = tree_sitter.Language(tspython.language())
        self.parser = tree_sitter.Parser(self.language)

    def parse_file(self, file_path: str, content: str) -> tuple[list[dict], list[dict]]:
        """Parse source content into an AST and extract symbol definitions and graph dependency edges.

        Applies a strict 500KB size limit to prevent Tree-sitter memory exhaustion.

        Tracks AST scope hierarchy:
        - If a function is enclosed in a class, sets parent_symbol = class_name and symbol_type = 'method'.
        - If top-level, sets parent_symbol = None and symbol_type = 'function'.
        - Extracts call expressions inside function bodies.

        Args:
            file_path: Relative or absolute path of the source file.
            content: Raw string content of the source code.

        Returns:
            A tuple of (chunks, edges) where chunks contains symbol metadata and
            edges contains 'calls', 'imports', and 'enclosed_by' graph relationships.
        """
        if not content:
            return [], []

        encoded_content = content.encode("utf8")
        content_size = len(encoded_content)

        # Enforce strict 500KB size limit
        if content_size > MAX_FILE_SIZE_BYTES:
            logger.warning(
                "File '%s' exceeds 500KB size limit (%d bytes > %d bytes). "
                "Skipping AST parsing to prevent resource exhaustion.",
                file_path,
                content_size,
                MAX_FILE_SIZE_BYTES,
            )
            return [], []

        tree = self.parser.parse(encoded_content)
        chunks: list[dict] = []
        edges: list[dict] = []
        seen_edges: set[tuple[str, str, str]] = set()
        scope_stack: list[str] = []

        def extract_callee_name(call_node: Any) -> Optional[str]:
            """Extract function or method identifier from a call node."""
            func_node = call_node.child_by_field_name("function") or (
                call_node.children[0] if call_node.children else None
            )
            if not func_node:
                return None

            target = func_node
            while target.type == "parenthesized_expression" and target.named_child_count > 0:
                target = target.named_children[0]

            if target.type == "identifier":
                return encoded_content[target.start_byte : target.end_byte].decode("utf8", errors="replace")
            elif target.type == "attribute":
                attr_child = target.child_by_field_name("attribute")
                if attr_child and attr_child.type == "identifier":
                    return encoded_content[attr_child.start_byte : attr_child.end_byte].decode("utf8", errors="replace")
                for ch in reversed(target.children):
                    if ch.type == "identifier":
                        return encoded_content[ch.start_byte : ch.end_byte].decode("utf8", errors="replace")
            return None

        def traverse(node: Any) -> None:
            # 1. Import statements
            if node.type == "import_statement":
                for child in node.children:
                    if child.type == "dotted_name":
                        mod_name = child.text.decode("utf8")
                        key = (file_path, mod_name, "imports")
                        if key not in seen_edges:
                            seen_edges.add(key)
                            edges.append(
                                {
                                    "source_symbol": file_path,
                                    "target_symbol": mod_name,
                                    "edge_type": "imports",
                                    "file_path": file_path,
                                }
                            )
                    elif child.type == "aliased_import":
                        orig = child.children[0].text.decode("utf8")
                        key = (file_path, orig, "imports")
                        if key not in seen_edges:
                            seen_edges.add(key)
                            edges.append(
                                {
                                    "source_symbol": file_path,
                                    "target_symbol": orig,
                                    "edge_type": "imports",
                                    "file_path": file_path,
                                }
                            )

            elif node.type == "import_from_statement":
                # First dotted_name is the module
                mod_node = next((c for c in node.children if c.type == "dotted_name"), None)
                if mod_node:
                    mod_name = mod_node.text.decode("utf8")
                    key = (file_path, mod_name, "imports")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            {
                                "source_symbol": file_path,
                                "target_symbol": mod_name,
                                "edge_type": "imports",
                                "file_path": file_path,
                            }
                        )

                # Symbols imported from module
                seen_import_keyword = False
                for child in node.children:
                    if child.type == "import":
                        seen_import_keyword = True
                        continue
                    if seen_import_keyword:
                        if child.type == "dotted_name":
                            sym_name = child.text.decode("utf8")
                            key = (file_path, sym_name, "imports")
                            if key not in seen_edges:
                                seen_edges.add(key)
                                edges.append(
                                    {
                                        "source_symbol": file_path,
                                        "target_symbol": sym_name,
                                        "edge_type": "imports",
                                        "file_path": file_path,
                                    }
                                )
                        elif child.type == "aliased_import":
                            orig = child.children[0].text.decode("utf8")
                            key = (file_path, orig, "imports")
                            if key not in seen_edges:
                                seen_edges.add(key)
                                edges.append(
                                    {
                                        "source_symbol": file_path,
                                        "target_symbol": orig,
                                        "edge_type": "imports",
                                        "file_path": file_path,
                                    }
                                )

            # 2. Class definition
            elif node.type == "class_definition":
                chunk_content = encoded_content[node.start_byte : node.end_byte].decode(
                    "utf8", errors="replace"
                )
                name_node = next(
                    (child for child in node.children if child.type == "identifier"), None
                )
                symbol_name = name_node.text.decode("utf8") if name_node else "unknown"
                parent_sym = scope_stack[-1] if scope_stack else None

                chunks.append(
                    {
                        "file_path": file_path,
                        "symbol_name": symbol_name,
                        "symbol_type": "class",
                        "parent_symbol": parent_sym,
                        "start_line": node.start_point.row,
                        "end_line": node.end_point.row,
                        "content": chunk_content,
                    }
                )

                if parent_sym:
                    key = (
                        f"{file_path}::{symbol_name}",
                        f"{file_path}::{parent_sym}",
                        "enclosed_by",
                    )
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            {
                                "source_symbol": f"{file_path}::{symbol_name}",
                                "target_symbol": f"{file_path}::{parent_sym}",
                                "edge_type": "enclosed_by",
                                "file_path": file_path,
                            }
                        )

                scope_stack.append(symbol_name)
                for child in node.children:
                    traverse(child)
                scope_stack.pop()
                return

            # 3. Function/method definition
            elif node.type == "function_definition":
                chunk_content = encoded_content[node.start_byte : node.end_byte].decode(
                    "utf8", errors="replace"
                )
                name_node = next(
                    (child for child in node.children if child.type == "identifier"), None
                )
                symbol_name = name_node.text.decode("utf8") if name_node else "unknown"
                parent_sym = scope_stack[-1] if scope_stack else None
                symbol_type = "method" if parent_sym else "function"

                chunks.append(
                    {
                        "file_path": file_path,
                        "symbol_name": symbol_name,
                        "symbol_type": symbol_type,
                        "parent_symbol": parent_sym,
                        "start_line": node.start_point.row,
                        "end_line": node.end_point.row,
                        "content": chunk_content,
                    }
                )

                source_qual_sym = f"{file_path}::{symbol_name}"

                if parent_sym:
                    key = (source_qual_sym, f"{file_path}::{parent_sym}", "enclosed_by")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            {
                                "source_symbol": source_qual_sym,
                                "target_symbol": f"{file_path}::{parent_sym}",
                                "edge_type": "enclosed_by",
                                "file_path": file_path,
                            }
                        )

                # Traverse function body for calls
                def traverse_func_body(body_node: Any) -> None:
                    if body_node.type in ("call", "call_expression"):
                        callee = extract_callee_name(body_node)
                        if callee:
                            key = (source_qual_sym, callee, "calls")
                            if key not in seen_edges:
                                seen_edges.add(key)
                                edges.append(
                                    {
                                        "source_symbol": source_qual_sym,
                                        "target_symbol": callee,
                                        "edge_type": "calls",
                                        "file_path": file_path,
                                    }
                                )

                    for c in body_node.children:
                        traverse_func_body(c)

                for child in node.children:
                    traverse_func_body(child)
                return

            for child in node.children:
                traverse(child)

        traverse(tree.root_node)
        return chunks, edges

    def extract_edges(
        self,
        file_path: Optional[str] = None,
        content: Optional[str] = None,
        source_symbol: Optional[str] = None,
    ) -> list[dict]:
        """Extract graph dependency edges (calls, imports, enclosed_by) from Python source content.

        Args:
            file_path: Path of the source file.
            content: Raw content of the source file.
            source_symbol: Alias for file_path for backwards compatibility.

        Returns:
            List of edge dictionaries.
        """
        if content is None and source_symbol is not None:
            actual_content = file_path or ""
            actual_file_path = source_symbol
        else:
            actual_file_path = file_path or source_symbol or ""
            actual_content = content or ""

        if not actual_content:
            return []

        _, edges = self.parse_file(actual_file_path, actual_content)
        return edges
