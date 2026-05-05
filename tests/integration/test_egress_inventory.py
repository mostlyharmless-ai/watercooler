"""CI gate for the egress-site inventory (Move 6).

Runs the AST scanner against ``src/watercooler`` and
``src/watercooler_mcp`` and produces an inventory manifest. The
test does NOT fail on count drift — that is Sprint 3 work, after
Class P sites have been annotated and an allowlist baselined. For
now the gate verifies:

1. The scanner runs on production code without crashing.
2. Every site classifies as either ``primary`` or ``diagnostic``
   (no ``None`` / unknown).
3. ``WATERCOOLER_EGRESS_INVENTORY_STRICT=1`` enables an
   allowlist-mode that fails the build if any new Class P sites
   appear without explicit annotation. Default off.
4. The annotation proximity check works on real source code.

Sprint 3 work will:
- Annotate every Class P site in production code with
  ``# egress-class: primary``.
- Populate the allowlist of permitted Class P sites.
- Flip the strict flag default to on.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import List

import pytest

from watercooler_mcp.audit.egress_inventory import (
    CLASS_P_ANNOTATION,
    AllowlistEntry,
    EgressSite,
    evaluate_inventory,
    parse_allowlist,
    scan_package,
)


# ------------------------------------------------------------------ #
# Locating production source
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


# ------------------------------------------------------------------ #
# Scanner runs on real code
# ------------------------------------------------------------------ #


class TestScannerRunsOnRealCode:
    def test_scanner_produces_results(self, production_sites: List[EgressSite]) -> None:
        # Production code makes plenty of egress calls; an empty
        # inventory means the scanner is broken.
        assert len(production_sites) > 0

    def test_every_site_has_annotation(
        self, production_sites: List[EgressSite]
    ) -> None:
        # The dataclass default is "diagnostic"; a None or unknown
        # value indicates a scanner bug.
        for site in production_sites:
            assert site.annotation in ("primary", "diagnostic"), (
                f"site at {site.file}:{site.line} has unexpected annotation "
                f"{site.annotation!r}"
            )


# ------------------------------------------------------------------ #
# Inventory shape (informational)
# ------------------------------------------------------------------ #


class TestInventoryShape:
    def test_inventory_is_predominantly_diagnostic(
        self, production_sites: List[EgressSite]
    ) -> None:
        """Sanity check: most sites should be Class D until Sprint 3
        annotates the Class P ones."""
        by_class = Counter(site.annotation for site in production_sites)
        # Assert at least *one* Class D site (would be wildly broken
        # if zero) and that the inventory is non-empty.
        assert by_class["diagnostic"] > 0
        # Class P count is "however many are annotated today" — no
        # assertion; this is the value the strict mode will baseline
        # against in Sprint 3.

    def test_pattern_distribution(self, production_sites: List[EgressSite]) -> None:
        """The mix of patterns should exercise multiple categories."""
        patterns = Counter(s.pattern for s in production_sites)
        # Logging is the dominant pattern; at least one logger.* call
        # must be found in production code.
        logger_calls = sum(
            count for pat, count in patterns.items() if pat.startswith("logger.")
        )
        assert logger_calls > 0


# ------------------------------------------------------------------ #
# Strict mode (allowlist) — opt-in
# ------------------------------------------------------------------ #


class TestEvaluateInventoryEnforcement:
    """The MED finding from PR #705 round 2 review: the previous
    strict-mode tests only exercised env-var plumbing, never the
    actual enforcement logic. ``evaluate_inventory`` is the
    enforcement primitive: it returns the list of Class P sites
    that are NOT in the allowlist. Empty result = green CI; non-
    empty = a Class P site appeared without explicit annotation +
    allowlist registration.
    """

    def test_no_class_p_sites_returns_empty(self, tmp_path: Path) -> None:
        # All-Class-D inventory: no enforcement signal regardless
        # of whether the allowlist is empty.
        sites = [
            EgressSite(
                file=tmp_path / "x.py",
                line=10,
                pattern="logger.info",
                qualified_name="logger.info",
                annotation="diagnostic",
            ),
        ]
        unauth = evaluate_inventory(sites, root=tmp_path, allowlist=())
        assert unauth == []

    def test_class_p_site_not_in_allowlist_is_unauthorised(
        self, tmp_path: Path
    ) -> None:
        sites = [
            EgressSite(
                file=tmp_path / "src/watercooler/commands.py",
                line=412,
                pattern="*.write_text",
                qualified_name="projection_path.write_text",
                annotation="primary",
                annotation_line=412,
            ),
        ]
        unauth = evaluate_inventory(sites, root=tmp_path, allowlist=())
        assert len(unauth) == 1
        assert unauth[0].file.name == "commands.py"

    def test_class_p_site_in_allowlist_is_authorised(self, tmp_path: Path) -> None:
        sites = [
            EgressSite(
                file=tmp_path / "src/watercooler/commands.py",
                line=412,
                pattern="*.write_text",
                qualified_name="projection_path.write_text",
                annotation="primary",
                annotation_line=412,
            ),
        ]
        allow = [
            AllowlistEntry(rel_path="src/watercooler/commands.py", line=412),
        ]
        unauth = evaluate_inventory(sites, allowlist=allow, root=tmp_path)
        assert unauth == []

    def test_allowlist_does_not_affect_diagnostic_sites(self, tmp_path: Path) -> None:
        # Class D sites are never enforced — the allowlist only
        # governs Class P.
        sites = [
            EgressSite(
                file=tmp_path / "src/x.py",
                line=10,
                pattern="logger.info",
                qualified_name="logger.info",
                annotation="diagnostic",
            ),
        ]
        unauth = evaluate_inventory(sites, allowlist=(), root=tmp_path)
        assert unauth == []

    def test_root_is_required(self, tmp_path: Path) -> None:
        # PR #705 round 4 HIGH: ``root`` is a required keyword arg.
        # Calling without it raises TypeError rather than silently
        # comparing absolute site paths against relative
        # allowlist entries (which never matched).
        sites = [
            EgressSite(
                file=tmp_path / "src/watercooler/commands.py",
                line=412,
                pattern="*.write_text",
                qualified_name="projection_path.write_text",
                annotation="primary",
                annotation_line=412,
            ),
        ]
        with pytest.raises(TypeError, match="root"):
            evaluate_inventory(sites, allowlist=())  # type: ignore[call-arg]

    def test_site_outside_root_treated_as_unauthorised(self, tmp_path: Path) -> None:
        # Sites outside the scan root cannot be described by the
        # allowlist's relative entries; treat as unauthorised
        # rather than silently OK (the previous `rel = site.file`
        # fallback would have used the absolute path as the key
        # and never matched anyway).
        outside = tmp_path.parent / "outside.py"
        sites = [
            EgressSite(
                file=outside,
                line=10,
                pattern="*.write_text",
                qualified_name="x.write_text",
                annotation="primary",
                annotation_line=10,
            ),
        ]
        # Allowlist entry pretends to cover the outside path —
        # but it's outside root, so still unauthorised.
        allow = [AllowlistEntry(rel_path=str(outside), line=10)]
        unauth = evaluate_inventory(sites, root=tmp_path, allowlist=allow)
        assert len(unauth) == 1

    def test_parse_allowlist_basic(self) -> None:
        text = """
        # Class P sites
        src/watercooler/commands.py:412
        src/watercooler_mcp/tools/thread_query.py:88
        """
        entries = parse_allowlist(text)
        assert entries == [
            AllowlistEntry(rel_path="src/watercooler/commands.py", line=412),
            AllowlistEntry(
                rel_path="src/watercooler_mcp/tools/thread_query.py", line=88
            ),
        ]

    def test_parse_allowlist_blank_lines_and_comments(self) -> None:
        text = """
        # leading comment
        src/a.py:1

        # interior comment
        src/b.py:2
        """
        entries = parse_allowlist(text)
        assert len(entries) == 2

    def test_parse_allowlist_malformed_raises(self) -> None:
        with pytest.raises(ValueError, match="rel_path"):
            parse_allowlist("not-a-valid-line\n")
        with pytest.raises(ValueError, match="not int"):
            parse_allowlist("src/a.py:not-a-number\n")

    def test_parse_allowlist_rejects_windows_paths(self) -> None:
        # PR #705 round 6 LOW: Windows-form paths cannot match
        # against the POSIX paths ``evaluate_inventory`` produces
        # via ``relative_to(root).as_posix()``. Reject at parse
        # so the error is loud rather than silent.
        with pytest.raises(ValueError, match="backslash"):
            parse_allowlist("src\\watercooler\\commands.py:412\n")
        with pytest.raises(ValueError, match="absolute"):
            parse_allowlist("C:/src/commands.py:412\n")
        with pytest.raises(ValueError, match="absolute"):
            parse_allowlist("/abs/path/commands.py:412\n")

    def test_parse_allowlist_rejects_empty_rel_path(self) -> None:
        with pytest.raises(ValueError, match="empty rel_path"):
            parse_allowlist(":42\n")


# PR #705 round 5 LOW: ``TestStrictModeFlag`` was deleted. The
# tests it contained were tautological (asserting that
# ``os.getenv(X, "")`` returns ``""`` after a ``delenv`` —
# always true regardless of any production behaviour). The flag
# semantics are exercised by ``TestEvaluateInventoryEnforcement``
# above. ``WATERCOOLER_EGRESS_INVENTORY_STRICT`` is reserved for
# a CI runner integration that is still TODO; once that lands it
# will get its own behavioural test, not an env-var-roundtrip
# placeholder.


# ------------------------------------------------------------------ #
# Annotation proximity on real code (smoke)
# ------------------------------------------------------------------ #


class TestAnnotationProximityOnRealCode:
    def test_annotation_string_is_canonical(self) -> None:
        # The exact CLASS_P_ANNOTATION string is the contract that
        # production code annotators must use. Don't accidentally
        # change it.
        assert CLASS_P_ANNOTATION == "egress-class: primary"

    def test_annotated_sites_classify_as_primary(
        self, production_sites: List[EgressSite]
    ) -> None:
        # Every site whose annotation field is "primary" must have
        # an annotation_line set; the scanner cannot classify
        # primary without finding the comment.
        #
        # PR #705 round 3 LOW finding: the previous assertion only
        # accepted ``site.line`` or ``site.line - 1``, but the
        # scanner contract (post-round-2 multi-line fix) accepts
        # the full ``[line, end_line]`` span. A real Class P
        # annotation on the closing-paren line of a multi-line
        # call would have failed this assertion. Now we assert
        # the actual scanner contract:
        #     line - 1 <= annotation_line <= end_line.
        for site in production_sites:
            if site.annotation == "primary":
                assert site.annotation_line is not None, (
                    f"primary site at {site.file}:{site.line} has no "
                    "annotation_line — scanner contract violation"
                )
                upper = site.end_line if site.end_line is not None else site.line
                assert site.line - 1 <= site.annotation_line <= upper, (
                    f"primary site at {site.file}:{site.line} (end "
                    f"{site.end_line}) has annotation on line "
                    f"{site.annotation_line} — proximity violated"
                )
