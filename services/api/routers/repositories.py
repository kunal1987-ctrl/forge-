"""FastAPI router for repository management and ingestion."""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, BackgroundTasks, status
from pydantic import BaseModel, Field

from services.api.core.database import async_session_factory
from services.api.forge.retrieval.ingest import IngestionPipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repositories", tags=["Repositories"])


class IngestRequest(BaseModel):
    """Payload to trigger repository cloning and RAG ingestion."""

    repo_url: str = Field(..., description="Git clone URL or local repository path", min_length=1)
    branch: str = Field("main", description="Target Git branch to index")


class IngestResponse(BaseModel):
    """Acknowledgement response for asynchronous ingestion dispatch."""

    status: str
    repo_url: str


async def _run_ingestion_worker(repo_url: str, branch: str) -> None:
    """Background worker execution for repository cloning and DB indexing."""
    try:
        async with async_session_factory() as session:
            pipeline = IngestionPipeline(session)
            repo_id = await pipeline.ingest_repository(repo_url=repo_url, branch=branch)
            logger.info("Background ingestion completed successfully for repo_id='%s'", repo_id)
    except Exception as exc:
        logger.exception("Background repository ingestion failed for '%s': %s", repo_url, exc)


@router.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_repository(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
) -> IngestResponse:
    """Trigger asynchronous cloning, AST parsing, embedding generation, and DB indexing.

    Args:
        request: IngestRequest specifying repo_url and target branch.
        background_tasks: FastAPI background tasks queue to prevent HTTP timeout.

    Returns:
        IngestResponse confirming ingestion has started.
    """
    logger.info("Enqueuing background ingestion for '%s' (branch: '%s')", request.repo_url, request.branch)
    background_tasks.add_task(_run_ingestion_worker, request.repo_url, request.branch)

    return IngestResponse(
        status="ingestion_started",
        repo_url=request.repo_url,
    )
