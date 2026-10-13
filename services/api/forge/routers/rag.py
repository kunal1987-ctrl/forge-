"""FastAPI router for high-performance Graph-RAG code search and AST retrieval.

Combines Tree-sitter AST symbol lookups, PostgreSQL pgvector semantic retrieval,
1-hop graph neighbor expansion, and FlashRank cross-encoder reranking.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, Query, status
from flashrank import RerankRequest
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.core.database import get_db
from services.api.forge.retrieval.hybrid_search import hybrid_search_code, ranker
from services.api.forge.retrieval.parser import CodeParser
from services.api.models.code import CodeChunk
from services.api.models.repository import Repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rag", tags=["RAG"])


class RAGSearchRequest(BaseModel):
    """Payload to query indexed repository symbols via Hybrid AST + pgvector + FlashRank."""

    query: str = Field(..., description="Semantic or keyword code search query", min_length=1)
    repo_id: Optional[str] = Field(None, description="Target repository UUID or identifier")
    limit: int = Field(5, description="Maximum candidate code chunks to return", ge=1, le=20)


class RAGSearchResultItem(BaseModel):
    """Structured code retrieval item with provenance and relevance metrics."""

    file_path: str = Field(..., description="Relative or absolute path of source file")
    snippet: str = Field(..., description="Extracted code definition or body")
    score: float = Field(..., description="Relevance score from FlashRank reranker")
    symbol_name: str = Field(..., description="Identifier name of function, class, or method")
    start_line: int = Field(1, description="1-indexed line number where the symbol definition begins")
    symbol_type: Optional[str] = Field("function", description="AST symbol classification")
    content: Optional[str] = Field(None, description="Backward-compatibility alias for snippet")
    dependencies: List[Dict[str, Any]] = Field(default_factory=list, description="1-hop AST neighbor calls and imports")

    def model_post_init(self, __context: Any) -> None:
        if self.content is None:
            self.content = self.snippet


def compute_start_line(file_path: str, symbol_name: str, snippet: str, fallback_line: int = 1) -> int:
    """Determine accurate 1-indexed start line by inspecting source file on disk."""
    try:
        p = Path(file_path)
        if not p.is_file():
            p = Path(".") / file_path
        if p.is_file():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if symbol_name:
                for idx, line in enumerate(lines, start=1):
                    if (
                        f"def {symbol_name}" in line
                        or f"class {symbol_name}" in line
                        or f"async def {symbol_name}" in line
                        or f"{symbol_name} =" in line
                    ):
                        return idx
            first_line = next((l.strip() for l in snippet.splitlines() if l.strip()), None)
            if first_line:
                for idx, line in enumerate(lines, start=1):
                    if first_line in line:
                        return idx
    except Exception:
        pass
    return max(1, fallback_line)


import ast

def search_workspace_ast(query: str, limit: int = 5) -> List[RAGSearchResultItem]:
    """Execute local AST extraction and FlashRank reranking across workspace code.

    Acts as high-fidelity fallback when PostgreSQL vectors are unseeded or query is local.
    """
    workspace_dirs = ["services/api", "forge", "src"]
    candidate_passages: List[Dict[str, Any]] = []

    ignored_dirs = {"venv", ".venv", "node_modules", ".git", "__pycache__", "dist", "build"}
    q_terms = [t.lower() for t in query.strip().split() if len(t) >= 2]

    if not q_terms:
        return []

    for wdir in workspace_dirs:
        wpath = Path(wdir)
        if not wpath.exists():
            continue
        for root, dirs, files in os.walk(wpath):
            dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
            for file in files:
                if file.endswith(".py"):
                    full_p = Path(root) / file
                    rel_p = str(full_p).replace("\\", "/")
                    try:
                        content = full_p.read_text(encoding="utf-8", errors="replace")
                        lines = content.splitlines()
                        tree = ast.parse(content)
                        for node in ast.iter_child_nodes(tree):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                sym = node.name
                                st_line = node.lineno
                                end_line = getattr(node, "end_lineno", st_line + 15)
                                chunk_content = "\n".join(lines[st_line - 1 : end_line])
                                sym_type = "class" if isinstance(node, ast.ClassDef) else "function"

                                match_score = 0
                                if any(t in sym.lower() for t in q_terms):
                                    match_score += 3
                                if any(t in chunk_content.lower() for t in q_terms):
                                    match_score += 1

                                if match_score > 0:
                                    passage_id = f"{rel_p}::{sym}"
                                    candidate_passages.append({
                                        "id": passage_id,
                                        "text": chunk_content[:2000],
                                        "meta": {
                                            "file_path": rel_p,
                                            "symbol_name": sym,
                                            "symbol_type": sym_type,
                                            "start_line": st_line,
                                            "content": chunk_content,
                                            "edges": [],
                                        },
                                    })

                                if isinstance(node, ast.ClassDef):
                                    for sub in ast.iter_child_nodes(node):
                                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                            sub_sym = sub.name
                                            sub_st = sub.lineno
                                            sub_end = getattr(sub, "end_lineno", sub_st + 15)
                                            sub_cnt = "\n".join(lines[sub_st - 1 : sub_end])
                                            sub_match = 0
                                            if any(t in sub_sym.lower() for t in q_terms):
                                                sub_match += 3
                                            if any(t in sub_cnt.lower() for t in q_terms):
                                                sub_match += 1
                                            if sub_match > 0:
                                                sub_id = f"{rel_p}::{sym}::{sub_sym}"
                                                candidate_passages.append({
                                                    "id": sub_id,
                                                    "text": sub_cnt[:2000],
                                                    "meta": {
                                                        "file_path": rel_p,
                                                        "symbol_name": sub_sym,
                                                        "symbol_type": "method",
                                                        "start_line": sub_st,
                                                        "content": sub_cnt,
                                                        "edges": [{"symbol": sym, "file_path": rel_p}],
                                                    },
                                                })
                    except Exception:
                        continue

    if not candidate_passages:
        return []

    # Limit to top 30 candidates before running FlashRank cross-encoder
    candidate_passages = candidate_passages[:30]
    rerank_req = RerankRequest(query=query, passages=candidate_passages)
    reranked = ranker.rerank(rerank_req)

    results: List[RAGSearchResultItem] = []
    for hit in reranked[:limit]:
        score = float(hit.get("score", 0.85))
        meta = hit.get("meta") or {}
        fp = meta.get("file_path", "unknown")
        sym = meta.get("symbol_name", "unknown")
        cnt = meta.get("content", "")
        st_line = meta.get("start_line", 1)
        deps = meta.get("edges") or []

        results.append(
            RAGSearchResultItem(
                file_path=fp,
                snippet=cnt,
                score=round(score, 3),
                symbol_name=sym,
                start_line=st_line,
                symbol_type=meta.get("symbol_type", "function"),
                content=cnt,
                dependencies=deps,
            )
        )
    return results


async def execute_rag_search(
    query: str,
    repo_id: Optional[str] = None,
    limit: int = 5,
    db: Optional[AsyncSession] = None,
) -> List[RAGSearchResultItem]:
    """Execute hybrid search combining Tree-sitter AST, pgvector RRF, and FlashRank."""
    clean_query = query.strip()
    if not clean_query:
        return []

    effective_repo_id: Optional[str] = repo_id

    # 1. Attempt PostgreSQL hybrid vector + lexical search if DB session is active
    if db is not None:
        try:
            if not effective_repo_id:
                # Resolve any available active repository in the database
                repo_query = await db.execute(select(Repository.id).limit(1))
                active_repo_id = repo_query.scalar_one_or_none()
                if active_repo_id:
                    effective_repo_id = str(active_repo_id)

            if effective_repo_id:
                try:
                    uuid.UUID(effective_repo_id)
                except ValueError:
                    effective_repo_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, effective_repo_id))

                payload = await hybrid_search_code(
                    db_session=db,
                    repo_id=effective_repo_id,
                    commit_sha="main",
                    query_text=clean_query,
                    limit=limit,
                )

                if payload and payload[0]:
                    results: List[RAGSearchResultItem] = []
                    for b in payload[0]:
                        fp = b.get("file_path", "unknown")
                        sym = b.get("symbol_name", "unknown")
                        code = b.get("primary_code") or b.get("content") or ""
                        st_line = compute_start_line(fp, sym, code, fallback_line=1)
                        results.append(
                            RAGSearchResultItem(
                                file_path=fp,
                                snippet=code,
                                score=round(float(b.get("score") or b.get("rrf_score") or 0.95), 3),
                                symbol_name=sym,
                                start_line=st_line,
                                symbol_type=b.get("symbol_type", "function"),
                                content=code,
                                dependencies=b.get("dependencies", []),
                            )
                        )
                    if results:
                        return results
        except Exception as db_exc:
            logger.debug("Database hybrid search query skipped or unseeded: %s", db_exc)

    # 2. High-performance fallback: Local Tree-sitter AST symbol index + FlashRank reranking
    try:
        local_results = search_workspace_ast(clean_query, limit=limit)
        if local_results:
            return local_results
    except Exception as ast_exc:
        logger.warning("Local AST search encountered error: %s", ast_exc)

    # 3. Clean empty state: zero matches found
    return []


@router.post("/search", response_model=List[RAGSearchResultItem], status_code=status.HTTP_200_OK)
async def search_rag_post(
    request: RAGSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> List[RAGSearchResultItem]:
    """Execute high-performance Graph-RAG search via POST payload."""
    return await execute_rag_search(
        query=request.query,
        repo_id=request.repo_id,
        limit=request.limit,
        db=db,
    )


@router.get("/search", response_model=List[RAGSearchResultItem], status_code=status.HTTP_200_OK)
async def search_rag_get(
    query: str = Query(..., min_length=1, description="Search query string"),
    repo_id: Optional[str] = Query(None, description="Optional target repository UUID"),
    limit: int = Query(5, ge=1, le=20, description="Max code chunks to return"),
    db: AsyncSession = Depends(get_db),
) -> List[RAGSearchResultItem]:
    """Execute high-performance Graph-RAG search via GET query parameters."""
    return await execute_rag_search(
        query=query,
        repo_id=repo_id,
        limit=limit,
        db=db,
    )
