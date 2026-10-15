"""Task-focused repository scope selection from the indexed AST and dependency graph."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Iterable, List, Optional, Set

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.core.database import async_session_factory
from services.api.models.code import CodeChunk, CodeEdge
from services.api.models.repository import RepositoryRevision

logger = logging.getLogger("forge.rag.scoper")

_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:[./:-][A-Za-z_$][A-Za-z0-9_$]*)*")
_STOP_WORDS = {
    "about",
    "after",
    "agent",
    "also",
    "and",
    "any",
    "are",
    "bug",
    "change",
    "code",
    "could",
    "create",
    "does",
    "ensure",
    "fix",
    "for",
    "from",
    "have",
    "implement",
    "into",
    "make",
    "need",
    "our",
    "please",
    "related",
    "should",
    "some",
    "that",
    "the",
    "their",
    "this",
    "through",
    "update",
    "use",
    "using",
    "with",
}
_SOURCE_EXTENSIONS = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
)
_MAX_PRIMARY_SYMBOLS = 5
_MAX_SCOPE_FILES = 20


def _extract_identifiers(task_prompt: str) -> List[str]:
    """Return ordered, unique identifier-like tokens while excluding task boilerplate."""
    identifiers: List[str] = []
    seen: Set[str] = set()
    for match in _IDENTIFIER_RE.finditer(task_prompt):
        token = match.group(0).strip("./:-")
        if len(token) < 3 or token.casefold() in _STOP_WORDS:
            continue
        normalized = token.casefold()
        if normalized not in seen:
            seen.add(normalized)
            identifiers.append(token)
    return identifiers[:20]


async def _latest_indexed_commit(session: AsyncSession, repo_uuid: uuid.UUID) -> Optional[str]:
    revision_result = await session.execute(
        select(RepositoryRevision.commit_sha)
        .where(RepositoryRevision.repo_id == repo_uuid)
        .order_by(RepositoryRevision.indexed_at.desc().nullslast())
        .limit(1)
    )
    commit_sha = revision_result.scalar_one_or_none()
    if commit_sha:
        return str(commit_sha)

    chunk_result = await session.execute(
        select(CodeChunk.commit_sha)
        .where(CodeChunk.repo_id == repo_uuid)
        .order_by(CodeChunk.commit_sha.desc())
        .limit(1)
    )
    commit_sha = chunk_result.scalar_one_or_none()
    return str(commit_sha) if commit_sha else None


def _edge_endpoint_path(endpoint: str) -> Optional[str]:
    """Extract a known file component from qualified AST edge symbols."""
    if "::" in endpoint:
        return endpoint.split("::", 1)[0]
    if endpoint.casefold().endswith(_SOURCE_EXTENSIONS):
        return endpoint
    return None


def _module_file_candidates(module: str, source_file: Optional[str] = None) -> Set[str]:
    """Map a module-style import edge to likely source file paths."""
    raw_module = module.strip()
    if raw_module.startswith(".") and source_file:
        leading_dots = len(raw_module) - len(raw_module.lstrip("."))
        source_parts = source_file.replace("\\", "/").split("/")[:-1]
        parent_parts = source_parts[: max(0, len(source_parts) - leading_dots + 1)]
        module_parts = raw_module.lstrip(".").split(".") if raw_module.lstrip(".") else []
        normalized = "/".join(parent_parts + module_parts)
    else:
        normalized = raw_module.lstrip(".").replace("::", ".").replace(".", "/")
    if not normalized:
        return set()
    base_path = normalized
    candidates = {base_path + extension for extension in _SOURCE_EXTENSIONS}
    candidates.update(
        f"{base_path}/__init__{extension}" for extension in (".py", ".js", ".ts")
    )
    return candidates


async def _load_neighbors(
    session: AsyncSession,
    repo_uuid: uuid.UUID,
    commit_sha: str,
    seed_chunks: Iterable[CodeChunk],
) -> List[str]:
    """Resolve one-hop callers and imports for the selected primary AST symbols."""
    seeds = list(seed_chunks)
    seed_files = {chunk.file_path for chunk in seeds}
    seed_symbol_names = {chunk.symbol_name for chunk in seeds if chunk.symbol_name}
    seed_endpoints = set(seed_files)
    seed_endpoints.update(
        f"{chunk.file_path}::{chunk.symbol_name}"
        for chunk in seeds
        if chunk.symbol_name
    )
    seed_endpoints.update(seed_symbol_names)

    if not seed_endpoints:
        return []

    edge_result = await session.execute(
        select(CodeEdge.source_symbol, CodeEdge.target_symbol)
        .where(
            CodeEdge.repo_id == repo_uuid,
            CodeEdge.commit_sha == commit_sha,
            CodeEdge.edge_type.in_(("calls", "imports")),
            or_(
                CodeEdge.source_symbol.in_(seed_endpoints),
                CodeEdge.target_symbol.in_(seed_endpoints),
            ),
        )
    )

    direct_paths: Set[str] = set()
    raw_neighbor_symbols: Set[str] = set()
    module_candidates: Set[str] = set()
    for source_symbol, target_symbol in edge_result.all():
        source = str(source_symbol)
        target = str(target_symbol)
        source_path = _edge_endpoint_path(source)
        target_path = _edge_endpoint_path(target)
        endpoints = (source, target)
        for endpoint in endpoints:
            endpoint_path = _edge_endpoint_path(endpoint)
            if endpoint_path and endpoint_path not in seed_files:
                direct_paths.add(endpoint_path)
        if source not in seed_endpoints:
            if not _edge_endpoint_path(source):
                module_candidates.update(_module_file_candidates(source, target_path))
                raw_neighbor_symbols.add(source)
        if target not in seed_endpoints:
            if not _edge_endpoint_path(target):
                module_candidates.update(_module_file_candidates(target, source_path))
                raw_neighbor_symbols.add(target)

    neighbor_conditions = []
    if direct_paths:
        neighbor_conditions.append(CodeChunk.file_path.in_(direct_paths))
    if raw_neighbor_symbols:
        neighbor_conditions.append(CodeChunk.symbol_name.in_(raw_neighbor_symbols))
    if module_candidates:
        neighbor_conditions.append(CodeChunk.file_path.in_(module_candidates))
    if not neighbor_conditions:
        return []

    neighbor_result = await session.execute(
        select(CodeChunk.file_path)
        .where(
            CodeChunk.repo_id == repo_uuid,
            CodeChunk.commit_sha == commit_sha,
            or_(*neighbor_conditions),
        )
        .order_by(CodeChunk.file_path.asc())
        .limit(100)
    )
    return list(dict.fromkeys(str(path) for path in neighbor_result.scalars().all()))


async def extract_task_scope(task_prompt: str, repo_id: str) -> List[str]:
    """Find primary AST matches and their one-hop import/caller files for a task.

    The repository's latest indexed revision is searched using symbol names,
    paths, and its PostgreSQL full-text index. The result is ordered with primary
    match files first, followed by deduplicated one-hop dependency files.
    """
    try:
        repo_uuid = uuid.UUID(str(repo_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"repo_id must be a repository UUID, got {repo_id!r}") from exc

    identifiers = _extract_identifiers(task_prompt)
    if not identifiers:
        logger.info("No searchable identifiers found in task prompt for repository %s", repo_uuid)
        return []

    normalized_identifiers = [term.casefold() for term in identifiers]
    symbol_matches = [
        CodeChunk.symbol_name.ilike(f"%{term}%")
        for term in identifiers
    ]
    path_matches = [CodeChunk.file_path.ilike(f"%{term}%") for term in identifiers]
    content_matches = [CodeChunk.content.ilike(f"%{term}%") for term in identifiers]
    full_text_match = CodeChunk.tsv_content.op("@@")(
        func.plainto_tsquery("english", task_prompt)
    )

    async with async_session_factory() as session:
        commit_sha = await _latest_indexed_commit(session, repo_uuid)
        if not commit_sha:
            logger.warning("No indexed AST revision found for repository %s", repo_uuid)
            return []

        relevance = case(
            (func.lower(CodeChunk.symbol_name).in_(normalized_identifiers), 0),
            (or_(*symbol_matches), 1),
            (or_(*path_matches), 2),
            else_=3,
        )
        primary_result = await session.execute(
            select(CodeChunk)
            .where(
                CodeChunk.repo_id == repo_uuid,
                CodeChunk.commit_sha == commit_sha,
                or_(*symbol_matches, *path_matches, *content_matches, full_text_match),
            )
            .order_by(
                relevance,
                CodeChunk.file_path.asc(),
                CodeChunk.line_number.asc().nullslast(),
            )
            .limit(25)
        )
        matching_chunks = list(primary_result.scalars().all())

        primary_chunks: List[CodeChunk] = []
        primary_files: List[str] = []
        seen_primary_symbols: Set[tuple[str, Optional[str]]] = set()
        for chunk in matching_chunks:
            key = (chunk.file_path, chunk.symbol_name)
            if key in seen_primary_symbols:
                continue
            seen_primary_symbols.add(key)
            primary_chunks.append(chunk)
            if chunk.file_path not in primary_files:
                primary_files.append(chunk.file_path)
            if len(primary_chunks) >= _MAX_PRIMARY_SYMBOLS:
                break

        if not primary_chunks:
            logger.info("No AST matches found for task in repository %s", repo_uuid)
            return []

        neighbors = await _load_neighbors(
            session=session,
            repo_uuid=repo_uuid,
            commit_sha=commit_sha,
            seed_chunks=primary_chunks,
        )

    scoped_files = list(dict.fromkeys(primary_files + neighbors))
    return scoped_files[:_MAX_SCOPE_FILES]


async def load_scoped_file_context(
    repo_id: str,
    file_paths: List[str],
    commit_sha: Optional[str] = None,
) -> List[dict[str, Any]]:
    """Load indexed AST code chunks only for the requested scope files."""
    if not file_paths:
        return []
    try:
        repo_uuid = uuid.UUID(str(repo_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"repo_id must be a repository UUID, got {repo_id!r}") from exc

    async with async_session_factory() as session:
        selected_commit = commit_sha or await _latest_indexed_commit(session, repo_uuid)
        if not selected_commit:
            logger.warning("No indexed AST revision found for repository %s", repo_uuid)
            return []
        result = await session.execute(
            select(CodeChunk)
            .where(
                CodeChunk.repo_id == repo_uuid,
                CodeChunk.commit_sha == selected_commit,
                CodeChunk.file_path.in_(list(dict.fromkeys(file_paths))),
            )
            .order_by(
                CodeChunk.file_path.asc(),
                CodeChunk.line_number.asc().nullslast(),
                CodeChunk.symbol_name.asc().nullslast(),
            )
            .limit(100)
        )
        return [
            {
                "file_path": chunk.file_path,
                "symbol_name": chunk.symbol_name or "",
                "symbol_type": chunk.symbol_type or "",
                "content": chunk.content,
            }
            for chunk in result.scalars().all()
        ]


__all__ = ["extract_task_scope", "load_scoped_file_context"]
