"""Constants for the Watercooler Cloud protocol."""

from __future__ import annotations

# Role names only. Behavioral definitions are in src/watercooler/data/roles.toml
# (project override: .watercooler/roles.toml).
ROLE_CHOICES = [
    "planner",
    "critic",
    "implementer",
    "tester",
    "pm",
    "scribe",
]

# Entry type choices
# These categorize the nature/purpose of an entry in a thread
ENTRY_TYPES = [
    "Note",      # General note, update, or comment
    "Plan",      # Planning document, proposal, roadmap
    "Decision",  # Binding decision or approval
    "PR",        # Pull request reference or review
    "Closure",   # Thread closure, resolution summary
]

# For backward compatibility, keep these as tuples too
ROLE_CHOICES_TUPLE = tuple(ROLE_CHOICES)
ENTRY_TYPES_TUPLE = tuple(ENTRY_TYPES)
