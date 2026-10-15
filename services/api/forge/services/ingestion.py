"""Repository Ingestion Service for FORGE.

Provides enterprise-grade Git clone operations, automated branch fallback probing
('main' -> 'master' -> default remote HEAD), strict subprocess timeout enforcement,
sensitive path sanitization in error reporting, and leak-proof ephemeral worktree cleanup.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from subprocess import CalledProcessError, TimeoutExpired
import sys
import tempfile
import time
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from fastapi import HTTPException
from openai import AsyncOpenAI
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
import tree_sitter

from services.api.forge.exceptions import ForgeBaseException
from services.api.forge.retrieval.parser import CodeParser
from services.api.forge.security.sanitizer import PromptSanitizer
from services.api.models.code import CodeChunk, CodeEdge
from services.api.models.repository import Repository, RepositoryRevision

logger = logging.getLogger("forge.services.ingestion")

MAX_GIT_CLONE_TIMEOUT_SECONDS = 60
MAX_AST_FILE_SIZE_BYTES = 500 * 1024
IGNORED_SOURCE_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
    }
)
SOURCE_GRAMMARS: Dict[str, Tuple[str, str]] = {
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"),
    ".cjs": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
}
SOURCE_EXTENSIONS = frozenset({".py", *SOURCE_GRAMMARS})


def _node_text(node: tree_sitter.Node, source: bytes) -> str:
    """Return a decoded node slice without assuming source files are valid UTF-8."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _tree_sitter_parser(extension: str) -> tree_sitter.Parser:
    """Create a parser for a supported non-Python source extension."""
    grammar_spec = SOURCE_GRAMMARS.get(extension)
    if grammar_spec is None:
        raise ValueError(f"No Tree-sitter grammar is configured for {extension!r}")

    module_name, factory_name = grammar_spec
    try:
        grammar_module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Tree-sitter grammar dependency {module_name!r} is required to parse "
            f"{extension} source files"
        ) from exc

    grammar_factory: Callable[[], Any] = getattr(grammar_module, factory_name)
    return tree_sitter.Parser(tree_sitter.Language(grammar_factory()))


def _parse_tree_sitter_file(
    file_path: str,
    content: str,
    extension: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Extract symbols and import edges from a supported Tree-sitter language."""
    source = content.encode("utf-8")
    if not source or len(source) > MAX_AST_FILE_SIZE_BYTES:
        return [], []

    parser = _tree_sitter_parser(extension)
    tree = parser.parse(source)
    chunks: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []
    seen_imports: set[str] = set()
    class_nodes = {
        ".js": {"class_declaration"},
        ".jsx": {"class_declaration"},
        ".mjs": {"class_declaration"},
        ".cjs": {"class_declaration"},
        ".ts": {"class_declaration"},
        ".tsx": {"class_declaration"},
        ".go": {"type_spec"},
        ".rs": {"struct_item", "enum_item", "trait_item"},
    }.get(extension, set())
    function_nodes = {
        ".js": {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
        },
        ".jsx": {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
        },
        ".mjs": {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
        },
        ".cjs": {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
        },
        ".ts": {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
        },
        ".tsx": {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
        },
        ".go": {"function_declaration", "method_declaration"},
        ".rs": {"function_item"},
    }.get(extension, set())
    import_nodes = {
        ".js": {"import_statement", "export_statement"},
        ".jsx": {"import_statement", "export_statement"},
        ".mjs": {"import_statement", "export_statement"},
        ".cjs": {"import_statement", "export_statement"},
        ".ts": {"import_statement", "export_statement"},
        ".tsx": {"import_statement", "export_statement"},
        ".go": {"import_spec"},
        ".rs": {"use_declaration"},
    }.get(extension, set())
    class_scope: List[str] = []

    def node_name(node: tree_sitter.Node) -> Optional[str]:
        name_node = node.child_by_field_name("name")
        if name_node is None and node.type in {"arrow_function", "function_expression"}:
            parent = node.parent
            if parent is not None and parent.type == "variable_declarator":
                name_node = parent.child_by_field_name("name")
            elif parent is not None and parent.type == "assignment_expression":
                name_node = parent.child_by_field_name("left")
        if name_node is None:
            name_node = next(
                (
                    child
                    for child in node.named_children
                    if child.type
                    in {
                        "identifier",
                        "property_identifier",
                        "type_identifier",
                        "field_identifier",
                    }
                ),
                None,
            )
        return _node_text(name_node, source) if name_node is not None else None

    def add_import_edge(node: tree_sitter.Node) -> None:
        target_node = (
            node.child_by_field_name("source")
            or node.child_by_field_name("path")
            or node.child_by_field_name("argument")
        )
        if node.type == "export_statement" and target_node is None:
            return
        target = _node_text(target_node, source).strip("\"'") if target_node else ""
        if not target:
            target = _node_text(node, source)
        if target and target not in seen_imports:
            seen_imports.add(target)
            edges.append(
                {
                    "source_symbol": file_path,
                    "target_symbol": target,
                    "edge_type": "imports",
                    "file_path": file_path,
                }
            )

    def walk(node: tree_sitter.Node) -> None:
        if node.type in import_nodes:
            add_import_edge(node)

        is_go_struct_or_interface = node.type == "type_spec" and any(
            child.type in {"struct_type", "interface_type"} for child in node.named_children
        )
        is_class = node.type in class_nodes and (extension != ".go" or is_go_struct_or_interface)
        is_function = node.type in function_nodes
        is_rust_impl = extension == ".rs" and node.type == "impl_item"

        if is_class or is_function:
            name = node_name(node)
            if name:
                parent = class_scope[-1] if class_scope else None
                symbol_type = (
                    "class"
                    if is_class
                    else ("method" if parent or node.type == "method_declaration" else "function")
                )
                chunks.append(
                    {
                        "file_path": file_path,
                        "symbol_name": name,
                        "symbol_type": symbol_type,
                        "parent_symbol": parent,
                        "start_line": node.start_point.row,
                        "end_line": max(node.start_point.row, node.end_point.row - 1),
                        "content": _node_text(node, source),
                    }
                )
            if (is_class or is_rust_impl) and name:
                class_scope.append(name)
                for child in node.named_children:
                    walk(child)
                class_scope.pop()
                return
        elif is_rust_impl:
            impl_type = next(
                (
                    child
                    for child in node.named_children
                    if child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}
                ),
                None,
            )
            if impl_type is not None:
                class_scope.append(_node_text(impl_type, source))
                for child in node.named_children:
                    walk(child)
                class_scope.pop()
                return

        for child in node.named_children:
            walk(child)

    walk(tree.root_node)
    return chunks, edges


def _parse_source_file(
    parser: CodeParser,
    file_path: str,
    content: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Parse one supported source file using its matching Tree-sitter grammar."""
    extension = Path(file_path).suffix.lower()
    if extension == ".py":
        python_chunks, python_edges = parser.parse_file(file_path, content)
        return (
            [dict(chunk) for chunk in python_chunks],
            [dict(edge) for edge in python_edges],
        )
    return _parse_tree_sitter_file(file_path, content, extension)


def _walk_and_parse_source_files(
    root: str | Path,
    parser: CodeParser,
    timeout_seconds: float = 60.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Recursively parse source files while pruning generated and dependency trees."""
    root_path = Path(root)
    chunks: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []
    started_at = time.monotonic()

    for directory, child_directories, file_names in os.walk(root_path):
        if time.monotonic() - started_at > timeout_seconds:
            raise TimeoutExpired(cmd="file-walking", timeout=timeout_seconds)
        child_directories[:] = [
            name for name in child_directories if name not in IGNORED_SOURCE_DIRECTORIES
        ]
        for file_name in file_names:
            if time.monotonic() - started_at > timeout_seconds:
                raise TimeoutExpired(cmd="file-walking", timeout=timeout_seconds)
            file_path = Path(directory) / file_name
            if file_path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            relative_path = file_path.relative_to(root_path).as_posix()
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                sanitized_content = PromptSanitizer.sanitize(content)
                file_chunks, file_edges = _parse_source_file(
                    parser,
                    relative_path,
                    sanitized_content,
                )
                if file_chunks:
                    chunks.extend(file_chunks)
                else:
                    chunks.append(
                        {
                            "file_path": relative_path,
                            "symbol_name": file_path.stem,
                            "symbol_type": "module",
                            "parent_symbol": None,
                            "start_line": 1,
                            "end_line": max(1, len(sanitized_content.splitlines())),
                            "content": sanitized_content[:8000],
                        }
                    )
                edges.extend(file_edges)
            except (OSError, UnicodeError) as parse_error:
                logger.warning("Unable to index source file '%s': %s", relative_path, parse_error)

    return chunks, edges


# ------------------------------------------------------------------------------
# 1. CUSTOM EXCEPTION & STRUCTURED ERROR TRANSLATION
# ------------------------------------------------------------------------------


class GitIngestionError(ForgeBaseException):
    """Raised when repository cloning, branch resolution, or checkout fails.

    Maps directly to HTTP 400 Bad Request responses.
    """

    default_code: str = "GIT_INGESTION_ERROR"

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> None:
        det = dict(details) if details else {}
        if repo_url is not None:
            det["repo_url"] = repo_url
        if branch is not None:
            det["branch"] = branch
        super().__init__(
            message=message,
            error_code=error_code or self.default_code,
            details=det,
            cause=cause,
        )


def sanitize_git_error(stderr: str, target_dir: Optional[str | Path] = None) -> str:
    """Parse and sanitize Git stderr output by stripping credentials and sensitive local paths.

    Args:
        stderr: Raw stderr output from git subprocess.
        target_dir: Optional local worktree path to scrub from output.

    Returns:
        A sanitized, user-facing error string.
    """
    if not stderr or not stderr.strip():
        return "Git operation failed with non-zero exit code."

    text = stderr.strip()

    # 1. Scrub credentials and tokens embedded in URLs (e.g., https://token@github.com)
    text = re.sub(r"https?://[^@\s/]+@", "https://", text)

    # 2. Scrub specific ephemeral worktree target directory if provided
    if target_dir:
        dir_str = str(target_dir)
        text = text.replace(dir_str, "[ephemeral_worktree]")
        text = text.replace(dir_str.replace("\\", "/"), "[ephemeral_worktree]")

    # 3. Scrub arbitrary absolute Windows and Unix file paths
    text = re.sub(r"[a-zA-Z]:\\[^:\n\r\'\"\s]+", "[ephemeral_worktree]", text)
    text = re.sub(
        r"(?:/(?:tmp|var|home|usr|Users|app|private)/[^\s\'\"\n\r]+)",
        "[ephemeral_worktree]",
        text,
    )

    # 4. Extract meaningful error lines
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fatal_lines = [
        line for line in lines if line.lower().startswith(("fatal:", "error:", "remote error:"))
    ]
    selected_lines = fatal_lines if fatal_lines else lines
    combined = " ".join(selected_lines)

    # 5. Clean redundant leading prefixes
    cleaned = re.sub(r"^(?:fatal|error):\s*", "", combined, flags=re.IGNORECASE).strip()
    return cleaned or "Git clone failed."


# ------------------------------------------------------------------------------
# 2. SAFE WORKTREE DIRECTORY CLEANUP
# ------------------------------------------------------------------------------


def _retry_remove_readonly(func: Callable[[str], Any], path: str) -> None:
    """Clear Windows read-only bits on git object files and retry removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError as cleanup_error:
        logger.warning("Unable to remove read-only path '%s': %s", path, cleanup_error)
        raise


def _handle_remove_readonly(
    func: Callable[[str], Any],
    path: str,
    _exc_info: Tuple[type[BaseException], BaseException, Optional[TracebackType]],
) -> None:
    _retry_remove_readonly(func, path)


def _handle_remove_readonly_exc(
    func: Callable[[str], Any],
    path: str,
    _exc: BaseException,
) -> None:
    _retry_remove_readonly(func, path)


def safe_rmtree(path: str | Path) -> None:
    """Safely remove a directory tree, handling Windows read-only file locks.

    Guarantees that ephemeral worktree directories are wiped without leaking
    disk space or raising unhandled teardown exceptions.
    """
    path_str = str(path)
    if os.path.exists(path_str):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path_str, onexc=_handle_remove_readonly_exc)
            else:
                shutil.rmtree(path_str, onerror=_handle_remove_readonly)
            logger.debug("Successfully cleaned up ephemeral worktree: %s", path_str)
        except Exception as exc:
            logger.warning("Failed to remove ephemeral directory '%s': %s", path_str, exc)


# ------------------------------------------------------------------------------
# 3. ROBUST SUBPROCESS GIT CLONE WRAPPER
# ------------------------------------------------------------------------------


def normalize_git_url(repo_url: str) -> str:
    """Normalize user-provided repository URLs or GitHub shorthands.

    - Strips leading/trailing whitespace and trailing slashes.
    - Resolves GitHub shorthands (e.g., 'owner/repo' -> 'https://github.com/owner/repo.git').
    - Automatically appends '.git' if missing for remote Git repositories.
    """
    clean_url = repo_url.strip().rstrip("/")
    if not (
        clean_url.startswith("http://")
        or clean_url.startswith("https://")
        or clean_url.startswith("git@")
    ):
        if clean_url.startswith("github.com/"):
            clean_url = f"https://{clean_url}"
        elif "/" in clean_url and not os.path.isdir(clean_url):
            clean_url = f"https://github.com/{clean_url}"

    # Automatically append .git if missing (for non-local paths)
    if not os.path.isdir(clean_url) and not clean_url.endswith(".git"):
        clean_url = f"{clean_url}.git"

    return clean_url


def safe_git_clone(
    repo_url: str,
    target_dir: str | Path,
    branch: Optional[str] = None,
    timeout: int = MAX_GIT_CLONE_TIMEOUT_SECONDS,
    task_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Execute git clone with --single-branch, automatic branch fallback, and strict timeout.

    Probing Strategy:
      1. If a specific branch is requested (e.g. 'dev', 'main'), attempt that branch first.
      2. If that fails or if the branch was omitted, probe 'main'.
      3. If 'main' fails, probe 'master'.
      4. Fallback to default clone without explicit -b (remote default HEAD).

    Args:
        repo_url: Git clone URL or shorthand.
        target_dir: Ephemeral destination directory.
        branch: Target branch to clone.
        timeout: Subprocess timeout in seconds (default 60s).

    Returns:
        Tuple of (commit_sha, resolved_branch_name).

    Raises:
        GitIngestionError: If all clone attempts fail or if timeout is exceeded.
    """
    if timeout <= 0:
        raise ValueError("Git clone timeout must be greater than zero.")
    timeout_seconds = min(timeout, MAX_GIT_CLONE_TIMEOUT_SECONDS)
    normalized_url = normalize_git_url(repo_url)
    safe_url = re.sub(r"(https?://)[^@/]+@", r"\1", normalized_url)
    target_path = Path(target_dir)

    # Environment preventing interactive terminal credential prompts from hanging subprocesses
    clone_env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
    }

    # Build ordered list of candidate branches to probe
    candidates: List[Optional[str]] = []
    clean_branch = branch.strip() if branch and branch.strip() else None

    if clean_branch:
        candidates.append(clean_branch)
        if clean_branch != "main":
            candidates.append("main")
        if clean_branch != "master":
            candidates.append("master")
        candidates.append(None)  # Default remote HEAD fallback
    else:
        candidates = ["main", "master", None]

    last_stderr = ""
    last_exc: Optional[BaseException] = None
    clone_succeeded = False

    try:
        for candidate in candidates:
            # Each retry starts with an empty destination; the finally block also
            # removes partial clones when any unexpected failure escapes below.
            safe_rmtree(target_path)
            target_path.mkdir(parents=True, exist_ok=True)

            cmd: List[str] = ["git", "clone", "--depth", "1", "--single-branch"]
            if candidate is not None:
                cmd.extend(["-b", candidate])
            cmd.extend([normalized_url, str(target_path)])

            branch_label = candidate or "default remote HEAD"
            logger.info(
                "Attempting git clone for '%s' (branch: %s, timeout: %ds)",
                safe_url,
                branch_label,
                timeout_seconds,
            )

            try:
                if task_id:
                    from services.api.forge.execution.process_manager import (
                        run_tracked_subprocess,
                    )

                    result = run_tracked_subprocess(
                        task_id=task_id,
                        cmd=cmd,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        env=clone_env,
                    )
                else:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        env=clone_env,
                    )

                if result.returncode != 0:
                    last_stderr = result.stderr
                    logger.warning(
                        "Git clone candidate '%s' failed for '%s': %s",
                        branch_label,
                        safe_url,
                        sanitize_git_error(result.stderr, target_path),
                    )
                    continue

                sha_res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(target_path),
                    capture_output=True,
                    text=True,
                    timeout=min(15, timeout_seconds),
                    check=True,
                )
                commit_sha = sha_res.stdout.strip()
                if not commit_sha:
                    raise GitIngestionError(
                        message=f"Git clone did not resolve a commit for repository '{safe_url}'.",
                        error_code="GIT_REVISION_FAILED",
                        repo_url=safe_url,
                        branch=clean_branch,
                    )

                actual_branch = candidate
                if not actual_branch:
                    try:
                        br_res = subprocess.run(
                            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=str(target_path),
                            capture_output=True,
                            text=True,
                            timeout=min(10, timeout_seconds),
                        )
                        actual_branch = br_res.stdout.strip() if br_res.returncode == 0 else "HEAD"
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            "Unable to resolve the default branch name for '%s' before timeout",
                            safe_url,
                        )
                        actual_branch = "HEAD"

                logger.info(
                    "Git clone succeeded for '%s' on branch '%s' (SHA: %s)",
                    safe_url,
                    actual_branch,
                    commit_sha,
                )
                clone_succeeded = True
                return commit_sha, actual_branch

            except subprocess.TimeoutExpired as timeout_err:
                error_msg = (
                    f"Git clone operation timed out after {timeout_seconds} seconds "
                    f"for repository '{safe_url}'."
                )
                logger.error(error_msg)
                raise GitIngestionError(
                    message=error_msg,
                    error_code="GIT_CLONE_TIMEOUT",
                    repo_url=safe_url,
                    branch=clean_branch,
                    cause=timeout_err,
                ) from timeout_err

            except subprocess.CalledProcessError as proc_err:
                last_stderr = str(proc_err.stderr or proc_err)
                last_exc = proc_err
                logger.warning(
                    "CalledProcessError on branch '%s' for '%s': %s",
                    branch_label,
                    safe_url,
                    sanitize_git_error(last_stderr, target_path),
                )

        clean_err = sanitize_git_error(last_stderr, target_path)
        final_message = f"Failed to clone repository '{safe_url}': {clean_err}"
        logger.error("All git clone attempts exhausted for '%s': %s", safe_url, clean_err)
        raise GitIngestionError(
            message=final_message,
            error_code="GIT_CLONE_FAILED",
            repo_url=safe_url,
            branch=clean_branch,
            cause=last_exc,
        )
    finally:
        if not clone_succeeded:
            safe_rmtree(target_path)


# ------------------------------------------------------------------------------
# 4. INGESTION RESULT & PIPELINE IMPLEMENTATION
# ------------------------------------------------------------------------------


class IngestionResult(tuple):
    """2-tuple of (repo_id, total_chunks) supporting property access, unpacking, and string coercion."""

    def __new__(cls, repo_id: str, total_chunks: int) -> "IngestionResult":
        return super().__new__(cls, (repo_id, total_chunks))

    def __init__(self, repo_id: str, total_chunks: int):
        self.repo_id = repo_id
        self.total_chunks = total_chunks

    def __str__(self) -> str:
        return self.repo_id


class IngestionPipeline:
    """Pipelines cloning, AST chunking, vector embedding generation, and DB ingestion."""

    def __init__(self, db_session: AsyncSession) -> None:
        """Initialize pipeline with active SQLAlchemy AsyncSession."""
        self.session = db_session
        self.parser = CodeParser()

    async def _generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate OpenAI embeddings for code snippets in batches with zero-vector fallback."""
        if not texts:
            return []

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.info("OPENAI_API_KEY not set. Using zero-vector embeddings.")
            return [[0.0] * 1536 for _ in texts]

        embeddings: List[List[float]] = []
        client = AsyncOpenAI(api_key=api_key)
        batch_size = 64
        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
        sem = asyncio.Semaphore(5)

        async def _embed_batch(batch_texts: List[str]) -> List[List[float]]:
            clean_batch = [t[:8000] for t in batch_texts]
            async with sem:
                try:
                    response = await client.embeddings.create(
                        model="text-embedding-3-small",
                        input=clean_batch,
                        timeout=15.0,
                    )
                    return [item.embedding for item in response.data]
                except Exception as exc:
                    logger.warning(
                        "OpenAI embedding API failed on batch (%s). Using fallback vectors.",
                        exc,
                    )
                    return [[0.0] * 1536 for _ in batch_texts]

        batch_results = await asyncio.gather(*(_embed_batch(b) for b in batches))
        for res in batch_results:
            embeddings.extend(res)

        return embeddings

    async def ingest_repository(
        self, repo_url: str, branch: Optional[str] = "main"
    ) -> IngestionResult:
        """Clone repository, extract AST symbols and import/call edges, and persist to PostgreSQL.

        Ensures ephemeral worktree directories are wiped clean via try...finally.

        Args:
            repo_url: Git clone URL or local directory path.
            branch: Target Git branch to index. Defaults to 'main'.

        Returns:
            IngestionResult containing newly created repository UUID and chunk count.

        Raises:
            GitIngestionError: If Git clone or revision resolution fails (maps to HTTP 400).
        """
        logger.info(
            "Starting repository ingestion for '%s' (requested branch: '%s')",
            repo_url,
            branch,
        )
        clone_dir = tempfile.mkdtemp(prefix="forge_ingest_")

        try:
            # 1. Clone repository safely in threadpool to prevent blocking the async event loop
            commit_sha, resolved_branch = await asyncio.to_thread(
                safe_git_clone,
                repo_url=repo_url,
                target_dir=clone_dir,
                branch=branch,
                timeout=60,
            )

            # 2. Insert repository and revision records
            normalized_url = normalize_git_url(repo_url)
            repo_name = Path(normalized_url).stem or "repository"
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]

            repo = Repository(
                id=uuid.uuid4(),
                name=repo_name,
                local_path=normalized_url,
                default_branch=resolved_branch,
            )
            self.session.add(repo)
            await self.session.flush()

            revision = RepositoryRevision(
                id=uuid.uuid4(),
                repo_id=repo.id,
                commit_sha=commit_sha,
                status="indexed",
                indexed_at=datetime.now(timezone.utc),
            )
            self.session.add(revision)

            # 3. Recursively parse supported source files with a strict timeout.
            try:
                parsed_chunks, parsed_edges = await asyncio.wait_for(
                    asyncio.to_thread(
                        _walk_and_parse_source_files,
                        clone_dir,
                        self.parser,
                        60.0,
                    ),
                    timeout=60.0,
                )
            except (asyncio.TimeoutError, TimeoutExpired) as timeout_err:
                logger.error("File-walking and AST parsing timed out after 60s for '%s'", repo_url)
                raise GitIngestionError(
                    message="File-walking and AST parsing timed out after 60 seconds.",
                    error_code="GIT_CLONE_TIMEOUT",
                    repo_url=repo_url,
                    branch=branch,
                    cause=timeout_err,
                ) from timeout_err
            except CalledProcessError as proc_err:
                stderr = getattr(proc_err, "stderr", None) or str(proc_err)
                clean_err = sanitize_git_error(str(stderr), clone_dir)
                logger.error(
                    "Subprocess error during file parsing for '%s': %s", repo_url, clean_err
                )
                raise GitIngestionError(
                    message=f"Git operation failed: {clean_err}",
                    error_code="GIT_CLONE_FAILED",
                    repo_url=repo_url,
                    branch=branch,
                    cause=proc_err,
                ) from proc_err

            logger.info(
                "Parsed %d chunks and %d dependency edges across repository '%s'",
                len(parsed_chunks),
                len(parsed_edges),
                repo_name,
            )

            # 4. Generate vector embeddings in batches
            chunk_contents = [chunk["content"] for chunk in parsed_chunks]
            embeddings = await self._generate_embeddings(chunk_contents)

            # 5. Bulk insert code_chunks into PostgreSQL
            chunk_records: List[CodeChunk] = []
            for chunk_data, emb in zip(parsed_chunks, embeddings):
                # 1-indexed line numbers for AST symbol explorer
                start_l = chunk_data.get("start_line")
                line_no: Optional[int] = None
                if start_l is not None and isinstance(start_l, int):
                    line_no = start_l + 1 if start_l >= 0 else 1
                elif chunk_data.get("line_number") is not None:
                    line_no = chunk_data.get("line_number")

                end_l = chunk_data.get("end_line")
                end_line_no: Optional[int] = None
                if end_l is not None and isinstance(end_l, int):
                    end_line_no = end_l + 1 if end_l >= 0 else 1

                chunk_record = CodeChunk(
                    id=uuid.uuid4(),
                    repo_id=repo.id,
                    commit_sha=commit_sha,
                    file_path=chunk_data["file_path"],
                    symbol_name=chunk_data.get("symbol_name"),
                    symbol_type=chunk_data.get("symbol_type"),
                    parent_symbol=chunk_data.get("parent_symbol"),
                    line_number=line_no,
                    end_line=end_line_no,
                    content=chunk_data["content"],
                    embedding=emb,
                    tsv_content=func.to_tsvector("english", chunk_data["content"]),
                )
                chunk_records.append(chunk_record)

            if chunk_records:
                self.session.add_all(chunk_records)

            # 6. Bulk insert code_edges into PostgreSQL
            edge_records: List[CodeEdge] = []
            for edge_data in parsed_edges:
                edge_record = CodeEdge(
                    id=uuid.uuid4(),
                    repo_id=repo.id,
                    commit_sha=commit_sha,
                    source_symbol=edge_data["source_symbol"],
                    target_symbol=edge_data["target_symbol"],
                    edge_type=edge_data.get("edge_type", "imports"),
                )
                edge_records.append(edge_record)

            if edge_records:
                self.session.add_all(edge_records)

            # 7. Commit all records in a single atomic transaction
            await self.session.commit()
            logger.info(
                "Successfully ingested repository '%s' (UUID: %s, Chunks: %d)",
                repo_name,
                repo.id,
                len(chunk_records),
            )
            return IngestionResult(str(repo.id), len(chunk_records))

        finally:
            # 8. Leak-proof cleanup: Ensure ephemeral worktree is completely wiped
            safe_rmtree(clone_dir)


def inject_auth_token(repo_url: str, auth_token: Optional[str] = None) -> str:
    """Inject authentication credentials into an HTTPS Git clone URL.

    Resolution order:
      1. Explicit ``auth_token`` argument (per-request override).
      2. ``GITHUB_TOKEN`` environment variable (global fallback).
      3. ``settings.GITHUB_TOKEN`` from pydantic-settings config.

    If the URL is not HTTPS or no token is available, the URL is returned unchanged.

    Args:
        repo_url: Normalized Git clone URL.
        auth_token: Optional per-request Personal Access Token override.

    Returns:
        URL with ``x-access-token`` credentials embedded, or the original URL.
    """
    token = (auth_token or "").strip()
    if not token:
        token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        try:
            from services.api.core.config import settings as _settings

            token = str(getattr(_settings, "GITHUB_TOKEN", "") or "").strip()
        except Exception:
            token = ""

    if not token:
        return repo_url

    # Only inject into HTTPS URLs; skip SSH / local paths
    if not repo_url.startswith("https://"):
        return repo_url

    # Strip any existing credentials already embedded in the URL
    stripped = re.sub(r"https://[^@/]+@", "https://", repo_url)
    # Inject x-access-token credentials
    authenticated_url = stripped.replace("https://", f"https://x-access-token:{token}@", 1)
    logger.debug("Injected auth token into clone URL (token length: %d)", len(token))
    return authenticated_url


class RepositoryIngestionService:
    """Service for robust Git repository cloning, branch fallback, AST parsing, and indexing.

    Delegates all clone operations to ``safe_git_clone`` which provides:
      - Automatic branch fallback probing (user-branch → main → master → remote HEAD)
      - Strict subprocess timeout enforcement (default 60s)
      - Sensitive path sanitization in error output
      - Leak-proof ephemeral worktree cleanup

    Supports optional per-request ``auth_token`` injection for private repositories.
    """

    def __init__(self, db_session: Optional[AsyncSession] = None) -> None:
        self.session = db_session
        self.parser = CodeParser()

    @staticmethod
    async def ingest_repository(
        repo_url: str,
        branch: str = "main",
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Clone repository using automated branch fallback, extract AST symbols, and clean worktree.

        Args:
            repo_url: Git clone URL, GitHub shorthand (``owner/repo``), or local path.
            branch: Target Git branch to index. Defaults to ``'main'``.
            auth_token: Optional GitHub Personal Access Token for private repositories.
                If omitted, falls back to ``GITHUB_TOKEN`` env var or settings.

        Returns:
            Dictionary containing ``status``, ``repo_url``, ``branch``,
            ``commit_sha``, ``resolved_branch``, ``symbols_indexed``, and ``worktree_path``.

        Raises:
            HTTPException(400): If Git clone fails across all branch candidates.
            HTTPException(408): If Git clone exceeds the 60-second timeout.
            HTTPException(500): If an unexpected internal error occurs during parsing.
        """
        # 1. Sanitize and normalize URL
        normalized_url = normalize_git_url(repo_url)

        # 2. Append .git suffix if missing (HTTPS GitHub URLs only)
        if (
            normalized_url.startswith("https://")
            and "github.com" in normalized_url
            and not normalized_url.endswith(".git")
        ):
            normalized_url = f"{normalized_url.rstrip('/')}.git"

        # 3. Inject auth credentials if available
        clone_url = inject_auth_token(normalized_url, auth_token)

        worktree_dir = tempfile.mkdtemp(prefix="forge_worktree_")
        keep_worktree = False

        try:
            # 4. Delegate to safe_git_clone (handles branch fallback, timeout, CalledProcessError)
            try:
                commit_sha, resolved_branch = await asyncio.wait_for(
                    asyncio.to_thread(
                        safe_git_clone,
                        repo_url=clone_url,
                        target_dir=worktree_dir,
                        branch=branch,
                        timeout=60,
                    ),
                    timeout=60.0,
                )
            except (asyncio.TimeoutError, TimeoutExpired) as timeout_err:
                safe_rmtree(worktree_dir)
                stderr = getattr(timeout_err, "stderr", None) or str(timeout_err)
                clean_err = sanitize_git_error(str(stderr), worktree_dir)
                logger.error("Git clone timed out after 60s for '%s'", repo_url)
                raise HTTPException(
                    status_code=400,
                    detail=f"Git clone operation timed out after 60 seconds: {clean_err}",
                ) from timeout_err
            except CalledProcessError as proc_err:
                safe_rmtree(worktree_dir)
                stderr = getattr(proc_err, "stderr", None) or str(proc_err)
                clean_err = sanitize_git_error(str(stderr), worktree_dir)
                logger.error("Git clone CalledProcessError for '%s': %s", repo_url, clean_err)
                raise HTTPException(
                    status_code=400,
                    detail=f"Git clone failed: {clean_err}",
                ) from proc_err

            # 5. Recursively parse source symbols and imports with a strict timeout.
            try:
                source_parser = CodeParser()
                indexed_chunks, _ = await asyncio.wait_for(
                    asyncio.to_thread(
                        _walk_and_parse_source_files,
                        worktree_dir,
                        source_parser,
                        60.0,
                    ),
                    timeout=60.0,
                )
                symbol_count = max(1, len(indexed_chunks))
            except (asyncio.TimeoutError, TimeoutExpired) as timeout_err:
                safe_rmtree(worktree_dir)
                stderr = getattr(timeout_err, "stderr", None) or "Timeout during AST traversal"
                clean_err = sanitize_git_error(str(stderr), worktree_dir)
                logger.error("AST file-walking timed out for '%s': %s", repo_url, clean_err)
                raise HTTPException(
                    status_code=400,
                    detail=f"Repository AST parsing timed out after 60 seconds: {clean_err}",
                ) from timeout_err
            except CalledProcessError as proc_err:
                safe_rmtree(worktree_dir)
                stderr = getattr(proc_err, "stderr", None) or str(proc_err)
                clean_err = sanitize_git_error(str(stderr), worktree_dir)
                logger.error(
                    "Subprocess error during AST parsing for '%s': %s", repo_url, clean_err
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"AST parsing subprocess error: {clean_err}",
                ) from proc_err

            keep_worktree = True
            return {
                "status": "success",
                "repo_url": repo_url,
                "branch": resolved_branch,
                "commit_sha": commit_sha,
                "resolved_branch": resolved_branch,
                "symbols_indexed": symbol_count,
                "worktree_path": worktree_dir,
            }

        except GitIngestionError as git_err:
            # safe_git_clone raises GitIngestionError on all clone failures
            safe_rmtree(worktree_dir)
            sanitized_message = git_err.message
            error_code = git_err.error_code

            if error_code == "GIT_CLONE_TIMEOUT":
                raise HTTPException(
                    status_code=408,
                    detail=sanitized_message,
                ) from git_err

            raise HTTPException(
                status_code=400,
                detail=f"Git clone failed: {sanitized_message}",
            ) from git_err

        except HTTPException:
            safe_rmtree(worktree_dir)
            raise

        except (CalledProcessError, TimeoutExpired) as sub_err:
            safe_rmtree(worktree_dir)
            stderr = getattr(sub_err, "stderr", None) or str(sub_err)
            clean_err = sanitize_git_error(str(stderr), worktree_dir)
            logger.error("Git subprocess failure for '%s': %s", repo_url, clean_err)
            raise HTTPException(
                status_code=400,
                detail=f"Git operation failed: {clean_err}",
            ) from sub_err

        except Exception as e:
            safe_rmtree(worktree_dir)
            clean_err = sanitize_git_error(str(e), worktree_dir)
            logger.exception(
                "Unexpected ingestion pipeline error for '%s': %s", repo_url, clean_err
            )
            raise HTTPException(
                status_code=400,
                detail=f"Git ingestion error: {clean_err}",
            ) from e
        finally:
            if not keep_worktree:
                safe_rmtree(worktree_dir)


__all__ = [
    "GitIngestionError",
    "IngestionPipeline",
    "IngestionResult",
    "RepositoryIngestionService",
    "inject_auth_token",
    "normalize_git_url",
    "safe_git_clone",
    "safe_rmtree",
    "sanitize_git_error",
]
