"""Codebase Ingestion Pipeline for FORGE.

Clones a target Git repository, parses AST code symbols (classes, functions)
and dependency import edges using Tree-Sitter, batches vector embeddings via
OpenAI text-embedding-3-small, and populates PostgreSQL with pgvector embeddings
and full-text tsvectors.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.forge.retrieval.parser import CodeParser
from services.api.models.code import CodeChunk, CodeEdge
from services.api.models.repository import Repository, RepositoryRevision

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Pipelines cloning, AST chunking, embedding generation, and DB ingestion."""

    def __init__(self, db_session: AsyncSession) -> None:
        """Initialize pipeline with an active SQLAlchemy AsyncSession.

        Args:
            db_session: Active AsyncSession database connection.
        """
        self.session = db_session
        self.parser = CodeParser()

    async def _generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate OpenAI embeddings for a list of text strings in batches.

        Args:
            texts: List of code strings to embed.

        Returns:
            List of 1536-dimensional float embedding vectors.
        """
        if not texts:
            return []

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.info("OPENAI_API_KEY not set. Using zero-vector embeddings.")
            return [[0.0] * 1536 for _ in texts]

        embeddings: List[List[float]] = []
        client = AsyncOpenAI(api_key=api_key)
        batch_size = 64

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            # Truncate text block to avoid token limit errors
            clean_batch = [t[:8000] for t in batch]
            try:
                response = await client.embeddings.create(
                    model="text-embedding-3-small",
                    input=clean_batch,
                )
                embeddings.extend([item.embedding for item in response.data])
            except Exception as exc:
                logger.warning("OpenAI embedding API failed on batch (%s). Using fallback vectors.", exc)
                embeddings.extend([[0.0] * 1536 for _ in batch])

        return embeddings

    async def ingest_repository(self, repo_url: str, branch: str = "main") -> str:
        """Clone repository, extract AST symbols and import edges, and persist to PostgreSQL.

        Args:
            repo_url: Git clone URL or local directory path.
            branch: Git branch to clone. Defaults to 'main'.

        Returns:
            The newly created repository UUID string.

        Raises:
            RuntimeError: If Git clone or revision resolution fails.
        """
        logger.info("Starting repository ingestion for '%s' (branch: '%s')", repo_url, branch)
        clone_dir = tempfile.mkdtemp(prefix="forge_ingest_")

        try:
            # 1. Clone repository to ephemeral directory
            clone_cmd = ["git", "clone", "--depth", "1", "-b", branch, repo_url, clone_dir]
            logger.info("Cloning repository into '%s'", clone_dir)
            result = subprocess.run(clone_cmd, capture_output=True, text=True)

            if result.returncode != 0:
                # If specific branch clone failed, try default clone
                logger.warning("Cloning with specific branch failed (%s). Retrying default clone.", result.stderr.strip())
                clone_cmd = ["git", "clone", "--depth", "1", repo_url, clone_dir]
                result = subprocess.run(clone_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(f"Failed to clone repository '{repo_url}': {result.stderr.strip()}")

            # 2. Get HEAD commit SHA
            sha_res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=clone_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            commit_sha = sha_res.stdout.strip()
            logger.info("Ingesting commit SHA: %s", commit_sha)

            # 3. Insert repository and revision records
            repo_name = Path(repo_url).stem or "repository"
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]

            repo = Repository(
                id=uuid.uuid4(),
                name=repo_name,
                local_path=repo_url,
                default_branch=branch,
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

            # 4. Recursively scan Python files
            ignored_dirs = {"venv", ".venv", "node_modules", ".git", "__pycache__", "dist", "build"}
            parsed_chunks: List[Dict[str, Any]] = []
            parsed_edges: List[Dict[str, str]] = []

            for root, dirs, files in os.walk(clone_dir):
                # Prune ignored and hidden directories
                dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]

                for file in files:
                    if file.endswith(".py"):
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, clone_dir).replace("\\", "/")

                        try:
                            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                                code_content = f.read()

                            # Extract symbols via CodeParser
                            file_symbols = self.parser.parse_file(rel_path, code_content)
                            if file_symbols:
                                parsed_chunks.extend(file_symbols)
                            else:
                                # Fallback chunk for module without functions/classes
                                parsed_chunks.append(
                                    {
                                        "file_path": rel_path,
                                        "symbol_name": Path(rel_path).stem,
                                        "symbol_type": "module",
                                        "start_line": 1,
                                        "end_line": len(code_content.splitlines()) or 1,
                                        "content": code_content,
                                    }
                                )

                            # Extract dependency import edges
                            file_edges = self.parser.extract_edges(code_content, source_symbol=rel_path)
                            parsed_edges.extend(file_edges)

                        except Exception as parse_err:
                            logger.warning("Error parsing file '%s': %s", rel_path, parse_err)

            logger.info(
                "Parsed %d chunks and %d import edges across codebase",
                len(parsed_chunks),
                len(parsed_edges),
            )

            # 5. Generate vector embeddings in batches
            chunk_contents = [chunk["content"] for chunk in parsed_chunks]
            embeddings = await self._generate_embeddings(chunk_contents)

            # 6. Bulk insert code_chunks into PostgreSQL
            for chunk_data, emb in zip(parsed_chunks, embeddings):
                chunk_record = CodeChunk(
                    id=uuid.uuid4(),
                    repo_id=repo.id,
                    commit_sha=commit_sha,
                    file_path=chunk_data["file_path"],
                    symbol_name=chunk_data.get("symbol_name"),
                    symbol_type=chunk_data.get("symbol_type"),
                    content=chunk_data["content"],
                    embedding=emb,
                    tsv_content=func.to_tsvector("english", chunk_data["content"]),
                )
                self.session.add(chunk_record)

            # 7. Bulk insert code_edges into PostgreSQL
            for edge_data in parsed_edges:
                edge_record = CodeEdge(
                    id=uuid.uuid4(),
                    repo_id=repo.id,
                    commit_sha=commit_sha,
                    source_symbol=edge_data["source_symbol"],
                    target_symbol=edge_data["target_symbol"],
                    edge_type=edge_data.get("edge_type", "imports"),
                )
                self.session.add(edge_record)

            # Commit all records in a single transaction
            await self.session.commit()
            logger.info("Successfully ingested repository '%s' (UUID: %s)", repo_name, repo.id)
            return str(repo.id)

        finally:
            # 8. Clean up temporary clone directory
            if os.path.exists(clone_dir):
                try:
                    shutil.rmtree(clone_dir, ignore_errors=True)
                    logger.debug("Deleted temporary clone directory '%s'", clone_dir)
                except Exception as clean_err:
                    logger.warning("Failed to remove clone directory '%s': %s", clone_dir, clean_err)
