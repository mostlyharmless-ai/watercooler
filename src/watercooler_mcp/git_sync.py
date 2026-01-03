"""Git-based synchronization for cloud-hosted watercooler MCP servers.

.. deprecated:: 0.5.0
   This module is deprecated and will be removed in a future version.
   Use the new modular sync package instead:

   - For errors: ``from watercooler_mcp.sync import SyncError, PushError, PullError``
   - For primitives: ``from watercooler_mcp.sync import push_with_retry, pull_rebase``
   - For state: ``from watercooler_mcp.sync import StateManager, ParityState``
   - For local-remote sync: ``from watercooler_mcp.sync import LocalRemoteSyncManager``
   - For branch parity: ``from watercooler_mcp.sync import BranchParityManager``

This module is preserved as a facade for backward compatibility.
New code should import from watercooler_mcp.sync directly.

Legacy documentation:
This module provides GitSyncManager for syncing .watercooler threads via git,
enabling multi-user collaboration with automatic conflict resolution.

Architecture (sync mode):
- Pull before writes (get latest from remote)
- Commit + push after writes (propagate changes)
- Retry logic handles concurrent modifications
- Append-only operations minimize conflicts

When async mode is enabled, commits are written locally immediately and pushed
from a background worker that batches git operations.
"""

import atexit
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Callable, List, Optional, TypeVar

# Import importlib.resources for accessing package data
try:
    # Python 3.9+
    from importlib.resources import files
except ImportError:
    # Python 3.7-3.8 fallback
    from importlib_resources import files

# GitPython for in-process git operations (avoids subprocess stdio issues on Windows)
import git
from git import Repo, GitCommandError, InvalidGitRepositoryError

# Unified logging (replaces old _diag system)
from .observability import log_debug, log_warning
from watercooler.config_facade import config

try:  # pragma: no cover - fallback for direct module import (tests)
    from .provisioning import ProvisioningError, provision_threads_repo
except ImportError:  # pragma: no cover - executed when module is loaded stand-alone
    import importlib.util
    import sys

    _current_dir = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "watercooler_mcp.provisioning", _current_dir / "provisioning.py"
    )
    if _spec and _spec.loader:
        _module = importlib.util.module_from_spec(_spec)
        sys.modules.setdefault("watercooler_mcp.provisioning", _module)
        _spec.loader.exec_module(_module)  # type: ignore[attr-defined]
        ProvisioningError = getattr(_module, "ProvisioningError")  # type: ignore[assignment]
        provision_threads_repo = getattr(_module, "provision_threads_repo")  # type: ignore[assignment]
    else:  # pragma: no cover - defensive guard if spec resolution fails
        raise

# Re-export types from sync/ for backward compatibility
# New code should import directly from watercooler_mcp.sync
from .sync import (
    BranchPairingError,
    BranchMismatch,
    BranchSyncResult,
    BranchDivergenceInfo,
    BranchPairingResult,
    validate_branch_pairing,
    sync_branch_history,
    _find_main_branch,
    _detect_squash_merge,
    _detect_behind_main_divergence,
    _detect_branch_divergence,
    _rebase_branch_onto,
)

T = TypeVar('T')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _positive_float(value: float, default: float) -> float:
    """Return value if positive, otherwise default."""
    return value if value > 0 else default


def _positive_int(value: int, default: int) -> int:
    """Return value if positive, otherwise default."""
    return value if value > 0 else default


@dataclass
class _PendingCommit:
    sequence: int
    entry_id: Optional[str]
    topic: Optional[str]
    commit_message: str
    timestamp: str
    created_ts: float

    def to_payload(self) -> dict:
        return {
            "sequence": self.sequence,
            "entry_id": self.entry_id,
            "topic": self.topic,
            "commit_message": self.commit_message,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "_PendingCommit":
        timestamp = payload.get("timestamp")
        created_ts = time.time()
        if isinstance(timestamp, str):
            try:
                created_ts = datetime.fromisoformat(timestamp).timestamp()
            except ValueError:
                pass
        return cls(
            sequence=int(payload["sequence"]),
            entry_id=payload.get("entry_id"),
            topic=payload.get("topic"),
            commit_message=payload.get("commit_message", ""),
            timestamp=timestamp or _now_iso(),
            created_ts=created_ts,
        )


def _checksum_payload(payload: dict) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return digest


class GitSyncError(Exception):
    """Base exception for git sync operations."""
    pass


class GitPullError(GitSyncError):
    """Failed to pull latest changes from remote."""
    pass


class GitPushError(GitSyncError):
    """Failed to push changes to remote."""
    pass


# Note: BranchPairingError, BranchMismatch, BranchPairingResult are now imported
# from .sync at the top of this module for backward compatibility.
# The authoritative definitions live in sync/branch_parity.py and sync/errors.py.


class GitSyncManager:
    """Manages git-based synchronization for watercooler threads directory.

    This class handles all git operations for cloud-hosted watercooler
    deployments, including pulling latest changes, committing local changes,
    and pushing to remote with retry logic.

    Attributes:
        repo_url: Git repository URL (SSH or HTTPS)
        local_path: Path to local .watercooler directory
        ssh_key_path: Optional path to SSH private key
        author_name: Git commit author name
        author_email: Git commit author email

    Thread Safety:
        This class is not thread-safe. Use advisory locks at a higher level
        for concurrent access to the same repository.
    """

    def __init__(
        self,
        repo_url: str,
        local_path: Path,
        ssh_key_path: Path | None = None,
        author_name: str = "Watercooler MCP",
        author_email: str = "mcp@watercooler.dev",
        *,
        threads_slug: str | None = None,
        code_repo: str | None = None,
        enable_provision: bool = False,
        remote_allowed: bool = True,
    ):
        """Initialize GitSyncManager.

        Args:
            repo_url: Git repository URL (e.g., git@github.com:org/repo.git)
            local_path: Path to local threads directory
            ssh_key_path: Optional path to SSH key for authentication
            author_name: Git commit author name
            author_email: Git commit author email

        Note:
            For best results, use a dedicated repository where the repo root
            is the threads directory. This minimizes unrelated changes and
            simplifies staging.
        """
        self.repo_url = repo_url
        self.local_path = local_path
        self.ssh_key_path = ssh_key_path
        self.author_name = author_name
        self.author_email = author_email
        self.threads_slug = threads_slug
        self.code_repo = code_repo
        self._provision_enabled = bool(enable_provision and threads_slug)
        self._last_provision_output: str | None = None
        self._remote_allowed = bool(remote_allowed)
        self._last_pull_error: str | None = None
        self._last_push_error: str | None = None
        self._last_remote_error: str | None = None
        self._remote_missing: bool = False
        self._remote_empty: bool = False
        self._async: Optional[_AsyncSyncCoordinator] = None

        # Prepare git environment once (propagated to all git operations)
        self._env = os.environ.copy()
        # Disable interactive prompts so git fails fast instead of hanging when
        # credentials are required (particularly important inside MCP tools).
        self._env.setdefault("GIT_TERMINAL_PROMPT", "0")
        self._env.setdefault("GCM_INTERACTIVE", "never")
        self._env.setdefault("GIT_HTTP_LOW_SPEED_LIMIT", "1")
        self._env.setdefault("GIT_HTTP_LOW_SPEED_TIME", "30")

        # Configure HTTPS authentication with token if available
        # For HTTPS URLs, git can use GIT_ASKPASS to get credentials
        # Priority: WATERCOOLER_GITHUB_TOKEN > GITHUB_TOKEN > GH_TOKEN
        github_token = (
            self._env.get('WATERCOOLER_GITHUB_TOKEN')
            or self._env.get('GITHUB_TOKEN')
            or self._env.get('GH_TOKEN')
        )
        if github_token and self.repo_url.startswith('https://'):
            # Create inline askpass script that returns the token
            # Git calls askpass with "Username for 'https://github.com':" and "Password for '...':"
            # We return the token for password, and 'x-access-token' or 'oauth2' for username
            import sys
            askpass_script = (
                f'{sys.executable} -c "import sys; '
                f'print(\\"x-access-token\\" if \\"Username\\" in sys.argv[1] '
                f'else \\"{github_token}\\")"'
            )
            self._env["GIT_ASKPASS"] = askpass_script
        else:
            # No token available, use echo to fail fast
            self._env.setdefault("GIT_ASKPASS", "echo")

        # Configure SSH to fail fast (BatchMode=yes) to prevent hanging on passphrase prompts
        # This is critical for MCP servers where stdin is not available for interactive input
        if self.ssh_key_path:
            self._env["GIT_SSH_COMMAND"] = (
                f"ssh -i {self.ssh_key_path} -o IdentitiesOnly=yes -o BatchMode=yes"
            )
        elif self.repo_url.startswith("git@") or self.repo_url.startswith("ssh://"):
            # SSH URL without explicit key - ensure BatchMode to prevent hangs
            self._env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")

        self._log_enabled = config.env.get_bool("WATERCOOLER_SYNC_LOG", False)
        self._log_path = self.local_path.parent / ".watercooler-sync.log"
        self._async_enabled = self._resolve_async_enabled()
        self._async_config = self._load_async_config()

        self._setup()
        if self._async_enabled:
            self._init_async()

    # ------------------------------------------------------------------
    # Branch pairing auto-sync
    # ------------------------------------------------------------------

    def _quick_branch_check(self) -> tuple[str, str, bool]:
        """Check if code and threads repos are on same branch.

        Returns:
            tuple of (code_branch, threads_branch, in_sync)

        Performance: ~0.1-1ms (just file reads)
        """
        if not self.code_repo:
            # No code repo configured, skip check
            return ("", "", True)

        try:
            # Read .git/HEAD - very fast, O(1) file read
            code_head_file = self.code_repo / ".git" / "HEAD"
            threads_head_file = self.local_path / ".git" / "HEAD"

            if not code_head_file.exists() or not threads_head_file.exists():
                # One or both not git repos
                return ("", "", True)

            code_head = code_head_file.read_text().strip()
            threads_head = threads_head_file.read_text().strip()

            # Extract branch names from refs
            code_branch = code_head.replace("ref: refs/heads/", "")
            threads_branch = threads_head.replace("ref: refs/heads/", "")

            # If HEAD is detached (SHA instead of ref), treat as in-sync
            # since detached state is intentional and not a pairing issue
            if not code_head.startswith("ref:") or not threads_head.startswith("ref:"):
                return (code_branch, threads_branch, True)

            in_sync = code_head == threads_head
            return (code_branch, threads_branch, in_sync)

        except Exception as e:
            # If we can't read HEAD files, assume in sync to avoid blocking operations
            self._log(f"Branch check failed: {e}")
            return ("", "", True)

    def _ensure_branch_sync(self) -> None:
        """Ensure threads repo is on same branch as code repo.

        Auto-syncs by checking out matching branch in threads repo if needed.
        Called before write operations to maintain branch pairing invariant.

        Raises:
            GitSyncError: If auto-sync fails
        """
        code_branch, threads_branch, in_sync = self._quick_branch_check()

        if in_sync:
            return  # Already synced, nothing to do

        if not code_branch or not threads_branch:
            return  # Can't determine branches, skip

        # Log the mismatch
        self._log(f"Branch mismatch detected: code={code_branch}, threads={threads_branch}")
        self._log(f"Auto-syncing threads repo to {code_branch}")

        try:
            # Check out matching branch in threads repo
            repo = self._repo()

            # Check if branch exists locally
            try:
                repo.git.checkout(code_branch)
                self._log(f"Checked out existing branch: {code_branch}")
            except GitCommandError:
                # Branch doesn't exist locally, try to create from remote
                try:
                    repo.git.checkout("-b", code_branch, f"origin/{code_branch}")
                    self._log(f"Created and checked out branch from remote: {code_branch}")
                except GitCommandError:
                    # Remote branch doesn't exist either, create new orphan branch
                    repo.git.checkout("--orphan", code_branch)
                    self._log(f"Created new orphan branch: {code_branch}")

        except Exception as e:
            error_msg = f"Failed to auto-sync branches: {e}"
            self._log(error_msg)
            raise GitSyncError(error_msg) from e

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        if not self._log_enabled:
            return
        try:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{timestamp} {message}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # GitPython helpers
    # ------------------------------------------------------------------

    @property
    def _repo(self) -> Repo:
        """Get GitPython Repo object for the local repository."""
        try:
            return Repo(self.local_path)
        except InvalidGitRepositoryError:
            raise GitSyncError(f"Not a git repository: {self.local_path}")

    def _run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess[str]:
        cwd = cwd or self.local_path if self.local_path.exists() else cwd
        cwd_str = str(cwd) if cwd is not None else None
        quoted = " ".join(shlex.quote(part) for part in cmd)
        self._log(f"RUN cwd={cwd_str or os.getcwd()} cmd={quoted}")
        start = time.time()
        try:
            # On Windows, explicitly close file descriptors to prevent handle inheritance
            # which can block parent stdio even with capture_output=True
            close_fds = sys.platform == "win32"

            result = subprocess.run(
                cmd,
                cwd=cwd_str,
                env=self._env,
                capture_output=True,
                text=True,
                timeout=timeout,
                close_fds=close_fds,
            )
        except TimeoutExpired as exc:
            elapsed = time.time() - start
            self._log(f"TIMEOUT after {elapsed:.2f}s cmd={quoted}")
            raise
        elapsed = time.time() - start
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        # Normalize attributes for test doubles that do not populate stdout/stderr.
        try:
            result.stdout = stdout
            result.stderr = stderr
        except Exception:
            pass

        if stdout:
            out = stdout.strip().splitlines()
            stdout_preview = out[0][:160] if out else ""
        else:
            stdout_preview = ""
        if stderr:
            err = stderr.strip().splitlines()
            stderr_preview = err[0][:160] if err else ""
        else:
            stderr_preview = ""
        self._log(
            f"DONE rc={result.returncode} elapsed={elapsed:.2f}s stdout='{stdout_preview}' stderr='{stderr_preview}'"
        )

        # Workaround for Windows stdio hang: Force flush and brief delay
        # On Windows, running git subprocess leaves stdio handles in bad state,
        # causing FastMCP's response to get stuck in stdout buffer.
        # Brief delay ensures subprocess handles are fully released.
        if sys.platform == "win32":
            try:
                import time as time_module
                time_module.sleep(0.01)  # 10ms delay for handle cleanup
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass

        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        return result

    def _setup(self):
        """Ensure repository is cloned and configured."""
        self._initialise_repository()
        self._configure_git()

    def _resolve_async_enabled(self) -> bool:
        # Default: async on Windows (avoids subprocess stdio hangs)
        return config.env.get_bool("WATERCOOLER_ASYNC_SYNC", sys.platform == "win32")

    def _load_async_config(self) -> dict:
        """Load async sync configuration from config system with env var overrides."""
        # Get defaults from config system
        try:
            from .config import get_sync_config
            sync_config = get_sync_config()
        except Exception:
            sync_config = {}

        # Helper to get float with positive validation
        def get_positive_float(key: str, cfg_key: str, default: float) -> float:
            cfg_default = sync_config.get(cfg_key, default)
            return _positive_float(config.env.get_float(key, cfg_default), cfg_default)

        # Helper to get int with positive validation
        def get_positive_int(key: str, cfg_key: str, default: int) -> int:
            cfg_default = sync_config.get(cfg_key, default)
            return _positive_int(config.env.get_int(key, cfg_default), cfg_default)

        return {
            "batch_window": get_positive_float("WATERCOOLER_BATCH_WINDOW", "batch_window", 5.0),
            "max_delay": get_positive_float("WATERCOOLER_MAX_BATCH_DELAY", "max_delay", 30.0),
            "max_batch_size": get_positive_int("WATERCOOLER_MAX_BATCH_SIZE", "max_batch_size", 50),
            "max_sync_retries": get_positive_int("WATERCOOLER_MAX_SYNC_RETRIES", "max_retries", 5),
            "max_backoff": get_positive_float("WATERCOOLER_MAX_BACKOFF", "max_backoff", 300.0),
            "log_enabled": self._log_enabled,
            "sync_interval": get_positive_float("WATERCOOLER_SYNC_INTERVAL", "interval", 30.0),
            "stale_threshold": get_positive_float("WATERCOOLER_STALE_THRESHOLD", "stale_threshold", 60.0),
        }

    def _init_async(self) -> None:
        if self._async is not None:
            return
        self._async = _AsyncSyncCoordinator(self, **self._async_config)

    def _ensure_local_repo_ready(self) -> None:
        """Re-initialise the repository if the working tree was removed."""
        git_dir = self.local_path / ".git"
        if git_dir.exists():
            return
        self._initialise_repository()

    def _initialise_repository(self) -> None:
        """Prepare the local git repository, preferring remote clone when possible."""
        git_dir = self.local_path / ".git"
        if git_dir.exists():
            return

        if self._try_clone_remote():
            return

        self._bootstrap_local_repo()

    def _try_clone_remote(self) -> bool:
        """Attempt to clone the remote threads repository.

        Returns True on success, False if the remote is unavailable or clone fails.
        """
        try:
            self._clone()
            return True
        except GitSyncError:
            if self._provision_enabled:
                raise
            return False

    def _clone(self):
        """Clone the watercooler repository using GitPython (no subprocess).

        Raises:
            GitSyncError: If clone fails
        """
        self._log(f"Cloning {self.repo_url} to {self.local_path}")
        try:
            # If directory exists but isn't a git repo, remove it first
            if self.local_path.exists() and not (self.local_path / ".git").exists():
                import shutil
                shutil.rmtree(self.local_path)

            # Ensure parent directories exist for the clone target
            parent = self.local_path.parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            # Use GitPython to clone (in-process, no subprocess)
            # Configure environment for the clone operation
            log_debug(f"GIT_OP_START: clone {self.repo_url}")
            with git.Git().custom_environment(**self._env):
                Repo.clone_from(
                    self.repo_url,
                    self.local_path,
                    env=self._env
                )
            log_debug(f"GIT_OP_END: clone {self.repo_url}")
            self._log(f"Clone completed successfully")
        except GitCommandError as e:
            # Check if we should attempt provisioning
            if self._should_attempt_provision(e):
                self._handle_provisioning_clone_gitpython(e)
                return
            message = f"Failed to clone {self.repo_url}: {e}"
            raise GitSyncError(message) from e
        except Exception as e:
            message = f"Failed to clone {self.repo_url}: {e}"
            raise GitSyncError(message) from e

    def _format_process_error(
        self,
        error: subprocess.CalledProcessError,
        *,
        prefix: str | None = None,
    ) -> str:
        parts = [error.stderr or "", error.stdout or ""]
        body = "\n".join(p.strip() for p in parts if p and p.strip())
        if prefix:
            return f"{prefix}: {body}" if body else prefix
        return body

    def _should_attempt_provision(self, error: GitCommandError) -> bool:
        """Check if we should attempt auto-provisioning based on GitCommandError."""
        if not self._provision_enabled or not self._remote_allowed:
            return False
        # GitCommandError stores error message as string
        text = str(error).strip().lower()
        if not text:
            return False
        if "repository not found" in text:
            return True
        if "repository" in text and "not found" in text:
            return True
        return "repository" in text and "does not exist" in text

    def _handle_provisioning_clone(self, error: subprocess.CalledProcessError) -> None:
        if not self._remote_allowed or not self._provision_enabled:
            self._bootstrap_local_repo()
            return
        try:
            output = provision_threads_repo(
                repo_url=self.repo_url,
                slug=self.threads_slug,
                code_repo=self.code_repo,
                env=self._env,
            )
            self._last_provision_output = output or None
        except ProvisioningError as provision_error:
            message = self._format_process_error(
                error,
                prefix=(
                    f"Failed to clone {self.repo_url}; "
                    f"auto-provision attempt aborted: {provision_error}"
                ),
            )
            raise GitSyncError(message) from provision_error

        # After provisioning, retry clone with GitPython (no subprocess).
        # If the remote is still empty, fall back to bootstrapping a local repo.
        try:
            with git.Git().custom_environment(**self._env):
                Repo.clone_from(
                    self.repo_url,
                    self.local_path,
                    env=self._env
                )
            return
        except GitCommandError as retry_error:
            combined = str(retry_error)
            if combined:
                self._last_provision_output = (
                    (self._last_provision_output or "") + "\n" + combined
                ).strip()
            try:
                if self.local_path.exists() and not (self.local_path / ".git").exists():
                    import shutil
                    shutil.rmtree(self.local_path)
                self._bootstrap_local_repo()
            except Exception as bootstrap_exc:  # pragma: no cover - defensive
                message = (
                    self._last_provision_output
                    or "Provisioned threads repo but bootstrap failed"
                )
                raise GitSyncError(message) from bootstrap_exc

    def _handle_provisioning_clone_gitpython(self, error: GitCommandError) -> None:
        """Handle provisioning when GitPython reports a missing repository."""
        if not self._remote_allowed or not self._provision_enabled:
            self._bootstrap_local_repo()
            return

        try:
            output = provision_threads_repo(
                repo_url=self.repo_url,
                slug=self.threads_slug,
                code_repo=self.code_repo,
                env=self._env,
            )
            if output:
                self._last_provision_output = output
        except ProvisioningError as provision_error:
            message = (
                f"Failed to clone {self.repo_url}; auto-provision attempt aborted: "
                f"{provision_error}"
            )
            raise GitSyncError(message) from provision_error

        # After provisioning, retry clone. If it still fails, bootstrap locally.
        try:
            with git.Git().custom_environment(**self._env):
                Repo.clone_from(
                    self.repo_url,
                    self.local_path,
                    env=self._env,
                )
            return
        except GitCommandError as retry_error:
            message = str(retry_error)
            if message:
                self._last_provision_output = (
                    (self._last_provision_output or "") + "\n" + message
                ).strip()
            self._bootstrap_local_repo()

    def _bootstrap_local_repo(self) -> None:
        """Initialize local git repository using GitPython (no subprocess)."""
        self._log("Bootstrapping local repository")
        parent = self.local_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        if not self.local_path.exists():
            self.local_path.mkdir(parents=True, exist_ok=True)

        # Use GitPython to init
        repo = Repo.init(self.local_path)

        # Add remote if not present
        if 'origin' not in [remote.name for remote in repo.remotes]:
            repo.create_remote('origin', self.repo_url)
        self._log("Local repository bootstrapped")

    def set_remote_allowed(self, allowed: bool) -> None:
        """Toggle whether remote interactions are permitted."""
        self._remote_allowed = bool(allowed)

    def _ensure_remote_repo_exists(self) -> bool:
        """Ensure the remote repository exists before network operations (GitPython, no subprocess).

        Returns True if the remote appears accessible (post-provisioning when
        enabled). Returns False if the remote is unavailable and cannot be
        auto-provisioned.
        """
        self._remote_missing = False
        self._last_remote_error = None
        self._remote_empty = False

        if not self._remote_allowed:
            self._remote_missing = True
            return False

        try:
            repo = self._repo
            # Safely get origin remote - repo.remotes returns IterableList, not dict
            origin = repo.remote('origin')

            # Use GitPython to list remote refs (equivalent to git ls-remote)
            log_debug("GIT_OP_START: ls-remote (via origin.refs)")
            with git.Git().custom_environment(**self._env):
                refs = origin.refs
                self._remote_empty = len(refs) == 0
            log_debug(f"GIT_OP_END: ls-remote (found {len(refs)} refs)")
            return True
        except GitCommandError as error:
            self._remote_empty = False
            message = str(error)
            lowered = (message or "").lower()

            if self._should_attempt_provision(error):
                try:
                    output = provision_threads_repo(
                        repo_url=self.repo_url,
                        slug=self.threads_slug,
                        code_repo=self.code_repo,
                        env=self._env,
                    )
                    if output:
                        self._last_provision_output = output

                    # Retry with GitPython after provisioning
                    repo = self._repo
                    origin = repo.remote('origin')
                    with git.Git().custom_environment(**self._env):
                        refs = origin.refs
                        self._remote_empty = len(refs) == 0
                    return True
                except ProvisioningError as provision_error:
                    self._last_provision_output = str(provision_error)
                    self._last_remote_error = self._last_provision_output
                    return False
                except GitCommandError as retry_error:
                    retry_message = str(retry_error)
                    self._last_remote_error = (
                        retry_message
                        or message
                        or "Unable to verify provisioned threads repository"
                    )
                    return False

            if "not found" in lowered or "repository" in lowered and "not found" in lowered:
                self._remote_missing = True
                self._last_remote_error = message or "Remote threads repository not found"
                return False

            self._last_remote_error = message or "Unable to contact remote threads repository"
            return False

    def _configure_git(self):
        """Configure git user, credential helper, and hooks using GitPython (no subprocess).

        Raises:
            GitSyncError: If configuration fails
        """
        self._log("Configuring git user and hooks")
        try:
            repo = self._repo
            with repo.config_writer() as config:
                config.set_value('user', 'name', self.author_name)
                config.set_value('user', 'email', self.author_email)

                # Configure credential helper for seamless GitHub authentication
                # Only configure if using HTTPS URLs and credential helper script exists
                if self.repo_url.startswith('https://'):
                    helper_script = self._get_credential_helper_path()

                    if helper_script and helper_script.exists():
                        # Ensure script is executable
                        if not helper_script.is_file():
                            self._log(f"Credential helper is not a file: {helper_script}")
                        else:
                            # Make executable (Unix/Mac only)
                            if os.name != 'nt':
                                helper_script.chmod(0o755)

                            # Configure git to use our credential helper for github.com
                            # Format: credential.https://github.com.helper <path>
                            config.set_value(
                                'credential "https://github.com"',
                                'helper',
                                str(helper_script)
                            )
                            self._log(f"Credential helper configured: {helper_script}")
                    else:
                        self._log("Credential helper script not found, skipping configuration")

                # Install git hooks for protocol enforcement
                # Hooks are CRITICAL for team collaboration to prevent data corruption
                self._install_git_hooks()

                # Configure git to use hooks directory
                config.set_value('core', 'hooksPath', '.githooks')

            self._log("Git user and hooks configured")
        except Exception as e:
            raise GitSyncError(f"Failed to configure git: {e}") from e

    def _get_credential_helper_path(self) -> Optional[Path]:
        """Get path to git credential helper script.

        Uses importlib.resources to locate the credential helper script from
        package data, ensuring it works regardless of installation method
        (development, pip, uvx, etc.).

        Returns:
            Path to credential helper script, or None if not found
        """
        try:
            # Try package data first (works for all installation methods)
            package_files = files('watercooler_mcp')
            script_resource = package_files / 'scripts' / 'git-credential-watercooler'

            # For Python 3.9+, we can use as_file() context manager
            # which extracts the resource to a temporary file if needed
            try:
                # Check if resource exists
                if hasattr(script_resource, 'is_file') and script_resource.is_file():
                    # Resource exists, get its path
                    # Note: In Python 3.9+, this returns a Path-like object
                    return Path(str(script_resource))
            except (AttributeError, TypeError):
                pass

            # Fallback: try development location (relative path from module)
            module_dir = Path(__file__).resolve().parent
            dev_script = module_dir / "scripts" / "git-credential-watercooler"
            if dev_script.exists():
                return dev_script

            # Last resort: check repo root (for editable install)
            repo_root = module_dir.parent.parent
            root_script = repo_root / "scripts" / "git-credential-watercooler"
            if root_script.exists():
                return root_script

            return None

        except Exception as e:
            self._log(f"Error locating credential helper: {e}")
            return None

    def _install_git_hooks(self):
        """Install git hooks into threads repository for protocol enforcement.

        Copies the pre-commit hook from watercooler-cloud installation to the
        threads repository's .githooks directory. This is REQUIRED for team
        collaboration to prevent manual edits that violate the append-only protocol.
        """
        try:
            # Find source hooks in watercooler-cloud installation
            module_dir = Path(__file__).resolve().parent
            repo_root = module_dir.parent.parent  # Up two levels from src/watercooler_mcp/
            source_hook = repo_root / ".githooks" / "pre-commit"

            if not source_hook.exists():
                self._log("Warning: Source pre-commit hook not found, skipping hook installation")
                return

            # Create .githooks directory in threads repo
            hooks_dir = self.local_path / ".githooks"
            hooks_dir.mkdir(exist_ok=True)

            # Copy pre-commit hook
            import shutil
            dest_hook = hooks_dir / "pre-commit"
            shutil.copy2(source_hook, dest_hook)

            # Make hook executable (Unix/Mac only)
            import stat
            try:
                dest_hook.chmod(dest_hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass  # Windows doesn't need executable bit

            self._log(f"Git hooks installed: {dest_hook}")
        except Exception as e:
            # Don't fail initialization if hook installation fails
            # but log the error for debugging
            self._log(f"Warning: Failed to install git hooks: {e}")

    def pull(self) -> bool:
        """Pull latest changes from remote with rebase.

        Uses --rebase --autostash to:
        - Replay local commits on top of remote changes
        - Automatically stash and re-apply local modifications

        Returns:
            True if pull succeeded, False if rebase failed

        Note:
            On rebase failure, automatically aborts the rebase and returns False.
            Caller should handle this gracefully (e.g., retry, alert user).
        """
        self._last_pull_error = None

        if not self._remote_allowed:
            return True

        self._ensure_local_repo_ready()

        remote_available = self._ensure_remote_repo_exists()
        if not remote_available:
            if self._remote_missing:
                return True
            self._last_pull_error = (
                self._last_remote_error
                or "remote threads repository unavailable"
            )
            return False

        if self._remote_empty:
            return True

        # Use GitPython to pull (in-process, no subprocess)
        self._log("Pulling with rebase and autostash")
        try:
            repo = self._repo
            log_debug("GIT_OP_START: fetch origin")
            with git.Git().custom_environment(**self._env):
                # Fetch first
                repo.remote('origin').fetch()
            log_debug("GIT_OP_END: fetch origin")
            log_debug("GIT_OP_START: pull --rebase --autostash")
            with git.Git().custom_environment(**self._env):
                # Pull with rebase
                repo.git.pull('--rebase', '--autostash', env=self._env)
            log_debug("GIT_OP_END: pull --rebase --autostash")
            self._log("Pull completed successfully")
            return True
        except GitCommandError as e:
            error_text = str(e).lower()
            # Fallback: explicitly specify remote/branch when git cannot infer upstream
            # This occurs when multiple refs match the branch name (e.g., remotes/origin/main and remotes/fork/main)
            #
            # Note on nested try-except structure:
            # This section has three levels of exception handling:
            # 1. Outer: Catch initial pull failure
            # 2. Middle: Catch retry pull failure (after explicitly specifying remote/branch)
            # 3. Inner: Catch tracking_branch() failure (may not be configured)
            # While complex, each level handles a distinct failure mode in the git pull sequence
            if "cannot rebase onto multiple branches" in error_text:
                self._log("Pull failed due to ambiguous upstream; retrying with explicit remote/branch")
                try:
                    tracking = None
                    try:
                        tracking = repo.active_branch.tracking_branch()
                    except (AttributeError, TypeError):
                        # Could not determine tracking branch, will use defaults
                        self._log("Warning: Could not determine tracking branch, using defaults (origin/<branch>)")
                        tracking = None
                    remote_name = "origin"
                    remote_branch = repo.active_branch.name
                    if tracking is not None:
                        remote_name = tracking.remote_name or remote_name
                        remote_branch = tracking.remote_head or remote_branch
                    log_debug(
                        f"GIT_OP_RETRY: pull --rebase --autostash {remote_name} {remote_branch}"
                    )
                    with git.Git().custom_environment(**self._env):
                        repo.git.pull(
                            remote_name,
                            remote_branch,
                            '--rebase',
                            '--autostash',
                            env=self._env,
                        )
                    self._log("Fallback pull completed successfully")
                    return True
                except GitCommandError as retry_error:
                    # Retry failed: Intentionally replace original error with retry error
                    # Rationale: The retry error is more relevant since it represents the
                    # failure after we tried to work around the ambiguous upstream issue.
                    # The original error ("cannot rebase onto multiple branches") is less
                    # actionable than the retry error (e.g., "network failure", "permission denied")
                    self._log(f"Fallback pull also failed: {str(retry_error)}")
                    error_text = str(retry_error).lower()
                    e = retry_error  # Replace for downstream error handling
            # Handle various non-error conditions
            if "couldn't find remote ref" in error_text or "could not find remote ref" in error_text:
                return True
            if "does not match any" in error_text:
                return True
            if "no tracking information for the current branch" in error_text:
                return True
            # Network errors
            network_tokens = (
                "could not read from remote repository",
                "could not resolve hostname",
                "permission denied",
                "network is unreachable",
                "failed to connect to",
                "failed in sandbox",
            )
            if any(token in error_text for token in network_tokens):
                self._last_pull_error = str(e) or "network failure contacting remote threads repository"
                self._last_remote_error = self._last_pull_error
                return False
            self._last_pull_error = str(e)
            self._last_remote_error = self._last_pull_error
            # Rebase conflict or other pull failure - abort any in-progress rebase
            self._log(f"Pull failed: {e}, aborting rebase")
            try:
                repo.git.rebase('--abort')
            except:
                pass
            return False
        except Exception as e:
            self._last_pull_error = f"Unexpected error during pull: {e}"
            return False

    def commit_local(self, message: str) -> bool:
        """Commit staged changes locally without pushing (GitPython, no subprocess).

        Returns True if a commit was created, False if there were no staged changes.
        Raises GitSyncError if git commit fails.
        """
        self._log(f"Committing: {message[:60]}...")
        try:
            repo = self._repo
            log_debug("GIT_OP_START: add -A")
            with git.Git().custom_environment(**self._env):
                # Stage all changes within local_path
                repo.git.add('-A')
            log_debug("GIT_OP_END: add -A")

            # Check if there are changes to commit
            if not repo.is_dirty(untracked_files=True):
                self._log("No changes to commit")
                return False

            log_debug(f"GIT_OP_START: commit -m '{message[:40]}'")
            with git.Git().custom_environment(**self._env):
                # Commit changes
                repo.git.commit('-m', message, env=self._env)
            log_debug(f"GIT_OP_END: commit")
            self._log("Commit completed successfully")
        except GitCommandError as e:
            raise GitSyncError(f"Failed to commit: {e}") from e
        except Exception as e:
            raise GitSyncError(f"Failed to commit: {e}") from e
        return True

    def commit_graph_changes(
        self,
        topic: str,
        entry_id: Optional[str] = None,
        max_retries: int = 5,
    ) -> bool:
        """Commit and push graph files (Phase 2 of Two-Phase Commit).

        This commits only graph/baseline/* files to keep the working tree clean
        after graph sync operations. This prevents uncommitted graph files from
        blocking future preflight pulls.

        Args:
            topic: Thread topic for commit message
            entry_id: Optional entry ID for commit message
            max_retries: Max push retry attempts

        Returns:
            True if commit+push succeeded or no changes, False on failure
        """
        try:
            repo = self._repo
            graph_path = Path(self.local_path) / "graph" / "baseline"

            # Check if graph directory exists
            if not graph_path.exists():
                log_debug("[GRAPH-COMMIT] No graph/baseline directory, skipping")
                return True

            # Check for changes in graph files
            log_debug("GIT_OP_START: status graph/baseline")
            with git.Git().custom_environment(**self._env):
                status_output = repo.git.status("--porcelain", "graph/baseline/")
            log_debug(f"GIT_OP_END: status graph/baseline: {status_output[:100] if status_output else '(clean)'}")

            if not status_output.strip():
                log_debug("[GRAPH-COMMIT] No graph changes to commit")
                return True

            # Stage only graph files
            log_debug("GIT_OP_START: add graph/baseline/")
            with git.Git().custom_environment(**self._env):
                repo.git.add("graph/baseline/")
            log_debug("GIT_OP_END: add graph/baseline/")

            # Build commit message
            if entry_id:
                message = f"graph: sync {topic}/{entry_id}"
            else:
                message = f"graph: sync {topic}"

            # Commit
            log_debug(f"GIT_OP_START: commit -m '{message}'")
            with git.Git().custom_environment(**self._env):
                repo.git.commit("-m", message, env=self._env)
            log_debug("GIT_OP_END: commit")
            self._log(f"[GRAPH-COMMIT] Committed graph changes: {message}")

            # Push with retry
            from watercooler_mcp.sync import push_after_commit

            try:
                branch_name = repo.active_branch.name
            except (TypeError, AttributeError):
                branch_name = "main"

            push_success, push_error = push_after_commit(
                self.local_path, branch_name, max_retries=max_retries
            )

            if not push_success:
                log_warning(f"[GRAPH-COMMIT] Push failed: {push_error}")
                return False

            self._log("[GRAPH-COMMIT] Graph changes pushed successfully")
            return True

        except GitCommandError as e:
            # No changes to commit is not an error
            if "nothing to commit" in str(e).lower():
                log_debug("[GRAPH-COMMIT] Nothing to commit")
                return True
            log_warning(f"[GRAPH-COMMIT] Git error: {e}")
            return False
        except Exception as e:
            log_warning(f"[GRAPH-COMMIT] Unexpected error: {e}")
            return False

    def commit_graph_changes_sync(
        self,
        commit_msg: str,
        max_retries: int = 5,
    ) -> None:
        """Commit and push graph files, blocking until confirmed.

        Unlike commit_graph_changes(), this method raises on failure instead
        of returning False. Use this when graph operations need reliable
        confirmation that changes are pushed.

        Args:
            commit_msg: Commit message for graph changes
            max_retries: Max push retry attempts

        Raises:
            GitSyncError: If commit or push fails after retries
        """
        try:
            repo = self._repo
            graph_path = Path(self.local_path) / "graph" / "baseline"

            # Check if graph directory exists
            if not graph_path.exists():
                log_debug("[GRAPH-COMMIT-SYNC] No graph/baseline directory, skipping")
                return

            # Check for changes in graph files
            with git.Git().custom_environment(**self._env):
                status_output = repo.git.status("--porcelain", "graph/baseline/")

            if not status_output.strip():
                log_debug("[GRAPH-COMMIT-SYNC] No graph changes to commit")
                return

            # Stage only graph files
            with git.Git().custom_environment(**self._env):
                repo.git.add("graph/baseline/")

            # Commit
            log_debug(f"[GRAPH-COMMIT-SYNC] Committing: {commit_msg}")
            with git.Git().custom_environment(**self._env):
                repo.git.commit("-m", commit_msg, env=self._env)
            self._log(f"[GRAPH-COMMIT-SYNC] Committed: {commit_msg}")

            # Push with retry (BLOCKING)
            from watercooler_mcp.sync import push_after_commit

            try:
                branch_name = repo.active_branch.name
            except (TypeError, AttributeError):
                branch_name = "main"

            push_success, push_error = push_after_commit(
                self.local_path, branch_name, max_retries=max_retries
            )

            if not push_success:
                raise GitSyncError(
                    f"Graph push failed after {max_retries} retries: {push_error}"
                )

            self._log("[GRAPH-COMMIT-SYNC] Graph changes pushed successfully")

        except GitCommandError as e:
            # No changes to commit is not an error
            if "nothing to commit" in str(e).lower():
                log_debug("[GRAPH-COMMIT-SYNC] Nothing to commit")
                return
            raise GitSyncError(f"Graph commit failed: {e}") from e
        except GitSyncError:
            raise  # Re-raise our own errors
        except Exception as e:
            raise GitSyncError(f"Graph commit failed: {e}") from e

    def push_pending(self, max_retries: int = 5) -> bool:
        """Push local commits to the remote with retry logic."""
        self._last_push_error = None
        if not self._remote_allowed:
            return True

        self._ensure_local_repo_ready()

        remote_available = self._ensure_remote_repo_exists()
        if not remote_available:
            if self._remote_missing:
                return True
            self._last_push_error = (
                self._last_remote_error
                or "remote threads repository unavailable"
            )
            return False

        # Use GitPython to push (in-process, no subprocess)
        for attempt in range(max_retries):
            self._log(f"Pushing (attempt {attempt+1}/{max_retries})")
            try:
                repo = self._repo
                log_debug(f"GIT_OP_START: push (attempt {attempt+1})")
                with git.Git().custom_environment(**self._env):
                    repo.remote('origin').push(env=self._env)
                log_debug(f"GIT_OP_END: push (attempt {attempt+1})")
                self._log("Push completed successfully")
                return True

            except GitCommandError as e:
                error_text = str(e).lower()

                # Network errors
                network_tokens = (
                    "could not read from remote repository",
                    "could not resolve hostname",
                    "permission denied",
                    "network is unreachable",
                    "failed to connect to",
                    "failed in sandbox",
                )
                if any(token in error_text for token in network_tokens):
                    self._last_push_error = str(e) or "network failure contacting remote threads repository"
                    return False

                # Handle missing upstream / no configured push destination
                if ("has no upstream branch" in error_text or
                    "no configured push destination" in error_text or
                    "does not match any" in error_text):
                    self._log("Setting upstream branch")
                    try:
                        repo.git.push('-u', 'origin', 'HEAD', env=self._env)
                        self._log("Upstream set successfully")
                        return True
                    except GitCommandError as push_error:
                        self._last_push_error = str(push_error) or "failed to set upstream for threads branch"
                        if attempt >= max_retries - 1:
                            return False
                        continue

                if attempt < max_retries - 1:
                    # Push rejected - pull and retry
                    self._log("Push rejected, pulling before retry")
                    if not self.pull():
                        # Pull failed (rebase conflict or no upstream yet)
                        # Give one more chance on next loop iteration
                        pass
                    # Small exponential backoff to reduce contention
                    try:
                        time.sleep(0.25 * (2 ** attempt))
                    except Exception:
                        pass
                    continue

                self._last_push_error = str(e) or "failed to push threads updates"
                # Max retries exceeded
                return False
        return False

    def commit_and_push(self, message: str, max_retries: int = 5) -> bool:
        """Commit changes and push to remote with retry logic."""
        committed = self.commit_local(message)
        if not committed:
            return True
        return self.push_pending(max_retries=max_retries)

    def ensure_branch(self, branch: str) -> bool:
        """Ensure the local repo is on the given branch, creating it if needed (GitPython, no subprocess).

        Also sets upstream to origin/<branch> on first creation.
        Returns True on success, False on failure (non-fatal; caller can proceed).
        """
        try:
            self._ensure_local_repo_ready()
            repo = self._repo

            # What branch are we on now?
            current = repo.active_branch.name
            if current == branch:
                return True

            # Does the branch exist locally?
            exists = branch in [ref.name for ref in repo.heads]

            remote_available = self._remote_allowed and self._ensure_remote_repo_exists()
            remote_has_branch = False

            if remote_available:
                # Check if remote has the branch
                with git.Git().custom_environment(**self._env):
                    origin = repo.remote('origin')
                    remote_has_branch = f"origin/{branch}" in [ref.name for ref in origin.refs]

            if exists:
                # Checkout existing local branch
                log_debug(f"GIT_OP_START: checkout {branch}")
                with git.Git().custom_environment(**self._env):
                    repo.git.checkout(branch, env=self._env)
                log_debug(f"GIT_OP_END: checkout {branch}")
            else:
                if remote_has_branch:
                    # Fetch and checkout remote branch
                    log_debug(f"GIT_OP_START: fetch {branch}")
                    with git.Git().custom_environment(**self._env):
                        origin = repo.remote('origin')
                        origin.fetch(refspec=f"{branch}:refs/heads/{branch}", env=self._env)
                    log_debug(f"GIT_OP_END: fetch {branch}")
                    log_debug(f"GIT_OP_START: checkout {branch}")
                    with git.Git().custom_environment(**self._env):
                        repo.git.checkout(branch, env=self._env)
                    log_debug(f"GIT_OP_END: checkout {branch}")
                else:
                    # Create new branch
                    log_debug(f"GIT_OP_START: checkout -b {branch}")
                    with git.Git().custom_environment(**self._env):
                        repo.git.checkout('-b', branch, env=self._env)
                    log_debug(f"GIT_OP_END: checkout -b {branch}")

            # Ensure upstream is set when remote branch exists
            try:
                # Check if upstream is set (will raise if not)
                repo.active_branch.tracking_branch()
                has_upstream = True
            except (AttributeError, TypeError):
                has_upstream = False

            if not has_upstream and remote_has_branch:
                # Set upstream to origin/branch
                with git.Git().custom_environment(**self._env):
                    repo.git.branch('--set-upstream-to', f'origin/{branch}', branch, env=self._env)
            elif not has_upstream and self._remote_allowed and not remote_has_branch:
                # Remote unavailable or branch missing - leave as-is (local-only)
                pass
            return True
        except Exception:
            return False

    def with_sync(
        self,
        operation: Callable[[], T],
        commit_message: str,
        *,
        topic: Optional[str] = None,
        entry_id: Optional[str] = None,
        priority_flush: bool = False,
    ) -> T:
        """Execute operation with git synchronization.

        When async mode is disabled this preserves the legacy behaviour of
        pull → operation → commit → push. When async mode is enabled the
        operation is executed immediately, the commit is recorded locally, and
        a background worker pushes it to the remote (flushing immediately for
        priority operations such as ball hand-offs).
        """
        # Auto-sync branches before any write operation
        self._ensure_branch_sync()

        if self._async is None:
            return self._with_sync_sync(
                operation,
                commit_message,
            )

        return self._with_sync_async(
            operation,
            commit_message,
            topic=topic,
            entry_id=entry_id,
            priority_flush=priority_flush,
        )

    def _with_sync_sync(self, operation: Callable[[], T], commit_message: str) -> T:
        if not self.pull():
            detail = self._last_pull_error or "unknown error"
            raise GitPullError(
                f"Failed to pull latest changes before operation: {detail}"
            )

        result = operation()

        # Commit locally first
        committed = self.commit_local(commit_message)
        if not committed:
            # No changes to commit, operation succeeded
            return result

        # Push with rebase-on-reject retry using branch parity helper
        from watercooler_mcp.sync import (
            push_after_commit,
            read_parity_state,
            write_parity_state,
            _now_iso,
            ParityStatus,
        )

        try:
            branch_name = self._repo.active_branch.name
        except (TypeError, AttributeError):
            branch_name = "main"
        max_retries = self._async_config.get("max_sync_retries", 5)
        push_success, push_error = push_after_commit(
            self.local_path, branch_name, max_retries=max_retries
        )

        # Update parity state after push attempt
        try:
            state = read_parity_state(self.local_path)
            if state:
                if push_success:
                    state.pending_push = False
                    state.last_error = None
                    state.status = ParityStatus.CLEAN.value
                else:
                    state.pending_push = True
                    state.last_error = push_error
                    state.status = ParityStatus.PENDING_PUSH.value
                state.last_check_at = _now_iso()
                write_parity_state(self.local_path, state)
        except Exception as state_err:
            log_debug(f"[PARITY] Failed to update state after push: {state_err}")

        if not push_success:
            self._last_push_error = push_error
            raise GitPushError(f"Failed to push changes: {push_error}")

        return result

    def _with_sync_async(
        self,
        operation: Callable[[], T],
        commit_message: str,
        *,
        topic: Optional[str],
        entry_id: Optional[str],
        priority_flush: bool,
    ) -> T:
        if self._async is None:
            return self._with_sync_sync(operation, commit_message)

        self._ensure_local_repo_ready()
        result = operation()

        committed = self.commit_local(commit_message)
        if committed:
            self._async.enqueue_commit(
                commit_message=commit_message,
                topic=topic,
                entry_id=entry_id,
                priority_flush=priority_flush,
            )
            if priority_flush:
                self._async.flush_now()
        elif priority_flush:
            # No commit was produced but we must honor the caller's expectation
            # that remote state is consistent (e.g., ball hand-off without body).
            self._async.flush_now()

        return result

    def flush_async(self, timeout: Optional[float] = None) -> None:
        """Force the async worker to push queued commits immediately."""
        if self._async is None:
            raise GitPushError("Async sync is disabled for this repository")
        self._async.flush_now(timeout=timeout or 60.0)

    def get_async_status(self) -> dict:
        """Return diagnostic information about the async queue."""
        if self._async is None:
            return {
                "mode": "sync",
                "pending": 0,
                 "pending_topics": [],
                "priority": False,
                "retry_at": None,
                "retry_in_seconds": None,
                "last_error": None,
                 "last_pull": None,
                 "last_pull_age_seconds": None,
                 "stale": False,
                 "stale_threshold": self._async_config.get("stale_threshold"),
                 "next_pull_eta_seconds": None,
                 "is_syncing": False,
                 "sync_interval": self._async_config.get("sync_interval"),
            }
        return self._async.status()


class _AsyncSyncCoordinator:
    """Background worker that batches git push operations."""

    def __init__(
        self,
        manager: "GitSyncManager",
        *,
        batch_window: float,
        max_delay: float,
        max_batch_size: int,
        max_sync_retries: int,
        max_backoff: float,
        log_enabled: bool,
        sync_interval: float,
        stale_threshold: float,
    ) -> None:
        self._manager = manager
        self._batch_window = batch_window
        self._max_delay = max_delay
        self._max_batch_size = max_batch_size
        self._max_sync_retries = max_sync_retries
        self._max_backoff = max_backoff
        self._log_enabled = log_enabled
        self._sync_interval = max(1.0, sync_interval)
        self._stale_threshold = max(1.0, stale_threshold)

        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()

        self._pending: list[_PendingCommit] = []
        self._next_sequence = 1
        self._last_flushed_sequence = 0
        self._priority_flush = False
        self._retry_at: Optional[float] = None
        self._current_backoff = self._batch_window
        self._last_error: Optional[str] = None
        self._last_error_sequence: int = 0
        self._last_pull: float = 0.0
        self._next_pull_due: float = time.time()
        self._is_syncing: bool = False

        self._queue_dir = manager.local_path.parent / ".watercooler-pending-sync"
        self._queue_file = self._queue_dir / "queue.jsonl"
        self._log_path = self._queue_dir / "async-sync.log"

        self._load_queue()
        if not self._pending:
            # Ensure we still wake for the first scheduled pull.
            self._wake_event.set()

        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"watercooler-async-sync-{id(self)}",
            daemon=True,
        )
        self._worker.start()
        atexit.register(self.shutdown)

        # Kick worker if we already had pending items from disk.
        if self._pending:
            self._wake_event.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue_commit(
        self,
        *,
        commit_message: str,
        topic: Optional[str],
        entry_id: Optional[str],
        priority_flush: bool,
    ) -> None:
        timestamp = _now_iso()
        created_ts = time.time()

        with self._lock:
            sequence = self._next_sequence
            self._next_sequence += 1
            commit = _PendingCommit(
                sequence=sequence,
                entry_id=entry_id,
                topic=topic,
                commit_message=commit_message,
                timestamp=timestamp,
                created_ts=created_ts,
            )
            self._pending.append(commit)
            self._append_to_queue_locked(commit)
            if priority_flush:
                self._priority_flush = True
                # Reset retry timer – priority flush should try immediately.
                self._retry_at = None

        self._wake_event.set()

    def flush_now(self, timeout: float = 60.0) -> None:
        """Block until all commits currently queued are pushed."""
        target_sequence: Optional[int]
        with self._lock:
            if not self._pending:
                self._last_error = None
                self._priority_flush = False
                return
            target_sequence = self._pending[-1].sequence
            self._priority_flush = True
            self._retry_at = None

        self._wake_event.set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._last_error and self._last_error_sequence >= (target_sequence or 0):
                    error = self._last_error
                    # Keep error so subsequent callers can see it
                    raise GitPushError(error)
                if self._last_flushed_sequence >= (target_sequence or 0):
                    return
            time.sleep(0.05)

        with self._lock:
            error = self._last_error or "timeout waiting for async sync flush"
        raise GitPushError(error)

    def shutdown(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._wake_event.set()
        try:
            if self._pending:
                self.flush_now(timeout=self._max_delay)
        except GitPushError:
            # Best-effort during shutdown
            pass
        if self._worker.is_alive():
            try:
                self._worker.join(timeout=2.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            timeout = self._time_until_next_action()
            self._wake_event.wait(timeout=timeout)
            self._wake_event.clear()
            try:
                self._process_once()
            except Exception as exc:  # pragma: no cover - defensive logging
                self._log(f"worker error: {exc}")

    def _time_until_next_action(self) -> float:
        now = time.time()
        with self._lock:
            if self._stop_event.is_set():
                return 0.1
            if self._priority_flush:
                return 0.05
            if self._retry_at is not None and now < self._retry_at:
                wait = self._retry_at - now
                return max(0.05, min(wait, 1.0))
            if self._pending:
                oldest = self._pending[0]
                if len(self._pending) >= self._max_batch_size:
                    return 0.05
                age = now - oldest.created_ts
                flush_due = min(
                    oldest.created_ts + self._batch_window,
                    oldest.created_ts + self._max_delay,
                )
                wait = flush_due - now
                if wait <= 0:
                    return 0.05
                return max(0.05, min(wait, 1.0))
            wait = self._next_pull_due - now
            if wait <= 0:
                return 0.05
            return max(0.05, min(wait, 1.0))

    def _process_once(self) -> None:
        now = time.time()
        with self._lock:
            if self._retry_at is not None and now < self._retry_at:
                return
            pending_count = len(self._pending)
            priority = self._priority_flush
            target_sequence = self._pending[-1].sequence if pending_count else None
            oldest_created = self._pending[0].created_ts if pending_count else None
            due_pull = now >= self._next_pull_due

        if priority or self._should_flush(now, pending_count, oldest_created):
            self._execute_flush(target_sequence)
            return

        if due_pull:
            self._execute_pull()

    def _should_flush(self, now: float, pending_count: int, oldest_created: Optional[float]) -> bool:
        if pending_count == 0:
            return False
        with self._lock:
            if self._priority_flush:
                return True
        if pending_count >= self._max_batch_size:
            return True
        if oldest_created is None:
            return False
        age = now - oldest_created
        if age >= self._max_delay:
            return True
        if age >= self._batch_window:
            return True
        return False

    def _execute_flush(self, target_sequence: Optional[int]) -> None:
        if target_sequence is None:
            with self._lock:
                self._priority_flush = False
            return

        success = self._perform_sync()

        with self._lock:
            if success:
                self._pending = [commit for commit in self._pending if commit.sequence > target_sequence]
                self._rewrite_queue_locked()
                self._last_flushed_sequence = max(self._last_flushed_sequence, target_sequence)
                self._priority_flush = False
                self._retry_at = None
                self._current_backoff = self._batch_window
                self._last_error = None
                self._last_error_sequence = 0
            else:
                now = time.time()
                self._current_backoff = min(self._current_backoff * 2, self._max_backoff)
                self._retry_at = now + self._current_backoff
                self._last_error = (
                    self._manager._last_push_error
                    or self._manager._last_pull_error
                    or "async sync failed"
                )
                self._last_error_sequence = target_sequence

    def _execute_pull(self) -> None:
        if self._perform_pull():
            return
        with self._lock:
            now = time.time()
            self._current_backoff = min(self._current_backoff * 2, self._max_backoff)
            self._retry_at = now + self._current_backoff
            self._last_error = (
                self._manager._last_pull_error
                or "async pull failed"
            )
            self._last_error_sequence = 0

    def _perform_sync(self) -> bool:
        pull_time = time.time()
        self._set_syncing(True)
        try:
            if not self._manager.pull():
                return False
            self._record_pull_success(pull_time)
            if not self._manager.push_pending(max_retries=self._max_sync_retries):
                return False
            return True
        finally:
            self._set_syncing(False)

    def _perform_pull(self) -> bool:
        pull_time = time.time()
        self._set_syncing(True)
        try:
            if not self._manager.pull():
                return False
            self._record_pull_success(pull_time)
            return True
        finally:
            self._set_syncing(False)

    def _record_pull_success(self, pull_time: float) -> None:
        with self._lock:
            self._last_pull = pull_time
            self._next_pull_due = pull_time + self._sync_interval
            self._retry_at = None
            self._current_backoff = self._batch_window
            if self._last_error_sequence == 0:
                self._last_error = None

    def _set_syncing(self, value: bool) -> None:
        with self._lock:
            self._is_syncing = value

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            pending = len(self._pending)
            pending_topics = sorted({commit.topic for commit in self._pending if commit.topic})
            priority = self._priority_flush
            oldest_ts = self._pending[0].timestamp if pending else None
            retry_at = self._retry_at
            last_error = self._last_error
            last_pull = self._last_pull
            next_pull_due = self._next_pull_due
            is_syncing = self._is_syncing
        retry_in = None
        if retry_at is not None:
            remaining = retry_at - now
            retry_in = remaining if remaining > 0 else 0.0
        last_pull_age = None
        stale = False
        if last_pull > 0:
            last_pull_age = now - last_pull
            stale = last_pull_age > self._stale_threshold
        next_pull_eta = max(0.0, next_pull_due - now)
        return {
            "mode": "async",
            "pending": pending,
            "pending_topics": pending_topics,
            "priority": priority,
            "oldest": oldest_ts,
            "retry_at": _iso_from_epoch(retry_at),
            "retry_in_seconds": retry_in,
            "last_error": last_error,
            "batch_window": self._batch_window,
            "max_delay": self._max_delay,
            "max_batch_size": self._max_batch_size,
            "sync_interval": self._sync_interval,
            "stale_threshold": self._stale_threshold,
            "last_pull": _iso_from_epoch(last_pull) if last_pull > 0 else None,
            "last_pull_age_seconds": last_pull_age,
            "stale": stale,
            "next_pull_eta_seconds": next_pull_eta,
            "is_syncing": is_syncing,
        }

    # ------------------------------------------------------------------
    # Queue persistence
    # ------------------------------------------------------------------

    def _load_queue(self) -> None:
        if not self._queue_file.exists():
            return
        try:
            with self._queue_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    checksum = payload.pop("checksum", "")
                    if checksum and checksum != _checksum_payload(payload):
                        self._log("skipping corrupt queue line (checksum mismatch)")
                        continue
                    commit = _PendingCommit.from_payload(payload)
                    self._pending.append(commit)
                    self._next_sequence = max(self._next_sequence, commit.sequence + 1)
            self._pending.sort(key=lambda item: item.sequence)
            if self._pending:
                self._last_flushed_sequence = self._pending[0].sequence - 1
                # Ensure queued commits flush promptly after restart.
                self._next_pull_due = time.time()
        except Exception as exc:  # pragma: no cover - defensive fallback
            self._log(f"failed to load queue: {exc}")
            try:
                backup = self._queue_file.with_suffix(".corrupt")
                self._queue_file.rename(backup)
            except Exception:
                pass
            self._pending = []
            self._next_sequence = 1

    def _append_to_queue_locked(self, commit: _PendingCommit) -> None:
        payload = commit.to_payload()
        payload["checksum"] = _checksum_payload(payload)
        try:
            self._queue_dir.mkdir(parents=True, exist_ok=True)
            with self._queue_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log(f"failed to append queue entry: {exc}")

    def _rewrite_queue_locked(self) -> None:
        if not self._pending:
            try:
                if self._queue_file.exists():
                    self._queue_file.unlink()
            except Exception:
                pass
            return

        payloads = []
        for commit in sorted(self._pending, key=lambda item: item.sequence):
            payload = commit.to_payload()
            payload["checksum"] = _checksum_payload(payload)
            payloads.append(payload)

        tmp = self._queue_file.with_suffix(".tmp")
        try:
            self._queue_dir.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                for payload in payloads:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            tmp.replace(self._queue_file)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log(f"failed to rewrite queue: {exc}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        if not self._log_enabled:
            return
        try:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._queue_dir.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{timestamp} {message}\n")
        except Exception:
            pass
