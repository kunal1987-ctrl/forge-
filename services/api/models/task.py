import uuid
from datetime import datetime
from typing import Any, Dict, List, TYPE_CHECKING
from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, func, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from services.api.core.database import Base

if TYPE_CHECKING:
    from services.api.models.repository import Repository


class Task(Base):
    __tablename__ = "tasks"

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
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50),
        default="pending",
        nullable=False,
        index=True,
    )  # e.g., 'pending', 'running', 'awaiting_approval', 'completed', 'failed'
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    repository: Mapped["Repository"] = relationship(
        "Repository",
        back_populates="tasks",
    )
    events: Mapped[List["TaskEvent"]] = relationship(
        "TaskEvent",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskEvent.sequence",
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    task: Mapped["Task"] = relationship(
        "Task",
        back_populates="events",
    )

    __table_args__ = (Index("ix_task_events_task_seq", "task_id", "sequence", unique=True),)
