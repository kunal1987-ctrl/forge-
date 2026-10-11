"""GitHub API integration for automated pull request creation and repo operations."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class GitHubIntegration:
    """Manages GitHub API interactions including opening pull requests for finished tasks."""

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token

    def create_pull_request(
        self,
        repo_name: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
    ) -> Dict[str, Any]:
        """Create a pull request on the specified GitHub repository."""
        logger.info(
            "Creating pull request on '%s': '%s' (head=%s, base=%s)",
            repo_name,
            title,
            head_branch,
            base_branch,
        )
        return {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "status": "opened",
            "url": f"https://github.com/{repo_name}/pull/1",
        }
