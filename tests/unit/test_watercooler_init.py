"""Tests for watercooler_init (MCP-native setup) and health detail=setup.

Covers the readiness contract, the opt-in push gate (the security-critical
path), idempotency, and that the read-only health mode mutates nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from watercooler_mcp import config
from watercooler_mcp.tools.diagnostic import _health_setup_impl
from watercooler_mcp.tools.setup import _init_impl


@pytest.fixture(autouse=True)
def isolated_config_cache():
    """Keep ambient repo config out of setup tests' temp repositories."""
    from watercooler.config_loader import clear_config_cache

    clear_config_cache()
    config._loaded_config = None
    yield
    clear_config_cache()
    config._loaded_config = None


def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fresh git repo with an isolated worktree base (no host pollution)."""
    code = tmp_path / "proj"
    code.mkdir()
    _git(["init"], code)
    _git(["config", "user.email", "t@t.t"], code)
    _git(["config", "user.name", "t"], code)
    _git(["commit", "--allow-empty", "-m", "init"], code)

    wt_base = tmp_path / "worktrees"
    monkeypatch.setattr(config, "WORKTREE_BASE", wt_base)
    monkeypatch.chdir(code)
    # Keep parity/sync probes offline and deterministic.
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    return code


def _init(code, **kw):
    return json.loads(_init_impl(None, code_path=str(code), **kw))


def test_fresh_repo_initializes_locally(repo):
    r = _init(repo)
    assert r["usable_now"] is True
    assert r["roles_customizable"] is True
    assert r["details"]["worktree"] == "created"
    assert r["details"]["roles_file"] == "created"
    assert r["sync_status"] == "no_remote"
    assert r["push_attempt"] == "not_requested"
    assert (repo / ".watercooler" / "roles.toml").is_file()


def test_credentials_are_gitignored_but_not_roles(repo):
    _init(repo)
    gitignore = (repo / ".gitignore").read_text()
    assert ".watercooler/credentials.toml" in gitignore
    # roles.toml/config.toml must NOT be ignored, and the dir not blanket-ignored.
    assert "roles.toml" not in gitignore.replace("credentials.toml", "")
    assert ".watercooler/\n" not in gitignore


def test_idempotent_reinit(repo):
    _init(repo)
    r2 = _init(repo)
    assert r2["details"]["worktree"] == "exists"
    assert r2["details"]["roles_file"] == "exists"
    assert r2["usable_now"] is True


def test_push_requires_confirmation_when_remote_visibility_unknown(repo):
    _git(["remote", "add", "origin", "https://example.com/x/y.git"], repo)
    r = _init(repo, push=True)
    # Bare push=true with an unknown-visibility remote must NOT publish.
    assert r["push_attempt"] == "failed"
    assert r["sync_status"] != "synced"
    assert any("can't confirm" in w for w in r["warnings"])


def test_push_with_no_remote_reports_no_remote(repo):
    r = _init(repo, push=True)
    assert r["push_attempt"] == "failed"
    assert r["sync_status"] == "no_remote"


def test_confirm_public_attempts_push(repo):
    _git(["remote", "add", "origin", "https://example.com/x/y.git"], repo)
    r = _init(repo, push=True, confirm_public=True)
    # The fake remote is unreachable → a real attempt that fails (not a refusal).
    assert r["push_attempt"] == "failed"
    assert r["sync_status"] == "auth_failed"
    assert any(a["tool"] == "watercooler_sync_repair" for a in r["next_actions"])


def test_allow_local_only_silences_unsynced_nudge(repo):
    _git(["remote", "add", "origin", "https://example.com/x/y.git"], repo)
    r = _init(repo, allow_local_only=True)
    assert r["sync_status"] == "local_only"
    assert "ask me to push" not in r["summary"]


def test_force_rescaffold_backs_up(repo):
    _init(repo)
    (repo / ".watercooler" / "roles.toml").write_text("# edited by user\n")
    r = _init(repo, force=True)
    assert r["details"]["roles_file"] == "created"
    backups = list((repo / ".watercooler").glob("roles.toml.bak-*"))
    assert backups and backups[0].read_text() == "# edited by user\n"


def test_non_git_path_reports_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "worktrees")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    r = json.loads(_init_impl(None, code_path=str(plain)))
    assert r["usable_now"] is False
    assert "git" in r["summary"].lower()


def test_needs_code_path_when_absent():
    r = json.loads(_init_impl(None, code_path=""))
    assert r["needs_code_path"] is True
    assert r["push_attempt"] == "not_applicable"


# ── health detail=setup (read-only) ──────────────────────────────────────────


def test_health_setup_is_read_only(repo):
    """detail=setup must not create the worktree or roles file."""
    r = json.loads(_health_setup_impl(None, code_path=str(repo)))
    assert r["usable_now"] is False
    assert r["details"]["worktree"] == "absent"  # never created, not an error
    assert r["push_attempt"] == "not_applicable"
    wt = config.WORKTREE_BASE / repo.name
    assert not wt.exists(), "health detail=setup must not bind a worktree"
    assert not (repo / ".watercooler" / "roles.toml").exists()


def test_health_setup_reports_initialized_after_init(repo):
    _init(repo)
    r = json.loads(_health_setup_impl(None, code_path=str(repo)))
    assert r["usable_now"] is True
    assert r["roles_customizable"] is True
    assert r["push_attempt"] == "not_applicable"


def test_health_setup_needs_code_path():
    r = json.loads(_health_setup_impl(None, code_path=""))
    assert r["needs_code_path"] is True


def test_health_setup_non_git_matches_init(tmp_path, monkeypatch):
    """Read-only check on a non-git dir must say 'git init first', not 'call init'."""
    monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "worktrees")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    h = json.loads(_health_setup_impl(None, code_path=str(plain)))
    i = json.loads(_init_impl(None, code_path=str(plain)))
    assert h["usable_now"] is False
    assert "git" in h["summary"].lower()
    # health and init give the same prerequisite + the same (non-init) next step.
    assert h["summary"] == i["summary"]
    assert [a["tool"] for a in h["next_actions"]] == [a["tool"] for a in i["next_actions"]]
    assert all(a["tool"] != "watercooler_init" for a in h["next_actions"])


# ── push gate: remote= is targeting only, never consent (#012) ────────────────


def test_remote_arg_does_not_bypass_public_gate(repo):
    """Naming remote= must NOT satisfy the consent gate (agent self-consent)."""
    _git(["remote", "add", "origin", "https://example.com/x/y.git"], repo)
    r = _init(repo, push=True, remote="origin")
    assert r["push_attempt"] == "failed"
    assert r["sync_status"] != "synced"
    assert any("can't confirm" in w for w in r["warnings"])


# ── push reporting consistency: no false "synced" (#013/#020) ─────────────────


def test_report_has_no_remote_is_public_field(repo):
    r = _init(repo)
    assert "remote_is_public" not in r


def test_named_remote_push_succeeds_and_health_agrees(repo, tmp_path):
    """A successful push to a reachable named remote yields sync_status='synced',
    and a later read-only health check reports the same status (init == health).

    Uses a local bare repo as origin so the push is offline and deterministic.
    """
    bare = tmp_path / "origin.git"
    _git(["init", "--bare", str(bare)], tmp_path)
    _git(["remote", "add", "origin", str(bare)], repo)

    r = _init(repo, push=True, confirm_public=True)
    assert r["push_attempt"] == "pushed"
    assert r["sync_status"] == "synced"

    # The read-only setup report must recompute the same status from git state.
    h = json.loads(_health_setup_impl(None, code_path=str(repo)))
    assert h["sync_status"] == "synced"
    assert h["usable_now"] is True


# ── secret safety: already-tracked credentials are flagged (#016) ─────────────


def test_tracked_credentials_are_flagged(repo):
    creds = repo / ".watercooler" / "credentials.toml"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("token = 'secret'\n")
    _git(["add", "-f", ".watercooler/credentials.toml"], repo)
    r = _init(repo)
    assert r["details"]["credentials_tracked"] is True
    assert any("already tracked" in w for w in r["warnings"])


# ── config validation is repo-scoped and read-only (#019) ─────────────────────


def test_malformed_project_config_reports_not_ok(repo, monkeypatch):
    # Local config validity is what's under test — pin the effective transport
    # local by clearing hosted credentials so the proxy default falls back to
    # stdio (otherwise a credentialed machine would report hosted-N/A).
    from watercooler.config_facade import config as _facade

    monkeypatch.setattr(_facade, "get_hosted_api_key", lambda: "")
    cfg = repo / ".watercooler" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("this is = = not valid toml\n")
    r = json.loads(_health_setup_impl(None, code_path=str(repo)))
    assert r["details"]["effective_config_ok"] is False
    assert any("configuration failed to load" in w for w in r["warnings"])
    # A repo whose effective config can't resolve is not "set up".
    assert r["usable_now"] is False


def test_init_malformed_config_is_not_usable(repo, monkeypatch):
    from watercooler.config_facade import config as _facade

    monkeypatch.setattr(_facade, "get_hosted_api_key", lambda: "")
    (repo / ".watercooler").mkdir(parents=True, exist_ok=True)
    (repo / ".watercooler" / "config.toml").write_text("broken = = toml\n")
    r = _init(repo)
    assert r["details"]["effective_config_ok"] is False
    assert r["usable_now"] is False


def test_health_setup_does_not_mutate_config_cache(repo):
    """detail=setup validates config repo-scoped via config_loader.load_config,
    never touching the module-global cache get_watercooler_config populates."""
    original = config._loaded_config
    try:
        primed = config.get_watercooler_config()  # populate the module cache
        assert config._loaded_config is primed
        json.loads(_health_setup_impl(None, code_path=str(repo)))
        # The read-only report must not swap or clear the cached object.
        assert config._loaded_config is primed
    finally:
        config._loaded_config = original


def test_config_state_total_when_config_loader_unavailable(repo, monkeypatch):
    """An unexpected ImportError must not crash the read-only report (#1019):
    config_state catches it and surfaces the cause instead of propagating."""
    from watercooler import config_loader
    from watercooler_mcp.setup_report import config_state

    def _boom(*_a, **_k):
        raise ImportError("config_loader unavailable")

    monkeypatch.setattr(config_loader, "load_config", _boom)
    ok, sources, error = config_state(repo)
    assert ok is False
    assert error is not None and error.startswith("ImportError")
    assert "built-in defaults" in sources


# ── worktree validity: wrong-branch worktree is not "present" (#P2) ───────────


def test_worktree_on_wrong_branch_not_counted(repo):
    """A worktree path on some other branch must not be counted as set up."""
    wt = config.WORKTREE_BASE / repo.name
    wt.mkdir(parents=True)
    _git(["init"], wt)
    _git(["config", "user.email", "t@t.t"], wt)
    _git(["config", "user.name", "t"], wt)
    _git(["commit", "--allow-empty", "-m", "not-the-orphan-branch"], wt)
    r = json.loads(_health_setup_impl(None, code_path=str(repo)))
    assert r["details"]["worktree"] == "absent"
    assert r["usable_now"] is False


# ── local-init applicability is the deployment axis, not the transport (#P2) ──


def _deployment_context_with(
    monkeypatch, *, transport, hosted, url="https://h.example/mcp/", api_key="wc_live"
):
    import watercooler_mcp.auth as auth
    import watercooler_mcp.config as cfg
    from watercooler.config_facade import config as facade
    from watercooler_mcp.setup_report import deployment_context

    monkeypatch.setattr(
        cfg, "get_mcp_transport_config", lambda: {"transport": transport, "url": url}
    )
    monkeypatch.setattr(auth, "is_hosted_mode", lambda: hosted)
    # proxy's effective transport depends on hosted credentials; control them.
    monkeypatch.setattr(facade, "get_hosted_api_key", lambda: api_key)
    return deployment_context()


def test_self_hosted_http_allows_local_init(monkeypatch):
    # Self-hosted HTTP has a local checkout — it MUST be able to initialize.
    transport, mode, applies = _deployment_context_with(
        monkeypatch, transport="http", hosted=False
    )
    assert (transport, mode, applies) == ("http", "local", True)


def test_hosted_http_disallows_local_init(monkeypatch):
    # The checkout-less hosted control plane provisions storage server-side.
    _, mode, applies = _deployment_context_with(
        monkeypatch, transport="http", hosted=True
    )
    assert mode == "hosted"
    assert applies is False


def test_proxy_disallows_local_init(monkeypatch):
    # A credentialed proxy forwards to a remote server — no local checkout to bind.
    _, _, applies = _deployment_context_with(
        monkeypatch, transport="proxy", hosted=False, api_key="wc_live"
    )
    assert applies is False


def test_proxy_without_credentials_falls_back_to_local(monkeypatch):
    # The proxy default with no hosted key actually runs local stdio, so a local
    # checkout applies and the report must reflect the effective transport.
    transport, _, applies = _deployment_context_with(
        monkeypatch, transport="proxy", hosted=False, api_key=""
    )
    assert transport == "stdio"
    assert applies is True
