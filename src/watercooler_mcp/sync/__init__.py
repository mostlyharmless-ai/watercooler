"""Sync package for watercooler-cloud git operations.

This package provides a clean, modular architecture for git synchronization:

Layers:
1. primitives - Pure git operations (no state, no side effects)
2. state - Unified state management with live checks
3. conflict - Conflict detection and resolution
4. local_remote - Single-repo sync operations (L2R)
5. branch_parity - Cross-repo coordination (T2C)
6. async_coordinator - Background sync operations
7. errors - Rich exception hierarchy

The old git_sync.py and branch_parity.py modules are preserved as
thin facades for backward compatibility.
"""

from .errors import (
    SyncError,
    PullError,
    PushError,
    ConflictError,
    BranchPairingError,
    LockError,
    NetworkError,
    AuthenticationError,
)

from .primitives import (
    # Constants
    MAX_PUSH_RETRIES,
    MAX_BRANCH_LENGTH,
    INVALID_BRANCH_PATTERNS,
    # Validation
    validate_branch_name,
    # Branch operations
    get_branch_name,
    is_detached_head,
    is_dirty,
    is_rebase_in_progress,
    has_conflicts,
    branch_exists_on_origin,
    get_ahead_behind,
    # Fetch/Pull/Push
    fetch_with_timeout,
    pull_ff_only,
    pull_rebase,
    push_with_retry,
    # Checkout
    checkout_branch,
    # Stash
    detect_stash,
    stash_changes,
    restore_stash,
)

from .state import (
    # Constants
    STATE_FILE_NAME,
    STATE_FILE_VERSION,
    # Enums
    ParityStatus,
    # Data classes
    ParityError,
    ParityState,
    # Classes
    StateManager,
    # Convenience functions
    read_parity_state,
    write_parity_state,
    get_state_file_path,
)

from .conflict import (
    # Enums
    ConflictType,
    ConflictScope,
    # Data classes
    ConflictInfo,
    # Classes
    ConflictResolver,
    # Pure merge functions
    merge_manifest_content,
    merge_jsonl_content,
    merge_sync_state_content,
    merge_thread_content,
    # Convenience functions
    has_graph_conflicts_only,
    has_thread_conflicts_only,
    has_state_conflicts_only,
)

from .local_remote import (
    # Data classes
    PullResult,
    CommitResult,
    PushResult,
    SyncResult,
    SyncStatus,
    # Classes
    LocalRemoteSyncManager,
)

from .async_coordinator import (
    # Constants
    QUEUE_FILE_NAME,
    DEFAULT_BATCH_WINDOW,
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_BATCH_SIZE,
    DEFAULT_SYNC_INTERVAL,
    # Data classes
    PendingCommit,
    AsyncConfig,
    AsyncStatus,
    # Classes
    AsyncSyncCoordinator,
    # Convenience functions
    get_queue_file_path,
)

from .branch_parity import (
    # Enums
    StateClass,
    # Data classes
    BranchMismatch,
    BranchSyncResult,
    BranchDivergenceInfo,
    PreflightResult,
    BranchPairingResult,
    # Classes
    BranchParityManager,
    # Standalone functions
    validate_branch_pairing,
    sync_branch_history,
    run_preflight,
    ensure_readable,
    get_branch_health,
    push_after_commit,
    acquire_topic_lock,
    auto_merge_to_main,
    _detect_squash_merge,
    _now_iso,
    # Helper functions (re-exported for backward compat)
    _find_main_branch,
    _sanitize_topic_for_filename,
    _lock_dir,
    _topic_lock_path,
    _detect_behind_main_divergence,
    _detect_branch_divergence,
    _rebase_branch_onto,
    # Locking constants
    LOCK_TIMEOUT_SECONDS,
    LOCK_TTL_SECONDS,
    LOCK_QUICK_RETRIES,
    LOCK_QUICK_RETRY_DELAY,
    LOCKS_DIR_NAME,
    # Topic validation constants
    MAX_TOPIC_LENGTH,
    UNSAFE_TOPIC_CHARS_PATTERN,
)

__all__ = [
    # Errors
    "SyncError",
    "PullError",
    "PushError",
    "ConflictError",
    "BranchPairingError",
    "LockError",
    "NetworkError",
    "AuthenticationError",
    # Constants
    "MAX_PUSH_RETRIES",
    "MAX_BRANCH_LENGTH",
    "INVALID_BRANCH_PATTERNS",
    # Primitives - Validation
    "validate_branch_name",
    # Primitives - Branch operations
    "get_branch_name",
    "is_detached_head",
    "is_dirty",
    "is_rebase_in_progress",
    "has_conflicts",
    "branch_exists_on_origin",
    "get_ahead_behind",
    # Primitives - Fetch/Pull/Push
    "fetch_with_timeout",
    "pull_ff_only",
    "pull_rebase",
    "push_with_retry",
    # Primitives - Checkout
    "checkout_branch",
    # Primitives - Stash
    "detect_stash",
    "stash_changes",
    "restore_stash",
    # State - Constants
    "STATE_FILE_NAME",
    "STATE_FILE_VERSION",
    # State - Enums
    "ParityStatus",
    # State - Data classes
    "ParityError",
    "ParityState",
    # State - Classes
    "StateManager",
    # State - Convenience functions
    "read_parity_state",
    "write_parity_state",
    "get_state_file_path",
    # Conflict - Enums
    "ConflictType",
    "ConflictScope",
    # Conflict - Data classes
    "ConflictInfo",
    # Conflict - Classes
    "ConflictResolver",
    # Conflict - Pure merge functions
    "merge_manifest_content",
    "merge_jsonl_content",
    "merge_sync_state_content",
    "merge_thread_content",
    # Conflict - Convenience functions
    "has_graph_conflicts_only",
    "has_thread_conflicts_only",
    "has_state_conflicts_only",
    # Local-Remote - Data classes
    "PullResult",
    "CommitResult",
    "PushResult",
    "SyncResult",
    "SyncStatus",
    # Local-Remote - Classes
    "LocalRemoteSyncManager",
    # Async Coordinator - Constants
    "QUEUE_FILE_NAME",
    "DEFAULT_BATCH_WINDOW",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_MAX_BATCH_SIZE",
    "DEFAULT_SYNC_INTERVAL",
    # Async Coordinator - Data classes
    "PendingCommit",
    "AsyncConfig",
    "AsyncStatus",
    # Async Coordinator - Classes
    "AsyncSyncCoordinator",
    # Async Coordinator - Convenience functions
    "get_queue_file_path",
    # Branch Parity - Enums
    "StateClass",
    # Branch Parity - Data classes
    "BranchMismatch",
    "BranchSyncResult",
    "BranchDivergenceInfo",
    "PreflightResult",
    "BranchPairingResult",
    # Branch Parity - Classes
    "BranchParityManager",
    # Branch Parity - Standalone functions
    "validate_branch_pairing",
    "sync_branch_history",
    "run_preflight",
    "ensure_readable",
    "get_branch_health",
    "push_after_commit",
    "acquire_topic_lock",
    "auto_merge_to_main",
    "_detect_squash_merge",
    "_now_iso",
    # Branch Parity - Helper functions
    "_find_main_branch",
    "_sanitize_topic_for_filename",
    "_lock_dir",
    "_topic_lock_path",
    "_detect_behind_main_divergence",
    "_detect_branch_divergence",
    "_rebase_branch_onto",
    # Branch Parity - Locking constants
    "LOCK_TIMEOUT_SECONDS",
    "LOCK_TTL_SECONDS",
    "LOCK_QUICK_RETRIES",
    "LOCK_QUICK_RETRY_DELAY",
    "LOCKS_DIR_NAME",
    # Branch Parity - Topic validation constants
    "MAX_TOPIC_LENGTH",
    "UNSAFE_TOPIC_CHARS_PATTERN",
]
