# SPDX-License-Identifier: Apache-2.0
"""Watercooler: MCP server with git-backed shared memory for agentic coding teams."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("watercooler")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"  # Fallback for editable installs without metadata

from .lock import AdvisoryLock  # noqa: F401
from .fs import read, write, thread_path  # noqa: F401

__all__ = [
    "AdvisoryLock",
    "read",
    "write",
    "thread_path",
    "__version__",
]

