"""Conflict detection and resolution for sync operations.

This module provides:
- ConflictType enum: Types of git conflicts
- ConflictInfo dataclass: Information about detected conflicts
- Pure merge functions: Content-level merging strategies
- ConflictResolver class: Unified conflict resolution API

Merge strategies:
- Thread files (.md): Entry-level merge by Entry-ID (ULID)
- Manifest files (manifest.json): Take newer timestamp, merge topics
- JSONL files (nodes.jsonl, edges.jsonl): Deduplicate by UUID
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from git import Repo
from git.exc import GitCommandError

from ..observability import log_debug
from .state import STATE_FILE_NAME


# =============================================================================
# Enums
# =============================================================================


class ConflictType(str, Enum):
    """Type of git conflict."""

    NONE = "none"  # No conflict
    MERGE = "merge"  # Standard merge conflict
    REBASE = "rebase"  # Rebase conflict
    CHERRY_PICK = "cherry_pick"  # Cherry-pick conflict


class ConflictScope(str, Enum):
    """Scope of conflicting files."""

    NONE = "none"  # No conflicts
    GRAPH_ONLY = "graph_only"  # Only graph/baseline/ files
    THREAD_ONLY = "thread_only"  # Only .md thread files
    STATE_ONLY = "state_only"  # Only state file (branch_parity_state.json)
    MIXED = "mixed"  # Mix of file types


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ConflictInfo:
    """Information about detected conflicts.

    Attributes:
        conflict_type: Type of conflict (merge, rebase, etc.)
        scope: Scope of conflicting files
        conflicting_files: List of files with conflicts
        can_auto_resolve: Whether conflicts can be auto-resolved
        resolution_strategy: Suggested resolution strategy
    """

    conflict_type: ConflictType = ConflictType.NONE
    scope: ConflictScope = ConflictScope.NONE
    conflicting_files: List[str] = field(default_factory=list)
    can_auto_resolve: bool = False
    resolution_strategy: Optional[str] = None

    @property
    def has_conflicts(self) -> bool:
        """Check if there are any conflicts."""
        return len(self.conflicting_files) > 0


# =============================================================================
# Pure Merge Functions
# =============================================================================


def merge_manifest_content(ours_content: str, theirs_content: str) -> str:
    """Pure function to merge manifest.json content.

    Merge strategy:
    - version: Take from ours
    - last_updated: Take max (newer timestamp)
    - topics_synced: Merge both dicts (theirs overwrites ours for same keys)
    - Other fields: Take from ours (generated_at, source_dir, etc.)

    Args:
        ours_content: JSON string of our version
        theirs_content: JSON string of their version

    Returns:
        Merged JSON string with pretty formatting

    Raises:
        json.JSONDecodeError: If content is not valid JSON
    """
    ours_json = json.loads(ours_content)
    theirs_json = json.loads(theirs_content)

    merged = {
        **ours_json,  # Start with ours (includes all base fields)
        "last_updated": max(
            ours_json.get("last_updated", ""),
            theirs_json.get("last_updated", ""),
        ),
        "topics_synced": {
            **ours_json.get("topics_synced", {}),
            **theirs_json.get("topics_synced", {}),
        },
    }

    return json.dumps(merged, indent=2) + "\n"


def merge_jsonl_content(ours_content: str, theirs_content: str) -> str:
    """Pure function to merge JSONL content by deduplicating entries by UUID.

    Both nodes.jsonl and edges.jsonl are additive - entries from both
    sides can coexist. We deduplicate by UUID to handle any duplicates.

    Args:
        ours_content: JSONL string of our version
        theirs_content: JSONL string of their version

    Returns:
        Merged JSONL string with deduplicated entries
    """
    seen_uuids: set[str] = set()
    merged_lines = []

    # Process ours first, then theirs
    for content in [ours_content, theirs_content]:
        for line in content.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                uuid = entry.get("uuid") or entry.get("id")
                if uuid and uuid not in seen_uuids:
                    seen_uuids.add(uuid)
                    merged_lines.append(json.dumps(entry))
            except json.JSONDecodeError:
                continue

    return "\n".join(merged_lines) + "\n" if merged_lines else ""


def merge_sync_state_content(ours_content: str, theirs_content: str) -> str:
    """Pure function to merge graph/baseline/sync_state.json content.

    This file tracks sync status per topic. Merge strategy:
    - For each topic, take the version with higher entries_synced count
    - If counts equal, take version with more recent last_sync_at timestamp
    - Merge all unique topics from both sides
    - Take the most recent last_updated timestamp

    Args:
        ours_content: JSON string of our version
        theirs_content: JSON string of their version

    Returns:
        Merged JSON string with pretty formatting

    Raises:
        json.JSONDecodeError: If content is not valid JSON
    """
    ours_json = json.loads(ours_content)
    theirs_json = json.loads(theirs_content)

    # Merge topics - prefer higher entry count, then more recent timestamp
    merged_topics: Dict[str, Any] = {}
    all_topics = set(ours_json.get("topics", {}).keys()) | set(
        theirs_json.get("topics", {}).keys()
    )

    for topic in all_topics:
        ours_topic = ours_json.get("topics", {}).get(topic, {})
        theirs_topic = theirs_json.get("topics", {}).get(topic, {})

        ours_count = ours_topic.get("entries_synced", 0) or 0
        theirs_count = theirs_topic.get("entries_synced", 0) or 0

        if ours_count > theirs_count:
            merged_topics[topic] = ours_topic
        elif theirs_count > ours_count:
            merged_topics[topic] = theirs_topic
        else:
            # Equal counts - prefer more recent timestamp
            ours_time = ours_topic.get("last_sync_at", "")
            theirs_time = theirs_topic.get("last_sync_at", "")
            if ours_time >= theirs_time:
                merged_topics[topic] = ours_topic if ours_topic else theirs_topic
            else:
                merged_topics[topic] = theirs_topic if theirs_topic else ours_topic

    # Build merged result
    merged = {
        "topics": merged_topics,
        "last_updated": max(
            ours_json.get("last_updated", ""),
            theirs_json.get("last_updated", ""),
        ),
    }

    return json.dumps(merged, indent=2) + "\n"


def merge_thread_content(ours_content: str, theirs_content: str) -> Tuple[str, bool]:
    """Pure function to merge two thread file versions at entry level.

    Entries are identified by their Entry-ID (ULID in HTML comment).
    This allows merging non-overlapping entries without conflict.

    Strategy:
    - Parse both versions into header + entries
    - Header: take theirs (metadata is more recent)
    - Entries identified by Entry-ID
    - Non-overlapping entries: merge (union of unique entries)
    - Same Entry-ID with different content: TRUE CONFLICT

    Args:
        ours_content: Our version of the thread file
        theirs_content: Their version of the thread file

    Returns:
        Tuple of (merged_content, had_true_conflicts)
        - merged_content: The merged thread file if successful, empty if conflicts
        - had_true_conflicts: True if same Entry-ID has different content
    """
    from watercooler.thread_entries import parse_thread_entries

    # Parse both versions
    ours_entries = parse_thread_entries(ours_content)
    theirs_entries = parse_thread_entries(theirs_content)

    # Build maps by Entry-ID
    ours_by_id: Dict[str, Any] = {}
    theirs_by_id: Dict[str, Any] = {}

    for entry in ours_entries:
        if entry.entry_id:
            ours_by_id[entry.entry_id] = entry

    for entry in theirs_entries:
        if entry.entry_id:
            theirs_by_id[entry.entry_id] = entry

    # Check for true conflicts (same ID, different content)
    for entry_id, ours_entry in ours_by_id.items():
        if entry_id in theirs_by_id:
            theirs_entry = theirs_by_id[entry_id]
            # Compare body content (normalized)
            if ours_entry.body.strip() != theirs_entry.body.strip():
                log_debug(
                    f"[CONFLICT] True conflict: Entry-ID {entry_id} has different body"
                )
                return "", True
            # Compare title
            if (ours_entry.title or "").strip() != (theirs_entry.title or "").strip():
                log_debug(
                    f"[CONFLICT] True conflict: Entry-ID {entry_id} has different title"
                )
                return "", True
            # Compare entry type
            if (ours_entry.entry_type or "").strip() != (
                theirs_entry.entry_type or ""
            ).strip():
                log_debug(
                    f"[CONFLICT] True conflict: Entry-ID {entry_id} has different type"
                )
                return "", True
            # Compare role
            if (ours_entry.role or "").strip() != (theirs_entry.role or "").strip():
                log_debug(
                    f"[CONFLICT] True conflict: Entry-ID {entry_id} has different role"
                )
                return "", True

    # No true conflicts - merge entries
    # Use theirs' header (more recent metadata)
    header_end = _find_header_end(theirs_content)
    merged_header = theirs_content[:header_end] if header_end > 0 else ""

    # Collect all unique entries by ID
    all_entries: Dict[str, Any] = {}

    # Add ours first
    for entry in ours_entries:
        key = entry.entry_id or f"_no_id_{entry.index}"
        all_entries[key] = entry

    # Add theirs (overwriting duplicates is fine since content matches)
    for entry in theirs_entries:
        key = entry.entry_id or f"_no_id_{entry.index}"
        all_entries[key] = entry

    # Sort entries by timestamp
    def sort_key(entry: Any) -> Tuple[int, str]:
        has_timestamp = 0 if entry.timestamp else 1
        timestamp = entry.timestamp or ""
        return (has_timestamp, timestamp)

    sorted_entries = sorted(all_entries.values(), key=sort_key)

    # Reconstruct thread file
    lines = [merged_header] if merged_header else []

    for entry in sorted_entries:
        # Add separator
        lines.append("\n---\n")

        # Reconstruct entry header
        entry_line = f"Entry: {entry.agent}"
        if entry.timestamp:
            entry_line += f" {entry.timestamp}"
        lines.append(entry_line + "\n")

        if entry.role:
            lines.append(f"Role: {entry.role}\n")
        if entry.entry_type:
            lines.append(f"Type: {entry.entry_type}\n")
        if entry.title:
            lines.append(f"Title: {entry.title}\n")

        # Add body
        if entry.body:
            lines.append("\n" + entry.body.strip() + "\n")

    merged = "".join(lines)
    log_debug(f"[CONFLICT] Successfully merged {len(sorted_entries)} entries")
    return merged, False


def _find_header_end(content: str) -> int:
    """Find the byte offset where the thread header ends (first ---).

    Args:
        content: Thread file content

    Returns:
        Byte offset of the first --- separator, or 0 if not found
    """
    lines = content.split("\n")
    offset = 0
    for line in lines:
        if line.strip() == "---":
            return offset
        offset += len(line) + 1  # +1 for newline
    return 0


# =============================================================================
# Conflict Resolver
# =============================================================================


class ConflictResolver:
    """Unified conflict resolution API.

    Provides methods to:
    - Detect conflicts and their scope
    - Check if auto-resolution is possible
    - Perform auto-resolution for different file types
    - Abort resolution if needed

    Usage:
        resolver = ConflictResolver(repo)
        info = resolver.detect()
        if info.can_auto_resolve:
            success = resolver.auto_resolve()
    """

    def __init__(self, repo: Repo):
        """Initialize conflict resolver.

        Args:
            repo: Git repository with potential conflicts
        """
        self.repo = repo
        self._repo_path = Path(repo.working_dir)

    def detect(self) -> ConflictInfo:
        """Detect conflicts and return detailed information.

        Returns:
            ConflictInfo with conflict type, scope, files, and resolution hints
        """
        # Get conflict type
        conflict_type = self._get_conflict_type()
        if conflict_type == ConflictType.NONE:
            return ConflictInfo()

        # Get conflicting files
        conflicting_files = self._get_conflicting_files()
        if not conflicting_files:
            return ConflictInfo()

        # Determine scope
        scope = self._determine_scope(conflicting_files)

        # Determine if auto-resolution is possible
        can_auto_resolve = scope in (
            ConflictScope.GRAPH_ONLY,
            ConflictScope.THREAD_ONLY,
            ConflictScope.STATE_ONLY,
        )

        # Determine resolution strategy
        strategy = None
        if scope == ConflictScope.GRAPH_ONLY:
            strategy = "deduplicate_by_uuid"
        elif scope == ConflictScope.THREAD_ONLY:
            strategy = "merge_by_entry_id"
        elif scope == ConflictScope.STATE_ONLY:
            strategy = "take_theirs"

        return ConflictInfo(
            conflict_type=conflict_type,
            scope=scope,
            conflicting_files=conflicting_files,
            can_auto_resolve=can_auto_resolve,
            resolution_strategy=strategy,
        )

    def can_auto_resolve(self, info: Optional[ConflictInfo] = None) -> bool:
        """Check if conflicts can be auto-resolved.

        Args:
            info: Optional ConflictInfo (will detect if not provided)

        Returns:
            True if auto-resolution is possible
        """
        if info is None:
            info = self.detect()
        return info.can_auto_resolve

    def auto_resolve(self, info: Optional[ConflictInfo] = None) -> bool:
        """Auto-resolve conflicts based on file type.

        Dispatches to appropriate resolver based on conflict scope.

        Args:
            info: Optional ConflictInfo (will detect if not provided)

        Returns:
            True if all conflicts resolved successfully
        """
        if info is None:
            info = self.detect()

        if not info.has_conflicts:
            return True

        if info.scope == ConflictScope.GRAPH_ONLY:
            return self.resolve_graph_conflicts()
        elif info.scope == ConflictScope.THREAD_ONLY:
            return self.resolve_thread_conflicts()
        elif info.scope == ConflictScope.STATE_ONLY:
            return self.resolve_state_conflicts()
        else:
            log_debug(f"[CONFLICT] Cannot auto-resolve mixed conflicts")
            return False

    def resolve_graph_conflicts(self) -> bool:
        """Auto-resolve conflicts in graph files using smart merge strategy.

        Handles:
        - manifest.json: Take newer timestamp, merge topics
        - sync_state.json: Take version with higher entry count per topic
        - *.jsonl: Deduplicate by UUID

        Returns:
            True if all conflicts resolved successfully
        """
        conflicting_files = self._get_conflicting_files()
        if not conflicting_files:
            return True

        for file_rel in conflicting_files:
            file_path = self._repo_path / file_rel

            if not file_path.exists():
                log_debug(f"[CONFLICT] Conflicted file doesn't exist: {file_rel}")
                return False

            if file_path.name == "manifest.json":
                if not self._merge_manifest_file(file_path, file_rel):
                    return False
            elif file_path.name == "sync_state.json":
                if not self._merge_sync_state_file(file_path, file_rel):
                    return False
            elif file_path.suffix == ".jsonl":
                if not self._merge_jsonl_file(file_path, file_rel):
                    return False
            else:
                log_debug(f"[CONFLICT] Unknown graph file type: {file_path.name}")
                return False

            # Stage resolved file
            self._stage_resolved_file(file_rel)

        return self._complete_merge_or_rebase("graph conflicts")

    def resolve_thread_conflicts(self) -> bool:
        """Auto-resolve conflicts in thread markdown files.

        Uses entry-level merge by Entry-ID.

        Returns:
            True if all conflicts resolved successfully
        """
        conflicting_files = self._get_conflicting_files()
        if not conflicting_files:
            return True

        for file_rel in conflicting_files:
            file_path = self._repo_path / file_rel

            if not file_path.exists():
                log_debug(f"[CONFLICT] Conflicted file doesn't exist: {file_rel}")
                return False

            if not file_rel.endswith(".md"):
                log_debug(f"[CONFLICT] Non-.md file in thread conflicts: {file_rel}")
                return False

            if not self._merge_thread_file(file_path, file_rel):
                return False

            # Stage resolved file
            self._stage_resolved_file(file_rel)

        return self._complete_merge_or_rebase("thread conflicts")

    def resolve_state_conflicts(self) -> bool:
        """Auto-resolve conflicts in state file using take-theirs strategy.

        The branch_parity_state.json file is metadata that tracks sync state.
        When conflicts occur (typically from concurrent sessions), we always
        take the remote version (theirs) because:
        1. It represents the most recent successful sync
        2. State is regenerated on each operation anyway
        3. Local state may be stale from an interrupted operation

        Returns:
            True if state file conflict resolved successfully
        """
        conflicting_files = self._get_conflicting_files()
        if not conflicting_files:
            return True

        for file_rel in conflicting_files:
            if file_rel != STATE_FILE_NAME:
                log_debug(f"[CONFLICT] Non-state file in state conflicts: {file_rel}")
                return False

            try:
                # Take the remote version (theirs) - last-write-wins for metadata
                self.repo.git.checkout("--theirs", file_rel)
                log_debug(f"[CONFLICT] Resolved state file conflict: {file_rel} (took theirs)")

                # Stage the resolved file
                self._stage_resolved_file(file_rel)

            except GitCommandError as e:
                log_debug(f"[CONFLICT] Failed to resolve state file: {e}")
                return False

        return self._complete_merge_or_rebase("state file conflict")

    def abort_resolution(self) -> bool:
        """Abort the current merge or rebase.

        Returns:
            True if abort succeeded
        """
        try:
            git_dir = Path(self.repo.git_dir)
            if (git_dir / "rebase-merge").exists() or (
                git_dir / "rebase-apply"
            ).exists():
                self.repo.git.rebase("--abort")
                log_debug("[CONFLICT] Aborted rebase")
            elif (git_dir / "MERGE_HEAD").exists():
                self.repo.git.merge("--abort")
                log_debug("[CONFLICT] Aborted merge")
            elif (git_dir / "CHERRY_PICK_HEAD").exists():
                self.repo.git.cherry_pick("--abort")
                log_debug("[CONFLICT] Aborted cherry-pick")
            return True
        except GitCommandError as e:
            log_debug(f"[CONFLICT] Failed to abort: {e}")
            return False

    # -------------------------------------------------------------------------
    # Private Methods
    # -------------------------------------------------------------------------

    def _get_conflict_type(self) -> ConflictType:
        """Determine the type of conflict based on git state."""
        git_dir = Path(self.repo.git_dir)

        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            return ConflictType.REBASE
        elif (git_dir / "CHERRY_PICK_HEAD").exists():
            return ConflictType.CHERRY_PICK
        elif (git_dir / "MERGE_HEAD").exists():
            return ConflictType.MERGE
        elif self._has_conflict_markers():
            return ConflictType.MERGE  # Assume merge if markers present
        else:
            return ConflictType.NONE

    def _has_conflict_markers(self) -> bool:
        """Check if repo has unresolved conflict markers."""
        try:
            status = self.repo.git.status("--porcelain")
            for line in status.split("\n"):
                if line and len(line) >= 2:
                    xy = line[:2]
                    if "U" in xy or xy == "AA" or xy == "DD":
                        return True
            return False
        except GitCommandError:
            return False

    def _get_conflicting_files(self) -> List[str]:
        """Get list of files with conflicts."""
        try:
            status = self.repo.git.status("--porcelain")
            conflicted = []

            for line in status.split("\n"):
                if line and len(line) >= 2:
                    xy = line[:2]
                    if "U" in xy or xy == "AA" or xy == "DD":
                        file_path = line[3:].strip()
                        conflicted.append(file_path)

            return conflicted
        except GitCommandError:
            return []

    def _determine_scope(self, files: List[str]) -> ConflictScope:
        """Determine the scope of conflicts based on file paths."""
        if not files:
            return ConflictScope.NONE

        all_graph = all(f.startswith("graph/baseline/") for f in files)
        all_thread = all(f.endswith(".md") and not f.startswith("graph/") for f in files)
        all_state = all(f == STATE_FILE_NAME for f in files)

        if all_state:
            return ConflictScope.STATE_ONLY
        elif all_graph:
            return ConflictScope.GRAPH_ONLY
        elif all_thread:
            return ConflictScope.THREAD_ONLY
        else:
            return ConflictScope.MIXED

    def _merge_manifest_file(self, file_path: Path, file_rel: str) -> bool:
        """Merge a manifest.json file."""
        try:
            ours_content = self.repo.git.show(f":2:{file_rel}")
            theirs_content = self.repo.git.show(f":3:{file_rel}")
            merged_content = merge_manifest_content(ours_content, theirs_content)
            file_path.write_text(merged_content)
            log_debug(f"[CONFLICT] Auto-merged manifest: {file_rel}")
            return True
        except GitCommandError as e:
            log_debug(f"[CONFLICT] Git error in manifest merge: {e}")
            return False
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log_debug(f"[CONFLICT] Parse error in manifest merge: {e}")
            return False
        except OSError as e:
            log_debug(f"[CONFLICT] IO error in manifest merge: {e}")
            return False

    def _merge_jsonl_file(self, file_path: Path, file_rel: str) -> bool:
        """Merge a JSONL file."""
        try:
            ours_content = self.repo.git.show(f":2:{file_rel}")
            theirs_content = self.repo.git.show(f":3:{file_rel}")
            merged_content = merge_jsonl_content(ours_content, theirs_content)
            file_path.write_text(merged_content)
            log_debug(f"[CONFLICT] Auto-merged JSONL: {file_rel}")
            return True
        except GitCommandError as e:
            log_debug(f"[CONFLICT] Git error in JSONL merge: {e}")
            return False
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log_debug(f"[CONFLICT] Parse error in JSONL merge: {e}")
            return False
        except OSError as e:
            log_debug(f"[CONFLICT] IO error in JSONL merge: {e}")
            return False

    def _merge_sync_state_file(self, file_path: Path, file_rel: str) -> bool:
        """Merge a graph/baseline/sync_state.json file."""
        try:
            ours_content = self.repo.git.show(f":2:{file_rel}")
            theirs_content = self.repo.git.show(f":3:{file_rel}")
            merged_content = merge_sync_state_content(ours_content, theirs_content)
            file_path.write_text(merged_content)
            log_debug(f"[CONFLICT] Auto-merged sync_state.json: {file_rel}")
            return True
        except GitCommandError as e:
            log_debug(f"[CONFLICT] Git error in sync_state merge: {e}")
            return False
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log_debug(f"[CONFLICT] Parse error in sync_state merge: {e}")
            return False
        except OSError as e:
            log_debug(f"[CONFLICT] IO error in sync_state merge: {e}")
            return False

    def _merge_thread_file(self, file_path: Path, file_rel: str) -> bool:
        """Merge a thread markdown file."""
        try:
            ours_content = self.repo.git.show(f":2:{file_rel}")
            theirs_content = self.repo.git.show(f":3:{file_rel}")
            merged_content, had_conflicts = merge_thread_content(
                ours_content, theirs_content
            )

            if had_conflicts:
                log_debug(
                    f"[CONFLICT] Thread {file_path.name} has true conflicts "
                    f"(same Entry-ID, different content)"
                )
                return False

            file_path.write_text(merged_content)
            log_debug(f"[CONFLICT] Auto-merged thread: {file_rel}")
            return True
        except GitCommandError as e:
            log_debug(f"[CONFLICT] Git error in thread merge: {e}")
            return False
        except (ValueError, AttributeError) as e:
            log_debug(f"[CONFLICT] Parse error in thread merge: {e}")
            return False
        except OSError as e:
            log_debug(f"[CONFLICT] IO error in thread merge: {e}")
            return False

    def _stage_resolved_file(self, file_rel: str) -> None:
        """Stage a resolved file, clearing conflict stages first."""
        try:
            # During rebase, index has 3 stages that prevent normal add
            try:
                self.repo.git.rm("--cached", file_rel)
            except Exception:
                pass  # May fail if not in conflict state
            self.repo.index.add([file_rel])
            log_debug(f"[CONFLICT] Staged resolved file: {file_rel}")
        except Exception as e:
            log_debug(f"[CONFLICT] Failed to stage {file_rel}: {e}")

    def _complete_merge_or_rebase(self, context: str) -> bool:
        """Complete the merge or rebase after resolving conflicts.

        Sets up non-interactive environment to prevent git from:
        - Opening an editor for commit messages
        - Prompting for user input
        - Hanging on credential prompts
        """
        try:
            git_dir = Path(self.repo.git_dir)
            is_rebase = (git_dir / "rebase-merge").exists() or (
                git_dir / "rebase-apply"
            ).exists()

            # Set up environment for non-interactive operation
            # Critical for MCP server context where there's no TTY
            env = os.environ.copy()
            env["GIT_EDITOR"] = "true"  # No-op editor - prevents editor opening
            env["GIT_TERMINAL_PROMPT"] = "0"  # Disable terminal prompts
            env["GCM_INTERACTIVE"] = "never"  # Disable credential manager prompts
            # Provide fallback identity if not configured (needed for CI/test environments)
            env.setdefault("GIT_AUTHOR_NAME", "Watercooler Auto-Merge")
            env.setdefault("GIT_AUTHOR_EMAIL", "noreply@watercooler.local")
            env.setdefault("GIT_COMMITTER_NAME", "Watercooler Auto-Merge")
            env.setdefault("GIT_COMMITTER_EMAIL", "noreply@watercooler.local")

            if is_rebase:
                log_debug(f"[CONFLICT] Running rebase --continue for {context}")
                self.repo.git.rebase("--continue", env=env)
                log_debug(f"[CONFLICT] Continued rebase after resolving {context}")
            else:
                log_debug(f"[CONFLICT] Running commit for {context}")
                self.repo.git.commit("-m", f"Auto-merge {context}", env=env)
                log_debug(f"[CONFLICT] Committed merged {context}")

            return True
        except GitCommandError as e:
            # Log full error details for debugging
            log_debug(f"[CONFLICT] Failed to complete merge/rebase for {context}")
            log_debug(f"[CONFLICT] GitCommandError: {e}")
            log_debug(f"[CONFLICT] Command: {getattr(e, 'command', 'unknown')}")
            log_debug(f"[CONFLICT] Stderr: {getattr(e, 'stderr', 'none')}")
            return False


# =============================================================================
# Convenience Functions
# =============================================================================


def has_conflicts(repo: Repo) -> bool:
    """Check if repo has unresolved merge/rebase conflicts.

    This is a convenience function. For detailed info, use ConflictResolver.

    Args:
        repo: Git repository

    Returns:
        True if there are unresolved conflicts
    """
    try:
        status = repo.git.status("--porcelain")
        for line in status.split("\n"):
            if line and len(line) >= 2:
                xy = line[:2]
                if "U" in xy or xy == "AA" or xy == "DD":
                    return True
        return False
    except GitCommandError:
        return False


def has_graph_conflicts_only(repo: Repo) -> bool:
    """Check if all conflicts are confined to graph/baseline/ files.

    Args:
        repo: Git repository

    Returns:
        True if conflicts exist AND all are in graph/baseline/
    """
    resolver = ConflictResolver(repo)
    info = resolver.detect()
    return info.has_conflicts and info.scope == ConflictScope.GRAPH_ONLY


def has_thread_conflicts_only(repo: Repo) -> bool:
    """Check if all conflicts are in thread markdown files.

    Args:
        repo: Git repository

    Returns:
        True if conflicts exist AND all are .md files not in graph/
    """
    resolver = ConflictResolver(repo)
    info = resolver.detect()
    return info.has_conflicts and info.scope == ConflictScope.THREAD_ONLY


def has_state_conflicts_only(repo: Repo) -> bool:
    """Check if all conflicts are in the state file only.

    This is useful for detecting conflicts that can be safely auto-resolved
    by taking the remote version (concurrent session metadata conflicts).

    Args:
        repo: Git repository

    Returns:
        True if conflicts exist AND all are in branch_parity_state.json
    """
    resolver = ConflictResolver(repo)
    info = resolver.detect()
    return info.has_conflicts and info.scope == ConflictScope.STATE_ONLY
