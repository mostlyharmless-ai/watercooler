# Authentication

> **Choose your authentication method:**
>
> - **Start here (recommended):** Run `gh auth login && gh auth setup-git`. Sets up both
>   git and MCP authentication in one step.
> - Prefer an explicit token? Set `GITHUB_TOKEN` in your shell.
> - Headless or CI environment? Use a GitHub PAT stored in `credentials.toml`.
> - SSH-only setup? See [Method 4](#method-4-ssh-only) below.
>
> **Upgrading from an older version?** Legacy `credentials.json` files are auto-migrated
> to `credentials.toml` on first use — no manual conversion needed.

---

## Method 1: GitHub CLI (recommended)

The GitHub CLI handles token storage and git credential setup automatically.

**Install the GitHub CLI** (if not already installed):

```bash
# macOS
brew install gh

# Debian/Ubuntu
sudo apt install gh

# Windows
winget install GitHub.cli
```

**Authenticate:**

```bash
gh auth login
gh auth setup-git
```

- `gh auth login` — opens a browser to authorize with GitHub and stores your token
- `gh auth setup-git` — configures git to use gh CLI as a credential helper for HTTPS

**Verify:**

```bash
gh auth status
```

Look for `Logged in to github.com` and `repo` among the listed token scopes.

---

## Method 2: Environment variable

Set `GITHUB_TOKEN` in your shell. This is the standard GitHub token environment variable
and is read by watercooler automatically. Alternatively, `GH_TOKEN` works the same way.

> **Note:** `WATERCOOLER_GITHUB_TOKEN` is a separate env var used only by the
> `git-credential-watercooler` helper script. For the MCP server and CLI, use
> `GITHUB_TOKEN` or `GH_TOKEN`.

```bash
# Add to ~/.bashrc, ~/.zshrc, or equivalent
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

Reload your shell:

```bash
source ~/.bashrc   # or ~/.zshrc
```

**Verify:**

```bash
echo $GITHUB_TOKEN
```

For CI/CD environments (GitHub Actions, etc.), `GITHUB_TOKEN` is typically set
automatically by the runner — no manual configuration needed.

---

## Method 3: credentials.toml (headless or persistent)

For environments where you can't store tokens in a shell profile, or when you want
persistent credentials separate from your shell environment.

**Location:** `~/.watercooler/credentials.toml`

**Minimal template:**

```toml
# ~/.watercooler/credentials.toml
# Keep this file out of version control.

[github]
token = "ghp_xxxxxxxxxxxxxxxxxxxx"
```

The full credentials template is bundled with the package. To find it:

```bash
python -c "import watercooler; import pathlib; print(pathlib.Path(watercooler.__file__).parent / 'templates' / 'credentials.example.toml')"
```

> **Format note:** Credentials are stored in TOML format only (`credentials.toml`). No
> JSON format is supported for new installs.

**Verify:**

```bash
watercooler config show
```

Check that the output loads without errors and shows no missing-credential warnings.
To confirm the token works end-to-end, run `watercooler_health` from your MCP client
after completing setup.

---

## Method 4: SSH-only

Use SSH if HTTPS is unavailable or blocked in your environment.

**Generate an SSH key** (if you don't have one):

```bash
ssh-keygen -t ed25519 -C "your@email.com"
```

**Add the public key to GitHub:**

```bash
gh ssh-key add ~/.ssh/id_ed25519.pub --title "watercooler"
```

Or add it manually at [github.com/settings/keys](https://github.com/settings/keys).

**Configure git to use SSH for your repo:**

```bash
git remote set-url origin git@github.com:<org>/<repo>.git
```

**Threads use the same repo over SSH:**

Watercooler threads live on an orphan branch inside your code repo — not a separate
repository. Once your code repo's remote is set to SSH (above), thread git operations
automatically use SSH too. No additional configuration is required.

Note: SSH auth does not require `GITHUB_TOKEN` for git operations, but the MCP server
still needs a token for API calls. For headless setups without a GitHub CLI session,
pair SSH with a token in `credentials.toml` (see [Method 3](#method-3-credentialstoml-headless-or-persistent)).

---

## Verifying authentication

Run the health check from your MCP client immediately after setup:

```python
watercooler_health(code_path=".")
```

Or use the CLI:

```bash
watercooler config show
gh auth status
```

A healthy setup shows:
- `gh auth status` — `Logged in to github.com`
- `watercooler config show` — no missing-credential warnings

---

## Revoking or rotating tokens

**GitHub CLI tokens:** Log out and re-authenticate:

```bash
gh auth logout
gh auth login
gh auth setup-git
```

**Personal access tokens:** Revoke at
[github.com/settings/tokens](https://github.com/settings/tokens) and set a new value in
your shell profile or `credentials.toml`.

After rotating, restart your MCP client so the server picks up the new token.

---

## Hosted mode authentication

When using the hosted MCP control plane (HTTP transport), authentication works
differently from local mode. There are two auth paths, both supported on the
same `/mcp` endpoint:

### Agent API keys (Bearer auth)

Coding agents authenticate with a per-user API key. This is the primary auth
method for agents connecting directly to the hosted MCP.

**Setup:**

1. Log into the dashboard at your hosted URL
2. Go to **Settings → Security → Agent API Keys**
3. Click **Create Key**, name it (e.g., "Claude Code — my-project")
4. Copy the `wc_...` key (shown once)
5. Add to `~/.watercooler/credentials.toml`:

```toml
[hosted]
api_key = "wc_..."
```

All agents on the machine read this key automatically. No per-agent config needed.

**How it works:**

1. Agent sends `Authorization: Bearer wc_...` to the hosted MCP endpoint
2. MCP server calls the token service to resolve the key → user identity → GitHub token
3. MCP tools execute on behalf of that user

Bearer auth works independently of HMAC — no `WATERCOOLER_INTERNAL_SECRET` required.

### HMAC v2 (dashboard proxy auth)

The dashboard and Slack integration authenticate via HMAC-signed requests.
This is handled automatically by the proxy (`mcpClient.ts`) — users don't
configure this directly.

**How it works:**

1. Dashboard sends `X-User-ID`, `X-Request-Timestamp`, and `X-Request-Signature` headers
2. Signature covers the canonical string `"{user_id}\n{timestamp}\n{body_hex}"`
3. MCP server verifies the signature using `WATERCOOLER_INTERNAL_SECRET`
4. Requests older than `WATERCOOLER_HMAC_WINDOW` (default 300s) are rejected

HMAC v2 binds the signature to the claimed identity — substituting `X-User-ID`
invalidates the signature. This prevents user impersonation.

**Requirements:**

- `WATERCOOLER_INTERNAL_SECRET` must be set on both the MCP server (Railway) and
  the dashboard proxy (Vercel) with the same value
- Without it, non-Bearer `/mcp` requests are rejected with 503 in hosted mode

### Webhook authentication

GitHub push webhooks are authenticated with per-repo secrets. When a user connects
a repo in the dashboard, the webhook is created automatically on GitHub with a
per-repo secret. No manual webhook configuration is needed.

### Auth decision tree

The staged middleware pipeline evaluates auth in this order:

1. **Non-MCP path** (e.g., `/health`) → skip auth
2. **Bearer token present** → resolve API key → success or 401
3. **No Bearer, hosted mode, no INTERNAL_SECRET** → 503
4. **No Bearer, INTERNAL_SECRET set** → verify HMAC v2 → success or 401
5. **Hosted mode** → require user identity + GitHub token → 403 if not found
6. **Non-hosted** → pass through
