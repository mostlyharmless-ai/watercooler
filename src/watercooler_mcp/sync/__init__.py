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
    ParityState,
    # Classes
    StateManager,
    # Convenience functions
    read_parity_state,
    write_parity_state,
    get_state_file_path,
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
    "ParityState",
    # State - Classes
    "StateManager",
    # State - Convenience functions
    "read_parity_state",
    "write_parity_state",
    "get_state_file_path",
]
