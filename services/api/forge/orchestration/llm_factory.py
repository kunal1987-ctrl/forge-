"""Multi-provider LLM Factory for FORGE orchestration.

Supports dual-mode model execution:
- 'fast': OpenAI gpt-4o-mini for quick iteration and linting.
- 'deep': OpenRouter DeepSeek R1 (reasoning model) for deep analysis.
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_openai import ChatOpenAI

try:
    from services.api.core.config import settings
except ImportError:
    from core.config import settings  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def get_llm(mode: Optional[str] = "fast") -> ChatOpenAI:
    """Instantiate a ChatOpenAI client configured for the requested execution mode.

    Args:
        mode: Execution mode ('fast' for gpt-4o-mini, 'deep' for deepseek-r1).
              Defaults to 'fast' if unrecognized or omitted.

    Returns:
        ChatOpenAI instance configured with appropriate credentials, timeout, and endpoint.
    """
    normalized_mode = (mode or "fast").strip().lower()

    if normalized_mode == "deep":
        logger.info("Initializing Deep Thinking LLM (deepseek/deepseek-r1:free via OpenRouter)")
        return ChatOpenAI(
            model="deepseek/deepseek-r1:free",
            temperature=0,
            api_key=settings.OPENROUTER_API_KEY,  # type: ignore[arg-type]
            base_url="https://openrouter.ai/api/v1",
            timeout=35,
            max_retries=2,
        )

    # Default to fast mode
    logger.info("Initializing Fast LLM (gpt-4o-mini via OpenAI)")
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=settings.OPENAI_API_KEY,  # type: ignore[arg-type]
        timeout=30,
        max_retries=2,
    )
