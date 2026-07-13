# Client plugin packages

Watercooler ships packaged plugins for supported coding agent clients. A plugin bundles
the 7 open-core skills (`recall`, `search-threads`, `threads`, `find-related`,
`watercooler-health`, `watercooler-onboarding`, `update-agent-context`) plus a generated
MCP server registration, so a single install brings both pieces instead of adding the
MCP server and dropping skill files separately.

This page covers prerequisites, first-run approval, and invocation — read it alongside
[QUICKSTART.md](./QUICKSTART.md) (manual MCP setup, no plugin) and
[MCP-CLIENTS.md](./MCP-CLIENTS.md) (per-client MCP config reference).

> **Availability.** Packaged plugin artifacts ship with the first packaged release.
> Until then, use the manual setup in [QUICKSTART.md](./QUICKSTART.md) — the MCP server
> and skills work the same way, just without a single-install plugin wrapper.

---

## Prerequisites

Regardless of client, the plugin's bundled MCP server runs via `uvx`. The plugin does
**not** install `uv` for you — install it once per machine:

```bash
# macOS / Linux — standalone installer (no Python required)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows — standalone installer
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Confirm `uvx` is on `PATH` (`uvx --version`) before installing a plugin — if it isn't,
the bundled MCP server will fail to start even though the plugin itself installed fine.

## Auth is separate from installing the plugin

Installing a plugin makes skills and the MCP server available locally. It does **not**
authenticate anything. GitHub-backed operations (pushing the `watercooler/threads`
orphan branch, dashboard sign-in) still need your own git/GitHub credentials — see
[AUTHENTICATION.md](./AUTHENTICATION.md). Expect to run `gh auth login` (or configure an
equivalent credential) separately from the plugin install.

---

## Claude Code

**What the plugin delivers:** the 7 skills under `skills/`, plus `.mcp.json` registering
the `watercooler` MCP server (so tool calls resolve as `mcp__watercooler__*`).

**Install channels.** Marketplace details are pending a product decision — the
mechanisms below are documented generically until the first packaged release ships
exact commands:

- **Marketplace add** — add the Watercooler-owned marketplace, then install the
  `watercooler` plugin from it. *Coming with the first packaged release.*
- **`--plugin-url` trial install** — load a release-asset `.zip` directly for a
  session, no marketplace required:

  ```bash
  claude --plugin-url https://<release-asset-url>/watercooler-claude-plugin.zip
  ```

  Zip archives passed to `--plugin-dir`/`--plugin-url` require **Claude Code
  ≥ v2.1.128**. *Asset URL coming with the first packaged release.*
- **Community marketplace** — Watercooler will also submit to the community
  marketplace for discoverability. *Coming with the first packaged release.*

**First-run MCP approval.** Claude Code prompts to trust a newly added MCP server the
first time it's used — approve the `watercooler` server when prompted. If the server
doesn't start, confirm `uvx` is on `PATH` (see Prerequisites above) and see
[TROUBLESHOOTING.md](./TROUBLESHOOTING.md#server-not-loading).

**Invocation.** Installed-plugin skills invoke with the plugin namespace:

```text
/watercooler:recall
/watercooler:search-threads
/watercooler:threads
/watercooler:find-related
/watercooler:watercooler-health
/watercooler:watercooler-onboarding
/watercooler:update-agent-context
```

This is expected Claude Code behavior for any *installed plugin* — skill names are
namespaced under the plugin's `plugin.json` name (`watercooler`). Bare `/recall`-style
invocation only applies to a **standalone** project or user skill install (skill files
copied directly into `.claude/skills/` outside of a plugin), not to plugin-delivered
skills. If you're used to bare `/recall` from a standalone setup, switch to the
`/watercooler:` prefix once you install via the plugin.

---

## Codex

**What the plugin delivers:** the same 7 skills under `skills/`, plus a generated
`.mcp.json` registering the `watercooler` MCP server, packaged as a Codex plugin
(`.codex-plugin/plugin.json`).

**Install channel.** Codex plugins distribute through a repo/team marketplace catalog
(`.agents/plugins/marketplace.json`), not ad hoc copying. *Marketplace entry and exact
install command are coming with the first packaged release.*

**First-run MCP approval — enable the server explicitly.** Unlike Claude Code's
zero-touch `.mcp.json` pickup, a Codex plugin's bundled MCP server is **not**
auto-registered on install. You must enable it in your Codex config:

```toml
[plugins.watercooler.mcp_servers.watercooler]
enabled = true
```

(Exact config key names and location depend on your Codex config file — confirm the
install with `codex plugin list --json` and see Codex's plugin configuration docs for
the precise key path.) Skills are available as soon as the plugin installs; the MCP
server only comes online after this config step.

**Invocation.** Codex has no slash-namespace equivalent to Claude's
`/watercooler:<skill>`. Skills invoke as:

```text
$recall
$search-threads
$threads
$find-related
$watercooler-health
$watercooler-onboarding
$update-agent-context
```

Type `$<skill-name>` to invoke explicitly, or run `/skills` to browse and pick one.
Codex can also auto-invoke a skill implicitly when your prompt matches its description
(`allow_implicit_invocation`, on by default). Do not expect or document a
`/watercooler:<skill>`-style command for Codex — that syntax is Claude Code-specific.

---

## OpenCode

The rendered skill files are format-compatible with OpenCode's native `SKILL.md`
discovery, but OpenCode does not scan the plugin package's own `skills/` directory. To
use them, copy the `skills/<name>` directories into one of OpenCode's scanned paths —
`.opencode/skills/`, `.agents/skills/`, or `.claude/skills/` in your project, or their
user-level equivalents (`~/.config/opencode/skills/`, `~/.agents/skills/`,
`~/.claude/skills/`). OpenCode has no plugin-manifest or marketplace concept for
skills; skills are found by filesystem presence only. MCP server registration is a
small manual addition to your
`opencode.json`'s `mcp` block, using the same `uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main[local] watercooler-mcp`
invocation as manual setup.

Full OpenCode packaging is future work — this section is a forward-pointer, not a v1
deliverable.

---

## Related docs

- [QUICKSTART.md](./QUICKSTART.md) — manual MCP server setup (works today, no plugin
  required).
- [MCP-CLIENTS.md](./MCP-CLIENTS.md) — per-client MCP configuration reference.
- [AUTHENTICATION.md](./AUTHENTICATION.md) — GitHub auth options for thread push/pull.
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) — first-connect failures, stale installs.
