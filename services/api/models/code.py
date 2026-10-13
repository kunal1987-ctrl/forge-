import uuid
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Text, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from services.api.core.database import Base

if TYPE_CHECKING:
    from services.api.models.repository import Repository


class CodeChunk(Base):
    __tablename__ = "code_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    symbol_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    symbol_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    parent_symbol: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Vector embedding column for semantic search (OpenAI 1536-dim standard)
    embedding: Mapped[Optional[list[float]]] = mapped_column(
        Vector(1536),
        nullable=True,
    )

    # Postgres full-text search vector
    tsv_content: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        nullable=True,
    )

    repository: Mapped["Repository"] = relationship(
        "Repository",
        back_populates="code_chunks",
    )

    __table_args__ = (
        Index("ix_code_chunks_tsv_content", "tsv_content", postgresql_using="gin"),
        Index("ix_code_chunks_repo_commit", "repo_id", "commit_sha"),
    )


class CodeEdge(Base):
    __tablename__ = "code_edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_symbol: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    target_symbol: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    edge_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # e.g., 'calls', 'imports', 'enclosed_by'

    repository: Mapped["Repository"] = relationship(
        "Repository",
        back_populates="code_edges",
    )

    __table_args__ = (
        Index("ix_code_edges_symbols", "source_symbol", "target_symbol"),
        Index("ix_code_edges_repo_commit_source", "repo_id", "commit_sha", "source_symbol"),
        Index("ix_code_edges_repo_commit_target", "repo_id", "commit_sha", "target_symbol"),
    )
