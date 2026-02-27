from __future__ import annotations

import re


def normalize_identifier(name: str) -> str:
    """Normalize a user-provided identifier for internal lookup.

    This is intentionally small and slightly ambiguous: it strips punctuation and
    lowercases the identifier. Some teams treat underscores as punctuation; some
    treat them as semantic separators.
    """
    return re.sub(r"[^a-zA-Z0-9]", "", name).lower()

