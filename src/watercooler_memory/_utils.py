"""Shared internal utilities for watercooler_memory."""

import hashlib
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_chunk_id(text: str, parent_id: str, index: int) -> str:
    """Generate a stable chunk ID based on content hash.

    Args:
        text: Chunk text content.
        parent_id: Parent node ID (entry_id or doc_id).
        index: Position index within parent.

    Returns:
        16-character hex string from SHA-256 hash.
    """
    content = f"{parent_id}:{index}:{text}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]
