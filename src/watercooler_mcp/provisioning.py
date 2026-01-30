"""Provisioning helpers for watercooler threads repositories.

This module centralises the logic for auto-provisioning of the
`<repo>-threads` repositories when they do not yet exist on the remote.

The workflow:

* Enabled by default – set `WATERCOOLER_THREADS_AUTO_PROVISION=0` to disable.
* Requires an explicit provisioning command via
  `WATERCOOLER_THREADS_CREATE_CMD`. This allows organisations to plug in the
  mechanism they trust (`gh repo create`, an internal API wrapper, etc.).
* The command string is formatted with a small context dictionary so callers
  can reference repo-specific values without fragile shell parsing.

When invoked, the provisioning command is executed with ``shell=True`` to
support complex one-liners. All stdout/stderr is captured and returned so the
caller can surface it in error messages or logs.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Dict

from watercooler.config_facade import config

AUTO_PROVISION_ENV = "WATERCOOLER_THREADS_AUTO_PROVISION"
PROVISION_CMD_ENV = "WATERCOOLER_THREADS_CREATE_CMD"

# Default provisioning command using GitHub CLI
DEFAULT_PROVISION_CMD = "gh repo create {slug} --private --disable-wiki --disable-issues"


class ProvisioningError(Exception):
    """Raised when auto-provisioning cannot be completed."""


@dataclass(frozen=True)
class ProvisioningContext:
    """Formatted values available to provisioning command templates."""

    slug: str
    repo_url: str
    code_repo: str
    namespace: str
    repo: str
    org: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "slug": self.slug,
            "repo_url": self.repo_url,
            "code_repo": self.code_repo,
            "namespace": self.namespace,
            "repo": self.repo,
            "org": self.org,
        }

    def as_shell_safe_dict(self) -> Dict[str, str]:
        """Return context values quoted for safe shell interpolation.

        All values are passed through shlex.quote() to prevent shell injection
        when used in command templates executed with shell=True.
        """
        return {k: shlex.quote(v) for k, v in self.as_dict().items()}


def _split_slug(slug: str) -> tuple[str, str]:
    slug = slug.strip("/")
    if not slug:
        return ("", "")
    parts = slug.split("/")
    if len(parts) == 1:
        return ("", parts[0])
    namespace = "/".join(parts[:-1])
    return (namespace, parts[-1])


def is_auto_provision_requested() -> bool:
    """Return True if auto-provisioning is enabled (default: enabled).

    Auto-provisioning is enabled by default. Set WATERCOOLER_THREADS_AUTO_PROVISION=0
    to disable it.
    """
    return config.env.get_bool(AUTO_PROVISION_ENV, True)


def _build_context(repo_url: str, slug: str | None, code_repo: str | None) -> ProvisioningContext:
    if not slug:
        raise ProvisioningError("Cannot auto-provision threads repo without a slug")
    namespace, repo = _split_slug(slug)
    # Fall back to the repo component for org if namespace unavailable
    if namespace:
        org = namespace.split("/", 1)[0]
    else:
        org = repo
    return ProvisioningContext(
        slug=slug,
        repo_url=repo_url,
        code_repo=code_repo or "",
        namespace=namespace,
        repo=repo,
        org=org,
    )


def _is_https_github_url(repo_url: str) -> bool:
    """Check if repo_url is an HTTPS GitHub URL."""
    return repo_url.startswith("https://github.com/")


def _provision_via_github_api(
    repo_url: str,
    slug: str | None,
    code_repo: str | None,
    env: dict[str, str] | None = None,
) -> str:
    """Provision repository via GitHub API for HTTPS URLs.

    This is used as a fallback when gh CLI isn't available or for programmatic access.
    Requires GITHUB_TOKEN environment variable.
    """
    try:
        import urllib.request
        import json
    except ImportError as e:
        raise ProvisioningError(f"Cannot use GitHub API provisioning: {e}") from e

    # Extract org/repo from URL
    # https://github.com/org/repo-threads.git -> org/repo-threads
    match = re.match(r'https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$', repo_url)
    if not match:
        raise ProvisioningError(f"Invalid GitHub HTTPS URL: {repo_url}")

    org, repo_name = match.groups()

    # Get token from environment
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    token = merged_env.get('GITHUB_TOKEN') or merged_env.get('GH_TOKEN')
    if not token:
        raise ProvisioningError(
            "GitHub API provisioning requires GITHUB_TOKEN or GH_TOKEN environment variable"
        )

    # Create repository via API
    api_url = f'https://api.github.com/orgs/{org}/repos'
    request_data = json.dumps({
        'name': repo_name,
        'private': True,
        'description': f'Watercooler threads for {code_repo or "project"}',
        'auto_init': False,  # Don't create initial commit
        'has_issues': False,
        'has_wiki': False,
    }).encode('utf-8')

    request = urllib.request.Request(
        api_url,
        data=request_data,
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
        },
        method='POST'
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            return f"Created repository via GitHub API: {result['html_url']}"
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        try:
            error_data = json.loads(error_body)
            error_message = error_data.get('message', error_body)
        except json.JSONDecodeError:
            error_message = error_body

        if e.code == 422 and 'already exists' in error_message.lower():
            # Repository already exists - this is OK
            return f"Repository already exists: {repo_url}"

        raise ProvisioningError(
            f"GitHub API request failed (HTTP {e.code}): {error_message}"
        ) from e
    except Exception as e:
        raise ProvisioningError(f"Failed to create repository via GitHub API: {e}") from e


def provision_threads_repo(
    repo_url: str,
    slug: str | None,
    code_repo: str | None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Execute the configured provisioning command.

    For HTTPS GitHub URLs, attempts to use GitHub API first (if GITHUB_TOKEN is available),
    then falls back to the configured CLI command.

    Args:
        repo_url: Full git URL for the threads repo.
        slug: The namespace/repo slug (e.g., `org/watercooler-dashboard-threads`).
        code_repo: Optional name of the paired code repository.
        env: Optional environment overrides to merge into the subprocess env.

    Returns:
        Combined stdout/stderr from the provisioning command for logging.

    Raises:
        ProvisioningError: If provisioning is requested but misconfigured or the
        command fails.
    """

    # For HTTPS GitHub URLs, try GitHub API first if token is available
    if _is_https_github_url(repo_url):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        token = merged_env.get('GITHUB_TOKEN') or merged_env.get('GH_TOKEN')
        if token:
            try:
                return _provision_via_github_api(repo_url, slug, code_repo, env)
            except ProvisioningError as e:
                # If GitHub API fails, fall back to CLI command
                # (user might have gh CLI configured instead)
                if "GITHUB_TOKEN" in str(e):
                    # Don't fall back if the issue is missing token
                    raise
                # Otherwise continue to try CLI command below
                pass

    # Fall back to CLI command (works for SSH and HTTPS if gh CLI is configured)
    template = config.env.get(PROVISION_CMD_ENV, DEFAULT_PROVISION_CMD)
    if not template:
        raise ProvisioningError(
            "Auto-provisioning requested but WATERCOOLER_THREADS_CREATE_CMD is not set"
        )

    ctx = _build_context(repo_url, slug, code_repo)
    try:
        # Use shell-safe quoting to prevent command injection
        command = template.format(**ctx.as_shell_safe_dict())
    except KeyError as exc:  # pragma: no cover - defensive guard
        raise ProvisioningError(
            f"Unknown placeholder {{{exc.args[0]}}} in WATERCOOLER_THREADS_CREATE_CMD"
        ) from exc

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
            env=merged_env,
        )
    except subprocess.CalledProcessError as exc:
        output = "\n".join(filter(None, [exc.stdout or "", exc.stderr or ""]))
        raise ProvisioningError(
            f"Provisioning command failed with exit code {exc.returncode}: {output.strip()}"
        ) from exc

    combined_output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
    return combined_output.strip()
