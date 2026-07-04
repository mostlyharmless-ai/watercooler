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
        repo="org/repo",
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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional


def _encode_contents_path(path: str) -> str:
    """URL-encode a Contents API path while preserving segment boundaries.

    GitHub's Contents API endpoint embeds the file path directly in the
    URL: ``/repos/{owner}/{repo}/contents/{path}``. Path segments may
    contain characters that are illegal in a URL (spaces, ``#``, ``?``,
    non-ASCII, etc.). Without encoding, ``urllib.request`` rejects the
    URL with ``"URL can't contain control characters"`` and the call
    fails opaquely.

    Topics produced by `mcpClient.say(create_if_missing=True)` from a
    well-behaved caller are kebab-case slugs and don't trigger this,
    but a future caller (Slack, custom integration, agent that bypasses
    the dashboard's slugify) could submit a path with spaces and crash
    the write. Encoding at the API boundary is defence-in-depth — the
    caller still ought to slugify, but a path that escapes that pass
    no longer takes the GitHub client down.

    Uses ``safe="/"`` so segment separators are preserved (a leading or
    embedded ``/`` is kept verbatim); only the in-segment characters get
    percent-encoded.
    """
    return urllib.parse.quote(path, safe="/")


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
            repo: Repository full name (e.g., "org/repo").
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
        endpoint = f"/repos/{self.owner}/{self.repo_name}/contents/{_encode_contents_path(path)}"
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
        endpoint = f"/repos/{self.owner}/{self.repo_name}/contents/{_encode_contents_path(path)}"

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

    def commit_files(
        self,
        files: list[tuple[str, str]],
        message: str,
        committer_name: Optional[str] = None,
        committer_email: Optional[str] = None,
        expected_blob_shas: Optional[dict[str, Optional[str]]] = None,
    ) -> tuple[str, dict[str, str]]:
        """Atomically commit multiple file changes in ONE git commit.

        Uses GitHub's Git Data API (refs / blobs / trees / commits) to
        write N files in a single commit. The Contents API ``put_file``
        produces one commit per file — calling it N times produces N
        commits and N push events, which fan out to N webhook deliveries
        downstream. ``commit_files`` produces ONE commit and ONE push,
        which is what most callers actually want when a logical
        operation touches multiple files (e.g. a per-thread graph
        write that updates ``meta.json`` + ``entries.jsonl`` +
        ``edges.jsonl`` + the ``.md`` projection together).

        The trade-off is more API round-trips per logical write
        (``ref → commit → blobs[N] → tree → commit → ref``) versus
        ``put_file``'s one round-trip per file. Both end up at roughly
        the same wall time for small N because the blob creates can run
        sequentially in well under the per-commit overhead they replace.
        The webhook-fanout reduction is the real win.

        Concurrency: by default ``commit_files`` only checks that the
        branch ref is unchanged between its OWN internal ref read
        (step 1) and the final ``PATCH`` (step 5). That window is
        narrower than the caller's transaction (caller read of the
        files happened earlier), which can mask a lost-write race
        when two callers compute their writes from the same starting
        state but commit sequentially. To match the prior
        ``put_file(sha=X)`` contract, callers should pass
        ``expected_blob_shas`` mapping each path they care about to
        the blob SHA they observed at THEIR read time. Step 1 of this
        method then verifies each expected SHA against the parent
        commit's tree before doing any blob/tree work; a mismatch
        raises ``GitHubConflictError`` so the caller retries from a
        fresh read. ``None`` as an expected SHA means the caller
        expects the file to NOT exist in the parent tree (creation
        case); a path that already exists in the parent tree under
        that key is treated as a conflict.

        Args:
            files: List of ``(path, content)`` tuples. ``content`` is
                a UTF-8 string. Paths are repo-root-relative
                (e.g. ``"graph/baseline/threads/foo/meta.json"``).
                An empty list raises ``ValueError``.
            message: Commit message.
            committer_name: Optional committer name override.
            committer_email: Optional committer email override.
            expected_blob_shas: Optional ``{path: blob_sha or None}``
                mapping. When provided, each entry is verified
                against the parent tree before the commit; a
                mismatch raises ``GitHubConflictError``. Paths in
                ``expected_blob_shas`` need not be a subset of
                ``files`` — a caller can guard a file it isn't
                writing against drift, e.g. asserting
                ``edges.jsonl`` is unchanged while only writing
                ``entries.jsonl``.

        Returns:
            ``(commit_sha, blob_shas)`` where ``blob_shas`` maps each
            written path to its new blob SHA. The blob SHA is the
            value GitHub's Contents API returns as the file's ``sha``,
            so callers that need a follow-up SHA-based ``put_file``
            can use it without a re-read.

        Raises:
            GitHubConflictError: If ``expected_blob_shas`` did not
                match the parent tree, OR if the branch ref moved
                between read and update. Caller should retry from a
                fresh read.
            GitHubAPIError: On other API errors.
        """
        if not files:
            raise ValueError("commit_files requires at least one file")
        if not self.branch:
            raise ValueError("commit_files requires self.branch to be set")

        repo_path = f"/repos/{self.owner}/{self.repo_name}"

        # 1. Read the current ref tip + its tree SHA. The ref is the
        #    parent of the new commit; its tree is the base for the new
        #    tree (so unmodified files are inherited rather than
        #    re-listed).
        ref = self._make_request("GET", f"{repo_path}/git/ref/heads/{self.branch}")
        parent_sha = ref.get("object", {}).get("sha")
        if not parent_sha:
            raise GitHubAPIError(
                f"Could not resolve ref heads/{self.branch}: missing object.sha",
            )
        parent_commit = self._make_request("GET", f"{repo_path}/git/commits/{parent_sha}")
        base_tree_sha = parent_commit.get("tree", {}).get("sha")
        if not base_tree_sha:
            raise GitHubAPIError(
                f"Could not resolve base tree from commit {parent_sha}",
            )

        # 1b. (Optional) Per-file conflict check against the parent
        #     tree. Closes the lost-write window between the caller's
        #     original read and our internal ref read by validating
        #     each expected blob SHA before doing any commit work.
        #     A mismatch means another writer already advanced one of
        #     the files we're about to overwrite, so retrying from a
        #     fresh read is the correct response — same as the prior
        #     ``put_file(sha=X)`` 422 → ``GitHubConflictError`` path.
        if expected_blob_shas:
            tree_index = self._build_tree_path_index(base_tree_sha)
            for path, expected in expected_blob_shas.items():
                actual = tree_index.get(path)
                if expected is None:
                    # Caller expects the file to be absent at the
                    # parent commit (creation case). If it's there,
                    # someone else created it between caller's read
                    # and our internal ref read — conflict.
                    if actual is not None:
                        raise GitHubConflictError(
                            f"commit_files: caller expected {path!r} to be absent "
                            f"at parent {parent_sha[:8]}, but found blob {actual[:8]} — "
                            "concurrent write detected. Retry from a fresh read.",
                            status_code=409,
                        )
                else:
                    if actual != expected:
                        raise GitHubConflictError(
                            f"commit_files: blob SHA mismatch for {path!r} "
                            f"at parent {parent_sha[:8]}: expected {expected[:8] if expected else None}, "
                            f"got {actual[:8] if actual else None} — concurrent "
                            "write detected. Retry from a fresh read.",
                            status_code=409,
                        )

        # 2. Create a blob for each file. Blobs hold the file contents;
        #    the tree (next step) maps paths to blob SHAs. We also
        #    accumulate ``blob_shas`` so the caller can use them as
        #    Contents-API ``sha`` values for follow-up writes without
        #    re-reading the file.
        tree_entries: list[dict] = []
        blob_shas: dict[str, str] = {}
        for path, content in files:
            blob = self._make_request(
                "POST",
                f"{repo_path}/git/blobs",
                data={"content": content, "encoding": "utf-8"},
            )
            blob_sha = blob.get("sha")
            if not blob_sha:
                raise GitHubAPIError(
                    f"Blob create returned no sha for path {path!r}",
                )
            tree_entries.append({
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })
            blob_shas[path] = blob_sha

        # 3. Create the new tree based on the parent's tree. Files not
        #    in our list are inherited from base_tree unchanged.
        tree = self._make_request(
            "POST",
            f"{repo_path}/git/trees",
            data={"base_tree": base_tree_sha, "tree": tree_entries},
        )
        new_tree_sha = tree.get("sha")
        if not new_tree_sha:
            raise GitHubAPIError("Tree create returned no sha")

        # 4. Create the commit pointing at the new tree, with the prior
        #    ref tip as its parent. This commit is unreachable until the
        #    ref is updated in step 5.
        commit_data: dict = {
            "message": message,
            "tree": new_tree_sha,
            "parents": [parent_sha],
        }
        if committer_name and committer_email:
            commit_data["author"] = {"name": committer_name, "email": committer_email}
            commit_data["committer"] = {"name": committer_name, "email": committer_email}
        commit = self._make_request(
            "POST",
            f"{repo_path}/git/commits",
            data=commit_data,
        )
        new_commit_sha = commit.get("sha")
        if not new_commit_sha:
            raise GitHubAPIError("Commit create returned no sha")

        # 5. Fast-forward the ref to the new commit. PATCH (without
        #    ``force``) requires fast-forward semantics — if the ref has
        #    moved since our step-1 read (concurrent writer), GitHub
        #    returns 422 and we surface ``GitHubConflictError`` to match
        #    the ``put_file`` SHA-mismatch contract.
        try:
            self._make_request(
                "PATCH",
                f"{repo_path}/git/refs/heads/{self.branch}",
                data={"sha": new_commit_sha},
            )
        except GitHubAPIError as e:
            if getattr(e, "status_code", None) == 422:
                raise GitHubConflictError(
                    f"Branch {self.branch} moved during commit_files; "
                    "concurrent writer detected. Retry from a fresh read.",
                    status_code=422,
                    response=getattr(e, "response", None),
                ) from e
            raise

        return new_commit_sha, blob_shas

    def _build_tree_path_index(self, tree_sha: str) -> dict[str, str]:
        """Walk a tree (recursively) and return ``{full_path: blob_sha}``.

        Used by ``commit_files`` to validate caller-supplied
        ``expected_blob_shas`` against the parent commit's tree before
        producing any blob/tree work. One ``recursive=1`` request
        gets the whole snapshot in a single round-trip — much cheaper
        than walking subtrees one level at a time, especially for
        nested paths like ``graph/baseline/threads/<topic>/meta.json``
        that would otherwise need 4-5 sequential GETs.

        GitHub truncates the recursive response when a tree exceeds
        ~100k entries or ~7MB. For the orphan-branch threads format
        (a few thousand files in steady state) this fits well within
        the limit. If the response IS truncated, we raise
        ``GitHubAPIError`` rather than returning a partial map: a
        partial map would silently miss conflicts on the unlisted
        paths, which is exactly the failure mode the caller asked us
        to prevent. A truncated response means the caller needs to
        either accept reduced concurrency safety (drop
        ``expected_blob_shas``) or use a different validation
        strategy.
        """
        repo_path = f"/repos/{self.owner}/{self.repo_name}"
        data = self._make_request(
            "GET", f"{repo_path}/git/trees/{tree_sha}?recursive=1"
        )
        if data.get("truncated"):
            raise GitHubAPIError(
                f"Tree {tree_sha[:8]} is too large for recursive listing "
                "(truncated). Per-blob conflict check is not safe on a "
                "partial map; caller must use a different validation "
                "strategy.",
            )
        index: dict[str, str] = {}
        for item in data.get("tree", []):
            if item.get("type") == "blob":
                p = item.get("path")
                s = item.get("sha")
                if isinstance(p, str) and isinstance(s, str):
                    index[p] = s
        return index

    def delete_file(
        self,
        path: str,
        message: str,
        sha: str,
    ) -> None:
        """Delete a file on GitHub.

        Args:
            path: File path relative to repo root.
            message: Commit message.
            sha: Current file SHA (required).

        Raises:
            GitHubAPIError: On API errors.
        """
        endpoint = f"/repos/{self.owner}/{self.repo_name}/contents/{_encode_contents_path(path)}"
        data = {
            "message": message,
            "sha": sha,
            "branch": self.branch,
        }
        self._make_request("DELETE", endpoint, data)

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
        endpoint = f"/repos/{self.owner}/{self.repo_name}/contents/{_encode_contents_path(path)}"
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
