#!/bin/bash
# cleanup-mcp-servers.sh
# Finds and optionally kills stale watercooler MCP server processes

set -euo pipefail

echo "Searching for watercooler MCP server processes..."
echo

# Find all watercooler_mcp and fastmcp processes related to watercooler
PROCS=$(ps aux | grep -E "watercooler_mcp|fastmcp.*watercooler" | grep -v grep || true)

if [ -z "$PROCS" ]; then
    echo "✓ No watercooler MCP server processes found"
    exit 0
fi

echo "Found watercooler MCP server processes:"
echo "----------------------------------------"
echo "$PROCS" | awk '{printf "PID: %-7s Started: %-12s User: %-10s CMD: %s\n", $2, $9, $1, substr($0, index($0,$11))}'
echo

# Count processes
COUNT=$(echo "$PROCS" | wc -l)
echo "Total: $COUNT process(es)"
echo

# Get PIDs
PIDS=$(echo "$PROCS" | awk '{print $2}')

# Ask for confirmation
read -p "Kill all these processes? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    for pid in $PIDS; do
        echo "Killing PID $pid..."
        kill "$pid" 2>/dev/null || echo "  (already gone or no permission)"
    done
    echo
    echo "✓ Cleanup complete"
else
    echo "Cleanup cancelled"
    exit 1
fi
