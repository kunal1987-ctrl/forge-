"""FastAPI router for Graph-RAG code search and retrieval."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.core.database import get_db
from services.api.forge.routers.rag import (
    RAGSearchResultItem,
    execute_rag_search,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/retrieval", tags=["Retrieval"])


class RAGSearchRequest(BaseModel):
    """Payload to query indexed repository symbols via Hybrid RRF + FlashRank."""

    repo_id: str = Field(..., description="Target repository UUID")
    query: str = Field(..., description="Semantic or keyword code search query", min_length=1)
    limit: int = Field(5, description="Maximum candidate code chunks to return", ge=1, le=20)


@router.post("/search", response_model=List[RAGSearchResultItem], status_code=status.HTTP_200_OK)
async def search_codebase(
    request: RAGSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> List[RAGSearchResultItem]:
    """Execute hybrid vector + full-text search with 1-hop AST neighbor expansion and FlashRank reranking."""
    logger.info(
        "Executing codebase search for repo_id='%s', query='%s'", request.repo_id, request.query
    )
    return await execute_rag_search(
        query=request.query,
        repo_id=request.repo_id,
        limit=request.limit,
        db=db,
    )
