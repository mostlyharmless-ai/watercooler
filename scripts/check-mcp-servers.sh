#!/bin/bash
# check-mcp-servers.sh
# Checks for stale watercooler MCP server processes and warns

set -euo pipefail

# Find all watercooler_mcp and fastmcp processes related to watercooler
PROCS=$(ps aux | grep -E "watercooler_mcp|fastmcp.*watercooler" | grep -v grep || true)

if [ -z "$PROCS" ]; then
    exit 0  # Silent success if no processes found
fi

# Count processes
COUNT=$(echo "$PROCS" | wc -l)

# Check if any are old (started more than 1 hour ago)
NOW=$(date +%s)
STALE=0

while IFS= read -r line; do
    PID=$(echo "$line" | awk '{print $2}')
    # Get process start time in epoch seconds
    START=$(ps -p "$PID" -o lstart= 2>/dev/null | xargs -I{} date -d "{}" +%s 2>/dev/null || echo "0")
    if [ "$START" != "0" ]; then
        AGE=$((NOW - START))
        # If older than 1 hour (3600 seconds)
        if [ "$AGE" -gt 3600 ]; then
            STALE=$((STALE + 1))
        fi
    fi
done <<< "$PROCS"

if [ "$STALE" -gt 0 ]; then
    echo "⚠️  WARNING: Found $STALE stale watercooler MCP server process(es) (>1 hour old)" >&2
    echo "$PROCS" | awk '{printf "   PID: %-7s Started: %-12s CMD: %s\n", $2, $9, substr($0, index($0,$11))}' >&2
    echo >&2
    echo "   Run './cleanup-mcp-servers.sh' to clean up" >&2
    exit 1
fi

exit 0
