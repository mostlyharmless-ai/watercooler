#!/bin/bash
# Watercooler MCP Server Daemon Management Script
#
# Manages the Watercooler MCP server as a background daemon.
# Supports start, stop, restart, and status commands.

set -e

# Configuration
LOGDIR="${WATERCOOLER_LOG_DIR:-$HOME/.watercooler}"
LOGFILE="$LOGDIR/mcp-server.log"
PIDFILE="$LOGDIR/mcp-server.pid"
HOST="${WATERCOOLER_MCP_HOST:-127.0.0.1}"
PORT="${WATERCOOLER_MCP_PORT:-3000}"

# Ensure log directory exists
mkdir -p "$LOGDIR"

start_server() {
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "MCP server already running (PID: $PID)"
            return 1
        else
            echo "Removing stale PID file"
            rm "$PIDFILE"
        fi
    fi

    echo "Starting Watercooler MCP Server..."

    # Export environment variables for HTTP transport
    export WATERCOOLER_MCP_TRANSPORT=http
    export WATERCOOLER_MCP_HOST="$HOST"
    export WATERCOOLER_MCP_PORT="$PORT"

    # Start server in background
    nohup python3 -m watercooler_mcp >> "$LOGFILE" 2>&1 &
    PID=$!
    echo $PID > "$PIDFILE"

    # Wait briefly and check if process is still running
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
        echo "✓ MCP server started successfully (PID: $PID)"
        echo "  MCP Endpoint: http://$HOST:$PORT/mcp"
        echo "  Transport: Streamable-HTTP (SSE)"
        echo "  Logs: $LOGFILE"
    else
        echo "✗ MCP server failed to start. Check logs: $LOGFILE"
        rm "$PIDFILE"
        return 1
    fi
}

stop_server() {
    if [ ! -f "$PIDFILE" ]; then
        echo "MCP server not running (no PID file)"
        return 1
    fi

    PID=$(cat "$PIDFILE")
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "MCP server not running (stale PID file)"
        rm "$PIDFILE"
        return 1
    fi

    echo "Stopping Watercooler MCP Server (PID: $PID)..."
    kill "$PID"

    # Wait for graceful shutdown (max 5 seconds)
    for i in {1..5}; do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "✓ MCP server stopped"
            rm "$PIDFILE"
            return 0
        fi
        sleep 1
    done

    # Force kill if still running
    echo "Forcing shutdown..."
    kill -9 "$PID" 2>/dev/null || true
    rm "$PIDFILE"
    echo "✓ MCP server stopped (forced)"
}

status_server() {
    if [ ! -f "$PIDFILE" ]; then
        echo "MCP server not running (no PID file)"
        return 1
    fi

    PID=$(cat "$PIDFILE")
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "MCP server not running (stale PID file)"
        rm "$PIDFILE"
        return 1
    fi

    echo "✓ MCP server running (PID: $PID)"
    echo "  MCP Endpoint: http://$HOST:$PORT/mcp"
    echo "  Transport: Streamable-HTTP (SSE)"
    echo ""
    echo "  Use watercooler_health tool via MCP protocol for health checks"

    return 0
}

restart_server() {
    echo "Restarting Watercooler MCP Server..."
    stop_server || true
    sleep 1
    start_server
}

logs_server() {
    if [ ! -f "$LOGFILE" ]; then
        echo "No log file found at $LOGFILE"
        return 1
    fi

    if [ "$1" = "-f" ] || [ "$1" = "--follow" ]; then
        tail -f "$LOGFILE"
    else
        tail -50 "$LOGFILE"
    fi
}

# Main command dispatch
case "${1:-}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        restart_server
        ;;
    status)
        status_server
        ;;
    logs)
        logs_server "${2:-}"
        ;;
    *)
        cat <<EOF
Usage: $0 {start|stop|restart|status|logs}

Commands:
  start    Start the MCP server in background
  stop     Stop the MCP server
  restart  Restart the MCP server
  status   Check if the MCP server is running
  logs     Show last 50 lines of logs (use -f to follow)

Environment Variables:
  WATERCOOLER_MCP_HOST   Server host (default: 127.0.0.1)
  WATERCOOLER_MCP_PORT   Server port (default: 3000)
  WATERCOOLER_LOG_DIR    Log directory (default: ~/.watercooler)

Examples:
  $0 start                  # Start server
  $0 status                 # Check status
  $0 logs                   # View recent logs
  $0 logs -f                # Follow logs in real-time
  $0 stop                   # Stop server

  WATERCOOLER_MCP_PORT=3001 $0 start   # Start on custom port
EOF
        exit 1
        ;;
esac
