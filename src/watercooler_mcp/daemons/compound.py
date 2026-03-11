"""Compound artifact generation daemon.

Reads CompoundConfig and dispatches artifact generation on thread closure.
Actual generation is a TODO stub pending full implementation.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def should_generate_compound_artifacts() -> bool:
  """Check if compound artifact generation is enabled in config.

  Returns:
      True if config.mcp.daemons.compound.enabled is True.
  """
  try:
    from watercooler.config_facade import config

    return config.full().mcp.daemons.compound.enabled
  except Exception as e:
    logger.debug("Could not load compound config: %s", e)
    return False


def generate_compound_artifacts(topic: str, threads_dir: Path) -> None:
  """Generate compound artifacts for a closed thread (stub).

  This is the runtime hook dispatched on thread closure when
  config.mcp.daemons.compound.enabled is True.

  Args:
      topic: The thread topic identifier.
      threads_dir: Path to threads directory.

  Note:
      TODO: Implement actual artifact generation (reports, learnings, suggestions).
      Tracked in GitHub issue #214.
  """
  if not should_generate_compound_artifacts():
    return
  logger.info(
    "Compound artifact generation enabled for topic '%s' "
    "(implementation pending — see issue #214)",
    topic,
  )
