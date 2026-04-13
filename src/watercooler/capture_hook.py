#!/usr/bin/env python3
"""Packaged PostCompact capture hook for Project Pulse.

Reads a compaction summary from stdin (PostCompact hook payload), extracts structured
session metadata via the configured LLM, and deposits it as a watercooler entry in the
contributor's session-context thread.

This module is the canonical, package-installed implementation of the capture hook.
Install with the package and configure via ``watercooler setup-pulse-hook``.

Usage (as hook):
    Configured in ~/.claude/settings.json as a PostCompact hook. Receives JSON via stdin:
    {"compact_summary": "...", "session_id": "...", "cwd": "...", "trigger": "auto"|"manual"}

Usage (manual queue drain):
    watercooler-capture-theme --drain-queue

Deposit is via ``watercooler say`` CLI. On failure, themes are queued to
~/.watercooler/pulse_queue.jsonl for retry.

Platform: Linux/macOS only (requires ``fcntl``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl  # not available on Windows
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

LOG_PATH = Path.home() / ".watercooler" / "pulse_capture.log"

VALID_KINDS = {
    "insight", "decision", "problem", "risk", "exploration",
    "lesson", "reasoning", "stopgap", "procedure",
    # D4 delivery signals (Phase 2)
    "pr_merged", "closure", "resolved_loop", "opened_loops", "closed_loops",
}
QUEUE_PATH = Path.home() / ".watercooler" / "pulse_queue.jsonl"
EXTRACTOR_VERSION = "pulse-extractor-v1"


def _log(msg: str) -> None:
    """Append a timestamped line to the capture log."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"{ts} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Config resolution (#139: tomllib-based, replaces hand-rolled parser)
# ---------------------------------------------------------------------------

# SYNC: duplicated in setup_hook.py — update both when changing
def _load_config(path: Path | None = None) -> dict[str, Any]:
    """Load and parse a TOML config file. Returns empty dict on failure."""
    if path is None:
        path = Path.home() / ".watercooler" / "config.toml"
    if not path.exists() or tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


# SYNC: duplicated in setup_hook.py — update both when changing
def get_contributor_name() -> str | None:
    """Read contributor name from ~/.watercooler/config.toml.

    Checks (in order): [contributor].name, [agent].user_tag.
    """
    cfg = _load_config()
    name = (
        cfg.get("contributor", {}).get("name")
        or cfg.get("agent", {}).get("user_tag")
        or None
    )
    if not name:
        agent_tag = cfg.get("mcp", {}).get("agent_tag")
        if agent_tag:
            _log(
                "DEPRECATED: [mcp].agent_tag is deprecated for contributor name. "
                "Use [contributor].name instead."
            )
            name = agent_tag
    return name


def get_llm_config() -> dict[str, str]:
    """Resolve LLM endpoint config from env vars, config.toml, and credentials.toml.

    Resolution order:
    1. Environment variables (LLM_API_BASE, LLM_API_KEY, LLM_MODEL)
    2. config.toml [pulse] / [pulse.llm] section
    3. config.toml [memory.graphiti] section (llm_api_base)
    4. config.toml [memory.llm] or [llm] section
    5. credentials.toml provider-specific key (e.g., [deepseek] api_key)
    6. Provider-specific env vars (DEEPSEEK_API_KEY, OPENAI_API_KEY)
    """
    api_base = os.environ.get("LLM_API_BASE", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")

    cfg = _load_config()

    # Collect candidates from multiple sections; prefer remote endpoints
    candidates: list[dict[str, str]] = []
    # [pulse] / [pulse.llm]
    pulse = cfg.get("pulse", {})
    pulse_llm = pulse.get("llm", {}) if isinstance(pulse, dict) else {}
    merged_pulse = {**pulse_llm, **{k: v for k, v in pulse.items() if not isinstance(v, dict)}}
    candidates.append({
        "api_base": merged_pulse.get("api_base", "") or merged_pulse.get("llm_api_base", ""),
        "api_key": merged_pulse.get("api_key", "") or merged_pulse.get("llm_api_key", ""),
        "model": merged_pulse.get("model", "") or merged_pulse.get("llm_model", ""),
    })
    # [memory.graphiti]
    graphiti = cfg.get("memory", {}).get("graphiti", {})
    candidates.append({
        "api_base": graphiti.get("llm_api_base", ""),
        "model": graphiti.get("llm_model", ""),
    })
    # [memory.llm] or [llm]
    mem_llm = cfg.get("memory", {}).get("llm", {}) or cfg.get("llm", {})
    llm_api_key = mem_llm.get("api_key", "")
    candidates.append({
        "api_base": mem_llm.get("api_base", ""),
        "api_key": llm_api_key if llm_api_key != "local" else "",
        "model": mem_llm.get("model", ""),
    })

    for c in candidates:
        if not api_base and c.get("api_base"):
            api_base = c["api_base"]
        if not api_key and c.get("api_key"):
            api_key = c["api_key"]
        if not model and c.get("model"):
            model = c["model"]

    # Resolve api_key from credentials.toml if still missing
    if not api_key:
        creds = _load_config(Path.home() / ".watercooler" / "credentials.toml")
        provider = _detect_provider(api_base)
        if provider and provider in creds:
            api_key = creds[provider].get("api_key", "")

    # Fallback to provider-specific env vars
    if not api_key and api_base:
        provider = _detect_provider(api_base)
        env_map = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
        env_var = env_map.get(provider, "")
        if env_var:
            api_key = os.environ.get(env_var, "")

    return {
        "api_base": api_base or "http://localhost:8000/v1",
        "api_key": api_key or "",
        "model": model or "deepseek-chat",
    }


def _detect_provider(api_base: str) -> str:
    """Detect LLM provider from API base URL."""
    if "deepseek" in api_base:
        return "deepseek"
    if "openai" in api_base:
        return "openai"
    if "anthropic" in api_base:
        return "anthropic"
    return ""


def get_freshness_days() -> int:
    """Read pulse.freshness_days from config.toml (default 7)."""
    cfg = _load_config()
    try:
        return int(cfg.get("pulse", {}).get("freshness_days", 7))
    except (TypeError, ValueError):
        return 7


# ---------------------------------------------------------------------------
# Git helpers (#148: single subprocess for both branch and repo name)
# ---------------------------------------------------------------------------

def get_git_context(cwd: str) -> tuple[str, str]:
    """Return (branch, repo_name) from a single git subprocess call."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            branch = lines[0] if lines else "unknown"
            repo_name = Path(lines[1]).name if len(lines) > 1 else Path(cwd).name
            return (branch if branch != "HEAD" else "unknown", repo_name)
    except Exception:
        pass
    return ("unknown", Path(cwd).name)


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a session theme extractor for a software development project. "
    "Given a conversation compaction summary, extract structured metadata about the session. "
    "Output valid JSON only. No markdown, no explanation, no preamble."
)

USER_PROMPT_TEMPLATE = """Extract structured session metadata from the following compaction summary.

Rules:
- technical_focus: 1-5 short topic labels (kebab-case, e.g. "graphiti-edge-search", "pulse-hook-design")
- session_intent: One sentence describing the primary goal of the session
- observations: Array of typed cognitive events that occurred. Only include kinds that actually happened.
  Valid kinds: insight, decision, problem, risk, exploration, lesson, reasoning, stopgap, procedure, pr_merged, closure, resolved_loop, opened_loops, closed_loops
  A procedure is a multi-step repeatable sequence (workflow, checklist, recipe) — distinct from a lesson (single rule/takeaway)
  - pr_merged: A pull request was merged (not just opened or submitted). Includes hotfixes, patches, feature PRs. One observation per merge event.
  - closure: A Watercooler thread was formally closed (coordination lifecycle). Use for thread closure events regardless of reason. Note: captured for context but does NOT count toward momentum landed_count.
  - resolved_loop: A specific tracked loop, open question, or follow-up from a prior session was resolved or answered. Use per individual item. Do NOT use if you are also emitting closed_loops for the same batch.
  - opened_loops: New work items, follow-up tasks, or open questions were created that remain unresolved at session end.
  - closed_loops: Multiple tracked loops or tasks were closed in a single sweep (batch closure). Use ONLY when not emitting individual resolved_loop observations. Do NOT combine with resolved_loop for the same items.
  Each observation has "kind" (string) and "text" (one sentence, specific and concrete)
- confidence: 0.0-1.0 reflecting how clearly the summary conveys what happened
  (1.0 = very clear session with concrete outcomes; 0.3 = ambiguous/sparse summary)

Respond with exactly this JSON structure:
{{"technical_focus": ["topic-a", "topic-b"], "session_intent": "one sentence", "observations": [{{"kind": "insight", "text": "concrete specific observation"}}], "confidence": 0.85}}

Compaction summary:
---
$summary
---"""


def call_llm(summary: str, llm_config: dict) -> dict | None:
    """Call the configured LLM for structured extraction."""
    from string import Template
    url = f"{llm_config['api_base'].rstrip('/')}/chat/completions"
    user_content = Template(USER_PROMPT_TEMPLATE).safe_substitute(summary=summary)
    payload = {
        "model": llm_config["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 1024,
    }

    headers = {"Content-Type": "application/json"}
    if llm_config["api_key"]:
        headers["Authorization"] = f"Bearer {llm_config['api_key']}"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            # Strip markdown fences if present
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
            return json.loads(content)
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        print(f"LLM call failed: {e}", file=sys.stderr)
        return None


def validate_extraction(data: dict[str, Any]) -> bool:
    """Validate the extracted theme against the schema with length bounds."""
    focus = data.get("technical_focus")
    if not isinstance(focus, list) or not focus or len(focus) > 10:
        return False
    if any(not isinstance(f, str) or len(f) > 100 for f in focus):
        return False
    intent = data.get("session_intent")
    if not isinstance(intent, str) or not intent or len(intent) > 500:
        return False
    observations = data.get("observations")
    if not isinstance(observations, list) or len(observations) > 20:
        return False
    for obs in observations:
        if not isinstance(obs, dict):
            return False
        if obs.get("kind") not in VALID_KINDS:
            return False
        text = obs.get("text")
        if not isinstance(text, str) or not text or len(text) > 500:
            return False
    if not isinstance(data.get("confidence"), (int, float)):
        return False
    if not (0.0 <= data["confidence"] <= 1.0):
        return False
    return True


def _sanitize_observations(observations: list[dict]) -> list[dict]:
    """Normalize mutual-exclusivity violations in extracted observations.

    If both resolved_loop and closed_loops appear, drop all closed_loops and keep
    resolved_loop. This is a session-wide conservative rule: when mixed granularity
    appears in one payload, we intentionally prefer undercounting over the risk of
    double-counting the same underlying events. A session that genuinely has both a
    specific resolved loop and a separate batch closure will lose the batch signal —
    that is an acceptable trade-off.
    """
    kinds_present = {o.get("kind") for o in observations}
    if "resolved_loop" in kinds_present and "closed_loops" in kinds_present:
        _log("WARNING: resolved_loop + closed_loops both present — dropping closed_loops")
        return [o for o in observations if o.get("kind") != "closed_loops"]
    return observations


def extract_theme(summary: str, llm_config: dict[str, str]) -> dict[str, Any]:
    """Extract structured theme from compaction summary.

    Retry logic (#136): network failure (result is None) -> fallback immediately.
    Validation failure (result exists but invalid) -> retry once.
    """
    t0 = time.monotonic()
    result = call_llm(summary, llm_config)
    elapsed = time.monotonic() - t0
    _log(f"LLM call: {elapsed:.2f}s, result={'ok' if result else 'None'}")

    if result is None:
        # Network/parse failure — no point retrying immediately
        _log("LLM network failure, skipping retry")
    elif validate_extraction(result):
        result["observations"] = _sanitize_observations(result.get("observations", []))
        return result
    else:
        # Got a response but it failed validation — retry once
        _log("LLM response failed validation, retrying")
        t0 = time.monotonic()
        result = call_llm(summary, llm_config)
        elapsed = time.monotonic() - t0
        _log(f"LLM retry: {elapsed:.2f}s, result={'ok' if result else 'None'}")
        if result and validate_extraction(result):
            result["observations"] = _sanitize_observations(result.get("observations", []))
            return result

    # Fallback: minimal theme
    return {
        "technical_focus": ["unknown"],
        "session_intent": "Session theme extraction failed",
        "observations": [{"kind": "problem", "text": "extraction failed — raw summary deposited"}],
        "confidence": 0.1,
    }


# ---------------------------------------------------------------------------
# CLI resolution
# ---------------------------------------------------------------------------


def _git_toplevel(cwd: str) -> str | None:
    """Return the git repo root for cwd, or None if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _resolve_watercooler_cmd(cwd: str) -> list[str]:
    """Resolve the watercooler CLI command.

    Resolution order:
      1. Sibling of the running hook binary (sys.argv[0])
         Covers pipx, ~/.local/bin, uv tool, or any install where
         watercooler-capture-theme and watercooler share a bin directory.
      2. Project venv at {repo_root}/.venv/bin/watercooler
         (cwd may be a subdirectory; resolve repo root via git first)
      3. VIRTUAL_ENV env var
      4. System PATH via shutil.which()
      5. Raise FileNotFoundError (no silent fallback)

    The PostCompactHook runs outside the project venv, so sys.executable
    points to system Python (/usr/bin/python3), not the venv Python.
    Module invocation (python -m watercooler) will NOT work.

    This module is Linux/macOS only (imports fcntl).
    """
    bin_dir = "bin"
    cmd_name = "watercooler"

    # 1. Sibling of the running hook binary.
    # When Claude runs the stored absolute watercooler-capture-theme path,
    # the matching watercooler CLI is always installed in the same bin dir
    # (same venv, same pipx env, same ~/.local/bin, etc.).  Checking the
    # sibling first avoids any PATH / activation dependency and is the most
    # reliable signal we have about where the tools actually live.
    hook_path = Path(sys.argv[0]).resolve()
    sibling = hook_path.parent / cmd_name
    if sibling.is_file() and os.access(sibling, os.X_OK):
        _log(f"CLI resolved: sibling of hook ({sibling})")
        return [str(sibling)]

    # 2. Project venv (repo-scoped and deterministic)
    # cwd may be a subdirectory — resolve repo root first
    repo_root = _git_toplevel(cwd) or cwd
    venv_bin = Path(repo_root) / ".venv" / bin_dir / cmd_name
    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
        _log(f"CLI resolved: venv ({venv_bin})")
        return [str(venv_bin)]

    # 3. Activated env
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        candidate = Path(venv_env) / bin_dir / cmd_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            _log(f"CLI resolved: VIRTUAL_ENV ({candidate})")
            return [str(candidate)]

    # 4. System PATH
    system_bin = shutil.which(cmd_name)
    if system_bin:
        _log(f"CLI resolved: PATH ({system_bin})")
        return [system_bin]

    # 5. Explicit failure — no silent fallback
    msg = (
        f"Cannot find '{cmd_name}' in the hook's bin dir ({hook_path.parent}), "
        f"project venv at {Path(cwd) / '.venv'}, VIRTUAL_ENV, or PATH. "
        f"Run `uv sync` in {cwd} or install the CLI into an activated environment."
    )
    _log(f"CLI resolution FAILED: {msg}")
    raise FileNotFoundError(msg)


# ---------------------------------------------------------------------------
# Deposit and queue
# ---------------------------------------------------------------------------

def build_entry_body(
    theme: dict,
    *,
    author_id: str,
    repo_id: str,
    branch: str,
    session_id: str,
    summary_hash: str,
    captured_at: str,
) -> str:
    """Build the structured JSON entry body."""
    record = {
        "record_kind": "extracted_theme",
        "author_id": author_id,
        "repo_id": repo_id,
        "branch": branch,
        "session_id": session_id,
        "captured_at": captured_at,
        "summary_hash": f"sha256t16:{summary_hash}",
        "technical_focus": theme["technical_focus"],
        "session_intent": theme["session_intent"],
        "observations": theme["observations"],
        "confidence": theme["confidence"],
        "extractor_version": EXTRACTOR_VERSION,
    }
    return json.dumps(record, indent=2)


def deposit_entry(topic: str, title: str, body: str, cwd: str) -> bool:
    """Deposit via watercooler say CLI.

    Writes body to a temp file and passes @path to --body, because the CLI's
    read_body() tries Path(text).exists() which throws OSError on long strings.
    """
    try:
        cmd = _resolve_watercooler_cmd(cwd)
    except FileNotFoundError:
        return False  # already logged by _resolve_watercooler_cmd

    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="pulse_body_", suffix=".json")
        os.write(tmp_fd, body.encode("utf-8"))
        os.close(tmp_fd)
        tmp_fd = None

        result = subprocess.run(
            cmd + [
                "say", topic,
                "--title", title,
                "--body", f"@{tmp_path}",
                "--role", "scribe",
                "--type", "Note",
                "--agent", "Pulse Hook",
            ],
            capture_output=True, text=True, timeout=300, cwd=cwd,
        )
        if result.returncode == 0:
            _log(f"deposit OK: {topic} — {title}")
            return True
        _log(f"deposit FAIL: {result.stderr.strip()[:200]}")
        return False
    except subprocess.TimeoutExpired as e:
        stderr = ""
        if e.stderr:
            raw = e.stderr
            stderr = (raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw).strip()[:200]
        _log(f"deposit ERROR: timeout (300s) — {topic} {stderr}")
        return False
    except Exception as e:
        _log(f"deposit ERROR: {type(e).__name__} — {e}")
        return False
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def queue_theme(entry: dict) -> None:
    """Append a theme to the local queue for later retry."""
    if not _FCNTL_AVAILABLE:
        raise RuntimeError("queue_theme requires fcntl (Linux/macOS only)")
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_PATH, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # type: ignore[name-defined]
        try:
            f.write(json.dumps(entry) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)  # type: ignore[name-defined]
    print(f"Theme queued to {QUEUE_PATH}", file=sys.stderr)


_MAX_STDIN_BYTES = 4 * 1024 * 1024  # 4 MB hard cap on hook payload
_MAX_SUMMARY_CHARS = 32_000  # ~8K tokens; truncated before LLM dispatch
_MAX_TITLE_CHARS = 200


def drain_queue() -> None:
    """Retry all queued themes.

    Uses fcntl.flock on the queue file to prevent TOCTOU races with
    concurrent hook invocations appending via queue_theme().

    Note: the lock is held for the entire drain (~3s per entry). For a
    36-entry backlog this is ~2 min. Concurrent hooks will block on the
    lock, which is acceptable for this batch size.
    """
    if not _FCNTL_AVAILABLE:
        raise RuntimeError("drain_queue requires fcntl (Linux/macOS only)")
    if not QUEUE_PATH.exists():
        print("No queued themes.", file=sys.stderr)
        return

    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_PATH, "r+", encoding="utf-8") as qf:
        fcntl.flock(qf, fcntl.LOCK_EX)  # type: ignore[name-defined]
        try:
            entries = []
            for line in qf:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

            if not entries:
                print("Queue empty.", file=sys.stderr)
                return

            print(f"Draining {len(entries)} queued themes...", file=sys.stderr)
            remaining = []
            dropped = 0
            for entry in entries:
                cwd = entry.get("cwd", str(Path.cwd()))

                # Validate cwd is inside a git repo (cwd may be a subdirectory)
                if not Path(cwd).is_dir() or _git_toplevel(cwd) is None:
                    _log(f"drain: cwd not in a git repo: {cwd}, skipping")
                    remaining.append(entry)
                    continue

                # Validate topic format (drop invalid — don't requeue)
                topic = entry.get("topic", "")
                if not topic.startswith("session-context-"):
                    _log(f"drain: unexpected topic format: {topic}, dropping")
                    dropped += 1
                    continue

                # Apply title length bound (safe — display string, not structured data)
                # Body is structured JSON; truncation would corrupt it, so pass as-is
                title = str(entry.get("title", ""))[:_MAX_TITLE_CHARS]
                body = str(entry.get("body", ""))

                ok = deposit_entry(topic, title, body, cwd)
                if not ok:
                    remaining.append(entry)

            if remaining:
                qf.seek(0)
                qf.truncate()
                for entry in remaining:
                    qf.write(json.dumps(entry) + "\n")
            else:
                qf.seek(0)
                qf.truncate()

            deposited = len(entries) - len(remaining) - dropped
            parts = [f"{deposited} deposited"]
            if remaining:
                parts.append(f"{len(remaining)} still queued")
            if dropped:
                parts.append(f"{dropped} dropped (invalid)")
            print(f"{', '.join(parts)}.", file=sys.stderr)
        finally:
            fcntl.flock(qf, fcntl.LOCK_UN)  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _sanitize_contributor(name: str) -> str:
    """Sanitize contributor name for use in topic names and CLI args."""
    sanitized = re.sub(r"[^\w\-.]", "", name)
    return sanitized[:50] or "unknown"


def _sanitize_focus_label(focus_items: list[str], max_items: int = 3, max_total: int = 150) -> str:
    """Sanitize focus items for use in subprocess args and titles (#138)."""
    clean = []
    for item in focus_items[:max_items]:
        # Strip control chars and truncate each item
        sanitized = re.sub(r"[\x00-\x1f\x7f]", "", item)[:50]
        clean.append(sanitized)
    label = ", ".join(clean)
    return label[:max_total]


def main(argv: list[str] | None = None) -> None:
    """Entry point for the watercooler-capture-theme console script."""
    if not _FCNTL_AVAILABLE:
        print(
            "watercooler-capture-theme: fcntl is not available on this platform. "
            "This hook requires Linux or macOS.",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Project Pulse PostCompactHook")
    parser.add_argument("--drain-queue", action="store_true", help="Retry queued themes")
    args = parser.parse_args(argv)

    if args.drain_queue:
        drain_queue()
        return

    _log("hook invoked")

    # Read PostCompact payload from stdin (#134: parse once, log after)
    raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        _log(f"stdin exceeds {_MAX_STDIN_BYTES} byte limit, aborting")
        print("Payload too large — aborting capture.", file=sys.stderr)
        sys.exit(1)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        _log(f"stdin parse FAIL: {e}")
        print("No valid JSON on stdin — not running as a hook?", file=sys.stderr)
        sys.exit(1)
    _log(f"stdin bytes={len(raw)} keys={list(payload.keys())}")

    summary = payload.get("compact_summary", "")
    if len(summary) > _MAX_SUMMARY_CHARS:
        _log(f"compact_summary truncated from {len(summary)} to {_MAX_SUMMARY_CHARS} chars")
        summary = summary[:_MAX_SUMMARY_CHARS]
    if not summary:
        _log(f"no compact_summary — payload keys: {list(payload.keys())}")
        print("No compact_summary in payload — skipping.", file=sys.stderr)
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", str(Path.cwd()))
    if not Path(cwd).is_dir():
        _log(f"cwd not a directory: {cwd}, falling back to cwd")
        cwd = str(Path.cwd())

    # Resolve identity and context
    contributor = get_contributor_name()
    if not contributor:
        print("No contributor name configured in ~/.watercooler/config.toml", file=sys.stderr)
        sys.exit(1)

    branch, repo_id = get_git_context(cwd)
    captured_at = datetime.now(timezone.utc).isoformat()
    summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]

    # Extract theme via LLM
    llm_config = get_llm_config()
    theme = extract_theme(summary, llm_config)

    # Build entry (#138: sanitize focus_label)
    topic = f"session-context-{_sanitize_contributor(contributor)}"
    focus_label = _sanitize_focus_label(theme["technical_focus"])
    title = f"Session: {focus_label}"
    body = build_entry_body(
        theme,
        author_id=contributor,
        repo_id=repo_id,
        branch=branch,
        session_id=session_id,
        summary_hash=summary_hash,
        captured_at=captured_at,
    )

    # Deposit
    ok = deposit_entry(topic, title, body, cwd)
    if not ok:
        queue_theme({
            "topic": topic,
            "title": title,
            "body": body,
            "cwd": cwd,
            "summary_hash": summary_hash,
            "captured_at": captured_at,
        })
    else:
        print(f"Theme deposited to {topic}: {title}", file=sys.stderr)
        # Drain any backlog now that we know the system is healthy
        drain_queue()


if __name__ == "__main__":
    main()
