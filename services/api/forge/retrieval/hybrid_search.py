"""Postgres Hybrid RAG search module using Reciprocal Rank Fusion (RRF).

Combines dense vector similarity search (pgvector) and sparse full-text lexical
search (PostgreSQL tsvector) inside a single SQL query via Reciprocal Rank Fusion.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Union

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion (RRF) query template in PostgreSQL
RRF_QUERY = text(
    """
    WITH semantic_search AS (
        SELECT
            id,
            file_path,
            symbol_name,
            content,
            ROW_NUMBER() OVER (
                ORDER BY embedding <=> CAST(:vector AS vector)
            ) AS rank
        FROM code_chunks
        WHERE repo_id = :repo_id
          AND commit_sha = :commit_sha
          AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:vector AS vector)
        LIMIT 50
    ),
    lexical_search AS (
        SELECT
            id,
            file_path,
            symbol_name,
            content,
            ROW_NUMBER() OVER (
                ORDER BY ts_rank_cd(tsv_content, plainto_tsquery('english', :query)) DESC
            ) AS rank
        FROM code_chunks
        WHERE repo_id = :repo_id
          AND commit_sha = :commit_sha
          AND tsv_content IS NOT NULL
          AND tsv_content @@ plainto_tsquery('english', :query)
        ORDER BY ts_rank_cd(tsv_content, plainto_tsquery('english', :query)) DESC
        LIMIT 50
    )
    SELECT
        COALESCE(semantic_search.id, lexical_search.id) AS id,
        COALESCE(semantic_search.file_path, lexical_search.file_path) AS file_path,
        COALESCE(semantic_search.symbol_name, lexical_search.symbol_name) AS symbol_name,
        COALESCE(semantic_search.content, lexical_search.content) AS content,
        COALESCE(1.0 / (60 + semantic_search.rank), 0.0) +
        COALESCE(1.0 / (60 + lexical_search.rank), 0.0) AS rrf_score
    FROM semantic_search
    FULL OUTER JOIN lexical_search ON semantic_search.id = lexical_search.id
    ORDER BY rrf_score DESC
    LIMIT 15;
    """
)


async def hybrid_search_code(
    session: AsyncSession,
    repo_id: Union[uuid.UUID, str],
    commit_sha: str,
    query_text: str,
    query_vector: List[float],
) -> List[Dict[str, Any]]:
    """Perform hybrid RAG code search using Reciprocal Rank Fusion (RRF) in PostgreSQL.

    Executes a raw SQL query with two Common Table Expressions (CTEs):
    - CTE 1 ('semantic_search'): Identifies top 50 semantic matches via cosine distance
      (<=>) in pgvector for the target repository revision.
    - CTE 2 ('lexical_search'): Identifies top 50 lexical matches using PostgreSQL full-text
      search (ts_rank_cd on tsv_content) for the target repository revision.
    - Final SELECT: Performs a full outer join on chunk id and fuses rankings via
      RRF: 1.0 / (60 + rank_semantic) + 1.0 / (60 + rank_lexical), returning the top 15 results.

    Args:
        session: Active SQLAlchemy AsyncSession connection.
        repo_id: UUID of the repository to filter against.
        commit_sha: Git commit SHA string to scope the revision.
        query_text: Natural language or keyword query string for full-text search.
        query_vector: 1536-dimensional embedding vector (e.g., from OpenAI text-embedding-3).

    Returns:
        A list of dictionaries representing the top matching code chunks, each containing:
            - 'file_path': str, relative path of the file.
            - 'symbol_name': Optional[str], function/class symbol name.
            - 'content': str, raw code chunk content.
            - 'id': str, unique chunk identifier UUID.
            - 'rrf_score': float, calculated reciprocal rank fusion score.
    """
    # Normalize repo_id to UUID
    parsed_repo_id = uuid.UUID(str(repo_id)) if not isinstance(repo_id, uuid.UUID) else repo_id

    # Format vector into postgres-compatible array literal [x1, x2, ...]
    if isinstance(query_vector, (list, tuple)):
        vector_str = f"[{','.join(str(float(x)) for x in query_vector)}]"
    else:
        vector_str = str(query_vector)

    params = {
        "repo_id": parsed_repo_id,
        "commit_sha": commit_sha,
        "query": query_text,
        "vector": vector_str,
    }

    logger.debug(
        "Executing hybrid_search_code for repo_id=%s, commit_sha=%s, query='%s'",
        parsed_repo_id,
        commit_sha,
        query_text,
    )

    result = await session.execute(RRF_QUERY, params)
    rows = result.mappings().all()

    formatted_results: List[Dict[str, Any]] = []
    for row in rows:
        formatted_results.append(
            {
                "id": str(row["id"]),
                "file_path": row["file_path"],
                "symbol_name": row["symbol_name"],
                "content": row["content"],
                "rrf_score": float(row["rrf_score"]),
            }
        )

    return formatted_results
