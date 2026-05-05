"""Tests for ``scripts/validate-copybara-allowlist.py`` — the cloud's
Copybara allowlist validator. Covers the existing structural checks
plus the new diff-time check for new ``docs/*.md`` files (added in
the post-v0.4.3 leak audit; see thread ``security-audit-followon-2026-05-04``
plan v1, entry ``01KQV0EZRXT82DKBEEQ75N7AG0``).

The new check defends against the leak shape that produced
``docs/RAILWAY_OPERATIONS.md`` reaching public in v0.4.3 — operationally
shaped names or bodies with private markers must be either explicitly
excluded or annotated with ``# COPYBARA-PUBLIC: <reason>``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "validate-copybara-allowlist.py"


@pytest.fixture(scope="module")
def validator():
    """Load the script as a module (it has a hyphenated filename)."""
    spec = importlib.util.spec_from_file_location("validator", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# _looks_private_by_name
# --------------------------------------------------------------------------- #


class TestLooksPrivateByName:
    """The name patterns triggering the private-doc gate."""

    @pytest.mark.parametrize(
        "path",
        [
            "docs/RAILWAY_OPERATIONS.md",
            "docs/OPS_RAILWAY_FALKORDB.md",
            "docs/OPS_T2_REBUILD.md",
            "docs/AUTHENTICATION_HOSTED.md",
            "docs/CONFIGURATION_HOSTED.md",
            "docs/DEPLOYMENT.md",
            "docs/DEPLOYMENT_HOSTED.md",
        ],
    )
    def test_operational_names_flagged(self, validator, path):
        assert validator._looks_private_by_name(path) is not None

    @pytest.mark.parametrize(
        "path",
        [
            "docs/USER_GUIDE.md",
            "docs/QUICKSTART.md",
            "docs/CONFIGURATION.md",
            "docs/AUTHENTICATION.md",
            "docs/MCP-CLIENTS.md",
            "docs/TROUBLESHOOTING.md",
            "docs/CHANGELOG.md",
            "docs/README.md",
        ],
    )
    def test_normal_names_pass(self, validator, path):
        assert validator._looks_private_by_name(path) is None

    def test_basename_only(self, validator):
        """The check uses basename, not full path. Nested dir doesn't cancel match."""
        assert validator._looks_private_by_name("docs/subdir/RAILWAY_OPS.md") is not None


# --------------------------------------------------------------------------- #
# get_added_docs_files — diff-failure must raise, not silently return []
# --------------------------------------------------------------------------- #


class TestGetAddedDocsFiles:
    """CR feedback HIGH on PR #764: subprocess failure must NOT silently
    return ``[]`` (which the caller would treat as "no new files"). It
    must raise ``DiffUnavailableError`` so the caller can fail the
    check explicitly."""

    def test_subprocess_failure_raises(self, validator, monkeypatch):
        """Simulate ``git diff`` exiting non-zero (e.g., shallow clone
        where the base ref isn't fetched). The function must raise,
        not silently return []."""
        import subprocess as _sp

        def fake_run(*args, **kwargs):
            raise _sp.CalledProcessError(128, args[0], stderr="bad revision")

        monkeypatch.setattr("subprocess.run", fake_run)
        with pytest.raises(validator.DiffUnavailableError):
            validator.get_added_docs_files("main")

    def test_subprocess_timeout_raises(self, validator, monkeypatch):
        """Network or hung git operations also surface as DiffUnavailableError."""
        import subprocess as _sp

        def fake_run(*args, **kwargs):
            raise _sp.TimeoutExpired(cmd=args[0], timeout=30)

        monkeypatch.setattr("subprocess.run", fake_run)
        with pytest.raises(validator.DiffUnavailableError):
            validator.get_added_docs_files("main")

    def test_subdir_files_included(self, validator, monkeypatch):
        """CR feedback MEDIUM on PR #764: subdirectory files like
        ``docs/ops/RAILWAY_OPS.md`` are included by Copybara's
        ``docs/**`` glob, so the gate must check them. The previous
        filter excluded them."""
        import subprocess as _sp

        class _Result:
            stdout = (
                "docs/USER_GUIDE.md\n"
                "docs/ops/RAILWAY_OPS.md\n"
                "docs/runbooks/incident-response.md\n"
                "docs/.cli-threads/some-thread.md\n"
                "src/some_file.py\n"
                "tests/unit/test_x.py\n"
            )

        def fake_run(*args, **kwargs):
            return _Result()

        monkeypatch.setattr("subprocess.run", fake_run)
        added = validator.get_added_docs_files("main")
        assert "docs/USER_GUIDE.md" in added
        assert "docs/ops/RAILWAY_OPS.md" in added
        assert "docs/runbooks/incident-response.md" in added
        assert "docs/.cli-threads/some-thread.md" in added
        assert "src/some_file.py" not in added
        assert "tests/unit/test_x.py" not in added


# --------------------------------------------------------------------------- #
# _looks_private_by_body
# --------------------------------------------------------------------------- #


class TestLooksPrivateByBody:
    """Body markers that trigger the gate. Operates on a pre-read
    head string (see ``_read_doc_head``) so OSError surfaces in
    the caller, not silently in the helper."""

    def test_proud_blessing_marker(self, validator):
        head = "# Title\n\nUses proud-blessing project.\n"
        assert validator._looks_private_by_body(head) == "proud-blessing"

    def test_railway_internal_marker(self, validator):
        head = "# Title\n\nHostname falkordb.railway.internal.\n"
        assert validator._looks_private_by_body(head) == ".railway.internal"

    def test_no_markers_passes(self, validator):
        head = "# Title\n\nPlain content with no private markers.\n"
        assert validator._looks_private_by_body(head) is None

    def test_only_first_16k_scanned_via_read_doc_head(self, validator, tmp_path):
        """``_read_doc_head`` reads at most 16 KB; a marker past that
        boundary is invisible to the body check (documented trade-off)."""
        f = tmp_path / "doc.md"
        f.write_text(("a" * 17_000) + "\nproud-blessing\n")
        head = validator._read_doc_head(str(f))
        assert validator._looks_private_by_body(head) is None

    def test_read_doc_head_raises_on_missing(self, validator, tmp_path):
        """``_read_doc_head`` raises FileNotFoundError on missing file —
        caller (``check_new_docs``) catches and fails closed rather
        than swallowing silently."""
        with pytest.raises(OSError):
            validator._read_doc_head(str(tmp_path / "nope.md"))


# --------------------------------------------------------------------------- #
# _has_copybara_public_ack
# --------------------------------------------------------------------------- #


class TestCopybaraPublicAck:
    """The opt-in annotation for genuinely public docs with operational-shaped names."""

    @pytest.mark.parametrize(
        "head",
        [
            "# COPYBARA-PUBLIC: this is fine\n\n# Real content\n",
            "<!-- COPYBARA-PUBLIC: ack -->\n\n# Real content\n",
            "COPYBARA-PUBLIC: bare form ok\n\n# Real content\n",
            # Leading whitespace tolerated (CR feedback LOW on PR #764:
            # docstring claimed "first non-whitespace content" but the
            # \A anchor without \s* didn't actually skip whitespace).
            "\n\n# COPYBARA-PUBLIC: leading newlines\n# Content\n",
            "   COPYBARA-PUBLIC: leading spaces\n",
        ],
    )
    def test_ack_forms_recognized(self, validator, head):
        assert validator._has_copybara_public_ack(head)

    @pytest.mark.parametrize(
        "head,expected",
        [
            # No reason — must be non-empty after the colon.
            ("# COPYBARA-PUBLIC:\n", False),
            # Mid-document ack: post-CR-tightening (\A anchor), the
            # ack MUST be at (whitespace-prefixed) start of the file.
            # A COPYBARA-PUBLIC line inside a code example or
            # mid-document body cannot bypass the gate.
            (
                "# Some other content\nCOPYBARA-PUBLIC: not at start\n",
                False,
            ),
            ("# Title\n\nNo annotation here.\n", False),
        ],
    )
    def test_ack_negative_or_corner(self, validator, head, expected):
        assert validator._has_copybara_public_ack(head) == expected


# --------------------------------------------------------------------------- #
# _is_excluded — glob-aware exclude matching
# --------------------------------------------------------------------------- #


class TestIsExcluded:
    """CR feedback MEDIUM on PR #764: the previous exact-string match
    missed glob-style entries in the Copybara exclude list. ``docs/
    brainstorms/**`` should match ``docs/brainstorms/anything.md``;
    ``tests/unit/test_*memory*.py`` should match
    ``tests/unit/test_memory_graph.py`` (though we typically only
    ask about docs/, we test both shapes for parity)."""

    def test_exact_path_match(self, validator):
        assert validator._is_excluded(
            "docs/DEPLOYMENT_HOSTED.md",
            ["docs/DEPLOYMENT_HOSTED.md", "docs/AGENT-SETUP_HOSTED.md"],
        )
        assert not validator._is_excluded(
            "docs/SOMETHING_ELSE.md",
            ["docs/DEPLOYMENT_HOSTED.md"],
        )

    def test_subtree_glob_matches_descendants(self, validator):
        excludes = ["docs/brainstorms/**", "docs/plans/**"]
        assert validator._is_excluded("docs/brainstorms/RAILWAY_ARCH.md", excludes)
        assert validator._is_excluded(
            "docs/brainstorms/sub/deeper.md", excludes
        )
        # The bare directory itself (without trailing slash) still matches
        # — defensive, in case the diff produces such a path.
        assert validator._is_excluded("docs/brainstorms", excludes)
        # Sibling paths NOT under the excluded subtree do not match.
        assert not validator._is_excluded("docs/USER_GUIDE.md", excludes)
        assert not validator._is_excluded(
            "docs/brainstormsSibling.md", excludes
        )

    def test_filename_glob(self, validator):
        excludes = ["tests/unit/test_*memory*.py"]
        assert validator._is_excluded(
            "tests/unit/test_memory_graph.py", excludes
        )
        assert not validator._is_excluded(
            "tests/unit/test_other.py", excludes
        )

    def test_double_star_anywhere_pattern(self, validator):
        """``**/_local/**`` — match any path with ``_local`` segment."""
        excludes = ["**/_local/**"]
        assert validator._is_excluded("foo/_local/bar.md", excludes)
        assert validator._is_excluded("a/b/_local/c/d.md", excludes)
        # Note: fnmatch with ``**`` collapsed to ``*`` is best-effort;
        # we don't promise perfect Copybara semantics here, only that
        # obvious matches pass.

    def test_v043_leak_shape_caught_via_glob_in_brainstorms(self, validator):
        """End-to-end retroactive: a private-shape doc nested under an
        excluded subtree MUST be reported as excluded so the gate
        doesn't false-positive. CR-MEDIUM regression test."""
        excludes = ["docs/brainstorms/**"]
        assert validator._is_excluded(
            "docs/brainstorms/RAILWAY_OPS.md", excludes
        )


# --------------------------------------------------------------------------- #
# check_new_docs (integration)
# --------------------------------------------------------------------------- #


class TestCheckNewDocs:
    """The actual gate that combines name/body/ack/exclude logic."""

    def test_empty_added_list_passes(self, validator):
        assert validator.check_new_docs([], excludes=[])

    def test_excluded_file_passes(self, validator, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "RAILWAY_OPS.md"
        f.write_text("# Internal runbook\n\nUses proud-blessing.\n")
        assert validator.check_new_docs(
            ["docs/RAILWAY_OPS.md"], excludes=["docs/RAILWAY_OPS.md"]
        )

    def test_private_name_no_ack_fails(self, validator, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "RAILWAY_OPS.md"
        f.write_text("# Plain content\n")  # no body marker, only name match
        assert not validator.check_new_docs(["docs/RAILWAY_OPS.md"], excludes=[])
        out = capsys.readouterr().out
        assert "RAILWAY_OPS.md" in out
        assert "name matches pattern" in out

    def test_private_body_no_ack_fails(self, validator, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "INNOCUOUS.md"
        f.write_text("# Title\n\nReferences proud-blessing.\n")
        assert not validator.check_new_docs(["docs/INNOCUOUS.md"], excludes=[])
        out = capsys.readouterr().out
        assert "INNOCUOUS.md" in out
        assert "body contains marker" in out

    def test_private_name_with_ack_passes(self, validator, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "OPS_DEMO.md"
        f.write_text(
            "# COPYBARA-PUBLIC: demo doc, no private content\n\n# Content\n"
        )
        assert validator.check_new_docs(["docs/OPS_DEMO.md"], excludes=[])
        out = capsys.readouterr().out
        assert "public-ack'd" in out

    def test_normal_name_no_markers_passes(self, validator, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "USER_GUIDE.md"
        f.write_text("# User guide\n\nPlain instructions.\n")
        assert validator.check_new_docs(["docs/USER_GUIDE.md"], excludes=[])

    def test_retroactive_v043_leak_caught(
        self, validator, tmp_path, monkeypatch, capsys
    ):
        """The exact shape of the v0.4.3 leak — RAILWAY_OPERATIONS.md with
        proud-blessing in body, no exclude, no ack — must be caught."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "RAILWAY_OPERATIONS.md"
        f.write_text(
            "# Railway operations runbook\n\n"
            "Applies to every service in the proud-blessing Railway project.\n"
            "Uses falkordb.railway.internal:6379.\n"
        )
        assert not validator.check_new_docs(
            ["docs/RAILWAY_OPERATIONS.md"], excludes=[]
        )
        out = capsys.readouterr().out
        assert "RAILWAY_OPERATIONS.md" in out
        # Both name-match AND body-marker should fire on this file.
        assert "name matches" in out
        assert "body contains marker" in out
