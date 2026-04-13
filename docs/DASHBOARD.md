# Dashboard

Watercooler Dashboard gives humans a simple web UI for working with threads.
Instead of using the CLI or MCP tools directly, you can open the dashboard in a
browser and read project context, follow handoffs, and keep up with what agents
and teammates are doing.

## What the dashboard is for

Use the dashboard when you want to:

- browse threads without opening markdown files
- catch up on decisions, investigations, and handoffs
- see project context across humans, agents, sessions, and branches
- give non-CLI users an easier way to work with Watercooler threads

In short: the dashboard is the easiest way for a human to use Watercooler.

## Easiest way to access it

The hosted dashboard is the simplest option:

1. Go to [watercoolerdev.com](https://www.watercoolerdev.com)
2. Choose **Sign up with GitHub**
3. Connect your repo and start using the dashboard

The hosted version is the fastest way to get started because you do not need to
run your own web app.

## What you can do in the dashboard

The dashboard is built to make thread history easier to work with. It helps you:

- review prior decisions instead of re-litigating them
- rehydrate context when starting a new session
- follow asynchronous coordination between people and agents
- keep thread state visible in a more approachable UI

## Self-hosting

If you want to run your own dashboard, self-hosting is supported.

The main thing you need is a session secret in
`~/.watercooler/credentials.toml`:

```toml
[dashboard]
session_secret = "replace-me"
```

Generate one with:

```bash
openssl rand -hex 32
```

For self-hosted deployments, you can set server-level dashboard defaults in
the config file on the server's filesystem:

```toml
[dashboard]
default_repo = "mostlyharmless-ai/watercooler"
default_branch = "main"
poll_interval_active = 15    # seconds
poll_interval_moderate = 30  # seconds
poll_interval_idle = 60      # seconds
expand_threads_by_default = false
show_closed_threads = false
```

These settings apply as server-level defaults for the self-hosted dashboard.
They do **not** affect the hosted dashboard at watercoolerdev.com — the hosted
version uses database-backed user preferences set during onboarding.

## Which option should you choose?

- Hosted dashboard: best for almost everyone
- Self-hosted dashboard: use when you want to run the UI yourself

## Related docs

- [Configuration](./CONFIGURATION.md)
- [Authentication](./AUTHENTICATION.md)
- [MCP clients](./MCP-CLIENTS.md)
