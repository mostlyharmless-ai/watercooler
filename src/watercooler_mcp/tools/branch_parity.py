"""Branch parity tools for watercooler MCP server.

Tools:
- watercooler_validate_branch_pairing: Validate branch pairing
- watercooler_sync_branch_state: Sync branch state
- watercooler_audit_branch_pairing: Audit branch pairing
- watercooler_recover_branch_state: Recover branch state
"""

import json
from typing import List, Optional

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from git import Repo, InvalidGitRepositoryError, GitCommandError

from ..git_sync import validate_branch_pairing, sync_branch_history


# Module-level references to registered tools (populated by register_branch_parity_tools)
validate_branch_pairing_tool = None
sync_branch_state_tool = None
audit_branch_pairing_tool = None
recover_branch_state_tool = None


# Runtime accessors for patchable functions (tests patch via server module)
def _require_context(code_path: str):
    """Access _require_context at runtime for test patching."""
    from .. import server
    return server._require_context(code_path)


def _validate_branch_pairing_impl(
    ctx: Context,
    code_path: str = "",
    strict: bool = True,
) -> ToolResult:
    """Validate branch pairing between code and threads repos.

    Checks that the code repo and threads repo are on matching branches.
    This validation is automatically performed before all write operations,
    but this tool allows explicit checking.

    Args:
        code_path: Path to code repository directory. Defaults to current directory.
        strict: If True, return valid=False on any mismatch. If False, only return
                valid=False on critical errors.

    Returns:
        JSON result with validation status, branch names, mismatches, and warnings.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        result = validate_branch_pairing(
            code_repo=context.code_root,
            threads_repo=context.threads_dir,
            strict=strict,
        )

        # Convert to JSON-serializable format
        output = {
            "valid": result.valid,
            "code_branch": result.code_branch,
            "threads_branch": result.threads_branch,
            "mismatches": [
                {
                    "type": m.type,
                    "code": m.code,
                    "threads": m.threads,
                    "severity": m.severity,
                    "recovery": m.recovery,
                }
                for m in result.mismatches
            ],
            "warnings": result.warnings,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error validating branch pairing: {str(e)}"
        )])


def _sync_branch_state_impl(
    ctx: Context,
    code_path: str = "",
    branch: Optional[str] = None,
    operation: str = "checkout",
    force: bool = False,
) -> ToolResult:
    """Synchronize branch state between code and threads repos.

    Performs branch lifecycle operations to keep repos in sync:
    - create: Create threads branch if code branch exists
    - delete: Delete threads branch if code branch deleted (with safeguards)
    - merge: Merge threads branch to main if code branch merged
    - checkout: Ensure both repos on same branch
    - recover: Recover from branch history divergence (e.g., after rebase/force-push)

    Note:
        This operational tool does **not** require ``agent_func`` or other
        provenance parameters. Unlike write operations (``watercooler_say``,
        ``watercooler_ack``, etc.), it only performs git lifecycle
        management, so pass just ``code_path``, ``branch``, ``operation``, and
        ``force``. FastMCP will automatically reject any unexpected parameters.

    Args:
        code_path: Path to code repository directory (default: current directory)
        branch: Specific branch to sync (default: current branch)
        operation: One of "create", "delete", "merge", "checkout", "recover" (default: "checkout")
        force: Skip safety checks (use with caution, default: False). For "recover",
               this controls whether to force-push after rebasing.

    Returns:
        Operation result with success/failure and any warnings.

    Example:
        >>> sync_branch_state(ctx, code_path=".", branch="feature-auth", operation="checkout")
        >>> sync_branch_state(ctx, code_path=".", branch="staging", operation="recover", force=True)
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        code_repo = Repo(context.code_root, search_parent_directories=True)
        threads_repo = Repo(context.threads_dir, search_parent_directories=True)

        target_branch = branch or context.code_branch
        if not target_branch:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: No branch specified and unable to detect current branch."
            )])

        warnings: List[str] = []
        result_msg = ""

        if operation == "checkout":
            # Ensure both repos on same branch
            if target_branch not in [b.name for b in code_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in code repo."
                )])

            # Checkout code branch
            if code_repo.active_branch.name != target_branch:
                code_repo.git.checkout(target_branch)
                result_msg += f"Checked out '{target_branch}' in code repo.\n"

            # Checkout or create threads branch
            if target_branch in [b.name for b in threads_repo.heads]:
                if threads_repo.active_branch.name != target_branch:
                    threads_repo.git.checkout(target_branch)
                    result_msg += f"Checked out '{target_branch}' in threads repo.\n"
            else:
                # Create threads branch
                threads_repo.git.checkout('-b', target_branch)
                result_msg += f"Created and checked out '{target_branch}' in threads repo.\n"

            result_msg += f"✅ Both repos now on branch '{target_branch}'"

        elif operation == "create":
            # Create threads branch if code branch exists
            if target_branch not in [b.name for b in code_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in code repo."
                )])

            if target_branch in [b.name for b in threads_repo.heads]:
                result_msg = f"Branch '{target_branch}' already exists in threads repo."
            else:
                threads_repo.git.checkout('-b', target_branch)
                result_msg = f"✅ Created branch '{target_branch}' in threads repo."

        elif operation == "delete":
            # Delete threads branch (with safeguards)
            if target_branch not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in threads repo."
                )])

            # Check for OPEN threads
            if not force:
                threads_repo.git.checkout(target_branch)
                threads_dir = context.threads_dir
                open_threads = []
                for thread_file in threads_dir.glob("*.md"):
                    try:
                        from watercooler.metadata import thread_meta, is_closed
                        title, status, ball, updated = thread_meta(thread_file)
                        if not is_closed(status):
                            open_threads.append(thread_file.stem)
                    except Exception:
                        pass

                if open_threads:
                    return ToolResult(content=[TextContent(
                        type="text",
                        text=(
                            f"Error: Cannot delete branch '{target_branch}' with OPEN threads:\n"
                            f"  {', '.join(open_threads)}\n"
                            f"Close threads first or use force=True to override."
                        )
                    )])

            # Switch to another branch before deleting
            if threads_repo.active_branch.name == target_branch:
                if "main" in [b.name for b in threads_repo.heads]:
                    threads_repo.git.checkout("main")
                else:
                    # Create main if it doesn't exist
                    threads_repo.git.checkout('-b', 'main')

            threads_repo.git.branch('-D', target_branch)
            result_msg = f"✅ Deleted branch '{target_branch}' from threads repo."

        elif operation == "merge":
            # Merge threads branch to main
            if target_branch not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in threads repo."
                )])

            if "main" not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text="Error: 'main' branch does not exist in threads repo."
                )])

            # Check for OPEN threads before merge
            if not force:
                threads_repo.git.checkout(target_branch)
                from watercooler.metadata import thread_meta, is_closed
                open_threads = []
                for thread_file in context.threads_dir.glob("*.md"):
                    try:
                        title, status, ball, updated = thread_meta(thread_file)
                        if not is_closed(status):
                            open_threads.append(thread_file.stem)
                    except Exception:
                        pass

                if open_threads:
                    warnings.append(f"Warning: {len(open_threads)} OPEN threads found on {target_branch}: {', '.join(open_threads)}")
                    warnings.append("Consider closing threads before merge or use force=True to proceed")

            # Detect squash merge in code repo
            squash_info = None
            if context.code_root:
                try:
                    from watercooler_mcp.git_sync import _detect_squash_merge
                    code_repo_obj = Repo(context.code_root, search_parent_directories=True)
                    is_squash, squash_sha = _detect_squash_merge(code_repo_obj, target_branch)
                    if is_squash:
                        squash_info = f"Detected squash merge in code repo"
                        if squash_sha:
                            squash_info += f" (squash commit: {squash_sha})"
                        warnings.append(squash_info)
                        warnings.append("Note: Original commits preserved in threads branch history")
                except Exception:
                    pass  # Ignore squash detection errors

            threads_repo.git.checkout("main")
            try:
                threads_repo.git.merge(target_branch, '--no-ff', '-m', f"Merge {target_branch} into main")

                # Check if code repo branch exists on remote - if yes, push threads merge too
                code_branch_on_remote = False
                if context.code_root:
                    try:
                        code_repo_obj = Repo(context.code_root, search_parent_directories=True)
                        remote_refs = [ref.name for ref in code_repo_obj.remote().refs]
                        code_branch_on_remote = f"origin/{target_branch}" in remote_refs
                    except Exception:
                        pass  # Ignore errors checking remote

                if code_branch_on_remote:
                    # Code branch is on remote, push threads merge too
                    threads_repo.git.push('origin', 'main')
                    result_msg = f"✅ Merged '{target_branch}' into 'main' in threads repo and pushed to remote."
                else:
                    # Code branch is local only, keep threads merge local
                    result_msg = f"✅ Merged '{target_branch}' into 'main' in threads repo (local only - code branch not on remote)."

                if warnings:
                    result_msg += "\n" + "\n".join(warnings)
            except GitCommandError as e:
                error_str = str(e)
                # Check if this is a merge conflict
                if "CONFLICT" in error_str or threads_repo.is_dirty():
                    # Detect conflicts in thread files
                    conflicted_files = []
                    for item in threads_repo.index.unmerged_blobs():
                        conflicted_files.append(item.path)

                    if conflicted_files:
                        conflict_msg = (
                            f"Merge conflict detected in {len(conflicted_files)} file(s):\n"
                            f"  {', '.join(conflicted_files)}\n\n"
                            f"Append-only conflict resolution:\n"
                            f"  - Both entries will be preserved in chronological order\n"
                            f"  - Status/Ball conflicts: Higher severity status wins, last entry author gets ball\n"
                            f"  - Manual resolution may be required for complex conflicts\n\n"
                            f"To resolve:\n"
                            f"  1. Review conflicted files\n"
                            f"  2. Keep both entries in chronological order\n"
                            f"  3. Resolve header conflicts (status/ball) manually\n"
                            f"  4. Run: git add <files> && git commit"
                        )
                        warnings.append(conflict_msg)
                        return ToolResult(content=[TextContent(
                            type="text",
                            text=f"Merge conflict: {error_str}\n\n{conflict_msg}"
                        )])

                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error merging branch: {error_str}"
                )])

        elif operation == "recover":
            # Recover from branch history divergence
            # This handles cases where threads branch has diverged from remote
            # (e.g., after force-push or rebase on code repo)

            if target_branch not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in threads repo."
                )])

            # Use sync_branch_history to attempt automatic recovery
            sync_result = sync_branch_history(
                threads_repo_path=context.threads_dir,
                branch=target_branch,
                strategy="rebase",  # Default to rebase to preserve local work
                force=force,  # Only force-push if user explicitly requests
            )

            if sync_result.success:
                result_msg = f"✅ Recovery successful: {sync_result.details}"
                if sync_result.commits_preserved > 0:
                    result_msg += f"\n  - Preserved {sync_result.commits_preserved} local commits"
                if sync_result.needs_manual_resolution:
                    result_msg += "\n  ⚠️ Manual push required: run with force=True to push changes"
                    warnings.append("Rebase complete but push not performed. Use force=True to push.")
            else:
                if sync_result.needs_manual_resolution:
                    result_msg = (
                        f"❌ Recovery requires manual intervention:\n"
                        f"  {sync_result.details}\n\n"
                        f"Manual recovery options:\n"
                        f"  1. Resolve conflicts and commit\n"
                        f"  2. Use operation='recover' with force=True to discard local changes\n"
                        f"  3. Manually rebase: git rebase origin/{target_branch}"
                    )
                else:
                    result_msg = f"❌ Recovery failed: {sync_result.details}"

                if sync_result.commits_lost > 0:
                    warnings.append(f"Warning: {sync_result.commits_lost} local commits may be lost")

                return ToolResult(content=[TextContent(
                    type="text",
                    text=result_msg
                )])

        else:
            return ToolResult(content=[TextContent(
                type="text",
                text=f"Error: Unknown operation '{operation}'. Must be one of: create, delete, merge, checkout, recover"
            )])

        output = {
            "success": True,
            "operation": operation,
            "branch": target_branch,
            "message": result_msg,
            "warnings": warnings,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except InvalidGitRepositoryError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error: Not a git repository: {str(e)}"
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error syncing branch state: {str(e)}"
        )])


def _audit_branch_pairing_impl(
    ctx: Context,
    code_path: str = "",
    include_merged: bool = False,
) -> ToolResult:
    """Comprehensive audit of branch pairing across entire repo pair.

    Scans all branches in both repos and identifies:
    - Synced branches (exist in both with same name)
    - Code-only branches (exist in code but not threads)
    - Threads-only branches (exist in threads but not code)
    - Mismatched branches

    Args:
        code_path: Path to code repository directory
        include_merged: Include fully merged branches (0 commits ahead) in report

    Returns:
        JSON report with categorized branches and recommendations.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        code_repo = Repo(context.code_root, search_parent_directories=True)
        threads_repo = Repo(context.threads_dir, search_parent_directories=True)

        # Get all branches
        code_branches = {b.name for b in code_repo.heads}
        threads_branches = {b.name for b in threads_repo.heads}

        # Categorize branches
        synced = []
        code_only = []
        threads_only = []
        recommendations = []

        # Find synced branches
        for branch in code_branches & threads_branches:
            try:
                code_sha = code_repo.heads[branch].commit.hexsha[:7]
                threads_sha = threads_repo.heads[branch].commit.hexsha[:7]
                synced.append({
                    "name": branch,
                    "code_sha": code_sha,
                    "threads_sha": threads_sha,
                })
            except Exception:
                synced.append({"name": branch, "code_sha": "unknown", "threads_sha": "unknown"})

        # Find code-only branches
        for branch in code_branches - threads_branches:
            try:
                branch_obj = code_repo.heads[branch]
                commits_ahead = len(list(code_repo.iter_commits(f"main..{branch}"))) if "main" in code_branches else 0
                code_only.append({
                    "name": branch,
                    "commits_ahead": commits_ahead,
                    "action": "create_threads_branch" if commits_ahead > 0 or include_merged else "delete_if_merged",
                })
                if commits_ahead == 0 and not include_merged:
                    recommendations.append(f"Code branch '{branch}' is fully merged - consider deleting")
                else:
                    recommendations.append(f"Create threads branch '{branch}' to match code branch")
            except Exception:
                code_only.append({"name": branch, "commits_ahead": 0, "action": "unknown"})

        # Find threads-only branches
        for branch in threads_branches - code_branches:
            try:
                branch_obj = threads_repo.heads[branch]
                commits_ahead = len(list(threads_repo.iter_commits(f"main..{branch}"))) if "main" in threads_branches else 0
                threads_only.append({
                    "name": branch,
                    "commits_ahead": commits_ahead,
                    "action": "delete_or_merge" if commits_ahead == 0 or include_merged else "create_code_branch",
                })
                if commits_ahead == 0:
                    recommendations.append(f"Threads branch '{branch}' is fully merged - safe to delete")
                else:
                    recommendations.append(f"Code branch '{branch}' was deleted - merge or delete threads branch")
            except Exception:
                threads_only.append({"name": branch, "commits_ahead": 0, "action": "unknown"})

        output = {
            "synced_branches": synced,
            "code_only_branches": code_only,
            "threads_only_branches": threads_only,
            "mismatched_branches": [],  # Future: detect name mismatches
            "recommendations": recommendations,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except InvalidGitRepositoryError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error: Not a git repository: {str(e)}"
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error auditing branch pairing: {str(e)}"
        )])


def _recover_branch_state_impl(
    ctx: Context,
    code_path: str = "",
    auto_fix: bool = False,
    diagnose_only: bool = False,
) -> ToolResult:
    """Recover from branch state inconsistencies.

    Diagnoses and optionally fixes branch pairing issues:
    - Branch name mismatches
    - Orphaned threads branches (code branch deleted)
    - Missing threads branches (code branch exists)
    - Git state issues (rebase conflicts, detached HEAD, etc.)

    Args:
        code_path: Path to code repository directory
        auto_fix: Automatically apply safe fixes
        diagnose_only: Only report issues, don't fix

    Returns:
        Diagnostic report with detected issues and recommended fixes.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        issues = []
        fixes_applied = []

        # Validate branch pairing
        validation_result = validate_branch_pairing(
            code_repo=context.code_root,
            threads_repo=context.threads_dir,
            strict=False,
        )

        if not validation_result.valid:
            for mismatch in validation_result.mismatches:
                issues.append({
                    "type": mismatch.type,
                    "severity": mismatch.severity,
                    "description": f"Code: {mismatch.code}, Threads: {mismatch.threads}",
                    "recovery": mismatch.recovery,
                })

                # Auto-fix if requested and safe
                if auto_fix and not diagnose_only:
                    if mismatch.type == "branch_name_mismatch" and mismatch.code:
                        try:
                            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
                            if mismatch.code in [b.name for b in threads_repo.heads]:
                                threads_repo.git.checkout(mismatch.code)
                                fixes_applied.append(f"Checked out '{mismatch.code}' in threads repo")
                            else:
                                threads_repo.git.checkout('-b', mismatch.code)
                                fixes_applied.append(f"Created branch '{mismatch.code}' in threads repo")
                        except Exception as e:
                            issues.append({
                                "type": "auto_fix_failed",
                                "severity": "warning",
                                "description": f"Failed to auto-fix {mismatch.type}: {str(e)}",
                                "recovery": "Manual intervention required",
                            })

        # Check for git state issues
        try:
            code_repo = Repo(context.code_root, search_parent_directories=True)
            if code_repo.head.is_detached:
                issues.append({
                    "type": "code_repo_detached_head",
                    "severity": "warning",
                    "description": "Code repo is in detached HEAD state",
                    "recovery": "Checkout a branch: git checkout <branch>",
                })
        except Exception:
            pass

        try:
            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
            if threads_repo.head.is_detached:
                issues.append({
                    "type": "threads_repo_detached_head",
                    "severity": "warning",
                    "description": "Threads repo is in detached HEAD state",
                    "recovery": "Checkout a branch: git checkout <branch>",
                })
        except Exception:
            pass

        # Check for rebase/merge conflicts
        try:
            code_repo = Repo(context.code_root, search_parent_directories=True)
            if (code_repo.git_dir / "rebase-merge").exists() or (code_repo.git_dir / "rebase-apply").exists():
                issues.append({
                    "type": "code_repo_rebase_in_progress",
                    "severity": "error",
                    "description": "Code repo has in-progress rebase",
                    "recovery": "Run: git rebase --abort (or --continue)",
                })
        except Exception:
            pass

        try:
            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
            if (threads_repo.git_dir / "rebase-merge").exists() or (threads_repo.git_dir / "rebase-apply").exists():
                issues.append({
                    "type": "threads_repo_rebase_in_progress",
                    "severity": "error",
                    "description": "Threads repo has in-progress rebase",
                    "recovery": "Run: git rebase --abort (or --continue)",
                })
        except Exception:
            pass

        output = {
            "diagnosis_complete": True,
            "issues_found": len(issues),
            "issues": issues,
            "fixes_applied": fixes_applied,
            "warnings": validation_result.warnings,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except InvalidGitRepositoryError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error: Not a git repository: {str(e)}"
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error recovering branch state: {str(e)}"
        )])


def register_branch_parity_tools(mcp):
    """Register branch parity tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global validate_branch_pairing_tool, sync_branch_state_tool
    global audit_branch_pairing_tool, recover_branch_state_tool

    # Register tools and store references for testing
    validate_branch_pairing_tool = mcp.tool(name="watercooler_validate_branch_pairing")(_validate_branch_pairing_impl)
    sync_branch_state_tool = mcp.tool(name="watercooler_sync_branch_state")(_sync_branch_state_impl)
    audit_branch_pairing_tool = mcp.tool(name="watercooler_audit_branch_pairing")(_audit_branch_pairing_impl)
    recover_branch_state_tool = mcp.tool(name="watercooler_recover_branch_state")(_recover_branch_state_impl)
