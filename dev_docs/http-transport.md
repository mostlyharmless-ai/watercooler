# HTTP Transport for Watercooler MCP Server

The Watercooler MCP server supports both **stdio** (default) and **HTTP** transports. HTTP mode enables running the MCP server as a local HTTP daemon for clients that prefer HTTP over stdio.

## Why HTTP Transport?

- **Reliability:** Better error handling than stdio
- **Debugging:** Easy to monitor with standard HTTP tools
- **Logging:** Standard HTTP access logs

## Quick Start

### Using the Daemon Script

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

### Manual Start

```bash
# Set environment variables
export WATERCOOLER_MCP_TRANSPORT=http
export WATERCOOLER_MCP_HOST=127.0.0.1
export WATERCOOLER_MCP_PORT=8080

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
  "mode": "local",
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
| `WATERCOOLER_CACHE_BACKEND` | `memory` | Cache: `memory` or `database` |
| `WATERCOOLER_CACHE_TTL` | `300` | Default cache TTL in seconds |
| `WATERCOOLER_LOG_DIR` | `~/.watercooler` | Directory for logs and PID files |

See [ENVIRONMENT_VARS.md](ENVIRONMENT_VARS.md) for complete reference.

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

## Backward Compatibility

The default transport is `stdio` for backward compatibility. Existing configurations will continue to work without changes.

To switch to HTTP, either:
1. Set `WATERCOOLER_MCP_TRANSPORT=http` environment variable
2. Use the daemon script (automatically uses HTTP)
3. Update your client configuration to use the HTTP URL

## Technical Details

- **Protocol:** MCP over Server-Sent Events (SSE) / JSON-RPC
- **Endpoint:** `http://127.0.0.1:8080/mcp` (default)
- **Transport:** Streamable-HTTP via FastMCP
- **Server:** Uvicorn (via FastMCP/FastAPI)
- **Authentication:** Local git credentials

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

### Server Not Responding

```bash
# Check if server is running
./scripts/mcp-server-daemon.sh status

# Restart server
./scripts/mcp-server-daemon.sh restart

# Check logs for errors
./scripts/mcp-server-daemon.sh logs
```

## See Also

- [MCP Server Guide](mcp-server.md) - Complete MCP tool reference
- [ENVIRONMENT_VARS.md](ENVIRONMENT_VARS.md) - All configuration options
- [QUICKSTART.md](QUICKSTART.md) - Initial setup guide
