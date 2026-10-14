"""Unit and integration tests for Tree-sitter AST repository indexing and chunking engine."""

from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from services.api.forge.rag.indexer import (
    CodeIndexerConfig,
    RepositoryIndexer,
    TreeSitterASTExtractor,
    semantic_chunk_content,
)


# ==============================================================================
# 1. CONFIGURATION TESTS
# ==============================================================================


def test_code_indexer_config_defaults():
    """Verify default supported extensions and ignored directories in configuration."""
    config = CodeIndexerConfig()

    # Multi-language extensions
    assert ".py" in config.supported_extensions
    assert ".ts" in config.supported_extensions
    assert ".js" in config.supported_extensions
    assert ".rs" in config.supported_extensions
    assert ".go" in config.supported_extensions

    # Ignored directories
    assert ".git" in config.ignored_directories
    assert "node_modules" in config.ignored_directories
    assert "venv" in config.ignored_directories
    assert ".venv" in config.ignored_directories
    assert "dist" in config.ignored_directories
    assert "build" in config.ignored_directories
    assert "__pycache__" in config.ignored_directories

    # Chunking limits
    assert config.chunk_size == 1200
    assert config.chunk_overlap == 200
    assert config.max_file_size_bytes == 1_048_576


# ==============================================================================
# 2. SEMANTIC CHUNKING TESTS
# ==============================================================================


def test_semantic_chunk_content_small():
    """Verify small content under chunk_size returns a single chunk with accurate line coordinates."""
    code = "def hello():\n    return 'world'\n"
    chunks = semantic_chunk_content(
        content=code,
        file_path="src/hello.py",
        symbol_name="hello",
        symbol_type="function",
        chunk_size=500,
        chunk_overlap=50,
        start_line_offset=1,
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.file_path == "src/hello.py"
    assert chunk.symbol_name == "hello"
    assert chunk.symbol_type == "function"
    assert chunk.start_line == 1
    assert chunk.end_line == 2
    assert "return 'world'" in chunk.content


def test_semantic_chunk_content_overlapping_large():
    """Verify large content creates multiple overlapping chunks with valid start/end lines."""
    lines = [f"line_{i} = {i * 10}" for i in range(1, 101)]
    code = "\n".join(lines)

    chunks = semantic_chunk_content(
        content=code,
        file_path="large_module.py",
        symbol_name="large_module",
        symbol_type="module",
        chunk_size=300,
        chunk_overlap=60,
        start_line_offset=1,
    )

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.start_line <= chunk.end_line
        assert chunk.start_line >= 1
        assert chunk.end_line <= len(lines)
        assert len(chunk.content) > 0


# ==============================================================================
# 3. TREE-SITTER PYTHON AST PARSER TESTS
# ==============================================================================


def test_python_ast_extractor_classes_and_methods():
    """Verify Python AST extractor detects classes, methods, docstrings, and hierarchy."""
    extractor = TreeSitterASTExtractor()
    code = '''
"""Module docstring."""
import os
from typing import List

class DataProcessor:
    """Class docstring."""
    def __init__(self, name: str):
        self.name = name

    def process(self, items: List[int]) -> int:
        return sum(items)

def standalone_helper(x: int) -> int:
    return x * 2
'''
    symbols, edges = extractor.extract_symbols_and_edges("services/processor.py", code)

    symbol_names = [s.symbol_name for s in symbols]
    assert "DataProcessor" in symbol_names
    assert "__init__" in symbol_names
    assert "process" in symbol_names
    assert "standalone_helper" in symbol_names

    # Check parent symbol attribution
    init_sym = next(s for s in symbols if s.symbol_name == "__init__")
    assert init_sym.parent_symbol == "DataProcessor"
    assert init_sym.symbol_type == "method"

    helper_sym = next(s for s in symbols if s.symbol_name == "standalone_helper")
    assert helper_sym.parent_symbol is None
    assert helper_sym.symbol_type == "function"

    # Check edges
    edge_types = [e.edge_type for e in edges]
    assert "enclosed_by" in edge_types
    assert "imports" in edge_types

    import_targets = [e.target_symbol for e in edges if e.edge_type == "imports"]
    assert "os" in import_targets
    assert "typing.List" in import_targets


def test_python_ast_extractor_function_calls():
    """Verify function calls create 'calls' dependency edges."""
    extractor = TreeSitterASTExtractor()
    code = """
def compute():
    result = math_engine.calculate(10)
    print(result)
    return result
"""
    symbols, edges = extractor.extract_symbols_and_edges("math.py", code)
    call_edges = [e for e in edges if e.edge_type == "calls"]
    targets = [e.target_symbol for e in call_edges]

    assert any("calculate" in t for t in targets)
    assert any("print" in t for t in targets)


# ==============================================================================
# 4. MULTI-LANGUAGE AST EXTRACTION TESTS (TS/JS, GO, RUST)
# ==============================================================================


def test_typescript_ast_extractor():
    """Verify TypeScript / JavaScript parser extracts classes, interfaces, and methods."""
    extractor = TreeSitterASTExtractor()
    ts_code = """
import { Injectable } from '@nestjs/common';
import axios from 'axios';

export interface UserResponse {
    id: string;
    email: string;
}

export class AuthService {
    private token: string;

    constructor() {
        this.token = '';
    }

    async authenticate(credentials: any): Promise<boolean> {
        return true;
    }
}

export function validateToken(token: string): boolean {
    return token.length > 0;
}
"""
    symbols, edges = extractor.extract_symbols_and_edges("src/auth.ts", ts_code)

    names = [s.symbol_name for s in symbols]
    assert "AuthService" in names
    assert "UserResponse" in names
    assert "authenticate" in names
    assert "validateToken" in names

    # Method should have parent AuthService
    auth_method = next(s for s in symbols if s.symbol_name == "authenticate")
    assert auth_method.parent_symbol == "AuthService"
    assert auth_method.symbol_type == "method"

    # Import edges
    import_targets = [e.target_symbol for e in edges if e.edge_type == "imports"]
    assert "@nestjs/common" in import_targets or "axios" in import_targets


def test_go_ast_extractor():
    """Verify Go source parser extracts structs, interfaces, methods, and functions."""
    extractor = TreeSitterASTExtractor()
    go_code = """package main

import (
    "fmt"
    "net/http"
)

type ServerConfig struct {
    Port int
    Host string
}

type Handler interface {
    ServeHTTP(w http.ResponseWriter, r *http.Request)
}

func (s *ServerConfig) ListenAndServe() error {
    fmt.Println("Starting server...")
    return nil
}

func NewConfig(port int) *ServerConfig {
    return &ServerConfig{Port: port, Host: "0.0.0.0"}
}
"""
    symbols, edges = extractor.extract_symbols_and_edges("main.go", go_code)

    names = [s.symbol_name for s in symbols]
    assert "ServerConfig" in names
    assert "Handler" in names
    assert "ListenAndServe" in names
    assert "NewConfig" in names

    # Method check
    method_sym = next(s for s in symbols if s.symbol_name == "ListenAndServe")
    assert method_sym.symbol_type == "method"
    assert method_sym.parent_symbol == "ServerConfig"

    # Edge check
    import_targets = [e.target_symbol for e in edges if e.edge_type == "imports"]
    assert "fmt" in import_targets or "net/http" in import_targets


def test_rust_ast_extractor():
    """Verify Rust source parser extracts structs, enums, traits, and impl methods."""
    extractor = TreeSitterASTExtractor()
    rust_code = """
use std::sync::Arc;
use tokio::sync::Mutex;

pub struct TaskQueue {
    tasks: Vec<String>,
}

pub enum Priority {
    High,
    Low,
}

pub trait Runner {
    fn run(&self);
}

impl TaskQueue {
    pub fn new() -> Self {
        TaskQueue { tasks: Vec::new() }
    }

    pub async fn push(&mut self, item: String) {
        self.tasks.push(item);
    }
}

pub fn spawn_worker() {
    let queue = TaskQueue::new();
}
"""
    symbols, edges = extractor.extract_symbols_and_edges("src/queue.rs", rust_code)

    names = [s.symbol_name for s in symbols]
    assert "TaskQueue" in names
    assert "Priority" in names
    assert "Runner" in names
    assert "new" in names
    assert "push" in names
    assert "spawn_worker" in names

    # Edge check
    import_targets = [e.target_symbol for e in edges if e.edge_type == "imports"]
    assert any("tokio" in t or "std" in t for t in import_targets)


def test_generic_fallback_chunker():
    """Verify non-AST files (e.g., Markdown, JSON, SQL) are gracefully chunked."""
    extractor = TreeSitterASTExtractor()
    markdown = "# Project Readme\n\nWelcome to FORGE.\n\n## Installation\nRun pip install."
    symbols, edges = extractor.extract_symbols_and_edges("README.md", markdown)

    assert len(symbols) >= 1
    assert symbols[0].symbol_name == "README"
    assert symbols[0].symbol_type == "document"
    assert edges == []


# ==============================================================================
# 5. REPOSITORY TRAVERSAL & FILTERING TESTS
# ==============================================================================


def test_traverse_directory_filters_and_walks():
    """Verify recursive directory traversal filters ignored directories and oversized files."""
    mock_session = AsyncMock()
    indexer = RepositoryIndexer(mock_session)

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        # Valid source files
        (root / "app.py").write_text("print('app')", encoding="utf-8")
        src_dir = root / "src"
        src_dir.mkdir()
        (src_dir / "index.ts").write_text("console.log('ts')", encoding="utf-8")
        (src_dir / "lib.rs").write_text("pub fn test() {}", encoding="utf-8")
        (src_dir / "main.go").write_text("package main", encoding="utf-8")

        # Ignored directory files
        git_dir = root / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("dummy git config", encoding="utf-8")

        node_dir = root / "node_modules" / "pkg"
        node_dir.mkdir(parents=True)
        (node_dir / "index.js").write_text("module.exports = {}", encoding="utf-8")

        venv_dir = root / "venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "site.py").write_text("pass", encoding="utf-8")

        # Ignored extension file
        (root / "binary.bin").write_bytes(b"\x00\x01\x02\x03")

        # Oversized file
        oversized = root / "giant.py"
        oversized.write_text("x = 1\n" * 300_000, encoding="utf-8")  # ~1.8MB > 1MB limit

        discovered = indexer.traverse_directory(root)
        discovered_names = [f.name for f in discovered]

        # Valid files discovered
        assert "app.py" in discovered_names
        assert "index.ts" in discovered_names
        assert "lib.rs" in discovered_names
        assert "main.go" in discovered_names

        # Ignored files excluded
        assert "config" not in discovered_names
        assert "site.py" not in discovered_names
        assert "binary.bin" not in discovered_names
        assert "giant.py" not in discovered_names

        # Exactly 4 valid files
        assert len(discovered) == 4


# ==============================================================================
# 6. TRANSACTION-SAFE PGVECTOR INDEXING TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_index_directory_transaction_success():
    """Verify directory indexing commits chunks and edges into AsyncSession."""
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.add_all = MagicMock()

    indexer = RepositoryIndexer(mock_session)

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "calc.py").write_text(
            "import math\n\ndef add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
            encoding="utf-8",
        )

        repo_id = uuid.uuid4()
        commit_sha = "abc12345"

        num_chunks, num_edges = await indexer.index_directory(
            root_dir=root,
            repo_id=repo_id,
            commit_sha=commit_sha,
            auto_commit=True,
        )

        assert num_chunks >= 2  # add, sub functions
        assert num_edges >= 1   # import math edge
        assert mock_session.add_all.called
        assert mock_session.commit.called
        assert not mock_session.rollback.called


@pytest.mark.asyncio
async def test_index_directory_transaction_rollback_on_error():
    """Verify database errors trigger transaction rollback and re-raise exception."""
    mock_session = AsyncMock()
    mock_session.add_all = MagicMock()
    mock_session.commit = AsyncMock(side_effect=RuntimeError("pgvector connection aborted"))
    mock_session.rollback = AsyncMock()

    indexer = RepositoryIndexer(mock_session)

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "main.py").write_text("def run(): pass\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="pgvector connection aborted"):
            await indexer.index_directory(
                root_dir=root,
                repo_id=uuid.uuid4(),
                commit_sha="fail_commit",
                auto_commit=True,
            )

        assert mock_session.rollback.called
