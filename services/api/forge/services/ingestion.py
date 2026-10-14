"""Repository Ingestion Service for FORGE.

Provides enterprise-grade Git clone operations, automated branch fallback probing
('main' -> 'master' -> default remote HEAD), strict subprocess timeout enforcement,
sensitive path sanitization in error reporting, and leak-proof ephemeral worktree cleanup.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import uuid

from openai import AsyncOpenAI
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.forge.exceptions import ForgeBaseException
from services.api.forge.retrieval.parser import CodeParser
from services.api.forge.security.sanitizer import PromptSanitizer
from services.api.models.code import CodeChunk, CodeEdge
from services.api.models.repository import Repository, RepositoryRevision

logger = logging.getLogger("forge.services.ingestion")


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


def _handle_remove_readonly(func: Any, path: str, exc_info: Any) -> None:
    """Clear Windows read-only bits on git object files and retry removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def safe_rmtree(path: str | Path) -> None:
    """Safely remove a directory tree, handling Windows read-only file locks.

    Guarantees that ephemeral worktree directories are wiped without leaking
    disk space or raising unhandled teardown exceptions.
    """
    path_str = str(path)
    if os.path.exists(path_str):
        try:
            shutil.rmtree(path_str, onerror=_handle_remove_readonly)
            logger.debug("Successfully cleaned up ephemeral worktree: %s", path_str)
        except Exception as exc:
            logger.warning("Failed to remove ephemeral directory '%s': %s", path_str, exc)


# ------------------------------------------------------------------------------
# 3. ROBUST SUBPROCESS GIT CLONE WRAPPER
# ------------------------------------------------------------------------------


def normalize_git_url(repo_url: str) -> str:
    """Normalize user-provided repository URLs or GitHub shorthands.

    Handles shorthand inputs such as 'owner/repo' or 'github.com/owner/repo'.
    """
    clean_url = repo_url.strip()
    if not (
        clean_url.startswith("http://")
        or clean_url.startswith("https://")
        or clean_url.startswith("git@")
    ):
        if clean_url.startswith("github.com/"):
            clean_url = f"https://{clean_url}"
        elif "/" in clean_url and not os.path.isdir(clean_url):
            clean_url = f"https://github.com/{clean_url}.git"
    return clean_url


def safe_git_clone(
    repo_url: str,
    target_dir: str | Path,
    branch: Optional[str] = None,
    timeout: int = 60,
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
    normalized_url = normalize_git_url(repo_url)
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

    for candidate in candidates:
        # Wipe destination to ensure git clone does not encounter a non-empty directory
        safe_rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)

        cmd: List[str] = ["git", "clone", "--depth", "1", "--single-branch"]
        if candidate is not None:
            cmd.extend(["-b", candidate])
        cmd.extend([normalized_url, str(target_path)])

        branch_label = candidate or "default remote HEAD"
        logger.info(
            "Attempting git clone for '%s' (branch: %s, timeout: %ds)",
            normalized_url,
            branch_label,
            timeout,
        )

        try:
            if task_id:
                from services.api.forge.execution.process_manager import run_tracked_subprocess

                result = run_tracked_subprocess(
                    task_id=task_id,
                    cmd=cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=clone_env,
                )
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=clone_env,
                )

            if result.returncode == 0:
                # Successfully cloned! Extract commit SHA and resolved branch
                sha_res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(target_path),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
                commit_sha = sha_res.stdout.strip()

                actual_branch = candidate
                if not actual_branch:
                    try:
                        br_res = subprocess.run(
                            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=str(target_path),
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        actual_branch = br_res.stdout.strip() if br_res.returncode == 0 else "main"
                    except Exception:
                        actual_branch = "main"

                logger.info(
                    "Git clone succeeded for '%s' on branch '%s' (SHA: %s)",
                    normalized_url,
                    actual_branch,
                    commit_sha,
                )
                return commit_sha, actual_branch

            # Process non-zero exit code
            last_stderr = result.stderr
            logger.warning(
                "Git clone candidate '%s' failed for '%s': %s",
                branch_label,
                normalized_url,
                sanitize_git_error(result.stderr, target_path),
            )

        except subprocess.TimeoutExpired as timeout_err:
            safe_rmtree(target_path)
            error_msg = f"Git clone operation timed out after {timeout} seconds for repository '{normalized_url}'."
            logger.error(error_msg)
            raise GitIngestionError(
                message=error_msg,
                error_code="GIT_CLONE_TIMEOUT",
                repo_url=normalized_url,
                branch=clean_branch,
                cause=timeout_err,
            ) from timeout_err

        except subprocess.CalledProcessError as proc_err:
            last_stderr = proc_err.stderr if hasattr(proc_err, "stderr") else str(proc_err)
            last_exc = proc_err
            logger.warning(
                "CalledProcessError on branch '%s' for '%s': %s",
                branch_label,
                normalized_url,
                sanitize_git_error(last_stderr, target_path),
            )

    # All candidate branches failed
    safe_rmtree(target_path)
    clean_err = sanitize_git_error(last_stderr, target_path)
    final_message = f"Failed to clone repository '{normalized_url}': {clean_err}"
    logger.error("All git clone attempts exhausted for '%s': %s", normalized_url, clean_err)

    raise GitIngestionError(
        message=final_message,
        error_code="GIT_CLONE_FAILED",
        repo_url=normalized_url,
        branch=clean_branch,
        cause=last_exc,
    )


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

            # 3. Recursively scan code files
            ignored_dirs = {
                "venv",
                ".venv",
                "node_modules",
                ".git",
                "__pycache__",
                "dist",
                "build",
            }
            supported_exts = {
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".jsx",
                ".md",
                ".json",
                ".rs",
                ".go",
            }
            parsed_chunks: List[Dict[str, Any]] = []
            parsed_edges: List[Dict[str, str]] = []

            for root, dirs, files in os.walk(clone_dir):
                # Prune ignored and hidden directories
                dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]

                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in supported_exts:
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, clone_dir).replace("\\", "/")

                        try:
                            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                                raw_content = f.read()

                            code_content = PromptSanitizer.sanitize(raw_content)

                            if len(parsed_chunks) >= 300:
                                break

                            if ext == ".py":
                                file_symbols, file_edges = self.parser.parse_file(
                                    rel_path, code_content
                                )
                                if file_symbols:
                                    parsed_chunks.extend(file_symbols)
                                else:
                                    parsed_chunks.append(
                                        {
                                            "file_path": rel_path,
                                            "symbol_name": Path(rel_path).stem,
                                            "symbol_type": "module",
                                            "parent_symbol": None,
                                            "start_line": 1,
                                            "end_line": len(code_content.splitlines()) or 1,
                                            "content": code_content,
                                        }
                                    )
                                if file_edges:
                                    parsed_edges.extend(file_edges)
                            else:
                                lines = code_content.splitlines()
                                parsed_chunks.append(
                                    {
                                        "file_path": rel_path,
                                        "symbol_name": Path(rel_path).stem,
                                        "symbol_type": "file",
                                        "parent_symbol": None,
                                        "start_line": 1,
                                        "end_line": len(lines) or 1,
                                        "content": code_content[:8000],
                                    }
                                )

                        except Exception as parse_err:
                            logger.warning("Error parsing file '%s': %s", rel_path, parse_err)

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
                chunk_record = CodeChunk(
                    id=uuid.uuid4(),
                    repo_id=repo.id,
                    commit_sha=commit_sha,
                    file_path=chunk_data["file_path"],
                    symbol_name=chunk_data.get("symbol_name"),
                    symbol_type=chunk_data.get("symbol_type"),
                    parent_symbol=chunk_data.get("parent_symbol"),
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


# Alias class name for semantic domain alignment
RepositoryIngestionService = IngestionPipeline

__all__ = [
    "GitIngestionError",
    "IngestionPipeline",
    "IngestionResult",
    "RepositoryIngestionService",
    "normalize_git_url",
    "safe_git_clone",
    "safe_rmtree",
    "sanitize_git_error",
]
