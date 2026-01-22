"""Integration tests for hosted thread creation.

Tests the full flow of creating a new thread via say_hosted
with create_if_missing=True, verifying the per-thread format
output and proper separation of thread title vs entry title.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass


@dataclass
class MockFileContent:
    """Mock GitHub file content response."""

    content: str
    sha: str


class MockGitHubClient:
    """Mock GitHub client for testing hosted operations."""

    def __init__(self):
        self.files: dict[str, MockFileContent] = {}
        self.put_calls: list[dict] = []

    def get_file(self, path: str) -> MockFileContent:
        if path not in self.files:
            from watercooler_mcp.github_api import GitHubNotFoundError

            raise GitHubNotFoundError(f"File not found: {path}")
        return self.files[path]

    def put_file(
        self, path: str, content: str, message: str, sha: str | None = None
    ) -> str:
        self.put_calls.append(
            {
                "path": path,
                "content": content,
                "message": message,
                "sha": sha,
            }
        )
        new_sha = f"sha-{len(self.put_calls)}"
        self.files[path] = MockFileContent(content=content, sha=new_sha)
        return new_sha

    def file_exists(self, path: str) -> bool:
        return path in self.files

    def list_files(self, path: str) -> list:
        return []


@pytest.fixture
def mock_http_context():
    """Create mock HTTP context for hosted mode."""
    from watercooler_mcp.context import HttpRequestContext

    return HttpRequestContext(
        github_token="mock-token",
        repo="testorg/testrepo-threads",
        branch="main",
        user_id="test-user",
    )


class TestNewThreadCreation:
    """Tests for new thread creation via say_hosted."""

    def test_new_thread_uses_topic_as_title(self, mock_http_context):
        """When creating a new thread, thread title should be the topic, not entry title."""
        from watercooler_mcp.hosted_ops import say_hosted

        mock_client = MockGitHubClient()

        with patch(
            "watercooler_mcp.hosted_ops.get_http_context",
            return_value=mock_http_context,
        ):
            with patch(
                "watercooler_mcp.hosted_ops._get_github_client",
                return_value=(None, mock_client),
            ):
                with patch(
                    "watercooler_mcp.hosted_ops._sync_entry_to_slack_site",
                    return_value=False,
                ):
                    error, result = say_hosted(
                        topic="my-new-topic",
                        title="This is the entry title",  # Entry title, NOT thread title
                        body="Entry body content",
                        agent="TestAgent",
                        role="implementer",
                        entry_type="Note",
                        create_if_missing=True,
                    )

        assert error is None, f"Unexpected error: {error}"

        # Find the meta.json write
        meta_writes = [c for c in mock_client.put_calls if "meta.json" in c["path"]]
        assert len(meta_writes) >= 1, "Expected meta.json to be written"

        meta_content = json.loads(meta_writes[-1]["content"])

        # Thread title should be the TOPIC, not the entry title
        assert (
            meta_content["title"] == "my-new-topic"
        ), f"Thread title should be 'my-new-topic', got '{meta_content['title']}'"
        assert meta_content["topic"] == "my-new-topic"

    def test_new_thread_entry_has_correct_title(self, mock_http_context):
        """The entry within the new thread should have its own title."""
        from watercooler_mcp.hosted_ops import say_hosted

        mock_client = MockGitHubClient()

        with patch(
            "watercooler_mcp.hosted_ops.get_http_context",
            return_value=mock_http_context,
        ):
            with patch(
                "watercooler_mcp.hosted_ops._get_github_client",
                return_value=(None, mock_client),
            ):
                with patch(
                    "watercooler_mcp.hosted_ops._sync_entry_to_slack_site",
                    return_value=False,
                ):
                    error, result = say_hosted(
                        topic="another-topic",
                        title="My Entry Title",
                        body="Entry content here",
                        agent="Claude",
                        role="planner",
                        entry_type="Plan",
                        create_if_missing=True,
                    )

        assert error is None

        # Find the entries.jsonl write
        entries_writes = [
            c for c in mock_client.put_calls if "entries.jsonl" in c["path"]
        ]
        assert len(entries_writes) >= 1, "Expected entries.jsonl to be written"

        # Parse the JSONL content
        entries_content = entries_writes[-1]["content"]
        entries = [
            json.loads(line)
            for line in entries_content.strip().split("\n")
            if line.strip()
        ]

        assert len(entries) >= 1, "Expected at least one entry"
        entry = entries[-1]

        # Entry should have its own title
        assert entry["title"] == "My Entry Title"
        assert entry["agent"] == "Claude"
        assert entry["role"] == "planner"
        assert entry["entry_type"] == "Plan"  # entry_type, not type (type is "entry")

    def test_existing_thread_preserves_title(self, mock_http_context):
        """When adding to existing thread, thread title should be preserved."""
        from watercooler_mcp.hosted_ops import say_hosted

        mock_client = MockGitHubClient()

        # Pre-populate with existing thread
        existing_meta = {
            "id": "thread:existing-topic",
            "type": "thread",
            "topic": "existing-topic",
            "title": "Original Thread Title",  # This should be preserved
            "status": "OPEN",
            "ball": "Agent",
            "entry_count": 1,
        }
        mock_client.files["graph/baseline/threads/existing-topic/meta.json"] = (
            MockFileContent(content=json.dumps(existing_meta), sha="existing-sha")
        )
        mock_client.files["graph/baseline/threads/existing-topic/entries.jsonl"] = (
            MockFileContent(
                content=json.dumps(
                    {
                        "id": "entry:old-entry-id",
                        "type": "entry",
                        "entry_id": "old-entry-id",
                        "thread_topic": "existing-topic",
                        "index": 0,
                        "agent": "OldAgent",
                        "body": "Old entry",
                    }
                ),
                sha="entries-sha",
            )
        )

        with patch(
            "watercooler_mcp.hosted_ops.get_http_context",
            return_value=mock_http_context,
        ):
            with patch(
                "watercooler_mcp.hosted_ops._get_github_client",
                return_value=(None, mock_client),
            ):
                with patch(
                    "watercooler_mcp.hosted_ops._sync_entry_to_slack_site",
                    return_value=False,
                ):
                    error, result = say_hosted(
                        topic="existing-topic",
                        title="New Entry Title",  # New entry title
                        body="New entry body",
                        agent="NewAgent",
                        role="implementer",
                        entry_type="Note",
                        create_if_missing=True,
                    )

        assert error is None

        # Find the final meta.json write
        meta_writes = [c for c in mock_client.put_calls if "meta.json" in c["path"]]
        assert len(meta_writes) >= 1

        meta_content = json.loads(meta_writes[-1]["content"])

        # Thread title should be PRESERVED, not changed to new entry title
        assert (
            meta_content["title"] == "Original Thread Title"
        ), f"Thread title should be preserved as 'Original Thread Title', got '{meta_content['title']}'"


class TestThreadMetadataValidation:
    """Tests for meta.json field validation."""

    def test_entry_count_incremented(self, mock_http_context):
        """Entry count in meta.json should be incremented when adding entries."""
        from watercooler_mcp.hosted_ops import say_hosted

        mock_client = MockGitHubClient()

        with patch(
            "watercooler_mcp.hosted_ops.get_http_context",
            return_value=mock_http_context,
        ):
            with patch(
                "watercooler_mcp.hosted_ops._get_github_client",
                return_value=(None, mock_client),
            ):
                with patch(
                    "watercooler_mcp.hosted_ops._sync_entry_to_slack_site",
                    return_value=False,
                ):
                    # Create first entry
                    say_hosted(
                        topic="count-test",
                        title="Entry 1",
                        body="First",
                        agent="Agent",
                        create_if_missing=True,
                    )

        # Check entry_count
        meta_writes = [c for c in mock_client.put_calls if "meta.json" in c["path"]]
        meta_content = json.loads(meta_writes[-1]["content"])

        assert (
            meta_content.get("entry_count", 0) >= 1
        ), "entry_count should be at least 1"
