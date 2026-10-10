"""AST-based code parser using Tree-Sitter for code intelligence.

This module provides the CodeParser class which uses tree-sitter and tree-sitter-python
to parse Python source code into syntax trees, extract structured symbol definitions
(functions and classes), and discover module dependency edges (imports).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from tree_sitter import Language, Node, Parser
import tree_sitter_python as tspython

logger = logging.getLogger(__name__)


class CodeParser:
    """Parses Python source code using Tree-Sitter AST grammar.

    Extracts functions, classes, line boundaries, raw code blocks, and import edges.
    """

    def __init__(self) -> None:
        """Initialize the Tree-Sitter parser with the Python language grammar."""
        self.language = Language(tspython.language())
        self.parser = Parser(self.language)

    def parse_file(self, file_path: str, content: str) -> List[Dict[str, Any]]:
        """Parse source content into an AST and extract symbol definitions.

        Traverses the AST to identify 'function_definition' and 'class_definition'
        nodes, capturing the symbol name, type, 1-indexed line spans, and the full
        code block content (including docstrings and decorators).

        Args:
            file_path: Relative or absolute path of the source file being parsed.
            content: Raw string content of the source code.

        Returns:
            A list of dictionaries, each containing:
                - 'symbol_name': str, identifier name of the function or class.
                - 'symbol_type': str, 'function' or 'class'.
                - 'start_line': int, 1-indexed start line of the symbol block.
                - 'end_line': int, 1-indexed end line of the symbol block.
                - 'content': str, raw source code of the symbol block.
        """
        if not content:
            return []

        content_bytes = content.encode("utf-8")
        tree = self.parser.parse(content_bytes)

        symbols: List[Dict[str, Any]] = []

        def _traverse(node: Node) -> None:
            if node.type in ("function_definition", "class_definition"):
                name_node = node.child_by_field_name("name")
                symbol_name = (
                    name_node.text.decode("utf-8") if name_node else "anonymous"
                )
                symbol_type = (
                    "class" if node.type == "class_definition" else "function"
                )

                # If wrapped in decorators, include them in the extracted block
                target_node = (
                    node.parent
                    if node.parent and node.parent.type == "decorated_definition"
                    else node
                )

                # Tree-sitter row indices are 0-indexed; convert to 1-indexed lines
                start_line = target_node.start_point.row + 1
                end_line = target_node.end_point.row + 1
                raw_code = target_node.text.decode("utf-8", errors="replace")

                symbols.append(
                    {
                        "file_path": file_path,
                        "symbol_name": symbol_name,
                        "symbol_type": symbol_type,
                        "start_line": start_line,
                        "end_line": end_line,
                        "content": raw_code,
                    }
                )

            # Continue traversing children (e.g. methods inside classes, nested functions)
            for child in node.children:
                _traverse(child)

        _traverse(tree.root_node)
        return symbols

    def extract_edges(
        self,
        content: str,
        source_symbol: str = "current_file",
    ) -> List[Dict[str, str]]:
        """Extract import dependency edges from Python source content.

        Finds 'import_statement' and 'import_from_statement' nodes in the AST and
        maps the primary module/file to the target modules it imports.

        Args:
            content: Raw string content of the source code.
            source_symbol: Identifier for the source file or module. Defaults to 'current_file'.

        Returns:
            A list of edge dictionaries, each containing:
                - 'source_symbol': str, identifier of the importing file/module.
                - 'target_symbol': str, imported module identifier.
                - 'edge_type': str, 'imports'.
        """
        if not content:
            return []

        content_bytes = content.encode("utf-8")
        tree = self.parser.parse(content_bytes)

        edges: List[Dict[str, str]] = []
        seen: Set[str] = set()

        def _record_edge(target: str) -> None:
            clean_target = target.strip()
            if clean_target and clean_target not in seen:
                seen.add(clean_target)
                edges.append(
                    {
                        "source_symbol": source_symbol,
                        "target_symbol": clean_target,
                        "edge_type": "imports",
                    }
                )

        def _find_imports(node: Node) -> None:
            if node.type == "import_statement":
                # Matches: import foo, bar as b
                for child in node.children:
                    if child.type == "dotted_name":
                        _record_edge(child.text.decode("utf-8"))
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            _record_edge(name_node.text.decode("utf-8"))
                        else:
                            _record_edge(child.text.decode("utf-8"))

            elif node.type == "import_from_statement":
                # Matches: from foo.bar import baz, from .pkg import mod
                module_node = node.child_by_field_name("module_name")
                if module_node:
                    module_name = module_node.text.decode("utf-8")
                    _record_edge(module_name)
                else:
                    # Fallback check for relative_import child nodes
                    for child in node.children:
                        if child.type in ("relative_import", "dotted_name"):
                            _record_edge(child.text.decode("utf-8"))
                            break

            for child in node.children:
                _find_imports(child)

        _find_imports(tree.root_node)
        return edges
