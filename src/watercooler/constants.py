"""Constants for the Watercooler Cloud protocol."""

from __future__ import annotations

# Role choices for agent participants
# These define what capacity an agent is acting in for a particular entry
ROLE_CHOICES = [
    "planner",      # Strategic planning, architecture decisions
    "critic",       # Code review, quality assurance, finding issues
    "implementer",  # Writing code, implementing features
    "tester",       # Test design, validation, QA
    "pm",           # Project management, coordination, prioritization
    "scribe",       # Documentation, recording decisions
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
