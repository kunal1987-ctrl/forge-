"""Pydantic models for execution results and sandbox outputs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExecutionResult(BaseModel):
    """Strict structured result from sandboxed test and lint execution."""

    exit_code: int = Field(
        ...,
        description="Exit status code of the command (0 for success, non-zero for failure)",
    )
    stdout: str = Field(
        default="",
        description="Standard output captured from the execution process",
    )
    stderr: str = Field(
        default="",
        description="Standard error captured from the execution process",
    )
    timeout: bool = Field(
        default=False,
        description="Indicates whether execution was terminated due to a timeout limit",
    )

    def __getitem__(self, item: str) -> Any:
        """Enable dictionary-like subscript access for backwards compatibility."""
        return getattr(self, item)

    def get(self, key: str, default: Any = None) -> Any:
        """Enable dictionary-like .get() access for backwards compatibility."""
        return getattr(self, key, default)

    @property
    def output(self) -> str:
        """Combined standard output and standard error."""
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr

    @property
    def success(self) -> bool:
        """True if command completed successfully (exit_code == 0 and not timed out)."""
        return self.exit_code == 0 and not self.timeout
