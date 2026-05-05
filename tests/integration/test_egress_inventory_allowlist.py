"""Egress-inventory allowlist baseline gate (Sprint 3 prep).

Runs the AST scanner against ``src/watercooler`` and
``src/watercooler_mcp``, loads ``egress_allowlist.txt`` (the
baselined permitted-set of Class P sites), and asserts that
every Class P site in the production scan is in the allowlist.

**Today's behaviour (post-#708):** any unauthorised Class P site
fails the test, period. There is no observe-mode warning path —
the gate is hard. The reason: a developer who adds a
``# egress-class: primary`` annotation MUST also baseline it in
``egress_allowlist.txt``, otherwise CI will refuse the change.
That coupling is intentional pre-strict-flip — the allowlist is
the audit trail for what's permitted.

**Sprint 4 work** will introduce a real observe-vs-strict mode
split gated on ``WATERCOOLER_EGRESS_INVENTORY_STRICT``, where
observe mode only warns about drift (so a CI run can complete
even with stale baselines, e.g. during a refactor) and strict
mode fails. That split needs production-side plumbing in
``evaluate_inventory`` (or a wrapper) to actually read the env
var, plus a logging integration. None of that exists yet — and
this file does NOT pretend to test mode-split behaviour. The
PR #708 round 2 reviewer correctly flagged the previous
"strict mode" placeholder as exercising no production code; it
has been removed. When Sprint 4 lands the real flag wiring,
this file gets a behavioural test for each mode.

Why a baseline file rather than asserting against a hard-coded
expected count: a count assertion would force every Class P
addition to also touch this test, which is friction. The
allowlist file is the natural place for "approved Class P sites";
the test reads it.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from watercooler_mcp.audit.egress_inventory import (
    AllowlistEntry,
    EgressSite,
    evaluate_inventory,
    parse_allowlist,
    scan_package,
)


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def repo_root() -> Path:
    here = Path(__file__).resolve()
    # tests/integration/ → tests/ → repo root
    return here.parent.parent.parent


@pytest.fixture(scope="module")
def production_sites(repo_root: Path) -> List[EgressSite]:
    """Run the scanner against both production packages once.

    Cached at module scope so repeated tests don't re-walk the tree.
    """
    sites: list[EgressSite] = []
    for pkg in ("watercooler", "watercooler_mcp"):
        src = repo_root / "src" / pkg
        if src.is_dir():
            sites.extend(scan_package(src))
    return sites


@pytest.fixture(scope="module")
def baseline_allowlist(repo_root: Path) -> List[AllowlistEntry]:
    """Load the committed allowlist file."""
    allowlist_path = (
        Path(__file__).parent / "egress_allowlist.txt"
    )
    return parse_allowlist(allowlist_path.read_text())


# ------------------------------------------------------------------ #
# Allowlist baseline shape
# ------------------------------------------------------------------ #


class TestAllowlistBaseline:
    def test_allowlist_file_parses(
        self, baseline_allowlist: List[AllowlistEntry]
    ) -> None:
        # Lower bound: at least the 2 Class P sites baselined in
        # PR #707 (fs.py:42 + pulse_report.py:757). The exact
        # count grows over time as new Class P sites are added.
        assert len(baseline_allowlist) >= 2

    def test_allowlist_paths_use_posix_separators(
        self, baseline_allowlist: List[AllowlistEntry]
    ) -> None:
        # ``parse_allowlist`` rejects backslash-form paths; this
        # is a safety check that no entry slipped past the parser.
        for entry in baseline_allowlist:
            assert "\\" not in entry.rel_path
            assert not entry.rel_path.startswith("/")

    def test_baseline_includes_known_class_p_sites(
        self, baseline_allowlist: List[AllowlistEntry]
    ) -> None:
        # Pin the two known sites from PR #707 with their EXACT
        # line numbers. ``evaluate_inventory`` keys on
        # ``(rel_path, site.line)``, so a Class P call that moves
        # to a different line silently invalidates its allowlist
        # entry. PR #708 round 1 HIGH: the previous version
        # checked only paths, which would let a line-shift slip
        # past this pinning test even though the production-scan
        # assertion downstream would have failed.
        entries = {(e.rel_path, e.line) for e in baseline_allowlist}
        assert ("src/watercooler/fs.py", 42) in entries, (
            f"fs.py:42 missing or moved; current entries: "
            f"{sorted(e for e in entries if e[0] == 'src/watercooler/fs.py')}"
        )
        assert (
            "src/watercooler_mcp/daemons/pulse_report.py",
            760,
        ) in entries, (
            f"pulse_report.py:760 missing or moved; current entries: "
            f"{sorted(e for e in entries if 'pulse_report' in e[0])}"
        )


# ------------------------------------------------------------------ #
# Production-scan gate (single mode today; Sprint 4 splits observe vs strict)
# ------------------------------------------------------------------ #


class TestProductionScanAgainstAllowlist:
    """Hard gate today: any unauthorised Class P site fails the
    test. This is intentional pre-Sprint-4 — the allowlist file
    is the contract for what's permitted, and a developer who
    annotates a new Class P site MUST also baseline it. Once
    Sprint 4 wires ``WATERCOOLER_EGRESS_INVENTORY_STRICT`` into
    ``evaluate_inventory`` (or a wrapper), this class will split
    into observe-mode (warn) and strict-mode (fail) tests.

    The PR #708 round 2 reviewer flagged the previous
    "strict-mode" placeholder as exercising no production code
    (the env var was a no-op because nothing reads it). Removed.
    No mode-split testing happens here until the flag is
    actually wired."""

    def test_class_p_sites_are_authorised(
        self,
        production_sites: List[EgressSite],
        baseline_allowlist: List[AllowlistEntry],
        repo_root: Path,
    ) -> None:
        """Every Class P site found in production code must be
        in the allowlist baseline. If this fails, the developer
        has either:

        1. Added a new ``# egress-class: primary`` annotation
           but forgot to update ``egress_allowlist.txt`` (most
           common — fix: add the new ``rel/path:line`` entry to
           the allowlist file).
        2. Moved an existing Class P call to a new line
           (fix: update the line number in the allowlist).
        3. Removed a Class P site (fix: remove the allowlist
           entry).
        """
        unauthorised = evaluate_inventory(
            production_sites,
            allowlist=baseline_allowlist,
            root=repo_root,
        )
        if unauthorised:
            details = "\n".join(
                f"  - {s.file.relative_to(repo_root).as_posix()}:{s.line} "
                f"({s.qualified_name})"
                for s in unauthorised
            )
            pytest.fail(
                "Unauthorised Class P egress sites found in production "
                "code (annotated as primary but not in "
                "tests/integration/egress_allowlist.txt):\n"
                f"{details}\n\n"
                "Add the offending entries to the allowlist file or "
                "remove the Class P annotation if the site should be "
                "Class D."
            )
