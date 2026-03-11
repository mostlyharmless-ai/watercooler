"""
Shared dependency-reference patterns for fetch_issues.py and analyze_relationships.py.

Keeping them in one module prevents silent divergence where a bug fix in one file
fails to propagate to the other.

Each list contains independent patterns, each with exactly one capturing group (the issue number).
Iterate the list with re.finditer(pattern, text) and use match.group(1) — never
use a compiled alternation regex with m.groups() positional indexing, which breaks
silently when patterns are added, removed, or reordered.
"""

# In body of issue A: "blocked by #B" → A blocked_by B, B blocks A
BLOCKED_BY_PATTERNS: list[str] = [
  r"blocked?\s+by\s+#(\d+)",
  r"depends?\s+on\s+#(\d+)",
  r"requires?\s+#(\d+)\b",
  r"after\s+#(\d+)\s+(?:is\s+)?(?:merged|closed|done|fixed|resolved|landed)",
  r"needs?\s+#(\d+)\s+(?:to\s+be\s+)?(?:merged|closed|done|fixed|resolved)",
  r"prerequisite[:\s]+#(\d+)",
  r"follow[- ]?up\s+(?:to|from|on)\s+#(\d+)",
]

# In body of issue A: "blocks #B" → A blocks B
BLOCKS_PATTERNS: list[str] = [
  r"\bblocks?\s+#(\d+)",
  r"\bblocking\s+#(\d+)",
]

# In body of issue A: "fixes/closes/resolves #N" — used for conflict detection
FIXES_CLOSES_PATTERNS: list[str] = [
  r"(?:fixes|closes|resolves)\s+#(\d+)",
]
