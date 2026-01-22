"""GitHub API client for hosted MCP mode.

This module provides a thin wrapper around GitHub's REST API for thread
operations in hosted mode. It handles:
- Reading thread files via Contents API
- Writing/updating thread files with commit messages
- Listing files in the threads directory
- Error handling and rate limiting

Usage:
    from .github_api import GitHubClient

    client = GitHubClient(
        token="ghp_...",
        repo="org/repo-threads",
        branch="main",
    )

    # Read a thread
    content, sha = client.get_file("my-thread.md")

    # Update a thread
    new_sha = client.put_file(
        path="my-thread.md",
        content="# New content...",
        message="Add entry to my-thread",
        sha=sha,  # Required for updates
    )

    # List threads
    files = client.list_files("")  # List root directory
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class GitHubAPIError(Exception):
    """Base exception for GitHub API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class GitHubNotFoundError(GitHubAPIError):
    """Raised when a file or resource is not found (404)."""
    pass


class GitHubConflictError(GitHubAPIError):
    """Raised when there's a conflict (409), e.g., sha mismatch."""
    pass


class GitHubRateLimitError(GitHubAPIError):
    """Raised when rate limit is exceeded (403 with rate limit message)."""
    pass


@dataclass
class FileContent:
    """Content of a file from GitHub API."""
    content: str
    sha: str
    path: str
    size: int


@dataclass
class FileInfo:
    """Info about a file from directory listing."""
    name: str
    path: str
    sha: str
    size: int
    type: str  # "file" or "dir"


class GitHubClient:
    """Client for GitHub Contents API operations.

    This client provides the core operations needed for hosted MCP mode:
    - Reading thread markdown files
    - Writing/updating thread files with proper commit messages
    - Listing files in the threads directory

    All operations are authenticated using the provided OAuth token.
    """

    def __init__(
        self,
        token: str,
        repo: str,
        branch: str = "main",
        base_url: str = "https://api.github.com",
    ):
        """Initialize GitHub client.

        Args:
            token: GitHub OAuth token for authentication.
            repo: Repository full name (e.g., "org/repo-threads").
            branch: Branch name (default: "main").
            base_url: GitHub API base URL (default: api.github.com).
        """
        self.token = token
        self.repo = repo
        self.branch = branch
        self.base_url = base_url.rstrip("/")

        # Parse owner and repo name
        if "/" not in repo:
            raise ValueError(f"Invalid repo format: {repo}. Expected 'owner/repo'.")
        self.owner, self.repo_name = repo.split("/", 1)

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated request to GitHub API.

        Args:
            method: HTTP method (GET, PUT, POST, DELETE).
            endpoint: API endpoint (e.g., /repos/{owner}/{repo}/contents/{path}).
            data: JSON data for request body.

        Returns:
            Parsed JSON response.

        Raises:
            GitHubAPIError: On API errors.
            GitHubNotFoundError: On 404 responses.
            GitHubConflictError: On 409 responses.
            GitHubRateLimitError: On rate limit exceeded.
        """
        url = f"{self.base_url}{endpoint}"

        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "watercooler-mcp/1.0",
        }

        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                response_data = response.read().decode("utf-8")
                if response_data:
                    return json.loads(response_data)
                return {}

        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
                error_data = json.loads(error_body) if error_body else {}
            except Exception:
                error_data = {"message": error_body or str(e)}

            message = error_data.get("message", str(e))

            if e.code == 404:
                raise GitHubNotFoundError(
                    f"Not found: {message}",
                    status_code=404,
                    response=error_data,
                )
            elif e.code == 409:
                raise GitHubConflictError(
                    f"Conflict: {message}",
                    status_code=409,
                    response=error_data,
                )
            elif e.code == 403 and "rate limit" in message.lower():
                raise GitHubRateLimitError(
                    f"Rate limit exceeded: {message}",
                    status_code=403,
                    response=error_data,
                )
            else:
                raise GitHubAPIError(
                    f"GitHub API error ({e.code}): {message}",
                    status_code=e.code,
                    response=error_data,
                )

        except urllib.error.URLError as e:
            raise GitHubAPIError(f"Connection error: {e.reason}")

    def get_file(self, path: str) -> FileContent:
        """Read file content from GitHub.

        Args:
            path: File path relative to repo root (e.g., "my-thread.md").

        Returns:
            FileContent with decoded content, sha, and metadata.

        Raises:
            GitHubNotFoundError: If file doesn't exist.
            GitHubAPIError: On other API errors.
        """
        endpoint = f"/repos/{self.owner}/{self.repo_name}/contents/{path}"
        if self.branch:
            endpoint += f"?ref={self.branch}"

        data = self._make_request("GET", endpoint)

        # Decode base64 content
        content_b64 = data.get("content", "")
        # GitHub returns content with newlines, remove them before decoding
        content_b64_clean = content_b64.replace("\n", "")
        content = base64.b64decode(content_b64_clean).decode("utf-8")

        return FileContent(
            content=content,
            sha=data.get("sha", ""),
            path=data.get("path", path),
            size=data.get("size", 0),
        )

    def put_file(
        self,
        path: str,
        content: str,
        message: str,
        sha: Optional[str] = None,
        committer_name: Optional[str] = None,
        committer_email: Optional[str] = None,
    ) -> str:
        """Create or update a file on GitHub.

        Args:
            path: File path relative to repo root.
            content: New file content (UTF-8 string).
            message: Commit message.
            sha: Current file SHA (required for updates, omit for creates).
            committer_name: Optional committer name.
            committer_email: Optional committer email.

        Returns:
            New file SHA after commit.

        Raises:
            GitHubConflictError: If sha doesn't match (concurrent modification).
            GitHubAPIError: On other API errors.
        """
        endpoint = f"/repos/{self.owner}/{self.repo_name}/contents/{path}"

        # Encode content as base64
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        data = {
            "message": message,
            "content": content_b64,
            "branch": self.branch,
        }

        if sha:
            data["sha"] = sha

        if committer_name and committer_email:
            data["committer"] = {
                "name": committer_name,
                "email": committer_email,
            }

        response = self._make_request("PUT", endpoint, data)
        return response.get("content", {}).get("sha", "")

    def list_files(self, path: str = "") -> list[FileInfo]:
        """List files in a directory.

        Args:
            path: Directory path relative to repo root (empty for root).

        Returns:
            List of FileInfo objects for each file/directory.

        Raises:
            GitHubNotFoundError: If directory doesn't exist.
            GitHubAPIError: On other API errors.
        """
        endpoint = f"/repos/{self.owner}/{self.repo_name}/contents/{path}"
        if self.branch:
            endpoint += f"?ref={self.branch}"

        data = self._make_request("GET", endpoint)

        # Handle case where path is a file, not directory
        if isinstance(data, dict):
            return [FileInfo(
                name=data.get("name", ""),
                path=data.get("path", ""),
                sha=data.get("sha", ""),
                size=data.get("size", 0),
                type=data.get("type", "file"),
            )]

        # Directory listing returns array
        files = []
        for item in data:
            files.append(FileInfo(
                name=item.get("name", ""),
                path=item.get("path", ""),
                sha=item.get("sha", ""),
                size=item.get("size", 0),
                type=item.get("type", "file"),
            ))

        return files

    def file_exists(self, path: str) -> bool:
        """Check if a file exists.

        Args:
            path: File path relative to repo root.

        Returns:
            True if file exists, False otherwise.
        """
        try:
            self.get_file(path)
            return True
        except GitHubNotFoundError:
            return False
        except GitHubAPIError:
            # On other errors, assume file might exist
            return True

    def list_threads(self) -> list[str]:
        """List all thread topics (*.md files in root).

        Returns:
            List of topic names (without .md extension).
        """
        try:
            files = self.list_files("")
            return [
                f.name[:-3]  # Remove .md extension
                for f in files
                if f.type == "file" and f.name.endswith(".md")
            ]
        except GitHubNotFoundError:
            return []
