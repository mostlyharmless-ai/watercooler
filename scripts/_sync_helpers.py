"""Shared sync helpers for baseline graph scripts.

Provides a common sync_to_remote() function to eliminate code duplication
across project_baseline_graph.py, recover_baseline_graph.py, and
enrich_baseline_graph.py.
"""

import sys
from pathlib import Path


def sync_to_remote(threads_dir: Path, commit_msg: str) -> bool:
    """Commit and push changes to remote.

    Args:
        threads_dir: Path to the threads directory
        commit_msg: Commit message to use

    Returns:
        True if sync succeeded, False otherwise

    Raises:
        SystemExit: On import error or sync failure
    """
    try:
        from watercooler_mcp.sync import LocalRemoteSyncManager

        manager = LocalRemoteSyncManager(threads_dir)

        sync_result = manager.commit_and_push(
            message=commit_msg,
            all_changes=True,
        )

        if sync_result.success:
            if sync_result.commit_result and sync_result.commit_result.commit_sha:
                print(f"Committed: {sync_result.commit_result.commit_sha[:8]}")
            if sync_result.push_result and sync_result.push_result.commits_pushed:
                print(f"Pushed {sync_result.push_result.commits_pushed} commit(s) to remote")
            else:
                print("No changes to push (already synced)")
            return True
        else:
            error = ""
            if sync_result.commit_result and sync_result.commit_result.error:
                error = sync_result.commit_result.error
            elif sync_result.push_result and sync_result.push_result.error:
                error = sync_result.push_result.error
            print(f"Sync failed: {error}", file=sys.stderr)
            sys.exit(1)
    except ImportError as e:
        print(f"Sync unavailable (missing dependency): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Sync error: {e}", file=sys.stderr)
        sys.exit(1)

    return False
