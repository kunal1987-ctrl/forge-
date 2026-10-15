from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
from typing import AsyncGenerator

import pytest

from services.api.forge.rag import scoper


def _result_with_scalar(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _result_with_scalars(values: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


@pytest.mark.asyncio
async def test_extract_task_scope_returns_primary_and_one_hop_import_neighbor():
    repo_id = str(uuid.uuid4())
    primary_chunk = SimpleNamespace(
        file_path="services/api/auth.py",
        symbol_name="validate_token",
        line_number=10,
    )

    revision_result = _result_with_scalar("indexed-sha")
    primary_result = _result_with_scalars([primary_chunk])
    edge_result = MagicMock()
    edge_result.all.return_value = [
        ("services/api/auth.py", "services.api.tokens"),
    ]
    neighbor_result = _result_with_scalars(["services/api/tokens.py"])

    session = AsyncMock()
    session.execute.side_effect = [
        revision_result,
        primary_result,
        edge_result,
        neighbor_result,
    ]

    @asynccontextmanager
    async def session_context() -> AsyncGenerator[AsyncMock, None]:
        yield session

    with patch.object(scoper, "async_session_factory", return_value=session_context()):
        scope = await scoper.extract_task_scope(
            "Fix validate_token expiry behavior",
            repo_id,
        )

    assert scope == ["services/api/auth.py", "services/api/tokens.py"]
    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_extract_task_scope_rejects_invalid_repository_id():
    with pytest.raises(ValueError, match="repo_id must be a repository UUID"):
        await scoper.extract_task_scope("Fix validate_token", "not-a-uuid")


def test_extract_identifiers_removes_common_task_words_and_deduplicates():
    assert scoper._extract_identifiers(
        "Fix validate_token in AuthService; update validate_token tests"
    ) == ["validate_token", "AuthService", "tests"]


def test_relative_import_edges_resolve_against_source_file_directory():
    assert "services/api/tokens.py" in scoper._module_file_candidates(
        ".tokens",
        "services/api/auth.py",
    )
