"""Codebase Ingestion Pipeline for FORGE.

Re-exports IngestionPipeline and supporting components from services.api.forge.services.ingestion
for backward compatibility across the codebase.
"""

from __future__ import annotations

from services.api.forge.services.ingestion import (
    GitIngestionError,
    IngestionPipeline,
    IngestionResult,
    RepositoryIngestionService,
    normalize_git_url,
    safe_git_clone,
    safe_rmtree,
    sanitize_git_error,
)

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
