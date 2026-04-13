"""Entry point for running watercooler MCP server via python -m watercooler_mcp"""

import sys

# Enforce runtime requirement early to avoid import-time errors on 3.9
if sys.version_info < (3, 10):
    print(
        f"Watercooler MCP requires Python 3.10+; found {sys.version.split()[0]}",
        file=sys.stderr,
    )
    sys.exit(1)

from .server import main

if __name__ == "__main__":
    main()
