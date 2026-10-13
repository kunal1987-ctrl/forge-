"""FastAPI router for repository management and ingestion."""

from __future__ import annotations

import logging

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.core.database import get_db
from services.api.forge.retrieval.ingest import IngestionPipeline

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Repositories"])


class IngestRequest(BaseModel):
    """Payload to trigger repository cloning and RAG ingestion."""

    repo_url: str = Field(..., description="Git clone URL or local repository path", min_length=1)
    branch: str = Field("main", description="Target Git branch to index")


class IngestResponse(BaseModel):
    """Response returned upon repository ingestion."""

    repo_id: str
    total_chunks: int = 0
    status: str = "ingestion_completed"
    repo_url: Optional[str] = None


@router.post(
    "/api/repositories/ingest", response_model=IngestResponse, status_code=status.HTTP_200_OK
)
@router.post("/api/repos/ingest", response_model=IngestResponse, status_code=status.HTTP_200_OK)
async def ingest_repository(
    request: IngestRequest,
    db: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """Clones repository, parses AST symbols, and inserts vectors into PostgreSQL.

    Args:
        request: IngestRequest specifying repo_url and target branch.
        db: Active AsyncSession database session.

    Returns:
        IngestResponse confirming ingestion has finished with repo_id and total_chunks.
    """
    logger.info("Ingesting repository '%s' (branch: '%s')", request.repo_url, request.branch)
    try:
        pipeline = IngestionPipeline(db)
        res = await pipeline.ingest_repository(repo_url=request.repo_url, branch=request.branch)
        repo_id = str(res)
        total_chunks = getattr(res, "total_chunks", 0)
        return IngestResponse(
            repo_id=repo_id,
            total_chunks=total_chunks,
            status="ingestion_completed",
            repo_url=request.repo_url,
        )
    except Exception as exc:
        logger.exception("Repository ingestion failed for '%s': %s", request.repo_url, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Repository ingestion failed: {exc}",
        )
