from __future__ import annotations

import logging
import uuid
from typing import Any, Literal, Optional

from flashrank import Ranker, RerankRequest
from pgvector.sqlalchemy import Vector
from pydantic import BaseModel
from sqlalchemy import String, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.models.code import CodeChunk
from services.api.models.repository import RepositoryRevision

logger = logging.getLogger(__name__)

# Initialize the local CPU reranker globally so the model stays loaded in memory
ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2")


class RetrievedCodeChunk(BaseModel):
    """Pydantic model representing a retrieved code chunk with provenance and retrieval role."""

    file_path: str
    symbol_name: str
    symbol_type: str
    parent_symbol: str | None = None
    content: str
    retrieval_role: Literal["seed", "parent_class", "dependency"] = "seed"
    score: float | None = None

    def get(self, key: str, default: Any = None) -> Any:
        """Dictionary compatibility helper."""
        return getattr(self, key, default)

    def __getitem__(self, item: str) -> Any:
        """Subscript compatibility helper."""
        try:
            return getattr(self, item)
        except AttributeError:
            raise KeyError(item)


class SearchResultPayload(tuple):
    """2-tuple container of (enriched_bundles, markdown_string) supporting unpacking and property access."""

    def __new__(cls, bundles: list[dict], markdown: str) -> "SearchResultPayload":
        return super().__new__(cls, (bundles, markdown))

    def __init__(self, bundles: list[dict], markdown: str):
        self.bundles = bundles
        self.markdown = markdown


async def _resolve_indexed_commit(
    session: AsyncSession,
    repo_id: Any,
    requested_commit: str,
) -> str:
    """Resolve common branch aliases to the most recently indexed immutable revision."""
    if requested_commit and requested_commit.strip().lower() not in {
        "head",
        "main",
        "master",
        "default",
    }:
        return requested_commit

    revision_result = await session.execute(
        select(RepositoryRevision.commit_sha)
        .where(RepositoryRevision.repo_id == repo_id)
        .order_by(RepositoryRevision.indexed_at.desc().nullslast())
        .limit(1)
    )
    indexed_commit = revision_result.scalar_one_or_none()
    if indexed_commit:
        return str(indexed_commit)

    chunk_result = await session.execute(
        select(CodeChunk.commit_sha)
        .where(CodeChunk.repo_id == repo_id)
        .order_by(CodeChunk.commit_sha.desc())
        .limit(1)
    )
    indexed_commit = chunk_result.scalar_one_or_none()
    return str(indexed_commit) if indexed_commit else requested_commit


async def _query_embedding(query_text: str) -> Optional[list[float]]:
    """Create a real query embedding when configured; lexical search remains available offline."""
    try:
        from services.api.core.config import settings

        api_key = settings.OPENAI_API_KEY
    except (ImportError, AttributeError):
        api_key = None

    if not api_key:
        return None

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, timeout=12.0, max_retries=1)
        result = await client.embeddings.create(
            model="text-embedding-3-small",
            input=query_text[:8000],
            timeout=12.0,
        )
        return list(result.data[0].embedding)
    except Exception as exc:
        logger.warning("Could not embed RAG query; using lexical retrieval: %s", exc)
        return None


def truncate_dependency_code(code: str) -> str:
    """Truncate dependency code to its top-level signature, docstring, and return statement.

    Guardrail against context window saturation when dependencies are massive utility functions.
    """
    lines = code.splitlines()
    if len(lines) <= 6:
        return code

    signature_lines: list[str] = []
    docstring_lines: list[str] = []
    return_lines: list[str] = []

    in_docstring = False
    docstring_delim = None

    i = 0
    # 1. Capture signature (def/async def ... :)
    while i < len(lines):
        line = lines[i]
        signature_lines.append(line)
        if ":" in line and not line.strip().startswith("#"):
            i += 1
            break
        i += 1

    # 2. Capture docstring if immediately following
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not in_docstring:
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_docstring = True
                docstring_delim = stripped[:3]
                docstring_lines.append(line)
                # Single-line docstring
                if stripped.count(docstring_delim) >= 2 and len(stripped) > 3:
                    in_docstring = False
                    i += 1
                    break
            elif stripped:
                break
        else:
            docstring_lines.append(line)
            if docstring_delim and docstring_delim in stripped:
                in_docstring = False
                i += 1
                break
        i += 1

    # 3. Search for return statement towards function end
    for line in reversed(lines[i:]):
        stripped = line.strip()
        if stripped.startswith("return ") or stripped == "return":
            return_lines.insert(0, line)
            break

    result_parts = signature_lines + docstring_lines
    result_parts.append("    # ... [body truncated for context budget defense] ...")
    if return_lines:
        result_parts.extend(return_lines)

    return "\n".join(result_parts)


async def hybrid_search_code(
    db_session: Optional[AsyncSession] = None,
    repo_id: str = "",
    commit_sha: str = "",
    query_text: str = "",
    query_vector: Optional[list[float]] = None,
    limit: int = 5,
    seed_limit: Optional[int] = None,
    session: Optional[AsyncSession] = None,
) -> SearchResultPayload:
    """Execute Graph-RAG retrieval combining Reciprocal Rank Fusion, FlashRank reranking,
    and 1-hop AST neighbor expansion (Enclosing Class + Called Dependencies).

    Args:
        db_session: Active SQLAlchemy AsyncSession.
        repo_id: Repository UUID or string.
        commit_sha: Commit SHA string.
        query_text: Natural language or keyword query string.
        query_vector: 1536-dimensional float vector list.
        limit: Number of top seeds to expand (default: 5).
        seed_limit: Alias for limit.
        session: Backwards-compatibility alias for db_session.

    Returns:
        SearchResultPayload (tuple of [enriched_bundles, rendered_markdown]).
    """
    active_session = db_session if db_session is not None else session
    if active_session is None:
        raise ValueError("Either db_session or session must be provided.")

    effective_limit = seed_limit if seed_limit is not None else limit
    if query_vector is not None and not any(query_vector):
        query_vector = None

    # Normalize repo_id to UUID object
    parsed_repo_id: Any
    try:
        parsed_repo_id = uuid.UUID(str(repo_id))
    except (ValueError, AttributeError):
        parsed_repo_id = repo_id

    commit_sha = await _resolve_indexed_commit(active_session, parsed_repo_id, commit_sha)
    if query_vector is None:
        query_vector = await _query_embedding(query_text)
    semantic_enabled = query_vector is not None
    vector_param = query_vector or [0.0] * 1536

    # -------------------------------------------------------------------------
    # STAGE 1: Seed Retrieval (Postgres RRF + FlashRank Cross-Encoder Rerank)
    # -------------------------------------------------------------------------
    rrf_stmt = text("""
    WITH semantic_search AS (
        SELECT id, 
               RANK() OVER (ORDER BY embedding <=> :vector) as rank
        FROM code_chunks
        WHERE repo_id = :repo_id AND commit_sha = :commit_sha
          AND :use_semantic
        LIMIT 30
    ),
    lexical_search AS (
        SELECT id, 
               RANK() OVER (ORDER BY ts_rank_cd(tsv_content, plainto_tsquery('english', :query)) DESC) as rank
        FROM code_chunks
        WHERE repo_id = :repo_id AND commit_sha = :commit_sha 
          AND tsv_content @@ plainto_tsquery('english', :query)
        LIMIT 30
    )
    SELECT c.file_path,
           c.symbol_name,
           c.symbol_type,
           c.parent_symbol,
           c.content,
           COALESCE(1.0 / (60 + s.rank), 0.0) + COALESCE(1.0 / (60 + l.rank), 0.0) AS rrf_score
    FROM code_chunks c
    LEFT JOIN semantic_search s ON c.id = s.id
    LEFT JOIN lexical_search l ON c.id = l.id
    WHERE s.id IS NOT NULL OR l.id IS NOT NULL
    ORDER BY rrf_score DESC
    LIMIT 30;
    """).bindparams(bindparam("vector", type_=Vector(1536)))

    result = await active_session.execute(
        rrf_stmt,
        {
            "repo_id": parsed_repo_id,
            "commit_sha": commit_sha,
            "vector": vector_param,
            "use_semantic": semantic_enabled,
            "query": query_text,
        },
    )

    broad_results = [dict(row) for row in result.mappings().all()]
    if not broad_results:
        return SearchResultPayload([], "")

    passages = [
        {
            "id": f"{row.get('file_path', '')}::{row.get('symbol_name') or ''}",
            "text": row.get("content", ""),
            "meta": row,
        }
        for row in broad_results
    ]

    rerank_req = RerankRequest(query=query_text, passages=passages)
    reranked_results = ranker.rerank(rerank_req)

    top_seeds = [hit["meta"] for hit in reranked_results[:effective_limit]]
    if not top_seeds:
        return SearchResultPayload([], "")

    # -------------------------------------------------------------------------
    # STAGE 2: 1-Hop Graph Neighbor Expansion (Batched PostgreSQL CTE Query)
    # -------------------------------------------------------------------------
    seed_qualified_symbols = [
        f"{s['file_path']}::{s['symbol_name']}"
        for s in top_seeds
        if s.get("file_path") and s.get("symbol_name")
    ]
    seed_raw_symbols = [s["symbol_name"] for s in top_seeds if s.get("symbol_name")]
    parent_symbols = list({s["parent_symbol"] for s in top_seeds if s.get("parent_symbol")})
    parent_files = list({s["file_path"] for s in top_seeds if s.get("parent_symbol")})

    parent_map: dict[tuple[str, str], str] = {}
    dependencies_by_source: dict[str, list[dict[str, Any]]] = {}

    # Only run neighbor query if there are parents or seed symbols to expand
    if parent_symbols or seed_qualified_symbols or seed_raw_symbols:
        neighbor_stmt = text("""
        WITH enclosing_scopes AS (
            SELECT c.file_path,
                   c.symbol_name,
                   c.symbol_type,
                   c.parent_symbol,
                   c.content,
                   'parent' AS neighbor_type,
                   c.file_path AS source_ref,
                   c.symbol_name AS match_key,
                   1::bigint AS rank_num
            FROM code_chunks c
            WHERE c.repo_id = :repo_id
              AND c.commit_sha = :commit_sha
              AND c.symbol_name = ANY(:parent_symbols)
              AND c.file_path = ANY(:parent_files)
        ),
        seed_calls AS (
            SELECT e.source_symbol,
                   e.target_symbol
            FROM code_edges e
            WHERE e.repo_id = :repo_id
              AND e.commit_sha = :commit_sha
              AND e.edge_type = 'calls'
              AND (
                  e.source_symbol = ANY(:seed_qualified_symbols) 
                  OR e.source_symbol = ANY(:seed_raw_symbols)
              )
        ),
        callee_chunks AS (
            SELECT c.file_path,
                   c.symbol_name,
                   c.symbol_type,
                   c.parent_symbol,
                   c.content,
                   'dependency' AS neighbor_type,
                   sc.source_symbol AS source_ref,
                   sc.target_symbol AS match_key,
                   ROW_NUMBER() OVER (
                       PARTITION BY sc.source_symbol
                       ORDER BY 
                         -- Disambiguation: resolve matching symbols in the same file first
                         CASE WHEN sc.source_symbol LIKE c.file_path || '%' THEN 0 ELSE 1 END,
                         c.id
                   ) AS rank_num
            FROM seed_calls sc
            JOIN code_chunks c
              ON c.repo_id = :repo_id
             AND c.commit_sha = :commit_sha
             AND c.symbol_name = sc.target_symbol
        )
        SELECT file_path, symbol_name, symbol_type, parent_symbol, content, neighbor_type, source_ref, match_key
        FROM enclosing_scopes
        UNION ALL
        SELECT file_path, symbol_name, symbol_type, parent_symbol, content, neighbor_type, source_ref, match_key
        FROM callee_chunks
        WHERE rank_num <= 2;
        """).bindparams(
            bindparam("parent_symbols", type_=ARRAY(String)),
            bindparam("parent_files", type_=ARRAY(String)),
            bindparam("seed_qualified_symbols", type_=ARRAY(String)),
            bindparam("seed_raw_symbols", type_=ARRAY(String)),
        )

        neighbor_res = await active_session.execute(
            neighbor_stmt,
            {
                "repo_id": parsed_repo_id,
                "commit_sha": commit_sha,
                "parent_symbols": parent_symbols or [],
                "parent_files": parent_files or [],
                "seed_qualified_symbols": seed_qualified_symbols or [],
                "seed_raw_symbols": seed_raw_symbols or [],
            },
        )

        for row in neighbor_res.mappings().all():
            n_type = row["neighbor_type"]
            if n_type == "parent":
                parent_map[(row["file_path"], row["symbol_name"])] = row["content"]
            elif n_type == "dependency":
                src = row["source_ref"]
                dependencies_by_source.setdefault(src, []).append(dict(row))

    # -------------------------------------------------------------------------
    # STAGE 3: Context Bundle Packing & Token Guard Defense
    # -------------------------------------------------------------------------
    enriched_bundles: list[dict[str, Any]] = []

    for seed in top_seeds:
        seed_fp = seed.get("file_path", "")
        seed_sym = seed.get("symbol_name") or ""
        qual_sym = f"{seed_fp}::{seed_sym}"

        # 1. Enclosing scope
        enclosing_code: Optional[str] = None
        if seed.get("parent_symbol"):
            enclosing_code = parent_map.get((seed_fp, seed["parent_symbol"]))

        # 2. Dependencies (max 2 per seed)
        raw_deps = dependencies_by_source.get(qual_sym, []) or dependencies_by_source.get(
            seed_sym, []
        )
        formatted_deps: list[dict[str, str]] = []
        for dep in raw_deps[:2]:
            formatted_deps.append(
                {
                    "symbol": dep["symbol_name"],
                    "file_path": dep["file_path"],
                    "code": dep["content"],
                }
            )

        bundle = {
            "file_path": seed_fp,
            "symbol_name": seed_sym,
            "symbol_type": seed.get("symbol_type", "function"),
            "primary_code": seed.get("content", ""),
            "content": seed.get("content", ""),  # Backwards-compatibility alias
            "parent_symbol": seed.get("parent_symbol"),
            "enclosing_context": enclosing_code,
            "dependencies": formatted_deps,
        }
        enriched_bundles.append(bundle)

    # Token guard defense: if aggregate characters exceed 24,000 (~6,000 tokens), truncate dependencies
    total_chars = sum(
        len(b.get("primary_code", ""))
        + len(b.get("enclosing_context") or "")
        + sum(len(d.get("code", "")) for d in b.get("dependencies", []))
        for b in enriched_bundles
    )

    if total_chars > 24000:
        logger.info(
            "Context budget defense triggered: aggregate characters (%d) > 24,000 (~6,000 tokens). Truncating dependency bodies.",
            total_chars,
        )
        for b in enriched_bundles:
            for dep in b.get("dependencies", []):
                dep["code"] = truncate_dependency_code(dep["code"])

    # -------------------------------------------------------------------------
    # STAGE 4: Pre-Rendered Markdown Formatting
    # -------------------------------------------------------------------------
    markdown_sections: list[str] = []
    for b in enriched_bundles:
        sec: list[str] = [
            f"### [PRIMARY TARGET]: {b['file_path']}::{b['symbol_name']}\n```python\n{b['primary_code'].strip()}\n```"
        ]
        if b.get("enclosing_context"):
            sec.append(
                f"#### Enclosing Class Context:\n```python\n{b['enclosing_context'].strip()}\n```"
            )

        for dep in b.get("dependencies", []):
            sec.append(
                f"#### Dependency Context: {dep['symbol']} ({dep['file_path']})\n```python\n{dep['code'].strip()}\n```"
            )

        markdown_sections.append("\n\n".join(sec))

    rendered_markdown = "\n\n---\n\n".join(markdown_sections)

    return SearchResultPayload(enriched_bundles, rendered_markdown)
