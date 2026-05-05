"""Unit tests for the egress-inventory AST scanner.

The scanner is the building block for the CI gate in
``tests/integration/test_egress_inventory.py``. These tests cover:

- Pattern matching (exact + glob)
- Annotation proximity (same line, line above, further away rejected)
- Dynamic-call ignoring
- Recursive package walk + exclude-dir filtering
"""

from __future__ import annotations

import io
import os
import textwrap
from pathlib import Path

import pytest

from watercooler_mcp.audit.egress_inventory import (
    CLASS_P_ANNOTATION,
    EgressSite,
    classify_site,
    scan_module,
    scan_package,
)


# ------------------------------------------------------------------ #
# Pattern matching
# ------------------------------------------------------------------ #


class TestPatternMatching:
    def test_logger_info_matched(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                import logging
                logger = logging.getLogger(__name__)

                def f():
                    logger.info("hello")
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].pattern == "logger.info"
        assert sites[0].qualified_name == "logger.info"

    def test_json_dumps_matched(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                import json
                def f(x):
                    return json.dumps(x)
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].pattern == "json.dumps"

    def test_print_matched(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text("print('hi')\n")
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].pattern == "print"

    def test_subprocess_run_matched(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                import subprocess
                subprocess.run(["ls"])
                """
            ).strip()
        )
        sites = scan_module(src)
        assert any(s.pattern == "subprocess.run" for s in sites)

    def test_path_instance_write_text_matched(self, tmp_path: Path) -> None:
        """PR #705 round 2 HIGH finding: the scanner now matches
        instance-method file writes via the ``*.write_text`` glob.
        Previously the pattern ``Path.write_text`` only matched
        the unbound-method form, so every real
        ``projection_path.write_text(...)`` call was missed.
        """
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                from pathlib import Path
                def f(p):
                    p.write_text("hello")
                    p.write_bytes(b"hello")
                """
            ).strip()
        )
        sites = scan_module(src)
        patterns = {s.pattern for s in sites}
        # Both glob patterns should have matched.
        assert "*.write_text" in patterns
        assert "*.write_bytes" in patterns

    def test_multi_segment_chain_write_text_matched(self, tmp_path: Path) -> None:
        """PR #705 round 3 MED finding: the previous
        ``_matches_pattern`` required exact segment-count match,
        so ``self.projection_path.write_text(...)`` (3 segments)
        silently missed the 2-segment ``*.write_text`` pattern.
        The fix changes ``*.X`` to suffix-glob semantics: matches
        any chain ending in ``.X``.
        """
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                class Writer:
                    def f(self):
                        self.projection_path.write_text("hello")
                        self.fs.handler.write_bytes(b"hello")
                """
            ).strip()
        )
        sites = scan_module(src)
        patterns = {s.pattern for s in sites}
        assert "*.write_text" in patterns
        assert "*.write_bytes" in patterns

    def test_open_read_only_filtered(self, tmp_path: Path) -> None:
        """PR #705 round 4 MED: the scanner skips read-only opens.

        Read-only ``open(path)`` / ``open(path, "r")`` /
        ``open(path, "rb")`` are not egress events and should
        not appear in the inventory — they would have inflated
        the inventory significantly and added noise to the
        Sprint 3 allowlist baseline.
        """
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                def f(p):
                    open(p)              # default mode = "r"
                    open(p, "r")         # explicit text read
                    open(p, "rb")        # explicit binary read
                    open(p, mode="r")    # keyword arg form
                """
            ).strip()
        )
        sites = scan_module(src)
        # Zero ``open`` sites — all four are read-only.
        assert all(s.pattern != "open" for s in sites), (
            f"read-only opens should be filtered, got: "
            f"{[s.qualified_name for s in sites]}"
        )

    def test_open_write_modes_kept(self, tmp_path: Path) -> None:
        # Anything that writes — "w", "a", "x", "r+" — IS egress.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                def f(p):
                    open(p, "w")
                    open(p, "a")
                    open(p, "wb")
                    open(p, "r+")
                """
            ).strip()
        )
        sites = scan_module(src)
        open_sites = [s for s in sites if s.pattern == "open"]
        # All four write modes should be in the inventory.
        assert len(open_sites) == 4

    def test_io_open_write_kept(self, tmp_path: Path) -> None:
        # PR #705 round 7+5+2 LOW: ``io.open`` is the same function
        # as ``open`` in Python 3 but didn't have a pattern entry,
        # so ``import io; io.open(p, "w")`` silently bypassed the
        # audit. Verify it's now matched.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                import io
                def f(p):
                    io.open(p, "w")
                    io.open(p, "ab")
                """
            ).strip()
        )
        sites = scan_module(src)
        io_sites = [s for s in sites if s.pattern == "io.open"]
        assert len(io_sites) == 2, (
            f"io.open writes should be matched, got: "
            f"{[s.qualified_name for s in sites]}"
        )

    def test_io_open_read_only_filtered(self, tmp_path: Path) -> None:
        # The same read-only filter applies — ``io.open(p, "r")``
        # is not egress, same semantics as bare ``open``.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                import io
                def f(p):
                    io.open(p, "r")
                    io.open(p, "rb")
                    io.open(p)  # default mode "r"
                """
            ).strip()
        )
        sites = scan_module(src)
        assert all(s.pattern != "io.open" for s in sites), (
            f"read-only io.open should be filtered, got: "
            f"{[s.qualified_name for s in sites]}"
        )

    def test_open_dynamic_mode_kept(self, tmp_path: Path) -> None:
        # If the scanner can't statically prove the mode is
        # read-only, the call is kept (conservative for audit).
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                def f(p, mode):
                    open(p, mode)
                """
            ).strip()
        )
        sites = scan_module(src)
        assert any(s.pattern == "open" for s in sites)

    def test_suffix_glob_does_not_match_unrelated_method(self, tmp_path: Path) -> None:
        # ``*.write_text`` should match ``.write_text`` ONLY, not
        # arbitrary other method names.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                def f(obj):
                    obj.read_text()
                    obj.write()
                """
            ).strip()
        )
        sites = scan_module(src)
        # No write_text/write_bytes calls — empty result.
        assert all(s.pattern not in ("*.write_text", "*.write_bytes") for s in sites)

    def test_bare_function_named_write_text_not_matched(
        self, tmp_path: Path
    ) -> None:
        # PR #705 round 7 MED: a bare ``write_text(data)`` call (a
        # local helper function with no receiver) is NOT a method
        # call on a Path/file object. The previous suffix-glob
        # condition ``qualified == suffix`` was True for the bare
        # form, inflating the Class P inventory with false
        # positives. Require at least one dot before the suffix.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                def write_text(buf, data):
                    return buf.append(data)

                def f(buf):
                    write_text(buf, "x")
                """
            ).strip()
        )
        sites = scan_module(src)
        assert all(s.pattern != "*.write_text" for s in sites)

    def test_scan_module_returns_empty_on_oserror(
        self, tmp_path: Path
    ) -> None:
        # PR #705 round 7+3 LOW: ``scan_module`` previously caught
        # only ``SyntaxError`` from ``ast.parse``. An ``OSError``
        # from ``read_text`` (broken symlink, EACCES, race-removed
        # path) would propagate through ``scan_package`` and
        # abort the full scan. Now both classes return ``[]`` so
        # one unreadable file does not invalidate the whole
        # inventory run.
        nonexistent = tmp_path / "does_not_exist.py"
        # File is missing entirely — read_text raises
        # FileNotFoundError (an OSError subclass).
        assert scan_module(nonexistent) == []

    def test_scan_package_warns_on_unreadable_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # PR #705 round 7+4 LOW: ``scan_module`` returns ``[]`` for
        # unreadable files (round 7+3); without accounting at the
        # package layer, a transient permission / encoding issue
        # could let CI pass on a partial inventory. Verify
        # ``scan_package`` warns when files are silently skipped.
        import logging as _logging

        from watercooler_mcp.audit import egress_inventory as ei

        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "good.py").write_text("import logging\nlogging.info('x')\n")
        # Create an unreadable file. chmod 000 reliably triggers
        # OSError on read_text on POSIX. Skip on platforms where
        # the chmod has no effect (e.g. running as root).
        bad = pkg / "unreadable.py"
        bad.write_text("import logging\n")
        try:
            os.chmod(bad, 0o000)
            if os.access(bad, os.R_OK):
                pytest.skip(
                    "running as a user that can read 0o000 files; "
                    "skip warning path can't be exercised"
                )
        except OSError:
            pytest.skip("chmod not supported on this platform")
        try:
            with caplog.at_level(_logging.WARNING, logger=ei.logger.name):
                sites = ei.scan_package(pkg)
        finally:
            # Restore so pytest cleanup can remove the temp dir.
            os.chmod(bad, 0o644)
        # Good file scanned; bad file skipped with a warning.
        assert any(s.pattern.startswith("logging.") for s in sites)
        # caplog uses propagation; if the logger has propagate=False
        # the message goes to the leaf logger handlers only — fall
        # back to checking the leaf logger directly via a local
        # StringIO handler if caplog missed it.
        if not any(
            "unreadable" in r.message for r in caplog.records
            if r.name == ei.logger.name
        ):
            # Re-run with a direct handler attached.
            buf = io.StringIO()
            handler = _logging.StreamHandler(buf)
            handler.setLevel(_logging.WARNING)
            ei.logger.addHandler(handler)
            try:
                os.chmod(bad, 0o000)
                ei.scan_package(pkg)
            finally:
                os.chmod(bad, 0o644)
                ei.logger.removeHandler(handler)
            assert "unreadable" in buf.getvalue()

    def test_scan_package_warns_on_unparseable_file(
        self, tmp_path: Path
    ) -> None:
        # PR #705 round 7+5+1 MED: ``scan_module`` returns ``[]`` for
        # both unreadable and unparseable files; the previous skip-
        # accounting re-check returned True for syntax-error files
        # (they exist + are readable), so they were silently dropped
        # from the warning. Once strict mode is on, an egress site
        # in a syntax-error file would silently bypass the gate.
        # Both classes now appear in the warning.
        import logging as _logging

        from watercooler_mcp.audit import egress_inventory as ei

        pkg = tmp_path / "pkg2"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "good.py").write_text("import logging\nlogging.info('x')\n")
        # Genuine syntax error — file is readable but ast.parse fails.
        (pkg / "broken.py").write_text("def f(:\n    pass\n")

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setLevel(_logging.WARNING)
        ei.logger.addHandler(handler)
        try:
            sites = ei.scan_package(pkg)
        finally:
            ei.logger.removeHandler(handler)

        assert any(s.pattern.startswith("logging.") for s in sites)
        captured = buf.getvalue()
        assert "unparseable=1" in captured, (
            f"expected unparseable=1 in skip warning, got: {captured!r}"
        )
        assert "broken.py" in captured

    def test_annotation_comment_lines_warns_on_tokenize_failure(
        self, tmp_path: Path
    ) -> None:
        # PR #705 round 7+5+1 LOW: tokenize failure used to return
        # ``frozenset()`` silently — a file with real Class P
        # annotations would have those sites silently downgraded
        # to "diagnostic" once strict mode is on (quiet
        # enforcement bypass). Now a WARNING is emitted so the
        # failure is visible in CI output.
        import logging as _logging

        from watercooler_mcp.audit import egress_inventory as ei

        # Unterminated string-with-backslash continuation —
        # tokenize raises TokenError.
        bad_text = "x = '\\\n"

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setLevel(_logging.WARNING)
        ei.logger.addHandler(handler)
        try:
            result = ei._annotation_comment_lines(bad_text, filename="bad.py")
        finally:
            ei.logger.removeHandler(handler)

        assert result == frozenset()
        captured = buf.getvalue()
        assert "tokenize failed on bad.py" in captured

    def test_scan_package_skips_directory_symlink_loop(
        self, tmp_path: Path
    ) -> None:
        # PR #705 round 7 LOW: ``Path.rglob`` follows directory
        # symlinks by default, and a symlink loop under the scan
        # root would hang the scanner indefinitely. Verify that the
        # ``os.walk(followlinks=False)`` switch makes
        # ``scan_package`` complete in finite time even with a
        # cyclic symlink under the root.
        import os as _os

        from watercooler_mcp.audit.egress_inventory import scan_package

        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "m.py").write_text("import logging\nlogging.info('x')\n")
        # Create a directory symlink loop: pkg/loop -> pkg
        loop = pkg / "loop"
        try:
            _os.symlink(str(pkg), str(loop), target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        # If ``scan_package`` followed symlinks this call would
        # hang or raise RecursionError. With followlinks=False it
        # completes and finds the single ``logging.info`` site.
        sites = scan_package(pkg)
        assert len(sites) >= 1
        assert any(s.pattern.startswith("logging.") for s in sites)

    def test_dynamic_call_ignored(self, tmp_path: Path) -> None:
        # `cb()` where cb is dynamic — the scanner can't classify, so
        # it's silently skipped (rather than hallucinating a pattern).
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                def f(cb):
                    cb("hello")
                """
            ).strip()
        )
        assert scan_module(src) == []

    def test_unrelated_call_not_matched(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                def f():
                    return sum([1, 2, 3])
                """
            ).strip()
        )
        assert scan_module(src) == []


# ------------------------------------------------------------------ #
# Class P annotation proximity
# ------------------------------------------------------------------ #


class TestAnnotationProximity:
    def test_same_line_annotation_classifies_primary(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                def f(x):
                    return json.dumps(x)  # {CLASS_P_ANNOTATION}
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "primary"
        assert sites[0].annotation_line == sites[0].line

    def test_line_above_annotation_classifies_primary(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                def f(x):
                    # {CLASS_P_ANNOTATION}
                    return json.dumps(x)
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "primary"
        assert sites[0].annotation_line == sites[0].line - 1

    def test_two_lines_above_does_not_classify(self, tmp_path: Path) -> None:
        # Proximity check is "same line or one above" — anything
        # further away is rejected because it rots.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                def f(x):
                    # {CLASS_P_ANNOTATION}
                    payload = x
                    return json.dumps(payload)
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "diagnostic"
        assert sites[0].annotation_line is None

    def test_annotation_in_string_literal_does_not_classify(
        self, tmp_path: Path
    ) -> None:
        # PR #705 round 7+3 LOW: a string literal containing the
        # exact annotation text must NOT be treated as a Class P
        # annotation. Previously the substring match would
        # misclassify the call below.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                DOC = "{CLASS_P_ANNOTATION}"
                def f(x):
                    return json.dumps(x)
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "diagnostic", (
            "string literal containing the annotation text was "
            "treated as an annotation — false positive"
        )

    def test_string_literal_with_hash_and_annotation_above_call_rejected(
        self, tmp_path: Path
    ) -> None:
        # PR #705 round 7+4 MED: the round 7+3 simple heuristic
        # accepted string literals like ``"# egress-class: primary"``
        # directly above the call as annotations. Sprint 3
        # baselining a false positive there permanently grants
        # Class P to a diagnostic call. Tokenize-based comment
        # detection now closes this — only real
        # ``tokenize.COMMENT`` tokens count.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                def f(x):
                    DOC = "# {CLASS_P_ANNOTATION}"
                    return json.dumps(x)
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "diagnostic", (
            "string literal containing # and the annotation text was "
            "treated as a real comment annotation — tokenize-based "
            "check failed"
        )

    def test_real_comment_above_multi_line_call_classifies(
        self, tmp_path: Path
    ) -> None:
        # Regression guard: the tokenize-based tightening must not
        # break the legitimate case — a real comment above a real
        # multi-line call still classifies.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                def f(x):
                    # {CLASS_P_ANNOTATION}
                    return json.dumps(
                        x,
                        indent=2,
                    )
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "primary"
        assert sites[0].annotation_line == sites[0].line - 1

    def test_default_is_diagnostic(self, tmp_path: Path) -> None:
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                """
                import json
                def f(x):
                    return json.dumps(x)
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "diagnostic"

    def test_classify_site_idempotent(self, tmp_path: Path) -> None:
        # classify_site is the explicit hook; it should produce
        # the same answer when called twice.
        site = EgressSite(
            file=Path("x.py"),
            line=2,
            pattern="json.dumps",
            qualified_name="json.dumps",
        )
        lines = [
            "# unrelated",
            f"json.dumps(x)  # {CLASS_P_ANNOTATION}",
        ]
        a = classify_site(site, lines)
        b = classify_site(a, lines)
        assert a.annotation == "primary"
        assert b.annotation == "primary"
        assert a == b

    def test_multi_line_call_annotation_on_closing_paren(self, tmp_path: Path) -> None:
        """PR #705 round 2 HIGH finding: a Class P annotation on
        the closing-paren line of a multi-line call was previously
        missed because proximity only inspected
        ``call_line - 1`` and ``call_line``. The fix scans
        ``[lineno, end_lineno]``.
        """
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                def f(x):
                    return json.dumps(
                        x,
                        sort_keys=True,
                    )  # {CLASS_P_ANNOTATION}
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "primary"
        # Annotation appears on the call's last line (end_line).
        assert sites[0].annotation_line == sites[0].end_line

    def test_multi_line_call_annotation_inside_call(self, tmp_path: Path) -> None:
        # Annotation on a comment inside the call argument list.
        src = tmp_path / "m.py"
        src.write_text(
            textwrap.dedent(
                f"""
                import json
                def f(x):
                    return json.dumps(
                        x,
                        # {CLASS_P_ANNOTATION}
                        sort_keys=True,
                    )
                """
            ).strip()
        )
        sites = scan_module(src)
        assert len(sites) == 1
        assert sites[0].annotation == "primary"


# ------------------------------------------------------------------ #
# Package walk
# ------------------------------------------------------------------ #


class TestScanPackage:
    def test_walks_subdirs(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_text("import json\njson.dumps({})")
        (tmp_path / "pkg" / "sub").mkdir()
        (tmp_path / "pkg" / "sub" / "b.py").write_text("print('hi')")
        sites = scan_package(tmp_path / "pkg")
        files = {s.file.name for s in sites}
        assert files == {"a.py", "b.py"}

    def test_excludes_default_dirs(self, tmp_path: Path) -> None:
        # tests/ is in the default exclude set
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.py").write_text("print('production')")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("print('test')")
        sites = scan_package(tmp_path)
        files = {s.file.name for s in sites}
        assert files == {"real.py"}

    def test_ancestor_path_with_excluded_name_does_not_exclude_repo(
        self, tmp_path: Path
    ) -> None:
        """Regression test for the LOW finding from PR #705 review.

        If the repo root happens to be installed under a path
        containing ``build`` / ``dist`` / etc. (e.g. a CI runner at
        ``/home/runner/build/...``), the previous implementation
        matched ``path.parts`` against the excludes set and silently
        dropped every module. The fix matches against
        ``path.relative_to(root).parts``.
        """
        # Simulate a repo installed under an ancestor whose name
        # collides with a default-excluded directory name.
        outer = tmp_path / "build"  # collides with default exclude
        outer.mkdir()
        repo = outer / "my-project"
        repo.mkdir()
        (repo / "real.py").write_text("print('production')")
        (repo / "sub").mkdir()
        (repo / "sub" / "more.py").write_text("import json\njson.dumps({})")

        sites = scan_package(repo)
        files = {s.file.name for s in sites}
        # Both files must appear despite ``build`` being an ancestor
        # of ``repo``.
        assert files == {"real.py", "more.py"}

    def test_explicit_empty_exclude_set_keeps_everything(self, tmp_path: Path) -> None:
        """PR #705 round 4 LOW: ``exclude_dirs=set()`` means
        "no exclusions", not "use defaults". The previous
        ``exclude_dirs or {default...}`` idiom treated empty set
        as falsy and silently fell back to defaults.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "real.py").write_text("print('keep')")
        # Create a 'tests' subdir which IS in default excludes —
        # but the caller explicitly asks for no exclusions.
        (repo / "tests").mkdir()
        (repo / "tests" / "t.py").write_text("print('also keep')")

        sites = scan_package(repo, exclude_dirs=set())
        files = {s.file.name for s in sites}
        # Both files appear because exclude_dirs=set() means
        # "no exclusions".
        assert files == {"real.py", "t.py"}

    def test_excluded_dir_under_root_still_skipped(self, tmp_path: Path) -> None:
        """The exclude semantics for components UNDER root are unchanged."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("print('keep')")
        # ``build`` directly under root still excluded by default.
        (repo / "build").mkdir()
        (repo / "build" / "b.py").write_text("print('drop')")
        sites = scan_package(repo)
        files = {s.file.name for s in sites}
        assert files == {"a.py"}

    def test_custom_excludes(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("print('keep')")
        (tmp_path / "ignored").mkdir()
        (tmp_path / "ignored" / "b.py").write_text("print('drop')")
        sites = scan_package(tmp_path, exclude_dirs={"ignored"})
        files = {s.file.name for s in sites}
        assert files == {"a.py"}

    def test_syntax_error_module_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "bad.py").write_text("def broken(:\n")
        (tmp_path / "good.py").write_text("print('hi')")
        sites = scan_package(tmp_path)
        files = {s.file.name for s in sites}
        assert files == {"good.py"}
