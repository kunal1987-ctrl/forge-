"""Rolling state summarization and artifact offloading for FORGE LangGraph orchestration."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

try:
    from services.api.core.config import settings
except ImportError:
    try:
        from core.config import settings  # type: ignore[no-redef]
    except ImportError:
        settings = None  # type: ignore[assignment]

from services.api.forge.orchestration.llm_factory import get_llm
from services.api.forge.orchestration.pruner import sanitize_test_logs

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. ARTIFACT SINK & OFFLOADING TABLE
# ===========================================================================


class ArtifactEntry(BaseModel):
    """Metadata and raw payload stored in the artifact sink table."""

    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    artifact_type: str
    size_bytes: int
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    content: Any


class ArtifactSink:
    """Thread-safe artifact sink preventing LangGraph checkpoint bloat."""

    _TABLE: Dict[str, ArtifactEntry] = {}

    @classmethod
    def store_artifact(cls, task_id: str, artifact_type: str, content: Any) -> str:
        raw_str = content if isinstance(content, str) else json.dumps(content, default=str)
        entry = ArtifactEntry(
            task_id=str(task_id or "unknown"),
            artifact_type=artifact_type,
            size_bytes=len(raw_str.encode("utf-8")),
            content=content,
        )
        cls._TABLE[entry.artifact_id] = entry
        return entry.artifact_id

    @classmethod
    def get_artifact(cls, artifact_id: str) -> Optional[ArtifactEntry]:
        return cls._TABLE.get(artifact_id)

    @classmethod
    def offload_test_and_rag_artifacts(
        cls,
        task_id: str,
        test_results: Optional[Dict[str, Any]] = None,
        context_chunks: Optional[List[Any]] = None,
        max_inline_chars: int = 1500,
    ) -> Dict[str, Any]:
        artifact_refs: Dict[str, str] = {}
        sanitized_error = ""
        trimmed_test_results = dict(test_results or {})

        raw_output = str(
            trimmed_test_results.get("raw_output") or trimmed_test_results.get("stdout") or ""
        )
        stderr_output = str(trimmed_test_results.get("stderr") or "")
        combined_logs = raw_output + "\n" + stderr_output

        if len(combined_logs) > max_inline_chars:
            art_id = cls.store_artifact(task_id, "pytest_raw_output", combined_logs)
            artifact_refs["test_output_artifact_id"] = art_id

            sanitized_error = sanitize_test_logs(combined_logs)[:200]
            trimmed_test_results["raw_output"] = (
                f"[OFFLOADED to artifact:{art_id}]\n" + sanitized_error
            )
            trimmed_test_results["stdout"] = trimmed_test_results.get("stdout", "")[:200]
            trimmed_test_results["stderr"] = sanitized_error
        else:
            sanitized_error = sanitize_test_logs(combined_logs)[:200]

        if context_chunks and (
            len(context_chunks) > 10 or sum(len(str(c)) for c in context_chunks) > 10000
        ):
            rag_art_id = cls.store_artifact(task_id, "rag_chunks", context_chunks)
            artifact_refs["rag_chunks_artifact_id"] = rag_art_id

        return {
            "test_results": trimmed_test_results,
            "sanitized_error": sanitized_error,
            "artifact_refs": artifact_refs,
            "artifact_id": artifact_refs.get("test_output_artifact_id", ""),
        }


# ===========================================================================
# 2. ROLLING STATE SUMMARIZATION
# ===========================================================================


class RollingSummary(BaseModel):
    """Structured rolling summary of historical repair attempts."""

    summary_text: str = Field(
        ...,
        description="Concise description of past failed attempts, exact root causes, and progressive fixes attempted.",
    )
    failed_attempts_count: int = Field(
        default=0,
        description="Total number of failed implementation/verification attempts recorded.",
    )


class RollingSummarizer:
    """Manages message history length and token consumption using LangChain's RemoveMessage."""

    @classmethod
    def should_summarize(
        cls, messages: Sequence[Any], token_threshold: int = 15000, retry_count: int = 0
    ) -> bool:
        cumulative_tokens = cls.estimate_tokens(list(messages))
        return cumulative_tokens > token_threshold or len(messages) > 12 or retry_count >= 2

    @staticmethod
    def summarize_messages(
        state_messages: list, existing_summary: str = "", llm: Any = None
    ) -> dict:
        api_key = getattr(settings, "OPENAI_API_KEY", "") if settings else ""
        new_summary = ""

        try:
            active_llm = llm or (get_llm("fast") if callable(get_llm) else None)
            if active_llm is None and api_key:
                active_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=str(api_key))  # type: ignore[arg-type]

            if active_llm is not None:
                prompt = f"""
This is a rolling summary of an autonomous software engineering agent's execution history:
Existing Summary: {existing_summary}

New messages to incorporate:
{chr(10).join([f"{type(m).__name__}: {getattr(m, 'content', '')}" for m in state_messages[:-2]])}

Provide a concise, structured summary focusing on failed attempts, missing imports, root causes, and fixes tried so far.
"""
                response = active_llm.invoke([HumanMessage(content=prompt)])
                raw_content = response.content if hasattr(response, "content") else str(response)
                new_summary = raw_content if isinstance(raw_content, str) else str(raw_content)

                if "{" in new_summary and "summary_text" in new_summary:
                    try:
                        data = json.loads(
                            new_summary[new_summary.find("{") : new_summary.rfind("}") + 1]
                        )
                        new_summary = data.get("summary_text", new_summary)
                    except (json.JSONDecodeError, KeyError, ValueError) as json_err:
                        logger.debug("Failed parsing json summary: %s", json_err)
        except Exception as exc:
            logger.warning("RollingSummarizer.summarize_messages LLM failed: %s", exc)

        if not new_summary:
            msg_count = len(state_messages)
            if existing_summary:
                new_summary = (
                    f"{existing_summary}; Attempt history updated with {msg_count} messages."
                )
            else:
                new_summary = (
                    f"Execution history: defect repair attempts executed across {msg_count} turns."
                )

        messages_to_remove = state_messages[:-2]
        remove_instructions = [
            RemoveMessage(id=str(m.id)) for m in messages_to_remove if hasattr(m, "id") and m.id
        ]

        return {
            "summary": new_summary,
            "messages": remove_instructions,
        }

    @classmethod
    def estimate_tokens(cls, messages: Sequence[BaseMessage]) -> int:
        if not messages:
            return 0
        total_chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
        return total_chars // 4

    @classmethod
    async def summarize_history(
        cls,
        messages: Sequence[BaseMessage],
        retry_count: int,
        repair_trajectory: Optional[List[Dict[str, Any]]] = None,
        mode: str = "fast",
    ) -> Tuple[RollingSummary, List[RemoveMessage], BaseMessage]:
        fast_llm = get_llm("fast") if callable(get_llm) else None
        summary_text = ""

        if fast_llm is not None:
            try:
                prompt = f"""
This is a rolling summary of an autonomous software engineering agent's execution history:
New messages to incorporate:
{chr(10).join([f"{type(m).__name__}: {getattr(m, 'content', '')}" for m in list(messages)[:-2]])}

Provide a concise, structured summary focusing on failed attempts, missing imports, root causes, and fixes tried so far.
"""
                resp = await fast_llm.ainvoke([HumanMessage(content=prompt)])
                resp_content = resp.content if hasattr(resp, "content") else str(resp)
                if isinstance(resp_content, str):
                    if "{" in resp_content and "summary_text" in resp_content:
                        import json

                        try:
                            d = json.loads(
                                resp_content[resp_content.find("{") : resp_content.rfind("}") + 1]
                            )
                            summary_text = str(d.get("summary_text", resp_content))
                        except Exception:
                            summary_text = resp_content
                    else:
                        summary_text = resp_content
            except Exception as exc:
                logger.warning("RollingSummarizer.summarize_history ainvoke error: %s", exc)

        if not summary_text:
            res = cls.summarize_messages(list(messages), existing_summary="")
            summary_text = str(res["summary"])

        if (
            "defect repair attempts executed" in summary_text or not summary_text
        ) and repair_trajectory:
            summary_text = "; ".join(
                [
                    f"Attempt {item.get('attempt', 1)}: [{item.get('failed_file', 'unknown')}] {item.get('failure_type', 'Error')} - {item.get('summary', '')}"
                    for item in repair_trajectory
                ]
            )

        messages_to_remove = list(messages)
        remove_cmds = [
            RemoveMessage(id=str(m.id)) for m in messages_to_remove if hasattr(m, "id") and m.id
        ]
        summary_obj = RollingSummary(summary_text=summary_text, failed_attempts_count=retry_count)
        new_msg = HumanMessage(
            content=f"[ROLLING REPAIR SUMMARY - Attempt {retry_count}]:\n{summary_text}",
            name="rolling_summary",
        )
        return summary_obj, remove_cmds, new_msg

    @classmethod
    async def condense_state(cls, state: Dict[str, Any]) -> Dict[str, Any]:
        messages: List[BaseMessage] = list(state.get("messages") or [])
        retry_count: int = state.get("retry_count", 0)
        existing_summary: str = state.get("summary", "")

        if not cls.should_summarize(messages, retry_count=retry_count):
            return {}

        res = cls.summarize_messages(messages, existing_summary=existing_summary)
        summary_obj = RollingSummary(summary_text=res["summary"], failed_attempts_count=retry_count)
        new_msg = HumanMessage(
            content=f"[ROLLING REPAIR SUMMARY - Attempt {retry_count}]:\n{res['summary']}",
            name="rolling_summary",
        )
        updated_messages: List[Any] = list(res["messages"]) + [new_msg]

        return {
            "summary": res["summary"],
            "messages": updated_messages,
            "rolling_summary": summary_obj.model_dump(),
        }
