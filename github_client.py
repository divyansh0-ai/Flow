"""github_client.py — Real GitHub REST API integration.

Turns the Flow Agent from a demo into an agent that operates on live
repositories. It can:

  * read real file contents from a repo (so the agent analyzes actual code),
  * create a fix branch,
  * commit a patched file to that branch,
  * open a genuine pull request.

Every write operation happens **only after** the Human-in-the-Loop approval
gate in :mod:`agent` has passed, so the LLM can never open a PR on its own.

Authentication uses a GitHub personal access token (classic, ``repo`` scope) or
a fine-grained token with Contents + Pull requests read/write, supplied via the
``GITHUB_TOKEN`` environment variable.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("flow.github")

GITHUB_API_BASE: str = os.getenv("GITHUB_API_BASE", "https://api.github.com")
GITHUB_TOKEN: Optional[str] = os.getenv("GITHUB_TOKEN")
REQUEST_TIMEOUT: int = int(os.getenv("GITHUB_TIMEOUT_SECONDS", "30"))


class GitHubError(RuntimeError):
    """Raised when the GitHub API returns an unexpected response."""


class GitHubClient:
    """Minimal, typed GitHub REST API client for the patch-and-PR workflow."""

    def __init__(
        self,
        token: Optional[str] = None,
        api_base: str = GITHUB_API_BASE,
        timeout: int = REQUEST_TIMEOUT,
    ) -> None:
        self.token = token or GITHUB_TOKEN
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

        if not self.token:
            raise GitHubError(
                "No GitHub token configured. Set the GITHUB_TOKEN environment "
                "variable to enable real repository operations."
            )

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "flow-agent",
            }
        )

    # -- low-level -----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
    ) -> Any:
        """Issue an API request and return the decoded JSON body.

        Args:
            method: HTTP verb.
            path: API path beginning with ``/``.
            json_body: Optional JSON payload.
            params: Optional query-string parameters.
            allow_404: When ``True``, a 404 returns ``None`` instead of raising.

        Raises:
            GitHubError: on transport failure or a non-2xx response.
        """
        url = f"{self.api_base}{path}"
        try:
            response = self._session.request(
                method,
                url,
                json=json_body,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:  # network-level failure
            raise GitHubError(f"GitHub request failed ({method} {path}): {exc}") from exc

        if response.status_code == 404 and allow_404:
            return None

        if not response.ok:
            raise GitHubError(
                f"GitHub API error {response.status_code} on {method} {path}: "
                f"{response.text[:500]}"
            )

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- repository metadata --------------------------------------------------
    def get_default_branch(self, repo: str) -> str:
        """Return the repository's default branch name (e.g. ``main``)."""
        data = self._request("GET", f"/repos/{repo}")
        branch = data.get("default_branch")
        if not branch:
            raise GitHubError(f"Could not determine default branch for '{repo}'.")
        return str(branch)

    def get_branch_sha(self, repo: str, branch: str) -> str:
        """Return the head commit SHA of ``branch``."""
        data = self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        sha = (data or {}).get("object", {}).get("sha")
        if not sha:
            raise GitHubError(f"Could not resolve branch '{branch}' in '{repo}'.")
        return str(sha)

    # -- file contents --------------------------------------------------------
    def get_file(
        self, repo: str, path: str, ref: Optional[str] = None
    ) -> Optional[Tuple[str, str]]:
        """Fetch a file's decoded text content and blob SHA.

        Returns:
            ``(content, sha)``, or ``None`` when the file does not exist.
        """
        params = {"ref": ref} if ref else None
        data = self._request(
            "GET", f"/repos/{repo}/contents/{path}", params=params, allow_404=True
        )
        if data is None:
            return None
        if isinstance(data, list):
            raise GitHubError(f"'{path}' in '{repo}' is a directory, not a file.")

        encoded = data.get("content", "")
        try:
            content = base64.b64decode(encoded).decode("utf-8", errors="replace")
        except (ValueError, TypeError) as exc:
            raise GitHubError(f"Could not decode '{path}' from '{repo}': {exc}") from exc
        return content, str(data.get("sha", ""))

    def search_file_by_name(self, repo: str, filename: str) -> Optional[str]:
        """Best-effort lookup of a file's full repo path from its basename.

        Uses the Git tree API on the default branch. Returns the first path
        whose basename matches, or ``None``.
        """
        try:
            default_branch = self.get_default_branch(repo)
            sha = self.get_branch_sha(repo, default_branch)
            tree = self._request(
                "GET",
                f"/repos/{repo}/git/trees/{sha}",
                params={"recursive": "1"},
            )
        except GitHubError as exc:
            logger.warning("Tree lookup failed for '%s': %s", repo, exc)
            return None

        target = filename.replace("\\", "/").split("/")[-1]
        entries: List[Dict[str, Any]] = (tree or {}).get("tree", [])
        for entry in entries:
            if entry.get("type") != "blob":
                continue
            entry_path = str(entry.get("path", ""))
            if entry_path.split("/")[-1] == target:
                return entry_path
        return None

    # -- write operations (post-approval only) --------------------------------
    def create_branch(self, repo: str, new_branch: str, base_sha: str) -> None:
        """Create ``new_branch`` pointing at ``base_sha`` (idempotent)."""
        existing = self._request(
            "GET", f"/repos/{repo}/git/ref/heads/{new_branch}", allow_404=True
        )
        if existing is not None:
            logger.info("Branch '%s' already exists in '%s'.", new_branch, repo)
            return

        self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json_body={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
        )
        logger.info("Created branch '%s' in '%s'.", new_branch, repo)

    def commit_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: Optional[str] = None,
    ) -> str:
        """Create or update ``path`` on ``branch``; returns the new commit SHA."""
        payload: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha  # required when updating an existing file

        data = self._request("PUT", f"/repos/{repo}/contents/{path}", json_body=payload)
        commit_sha = (data or {}).get("commit", {}).get("sha", "")
        logger.info("Committed '%s' to '%s' on branch '%s'.", path, repo, branch)
        return str(commit_sha)

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> str:
        """Open a pull request and return its HTML URL."""
        data = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json_body={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft,
            },
        )
        url = (data or {}).get("html_url", "")
        if not url:
            raise GitHubError(f"Pull request creation returned no URL for '{repo}'.")
        logger.info("Opened pull request: %s", url)
        return str(url)


# ---------------------------------------------------------------------------
# Helpers usable without an authenticated client
# ---------------------------------------------------------------------------
def is_github_configured() -> bool:
    """Return ``True`` when a GitHub token is present in the environment."""
    return bool(os.getenv("GITHUB_TOKEN"))


def apply_snippet_patch(full_content: str, original: str, patched: str) -> str:
    """Splice ``patched`` in place of ``original`` inside ``full_content``.

    Falls back to returning ``patched`` when the original snippet is not found
    verbatim but the patch clearly represents the whole file.

    Raises:
        ValueError: when the original snippet cannot be located and the patch
            does not look like a full-file replacement.
    """
    if not original.strip():
        raise ValueError("Original snippet is empty; cannot locate patch site.")

    if original in full_content:
        return full_content.replace(original, patched, 1)

    # Tolerate differences in leading/trailing whitespace.
    stripped = original.strip()
    if stripped and stripped in full_content:
        return full_content.replace(stripped, patched.strip(), 1)

    # If the agent returned the entire file as the snippet, accept it wholesale.
    if original.strip() == full_content.strip():
        return patched

    raise ValueError(
        "Could not locate the original snippet in the fetched file; "
        "refusing to guess at the patch site."
    )


def build_pr_body(
    task_id: str,
    summary: str,
    root_cause: str,
    confidence: float,
    unified_diff: str,
) -> str:
    """Compose a descriptive pull-request body for an approved patch."""
    return (
        f"## 🩺 Automated fix from Flow Agent\n\n"
        f"**Summary:** {summary}\n\n"
        f"### Root cause\n{root_cause}\n\n"
        f"### Agent confidence\n`{confidence:.2f}`\n\n"
        f"### Proposed diff\n```diff\n{unified_diff or '(no diff available)'}\n```\n\n"
        f"---\n"
        f"*Generated by an ADK + Gemini agent and **approved by a human** "
        f"before this pull request was opened.*\n\n"
        f"Task ID: `{task_id}`\n"
    )
