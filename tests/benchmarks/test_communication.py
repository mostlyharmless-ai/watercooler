"""Category 1: Communication Quality Benchmark Tests.

Structural protocol tests that measure watercooler entry properties:
type diversity, role coverage, file path specificity, and coordination
failure detection. All tests are deterministic -- no live agents needed.
"""
from __future__ import annotations

import math
import re
from collections import Counter

import pytest


@pytest.mark.benchmark
def test_protocol_structure(structured_thread):
    """Structured entries have type diversity and explicit roles."""
    entries = structured_thread["entries"]

    types = Counter(e["entry_type"] for e in entries)
    roles = Counter(e["role"] for e in entries)

    # Shannon entropy of type distribution (higher = more diverse)
    total = sum(types.values())
    type_entropy = -sum(
        (c / total) * math.log2(c / total) for c in types.values()
    )
    assert type_entropy >= 1.0, (
        f"Low type diversity: entropy={type_entropy:.2f}, types={dict(types)}"
    )
    assert len(roles) >= 2, f"Only {len(roles)} role(s) used: {dict(roles)}"


@pytest.mark.benchmark
def test_entry_specificity(all_entries):
    """Entries with code references score higher specificity."""
    FILE_RE = re.compile(r"[\w/]+\.\w{1,5}")  # file.ext pattern
    LINE_RE = re.compile(r":\d+")              # :42 pattern

    specific_count = 0
    for entry in all_entries:
        body = entry.get("body", "")
        has_files = bool(FILE_RE.search(body))
        has_lines = bool(LINE_RE.search(body))
        if has_files or has_lines:
            specific_count += 1

    ratio = specific_count / max(len(all_entries), 1)
    assert ratio >= 0.3, (
        f"Only {ratio:.0%} of entries have code references "
        f"({specific_count}/{len(all_entries)})"
    )


@pytest.mark.benchmark
def test_coordination_failures(structured_thread):
    """Detect coordination anti-patterns in thread protocol."""
    entries = sorted(structured_thread["entries"], key=lambda e: e["index"])
    failures = []

    # F1: Ball confusion -- agent switch without Ack/Handoff
    for i in range(len(entries) - 2):
        e0, e1, e2 = entries[i], entries[i + 1], entries[i + 2]
        if (
            e0["agent"] != e1["agent"]
            and e1["entry_type"] not in ("Ack", "Handoff")
            and e2["entry_type"] not in ("Ack", "Handoff")
        ):
            failures.append("ball_confusion")

    # F3: Unacked decisions
    for i, entry in enumerate(entries):
        if entry["entry_type"] == "Decision":
            subsequent = entries[i + 1:]
            has_ack = any(
                e["entry_type"] in ("Ack", "Note")
                and e["agent"] != entry["agent"]
                for e in subsequent
            )
            if not has_ack:
                failures.append("orphaned_decision")

    # Healthy threads should have few coordination failures
    failure_rate = len(failures) / max(len(entries), 1)
    assert failure_rate <= 0.3, (
        f"High coordination failure rate: {failure_rate:.2f} "
        f"({len(failures)} failures in {len(entries)} entries: {failures})"
    )
