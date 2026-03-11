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
