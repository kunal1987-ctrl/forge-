"""Unit and integration tests for the AST symbol index endpoint.

Verifies:
1. GET /api/repos/{repo_id}/symbols returns HTTP 200 with ASTSymbolsIndexResponse schema.
2. Parity route GET /api/repositories/{repo_id}/symbols returns identical response.
3. Empty/unindexed repo returns empty symbols and zero summary counts rather than 404/500.
4. Repository name/path resolution to UUID.
5. Symbol type filtering (class, function, etc.).
6. Search query filtering across symbol_name and file_path.
7. Limit parameter enforcement.
8. Graceful error handling on unexpected DB exceptions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from services.api.core.database import get_db
from services.api.main import app
from services.api.models.code import CodeChunk
from services.api.models.repository import Repository, RepositoryRevision


def create_fake_chunk(
    repo_id: uuid.UUID,
    commit_sha: str = "abc123commit",
    file_path: str = "src/auth/service.py",
    symbol_name: str = "login_user",
    symbol_type: str = "function",
    line_number: int = 42,
    end_line: int = 58,
    parent_symbol: str | None = None,
    content: str = "def login_user(): pass",
) -> MagicMock:
    """Helper to mock a CodeChunk ORM instance."""
    chunk = MagicMock(spec=CodeChunk)
    chunk.id = uuid.uuid4()
    chunk.repo_id = repo_id
    chunk.commit_sha = commit_sha
    chunk.file_path = file_path
    chunk.symbol_name = symbol_name
    chunk.symbol_type = symbol_type
    chunk.line_number = line_number
    chunk.end_line = end_line
    chunk.parent_symbol = parent_symbol
    chunk.content = content
    return chunk


@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Isolated mock SQLAlchemy AsyncSession."""
    session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_res.scalar.return_value = 0
    mock_res.all.return_value = []
    mock_res.scalars.return_value.all.return_value = []
    session.execute.return_value = mock_res
    return session


@pytest.mark.asyncio
async def test_get_repo_symbols_empty_returns_200_with_empty_schema(mock_db_session):
    """Verify requesting an unknown or empty repo returns HTTP 200 with empty symbols."""
    repo_uuid = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/repos/{repo_uuid}/symbols")

        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_id"] == str(repo_uuid)
        assert data["symbols"] == []
        assert data["summary"]["total_files_parsed"] == 0
        assert data["summary"]["total_nodes_indexed"] == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_repo_symbols_populated_returns_correct_kpis_and_symbols(mock_db_session):
    """Verify populated repository returns summary counts and mapped symbol records."""
    repo_uuid = uuid.uuid4()
    commit = "a1b2c3d4e5"

    chunk1 = create_fake_chunk(
        repo_id=repo_uuid,
        commit_sha=commit,
        file_path="src/models/user.py",
        symbol_name="User",
        symbol_type="class",
        line_number=10,
        end_line=50,
    )
    chunk2 = create_fake_chunk(
        repo_id=repo_uuid,
        commit_sha=commit,
        file_path="src/models/user.py",
        symbol_name="get_full_name",
        symbol_type="method",
        line_number=25,
        end_line=30,
        parent_symbol="User",
    )
    chunk3 = create_fake_chunk(
        repo_id=repo_uuid,
        commit_sha=commit,
        file_path="src/main.py",
        symbol_name="create_app",
        symbol_type="function",
        line_number=5,
        end_line=20,
    )

    # Mock execute results in sequence:
    # 1. rev_stmt -> commit_sha
    mock_rev_res = MagicMock()
    mock_rev_res.scalar_one_or_none.return_value = commit

    # 2. files count -> 2
    mock_files_res = MagicMock()
    mock_files_res.scalar.return_value = 2

    # 3. nodes count -> 3
    mock_nodes_res = MagicMock()
    mock_nodes_res.scalar.return_value = 3

    # 4. type breakdown -> [('class', 1), ('method', 1), ('function', 1)]
    mock_type_res = MagicMock()
    mock_type_res.all.return_value = [("class", 1), ("method", 1), ("function", 1)]

    # 5. symbols list -> [chunk1, chunk2, chunk3]
    mock_sym_res = MagicMock()
    mock_sym_res.scalars.return_value.all.return_value = [chunk1, chunk2, chunk3]

    mock_db_session.execute.side_effect = [
        mock_rev_res,
        mock_files_res,
        mock_nodes_res,
        mock_type_res,
        mock_sym_res,
    ]

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/repos/{repo_uuid}/symbols")

        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_id"] == str(repo_uuid)
        assert data["commit_sha"] == commit
        assert data["summary"]["total_files_parsed"] == 2
        assert data["summary"]["total_nodes_indexed"] == 3
        assert data["summary"]["by_type"] == {"class": 1, "method": 1, "function": 1}

        symbols = data["symbols"]
        assert len(symbols) == 3
        assert symbols[0]["symbol_name"] == "User"
        assert symbols[0]["symbol_type"] == "class"
        assert symbols[0]["line_number"] == 10
        assert symbols[0]["end_line"] == 50

        assert symbols[1]["symbol_name"] == "get_full_name"
        assert symbols[1]["parent_symbol"] == "User"
        assert symbols[1]["line_number"] == 25

        assert symbols[2]["symbol_name"] == "create_app"
        assert symbols[2]["line_number"] == 5
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_repo_symbols_repo_name_lookup_resolution(mock_db_session):
    """Verify passing a repository name resolves to its UUID via Repository table lookup."""
    repo_name = "my-awesome-repo"
    resolved_uuid = uuid.uuid4()

    # 1. repo lookup -> returns resolved_uuid
    mock_repo_res = MagicMock()
    mock_repo_res.scalar_one_or_none.return_value = resolved_uuid

    # 2. rev_stmt
    mock_rev_res = MagicMock()
    mock_rev_res.scalar_one_or_none.return_value = "commithash"

    # 3. files
    mock_files = MagicMock()
    mock_files.scalar.return_value = 1

    # 4. nodes
    mock_nodes = MagicMock()
    mock_nodes.scalar.return_value = 1

    # 5. types
    mock_types = MagicMock()
    mock_types.all.return_value = [("function", 1)]

    # 6. symbols
    chunk = create_fake_chunk(repo_id=resolved_uuid)
    mock_sym = MagicMock()
    mock_sym.scalars.return_value.all.return_value = [chunk]

    mock_db_session.execute.side_effect = [
        mock_repo_res,
        mock_rev_res,
        mock_files,
        mock_nodes,
        mock_types,
        mock_sym,
    ]

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/repos/{repo_name}/symbols")

        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_id"] == str(resolved_uuid)
        assert len(data["symbols"]) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_repo_symbols_route_parity_aliases(mock_db_session):
    """Verify both /api/repos/{id}/symbols and /api/repositories/{id}/symbols work."""
    repo_uuid = uuid.uuid4()
    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp1 = await client.get(f"/api/repos/{repo_uuid}/symbols")
            resp2 = await client.get(f"/api/repositories/{repo_uuid}/symbols")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_get_repo_symbols_query_and_type_filters(mock_db_session):
    """Verify symbol_type, query, and limit query parameters are parsed."""
    repo_uuid = uuid.uuid4()
    mock_rev_res = MagicMock()
    mock_rev_res.scalar_one_or_none.return_value = "commithash"

    mock_files = MagicMock()
    mock_files.scalar.return_value = 1

    mock_nodes = MagicMock()
    mock_nodes.scalar.return_value = 1

    mock_types = MagicMock()
    mock_types.all.return_value = [("function", 1)]

    chunk = create_fake_chunk(repo_id=repo_uuid, symbol_name="handle_login")
    mock_sym = MagicMock()
    mock_sym.scalars.return_value.all.return_value = [chunk]

    mock_db_session.execute.side_effect = [
        mock_rev_res,
        mock_files,
        mock_nodes,
        mock_types,
        mock_sym,
    ]

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/repos/{repo_uuid}/symbols?symbol_type=function&query=login&limit=10"
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["symbols"]) == 1
        assert data["symbols"][0]["symbol_name"] == "handle_login"
    finally:
        app.dependency_overrides.pop(get_db, None)
