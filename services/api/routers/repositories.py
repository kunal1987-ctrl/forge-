"""FastAPI router for repository management and ingestion."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from subprocess import CalledProcessError, TimeoutExpired
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.core.database import get_db
from services.api.forge.concurrency.locks import ConcurrencyLockError, async_repo_lock
from services.api.forge.services.ingestion import (
    GitIngestionError,
    IngestionPipeline,
    RepositoryIngestionService,
    inject_auth_token,
    sanitize_git_error,
)
from services.api.models.code import CodeChunk
from services.api.models.repository import Repository, RepositoryRevision

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Repositories"])


class IngestRequest(BaseModel):
    """Payload to trigger repository cloning and RAG ingestion."""

    repo_url: str = Field(..., description="Git clone URL or local repository path", min_length=1)
    branch: str = Field("main", description="Target Git branch to index")
    auth_token: Optional[str] = Field(
        None,
        description="Optional GitHub Personal Access Token for private repositories. "
        "Falls back to GITHUB_TOKEN env var if omitted.",
    )


class IngestResponse(BaseModel):
    """Response returned upon repository ingestion."""

    repo_id: str
    total_chunks: int = 0
    status: str = "ingestion_completed"
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    commit_sha: Optional[str] = None


class ASTSymbolItem(BaseModel):
    """Flat Tree-sitter AST symbol projection persisted to code_chunks."""

    file_path: str
    symbol_name: str
    symbol_type: str = "chunk"
    line_number: Optional[int] = None
    parent_symbol: Optional[str] = None
    end_line: Optional[int] = None
    content: Optional[str] = None


class ASTSymbolsSummary(BaseModel):
    """Summary KPI metrics for indexed repository AST symbols."""

    total_files_parsed: int = 0
    total_nodes_indexed: int = 0
    by_type: Optional[Dict[str, int]] = None


class ASTSymbolsIndexResponse(BaseModel):
    """Response schema for the AST symbol index."""

    repo_id: str
    commit_sha: Optional[str] = None
    summary: Optional[ASTSymbolsSummary] = None
    symbols: List[ASTSymbolItem] = []


@router.post(
    "/api/repositories/ingest", response_model=IngestResponse, status_code=status.HTTP_200_OK
)
@router.post("/api/repos/ingest", response_model=IngestResponse, status_code=status.HTTP_200_OK)
async def ingest_repository(
    request: IngestRequest,
    db: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """Clones repository, parses AST symbols, and inserts vectors into PostgreSQL.

    Acquires exclusive distributed Redis lock before provisioning worktree/clone.
    Supports optional ``auth_token`` for private repositories (falls back to
    ``GITHUB_TOKEN`` env var or settings).

    Args:
        request: IngestRequest specifying repo_url, target branch, and optional auth_token.
        db: Active AsyncSession database session.

    Returns:
        IngestResponse confirming ingestion has finished with repo_id, total_chunks,
        resolved branch, and commit SHA.
    """
    logger.info("Ingesting repository '%s' (branch: '%s')", request.repo_url, request.branch)
    try:
        async with async_repo_lock(
            request.repo_url, timeout=120.0, blocking=True, blocking_timeout=15.0
        ):
            pipeline = IngestionPipeline(db)
            # Inject auth token into the clone URL before passing to pipeline
            from services.api.forge.services.ingestion import inject_auth_token as _inject_token

            authenticated_url = _inject_token(request.repo_url, request.auth_token)
            res = await pipeline.ingest_repository(
                repo_url=authenticated_url, branch=request.branch
            )
            repo_id = str(res)
            total_chunks = getattr(res, "total_chunks", 0)
            return IngestResponse(
                repo_id=repo_id,
                total_chunks=total_chunks,
                status="ingestion_completed",
                repo_url=request.repo_url,
                branch=request.branch,
                commit_sha=None,  # Full pipeline returns IngestionResult (repo_id, chunks)
            )
    except ConcurrencyLockError as lock_err:
        logger.warning(
            "Repository ingestion lock conflict for '%s': %s", request.repo_url, lock_err
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Repository ingestion locked: {lock_err}",
        )
    except GitIngestionError as git_err:
        logger.warning("Repository ingestion Git failure for '%s': %s", request.repo_url, git_err)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=git_err.message,
        )
    except (CalledProcessError, TimeoutExpired) as sub_err:
        stderr = getattr(sub_err, "stderr", None) or str(sub_err)
        sanitized = sanitize_git_error(str(stderr))
        logger.warning(
            "Subprocess error during repository ingestion for '%s': %s",
            request.repo_url,
            sanitized,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Git operation failed: {sanitized}",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Repository ingestion failed for '%s': %s", request.repo_url, exc)
        sanitized = sanitize_git_error(str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Repository ingestion failed: {sanitized}",
        )


@router.get(
    "/api/repos/{repo_id}/symbols",
    response_model=ASTSymbolsIndexResponse,
    status_code=status.HTTP_200_OK,
)
@router.get(
    "/api/repositories/{repo_id}/symbols",
    response_model=ASTSymbolsIndexResponse,
    status_code=status.HTTP_200_OK,
)
async def get_repository_symbols(
    repo_id: str,
    commit_sha: Optional[str] = Query(None, description="Optional commit SHA filter"),
    symbol_type: Optional[str] = Query(
        None, description="Filter by symbol type (e.g. class, function, module)"
    ),
    query: Optional[str] = Query(
        None, description="Case-insensitive substring search across symbol name and file path"
    ),
    limit: Optional[int] = Query(
        None, ge=1, le=10000, description="Maximum number of symbols to return"
    ),
    db: AsyncSession = Depends(get_db),
) -> ASTSymbolsIndexResponse:
    """Retrieves the flattened Tree-sitter AST symbol index for an ingested repository.

    Supports symbol type filtering, search query filtering, and limits. Returns
    aggregate summary KPI counts (total files, total symbols, breakdown by type)
    alongside the symbol records.
    """
    clean_repo_id = (repo_id or "").strip()
    target_repo_uuid: Optional[uuid.UUID] = None

    if clean_repo_id:
        try:
            target_repo_uuid = uuid.UUID(clean_repo_id)
        except (ValueError, AttributeError):
            target_repo_uuid = None

    # If repo_id is not a valid UUID string, look up by name or local_path
    if target_repo_uuid is None and clean_repo_id:
        try:
            repo_stmt = (
                select(Repository.id)
                .where(
                    or_(
                        Repository.name == clean_repo_id,
                        Repository.local_path == clean_repo_id,
                    )
                )
                .limit(1)
            )
            repo_res = await db.execute(repo_stmt)
            target_repo_uuid = repo_res.scalar_one_or_none()
        except Exception as lookup_err:
            logger.warning("Repository lookup failed for '%s': %s", clean_repo_id, lookup_err)

    if target_repo_uuid is None:
        return ASTSymbolsIndexResponse(
            repo_id=clean_repo_id,
            commit_sha=commit_sha,
            summary=ASTSymbolsSummary(
                total_files_parsed=0,
                total_nodes_indexed=0,
                by_type={},
            ),
            symbols=[],
        )

    target_commit_sha: Optional[str] = (
        commit_sha.strip() if commit_sha and commit_sha.strip() else None
    )

    # If commit_sha was not provided, resolve the latest revision
    if target_commit_sha is None:
        try:
            rev_stmt = (
                select(RepositoryRevision.commit_sha)
                .where(RepositoryRevision.repo_id == target_repo_uuid)
                .order_by(RepositoryRevision.indexed_at.desc().nullslast())
                .limit(1)
            )
            rev_res = await db.execute(rev_stmt)
            target_commit_sha = rev_res.scalar_one_or_none()

            if target_commit_sha is None:
                chunk_sha_stmt = (
                    select(CodeChunk.commit_sha)
                    .where(CodeChunk.repo_id == target_repo_uuid)
                    .limit(1)
                )
                chunk_sha_res = await db.execute(chunk_sha_stmt)
                target_commit_sha = chunk_sha_res.scalar_one_or_none()
        except Exception as rev_err:
            logger.debug("Failed resolving latest revision for repo '%s': %s", target_repo_uuid, rev_err)

    base_conditions = [CodeChunk.repo_id == target_repo_uuid]
    if target_commit_sha:
        base_conditions.append(CodeChunk.commit_sha == target_commit_sha)

    # 1. Summary calculations
    total_files = 0
    total_nodes = 0
    by_type: Dict[str, int] = {}
    try:
        files_stmt = select(func.count(func.distinct(CodeChunk.file_path))).where(*base_conditions)
        files_res = await db.execute(files_stmt)
        total_files = files_res.scalar() or 0

        nodes_stmt = select(func.count(CodeChunk.id)).where(*base_conditions)
        nodes_res = await db.execute(nodes_stmt)
        total_nodes = nodes_res.scalar() or 0

        type_stmt = (
            select(CodeChunk.symbol_type, func.count(CodeChunk.id))
            .where(*base_conditions)
            .group_by(CodeChunk.symbol_type)
        )
        type_res = await db.execute(type_stmt)
        for stype, scount in type_res.all():
            if stype:
                by_type[stype] = int(scount)
    except Exception as summary_err:
        logger.warning(
            "Summary calculation error for repo '%s': %s", target_repo_uuid, summary_err
        )

    # 2. Query conditions for symbols listing
    query_conditions = list(base_conditions)

    if symbol_type and symbol_type.strip():
        clean_st = symbol_type.strip().lower()
        if clean_st in ("class", "classes"):
            query_conditions.append(
                func.lower(CodeChunk.symbol_type).in_(
                    ["class", "interface", "struct", "trait", "enum", "type"]
                )
            )
        elif clean_st in ("function", "functions", "method", "methods"):
            query_conditions.append(
                func.lower(CodeChunk.symbol_type).in_(
                    ["function", "method", "constructor", "func"]
                )
            )
        elif clean_st in ("module", "modules", "file", "files"):
            query_conditions.append(
                func.lower(CodeChunk.symbol_type).in_(["module", "file"])
            )
        elif clean_st != "all":
            query_conditions.append(func.lower(CodeChunk.symbol_type) == clean_st)

    if query and query.strip():
        q_clean = query.strip()
        query_conditions.append(
            or_(
                CodeChunk.symbol_name.ilike(f"%{q_clean}%"),
                CodeChunk.file_path.ilike(f"%{q_clean}%"),
            )
        )

    symbols_list: List[ASTSymbolItem] = []
    try:
        symbols_stmt = (
            select(CodeChunk)
            .where(*query_conditions)
            .order_by(
                CodeChunk.file_path.asc(),
                CodeChunk.line_number.asc().nullslast(),
                CodeChunk.symbol_name.asc().nullslast(),
            )
        )
        if limit is not None and limit > 0:
            symbols_stmt = symbols_stmt.limit(limit)

        symbols_res = await db.execute(symbols_stmt)
        chunk_records = symbols_res.scalars().all()

        for chunk in chunk_records:
            name = chunk.symbol_name or (
                Path(chunk.file_path).name if chunk.file_path else "unnamed"
            )
            stype = chunk.symbol_type or "chunk"
            symbols_list.append(
                ASTSymbolItem(
                    file_path=chunk.file_path,
                    symbol_name=name,
                    symbol_type=stype,
                    line_number=chunk.line_number,
                    parent_symbol=chunk.parent_symbol,
                    end_line=chunk.end_line,
                    content=chunk.content,
                )
            )
    except Exception as fetch_err:
        logger.warning(
            "Failed fetching symbols for repo '%s': %s", target_repo_uuid, fetch_err
        )

    return ASTSymbolsIndexResponse(
        repo_id=str(target_repo_uuid) if target_repo_uuid else clean_repo_id,
        commit_sha=target_commit_sha,
        summary=ASTSymbolsSummary(
            total_files_parsed=total_files,
            total_nodes_indexed=total_nodes,
            by_type=by_type,
        ),
        symbols=symbols_list,
    )
