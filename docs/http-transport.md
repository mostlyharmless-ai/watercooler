# HTTP Transport for Watercooler MCP Server

The Watercooler MCP server supports both **stdio** (default) and **HTTP** transports. HTTP mode enables hosted deployments where web applications call MCP tools directly.

## Why HTTP Transport?

- **Hosted Deployments:** Run as a centralized service for web applications
- **Multi-User:** Authenticate requests per-user via token service
- **Reliability:** Better error handling than stdio
- **Debugging:** Easy to monitor with standard HTTP tools
- **Scalability:** Deploy on serverless platforms (Vercel, Railway, Fly.io)
- **Logging:** Standard HTTP access logs

## Architecture

```
STDIO Mode (Local):
  Claude Code → STDIO → MCP Server → Local Git → GitHub

HTTP Mode (Hosted):
  Dashboard/Slack → HTTP → MCP Server → Token Service → GitHub
                            ↓
                         Cache Layer
```

## Quick Start

### Option 1: Using the Daemon Script (Local Development)

```bash
# Start server
./scripts/mcp-server-daemon.sh start

# Check status
./scripts/mcp-server-daemon.sh status

# View logs
./scripts/mcp-server-daemon.sh logs

# Follow logs in real-time
./scripts/mcp-server-daemon.sh logs -f

# Stop server
./scripts/mcp-server-daemon.sh stop
```

### Option 2: Manual Start (Development)

```bash
# Set environment variables
export WATERCOOLER_MCP_TRANSPORT=http
export WATERCOOLER_MCP_HOST=127.0.0.1
export WATERCOOLER_MCP_PORT=8080

# Start server
python3 -m watercooler_mcp
```

### Option 3: Hosted Mode (Production)

```bash
# Required for multi-user hosted deployments
export WATERCOOLER_MCP_TRANSPORT=http
export WATERCOOLER_MCP_HOST=0.0.0.0
export WATERCOOLER_MCP_PORT=8080

# Enable token-based authentication
export WATERCOOLER_AUTH_MODE=hosted
export WATERCOOLER_TOKEN_API_URL=https://your-watercooler-site.com
export WATERCOOLER_TOKEN_API_KEY=your-secure-api-key

# Start server
python3 -m watercooler_mcp
```

## HTTP Server Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API information and available endpoints |
| `/health` | GET | Health check (returns auth mode, cache stats) |
| `/mcp` | POST | MCP protocol endpoint (JSON-RPC style) |
| `/docs` | GET | OpenAPI/Swagger documentation |

**Health Check Response:**
```json
{
  "status": "healthy",
  "mode": "hosted",
  "cache": {
    "backend": "memory",
    "total_entries": 42,
    "active_entries": 38
  }
}
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WATERCOOLER_MCP_TRANSPORT` | `stdio` | Transport type: `http` or `stdio` |
| `WATERCOOLER_MCP_HOST` | `127.0.0.1` | HTTP server host |
| `WATERCOOLER_MCP_PORT` | `8080` | HTTP server port |
| `WATERCOOLER_AUTH_MODE` | `local` | Authentication: `local` or `hosted` |
| `WATERCOOLER_TOKEN_API_URL` | - | Token service base URL (hosted mode) |
| `WATERCOOLER_TOKEN_API_KEY` | - | Token service API key (hosted mode) |
| `WATERCOOLER_CACHE_BACKEND` | `memory` | Cache: `memory` or `database` |
| `WATERCOOLER_CACHE_TTL` | `300` | Default cache TTL in seconds |
| `WATERCOOLER_CORS_ORIGINS` | `*` | Allowed CORS origins |
| `WATERCOOLER_LOG_DIR` | `~/.watercooler` | Directory for logs and PID files |

See [ENVIRONMENT_VARS.md](./ENVIRONMENT_VARS.md) for complete reference.

---

## Hosted Mode Architecture

Hosted mode enables multi-user deployments where each request is authenticated via the token service.

### Authentication Module (`auth.py`)

The auth module handles GitHub token resolution for multi-user deployments.

**Token Resolution Flow:**

1. Request arrives with `X-User-ID` header
2. MCP server calls token service: `GET /api/github/token?userId={user_id}`
3. Token service decrypts stored OAuth token and returns it
4. MCP server uses token for GitHub API calls
5. Tokens are cached in-memory for subsequent requests

**Usage in Code:**

```python
from watercooler_mcp.auth import get_github_token, is_hosted_mode, get_auth_headers

if is_hosted_mode():
    token_info = get_github_token(user_id="user_123")
    if token_info:
        print(f"GitHub user: {token_info.github_username}")
        # Use token for API calls
        headers = get_auth_headers(user_id="user_123")
```

**Token Cache Management:**

```python
from watercooler_mcp.auth import invalidate_user_token, clear_token_cache

# Invalidate a specific user's token (e.g., on 401 from GitHub)
invalidate_user_token("user_123")

# Clear all cached tokens (for testing/rotation)
clear_token_cache()
```

### Cache Module (`cache.py`)

The cache module reduces API calls and improves response times.

**Cache Backends:**

| Backend | Description | Use Case |
|---------|-------------|----------|
| `memory` | Thread-safe in-memory cache | Local dev, single-process |
| `database` | Remote cache via API | Serverless, multi-instance |

**Usage:**

```python
from watercooler_mcp.cache import cache, CacheKey

# Simple caching
data = cache.get("thread:my-topic")
if data is None:
    data = load_thread_data()
    cache.set("thread:my-topic", data, ttl=300)

# Structured cache keys
key = CacheKey(resource="thread", topic="my-topic", branch="main")
cache.set(str(key), data)

# Invalidate by pattern
cache.invalidate_pattern("thread:my-topic")

# Get cache stats
stats = cache.stats()
# {"backend": "memory", "total_entries": 42, "active_entries": 38}
```

### Request Context

HTTP requests include context headers:

| Header | Description |
|--------|-------------|
| `X-User-ID` | User identifier (required in hosted mode) |
| `X-Session-ID` | Session identifier (optional) |

Query parameters for context:
- `repo` - Repository context (e.g., `org/repo`)
- `branch` - Branch context (e.g., `main`)

---

## Token Service API Contract

The MCP server expects the token service (watercooler-site) to implement:

### GET /api/github/token

Fetches a GitHub OAuth token for a user.

**Request:**
```http
GET /api/github/token?userId={user_id}
Host: your-watercooler-site.com
x-api-key: your-api-key
Accept: application/json
```

**Response (Success - 200):**
```json
{
  "token": "gho_xxxxxxxxxxxx",
  "githubUsername": "user123",
  "scopes": "repo,read:org,read:user",
  "expiresAt": "2025-12-31T23:59:59Z"
}
```

**Response (User Not Found - 404):**
```json
{
  "error": "User not found"
}
```

**Response (Unauthorized - 401):**
```json
{
  "error": "Invalid API key"
}
```

---

## Deployment Examples

### Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install -e .[http]

ENV WATERCOOLER_MCP_TRANSPORT=http
ENV WATERCOOLER_MCP_HOST=0.0.0.0
ENV WATERCOOLER_MCP_PORT=8080

EXPOSE 8080
CMD ["python", "-m", "watercooler_mcp"]
```

### Docker Compose

```yaml
version: '3.8'
services:
  watercooler-mcp:
    build: .
    ports:
      - "8080:8080"
    environment:
      WATERCOOLER_MCP_TRANSPORT: http
      WATERCOOLER_AUTH_MODE: hosted
      WATERCOOLER_TOKEN_API_URL: https://your-site.com
      WATERCOOLER_TOKEN_API_KEY: ${MCP_API_KEY}
```

### Railway

```toml
# railway.toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "python -m watercooler_mcp"
healthcheckPath = "/health"
healthcheckTimeout = 10

[variables]
WATERCOOLER_MCP_TRANSPORT = "http"
WATERCOOLER_AUTH_MODE = "hosted"
```

### Vercel (Python Runtime)

Create `api/mcp.py`:

```python
from watercooler_mcp.server_http import app

# Vercel auto-discovers FastAPI/Starlette apps
```

---

## Client Integration

### TypeScript/JavaScript (watercooler-site)

```typescript
// lib/mcpClient.ts
const MCP_API_URL = process.env.MCP_API_URL;
const MCP_API_KEY = process.env.MCP_API_KEY;

export async function callMcpTool(
  userId: string,
  tool: string,
  params: Record<string, unknown>
) {
  const response = await fetch(`${MCP_API_URL}/mcp`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': MCP_API_KEY,
      'X-User-ID': userId,
    },
    body: JSON.stringify({
      method: 'tools/call',
      params: { name: tool, arguments: params },
    }),
  });
  return response.json();
}
```

### Python

```python
import httpx

async def call_mcp_tool(user_id: str, tool: str, params: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{MCP_API_URL}/mcp",
            headers={
                "x-api-key": MCP_API_KEY,
                "X-User-ID": user_id,
            },
            json={
                "method": "tools/call",
                "params": {"name": tool, "arguments": params},
            },
        )
        return response.json()
```

---

## Client Configuration (Local HTTP)

### Claude Code

Update `~/.config/claude/claude-code/mcp-settings.json`:

**Before (stdio):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "python3",
      "args": ["-m", "watercooler_mcp"],
      "env": {}
    }
  }
}
```

**After (HTTP):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "url": "http://127.0.0.1:8080/mcp",
      "transport": "sse"
    }
  }
}
```

### Cursor

Update `.cursor/mcp.json` with the same HTTP configuration.

---

## Security Considerations

### Production Checklist

- [ ] Set `WATERCOOLER_AUTH_MODE=hosted` for multi-user deployments
- [ ] Generate strong random API keys (32+ characters)
- [ ] Configure `WATERCOOLER_CORS_ORIGINS` to restrict allowed domains
- [ ] Use HTTPS for all production traffic
- [ ] Store API keys in environment variables, never in code
- [ ] Monitor logs for unauthorized access attempts

### CORS Configuration

```bash
# Production - restrict to your domains
export WATERCOOLER_CORS_ORIGINS="https://watercoolerdev.com,https://api.watercooler.dev"

# Development - allow all
export WATERCOOLER_CORS_ORIGINS="*"
```

---

## Troubleshooting

### Port Already in Use

```bash
# Use a different port
WATERCOOLER_MCP_PORT=8081 ./scripts/mcp-server-daemon.sh start
```

### Server Won't Start

Check the logs:
```bash
cat ~/.watercooler/mcp-server.log
```

Common issues:
- Python environment not activated
- Missing dependencies: `pip install -e ".[http]"`
- Port already in use
- Invalid environment variable values

### Token Service Connection Errors

Check:
- `WATERCOOLER_TOKEN_API_URL` is correct and reachable
- `WATERCOOLER_TOKEN_API_KEY` matches token service configuration
- Token service is running and healthy

```bash
# Test token service connectivity
curl -H "x-api-key: your-key" \
  "https://your-site.com/api/github/token?userId=test"
```

### CORS Errors in Browser

```bash
# Check current config
curl -I http://localhost:8080/

# Update allowed origins and restart
export WATERCOOLER_CORS_ORIGINS="http://localhost:3000"
./scripts/mcp-server-daemon.sh restart
```

### Server Not Responding

```bash
# Check if server is running
./scripts/mcp-server-daemon.sh status

# Restart server
./scripts/mcp-server-daemon.sh restart

# Check logs for errors
./scripts/mcp-server-daemon.sh logs
```

---

## Backward Compatibility

The default transport is `stdio` for backward compatibility. Existing configurations will continue to work without changes.

To switch to HTTP, either:
1. Set `WATERCOOLER_MCP_TRANSPORT=http` environment variable
2. Use the daemon script (automatically uses HTTP)
3. Update your client configuration to use the HTTP URL

---

## Technical Details

- **Protocol:** MCP over Server-Sent Events (SSE) / JSON-RPC
- **Endpoint:** `http://127.0.0.1:8080/mcp` (default)
- **Transport:** Streamable-HTTP via FastMCP
- **Server:** Uvicorn (via FastMCP/FastAPI)
- **Authentication:** Token-based (hosted mode) or local git credentials

---

## See Also

- [MCP Server Guide](./mcp-server.md) - Complete MCP tool reference
- [ENVIRONMENT_VARS.md](./ENVIRONMENT_VARS.md) - All configuration options
- [SETUP_AND_QUICKSTART.md](./SETUP_AND_QUICKSTART.md) - Initial setup guide
