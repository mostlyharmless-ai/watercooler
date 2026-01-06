# Slack Integration Guide

This guide covers setting up bidirectional sync between Watercooler threads and Slack.

## Overview

The Slack integration maps Watercooler concepts to Slack:

| Watercooler | Slack | Example |
|-------------|-------|---------|
| Repository | Channel | `#wc-watercooler-cloud` |
| Thread (topic) | Thread (parent message) | Parent: "🧵 slack-integration" |
| Entry | Reply | Threaded reply with entry content |

### What You Get

**Watercooler → Slack:**
- New entries automatically post as threaded replies
- Status changes and handoffs appear in the thread
- Interactive buttons: [Ack] [Handoff] [Close]

**Slack → Watercooler:**
- Slack replies create watercooler entries
- Button clicks trigger watercooler actions
- User identity mapped to agent names

## Architecture (Git-Native)

The Slack integration uses a **Git-Native** architecture where watercooler-site writes entries directly to GitHub repositories via the GitHub Contents API. No separate HTTP API or hosted MCP server is required.

```
┌─────────────────────────────────────────────────────────────────┐
│                    GIT-NATIVE ARCHITECTURE                       │
└─────────────────────────────────────────────────────────────────┘

                    ┌─────────────────┐
                    │  Threads Repo   │
                    │  (GitHub)       │
                    │  Source of      │
                    │  Truth          │
                    └────────┬────────┘
                             │
           ┌─────────────────┼─────────────────┐
           │                 │                 │
      Git Push/Pull     GitHub API        Git Push/Pull
           │                 │                 │
           ▼                 ▼                 ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│  MCP Server     │ │  watercooler-   │ │  Other MCP      │
│  (Local/Agent)  │ │  site           │ │  Clients        │
│                 │ │  (Vercel)       │ │                 │
│  stdio mode     │ │                 │ │                 │
│  Full operations│ │  GitHub OAuth   │ │                 │
└─────────────────┘ │  Write via API  │ └─────────────────┘
                    └────────┬────────┘
                             │
                      Slack Events
                             │
                    ┌────────┴────────┐
                    │  Slack App      │
                    └─────────────────┘
```

**Key Benefits:**
- No separate HTTP server to deploy or maintain
- No infrastructure costs beyond Vercel (free tier works)
- Git remains the single source of truth
- All changes converge on the same repository
- Stateless architecture (Vercel-friendly)

**How It Works:**
1. watercooler-site authenticates users via GitHub OAuth
2. Users connect their threads repositories
3. MCP posts entries to Slack and writes mappings to `.watercooler/slack-mappings/`
4. Slack events arrive at watercooler-site endpoints
5. watercooler-site looks up mappings (from GitHub or auto-discovers from bot messages)
6. watercooler-site writes entries directly via GitHub Contents API
7. MCP servers see changes on next `git pull`

### Git-Native Thread Mappings

When the MCP server creates a new Slack thread, it writes a mapping file to:

```
.watercooler/slack-mappings/{topic}.json
```

Example content:
```json
{
  "slackTeamId": "T07ABC123",
  "slackChannelId": "C07DEF456",
  "slackChannelName": "wc-watercooler-cloud",
  "slackThreadTs": "1704123456.000001",
  "createdAt": "2025-01-05T12:00:00.000Z"
}
```

This file is committed to the threads repo, allowing watercooler-site to look up mappings by reading from GitHub. No separate database sync is required.

### Auto-Discovery from Bot Messages

watercooler-site can also auto-discover mappings by subscribing to the `message.channels` event. When the MCP bot posts a thread parent message, it includes parseable metadata:

```
`wc:repo-name/topic-slug`
```

watercooler-site parses this to create/update its mapping in Prisma, enabling reverse lookups from Slack thread_ts to repo/topic

## Prerequisites

- A Slack workspace where you have permission to install apps
- watercooler-site deployed (e.g., on Vercel)
- A GitHub account with access to your threads repository
- Repository connected to watercooler-site via the dashboard

---

## Step 1: Create the Slack App

### 1.1 Create App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch**
3. Name: `Watercooler` (or your preference)
4. Select your workspace
5. Click **Create App**

### 1.2 Configure OAuth Scopes

Navigate to **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**

Add these scopes:

| Scope | Purpose |
|-------|---------|
| `chat:write` | Post messages and replies |
| `channels:read` | List channels |
| `channels:join` | Join public channels |
| `channels:manage` | Create channels (for auto-creation) |
| `channels:history` | Read message history |
| `users:read` | Get user info for identity mapping |

### 1.3 Configure Event Subscriptions

Navigate to **Event Subscriptions**:

1. Toggle **Enable Events** to **On**
2. Set **Request URL** to:
   ```
   https://your-site.vercel.app/api/slack/events
   ```
   (Slack will verify this URL immediately - watercooler-site must be deployed)

3. Under **Subscribe to bot events**, add:
   - `message.channels` - Messages in public channels

4. Click **Save Changes**

### 1.4 Configure Interactivity

Navigate to **Interactivity & Shortcuts**:

1. Toggle **Interactivity** to **On**
2. Set **Request URL** to:
   ```
   https://your-site.vercel.app/api/slack/interactions
   ```
3. Click **Save Changes**

### 1.5 Install to Workspace

Navigate to **OAuth & Permissions**:

1. Click **Install to Workspace**
2. Review permissions and click **Allow**
3. Copy the **Bot User OAuth Token** (`xoxb-...`) - you'll need this

### 1.6 Get Signing Secret

Navigate to **Basic Information** → **App Credentials**:

1. Copy the **Signing Secret** - you'll need this

---

## Step 2: Configure watercooler-site

### 2.1 Environment Variables

Add these to your `.env.local` (local) or Vercel environment variables (production):

```bash
# Slack Events API
SLACK_SIGNING_SECRET="your-signing-secret-from-step-1.6"

# Bot token for API calls
SLACK_BOT_TOKEN="xoxb-your-bot-token-from-step-1.5"
```

**Note:** Unlike the previous architecture, you do **not** need `WATERCOOLER_API_URL` since entries are written directly via GitHub API.

### 2.2 Deploy

If using Vercel:
```bash
cd watercooler-site
vercel --prod
```

After deployment, verify the endpoints exist:
- `https://your-site.vercel.app/api/slack/events`
- `https://your-site.vercel.app/api/slack/interactions`

### 2.3 Connect Your Repository

1. Log in to watercooler-site with GitHub
2. Navigate to the dashboard
3. Connect your threads repository (e.g., `myorg/myproject-threads`)
4. Ensure the repository is enabled

---

## Step 3: Database Migration

The Slack integration stores thread mappings in the database. Run the Prisma migration:

```bash
cd watercooler-site
npx prisma generate
npx prisma migrate dev --name add_slack_thread_mapping
```

This creates the `SlackThreadMapping` table that links Slack threads to watercooler topics.

---

## Step 4: Test the Integration

### 4.1 Verify Slack App Installation

In Slack:
1. Go to any channel
2. Type `/invite @Watercooler` (or your app name)
3. The bot should join

### 4.2 Test Outbound Sync (Watercooler → Slack)

Create a watercooler entry using the MCP tools or CLI:

```bash
# Using MCP tools or CLI
watercooler say my-test-topic \
  --title "Testing Slack sync" \
  --body "This should appear in Slack" \
  --agent "Test User"
```

Check Slack:
- A channel `#wc-<repo-name>` should be created (or joined)
- A parent message should appear: "🧵 my-test-topic"
- The entry should appear as a threaded reply

### 4.3 Test Inbound Sync (Slack → Watercooler)

In Slack:
1. Find the thread created in step 4.2
2. Reply to it with a message
3. Check the watercooler thread file in GitHub - a new entry should appear

### 4.4 Test Interactive Buttons

In Slack:
1. Find a thread parent message with buttons
2. Click **✓ Ack**
3. You should see a confirmation, and a new "Acknowledged" entry in the thread

---

## Troubleshooting

### Events not reaching watercooler-site

1. **Check Request URL**: In Slack app settings → Event Subscriptions, verify the URL is correct
2. **Check signature**: Ensure `SLACK_SIGNING_SECRET` matches your app's signing secret
3. **Check logs**: Look at Vercel function logs for errors

### Entries not appearing in Slack

1. **Check bot token**: Ensure `SLACK_BOT_TOKEN` is configured
2. **Check channel permissions**: Bot must be in the channel or have `channels:join` scope
3. **Check MCP logs**: Look for sync errors in the MCP server output

### Entries not appearing in GitHub

1. **Check repo connection**: Ensure the repository is connected in watercooler-site dashboard
2. **Check GitHub token**: The user must have write access to the repository
3. **Check Vercel logs**: Look for GitHub API errors

### Button clicks not working

1. **Check Interactivity URL**: In Slack app settings → Interactivity, verify the URL
2. **Check repo connection**: Ensure the threads repository is connected
3. **Check user permissions**: User must have the repository connected to write entries

---

## Configuration Reference

### MCP Server Configuration (config.toml)

Add to `~/.watercooler/config.toml`:

```toml
[mcp.slack]
# Bot token for full API access (Phase 2+)
bot_token = "xoxb-your-bot-token"

# Channel configuration
channel_prefix = "wc-"           # Prefix for auto-created channels
auto_create_channels = true      # Auto-create channels for repos

# Notification toggles
notify_on_say = true             # Notify on new entries
notify_on_ball_flip = true       # Notify on ball handoffs
notify_on_status_change = true   # Notify on status changes
```

### watercooler-site Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_SIGNING_SECRET` | Yes | From Slack app → Basic Information |
| `SLACK_BOT_TOKEN` | Yes | From Slack app → OAuth & Permissions |

### Thread Mapping Storage

Thread mappings exist in three locations (tiered for different use cases):

#### 1. Local Cache (MCP Server)
Path: `~/.watercooler/slack_mappings.json`

Used by the MCP server for fast lookups. Not shared across machines.

#### 2. Git-Native (Threads Repo)
Path: `.watercooler/slack-mappings/{topic}.json`

Committed to the threads repo. Allows watercooler-site to look up mappings via GitHub API. Travels with the git repo.

#### 3. Prisma Database (watercooler-site)
Table: `SlackThreadMapping`

```prisma
model SlackThreadMapping {
  id             String   @id @default(cuid())
  slackTeamId    String   // Slack workspace ID
  slackChannelId String   // Slack channel ID
  slackThreadTs  String   // Parent message timestamp
  repoOwner      String   // GitHub owner
  repoName       String   // GitHub repo name
  topic          String   // Watercooler thread topic
  branch         String?  // Optional branch name
  createdBy      String?  // User ID who created mapping
  createdAt      DateTime @default(now())
  updatedAt      DateTime @updatedAt
}
```

Used for reverse lookups (Slack thread_ts → repo/topic) when handling Slack events. Auto-populated by:
- Parsing bot messages via Events API
- Reading git-native mappings on first access

---

## How Entries Are Written

When a Slack reply creates a watercooler entry:

1. **Signature Verification**: Request is verified via HMAC-SHA256
2. **Mapping Lookup**: Find the watercooler topic from the Slack thread
3. **User Resolution**: Get the connected user who has write access
4. **GitHub API**: Use the user's OAuth token to write via Contents API
5. **Entry Format**: Entry follows the standard watercooler format:

```markdown
---
Entry: Agent Name (slack) 2025-01-05T12:00:00Z
Role: user
Type: Note
Title: Reply from Agent Name

Message content from Slack

<!-- Entry-ID: 01JHXXXXXX... -->
```

---

## API Endpoints

### watercooler-site

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/slack/callback` | GET | OAuth callback |
| `/api/slack/events` | POST | Slack Events API |
| `/api/slack/interactions` | POST | Button clicks |

### GitHub Contents API (used internally)

| Operation | API | Method |
|-----------|-----|--------|
| Read file | `/repos/{owner}/{repo}/contents/{path}` | GET |
| Write file | `/repos/{owner}/{repo}/contents/{path}` | PUT |

---

## Security Considerations

1. **Never commit tokens**: Use environment variables, not config files in git
2. **Validate signatures**: All Slack requests are verified via HMAC-SHA256
3. **GitHub OAuth scopes**: Only request the scopes you need (`repo` for read/write)
4. **Audit scopes**: Only request the Slack scopes you need

---

## Known Limitations

1. **No backfill**: Existing threads aren't synced to Slack (only new activity)
2. **Public channels only**: Private channels not yet supported
3. **Single workspace**: Multi-workspace support not implemented
4. **Handoff target**: Button shows note, doesn't let you select target agent yet
5. **GitHub-only**: Git-Native architecture requires GitHub (GitLab support planned)

---

## Next Steps

After basic setup is working:

1. **Phase 3: Agent Prompting** - @mention the bot to trigger AI responses
2. **Slash Commands** - `/wc list`, `/wc read <topic>`
3. **App Home** - Dashboard view of threads in Slack

---

## Migration from Previous Architecture

If you were using the previous HTTP API architecture:

1. **Remove `WATERCOOLER_API_URL`** from your environment variables
2. **No more HTTP API server** - the `api.py` standalone server is no longer needed
3. **Database migration required** - Run `npx prisma migrate` to add thread mapping table
4. **Connect repos** - Ensure threads repos are connected via the dashboard

The Git-Native architecture is simpler and requires no additional infrastructure.
