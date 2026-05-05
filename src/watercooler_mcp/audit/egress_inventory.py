"""AST-based egress-site inventory for Move 6 of the consolidation.

Walks ``src/`` with the :mod:`ast` module and produces a list of
:class:`EgressSite` records — one per call/write that crosses a
process boundary (stdlib log, JSON dump, HTTP, subprocess, file
write, print, FastMCP tool return). Each site is classified as
**Class D** (diagnostic — must redact) or **Class P** (primary user
data — annotated as such, must obey storage hygiene).

The data model is what matters; the scanner itself can be replaced
with Semgrep later without changing test logic.

## Class P annotation convention

A site is Class P if and only if it carries a ``# egress-class:
primary`` comment on the same line **or** the line immediately
above. Anything else is Class D by default. The proximity check
exists because comments further away rot — moving code or
inserting lines between the annotation and the call leaves a
stale label.

## Why not "auto-classify Class P from the call shape"

Because the same call shape can be Class P or Class D depending on
intent. ``Path.write_text`` writing a thread entry projection is
Class P; the same call writing a daemon snapshot is Class D. The
caller knows which; the scanner can't infer it. So we require an
explicit annotation for Class P and treat absence as Class D.

## Public surface

* :data:`CLASS_P_ANNOTATION` — exact comment string.
* :data:`EGRESS_PATTERNS` — the call-shapes the scanner looks for.
* :class:`EgressClass` — ``primary`` / ``diagnostic``.
* :class:`EgressSite` — one record per emit site.
* :func:`scan_module` — single-file scan (testable in isolation).
* :func:`scan_package` — recursive walk of a directory tree.
* :func:`classify_site` — given a site + the source lines, returns
  the resolved class.

Pure import-time execution; no I/O outside the explicit scan calls.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import logging
import os
import tokenize
from pathlib import Path
from typing import FrozenSet, Iterable, List, Literal, Optional, Sequence, Set


logger = logging.getLogger(__name__)


CLASS_P_ANNOTATION = "egress-class: primary"

EgressClass = Literal["primary", "diagnostic"]


# Call patterns of interest. Each entry is matched against the
# *qualified* call name produced by :func:`_qualified_call_name`.
# Glob-style ``*`` matches a single attribute segment. We intentionally
# keep the pattern list explicit rather than auto-discovering — the
# audit is meant to be a closed inventory.
EGRESS_PATTERNS: tuple[str, ...] = (
    # Logging
    "logger.info",
    "logger.warning",
    "logger.error",
    "logger.debug",
    "logger.critical",
    "logger.exception",
    "logging.info",
    "logging.warning",
    "logging.error",
    "logging.debug",
    "logging.critical",
    "logging.exception",
    # JSON serialization
    "json.dumps",
    "json.dump",
    # Stdout/stderr
    "print",
    "sys.stdout.write",
    "sys.stderr.write",
    # HTTP egress
    "httpx.get",
    "httpx.post",
    "httpx.put",
    "httpx.patch",
    "httpx.delete",
    "httpx.AsyncClient",
    "httpx.Client",
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.patch",
    "requests.delete",
    "urllib.request.urlopen",
    "urllib.request.Request",
    "aiohttp.ClientSession",
    "http.client.HTTPConnection",
    "http.client.HTTPSConnection",
    # Subprocess
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_output",
    "subprocess.check_call",
    # File writes — Path instances are typed as their variable name
    # (``projection_path.write_text``), so the glob ``*.write_text``
    # is required to match real call sites. ``Path.write_text``
    # would only match the unbound-method form ``Path.write_text(p,
    # ...)`` which production code never uses.
    "*.write_text",
    "*.write_bytes",
    # Builtin open is matched directly. Instance ``.open()`` calls
    # are not matched because most match unrelated APIs
    # (``request.open``, ``contextlib.open_dir``, etc.); add a
    # specific glob if a Path-bound case needs the audit.
    "open",
    # ``io.open`` is the same function object as ``open`` in
    # Python 3, but code that does ``import io; io.open(path, "w")``
    # silently bypasses the bare ``open`` pattern. Match it
    # explicitly. PR #705 round 7+5+2 LOW.
    "io.open",
)


@dataclasses.dataclass(frozen=True, slots=True)
class EgressSite:
    """One egress emit site found by the scanner.

    Attributes:
        file: Path to the source file containing the site.
        line: 1-indexed line number of the call's first line.
        end_line: 1-indexed line number of the call's last line
            (same as ``line`` for single-line calls). Used by the
            annotation-proximity check so multi-line calls can
            carry their ``# egress-class: primary`` comment on the
            closing-paren line as well as the opening line.
        pattern: Matched entry from :data:`EGRESS_PATTERNS`.
        qualified_name: The reconstructed call name (e.g.
            ``logger.info``, ``json.dumps``).
        annotation: Resolved annotation string ("primary" or
            "diagnostic"), populated by :func:`classify_site` when
            the site is examined alongside the source lines.
        annotation_line: The line where the Class P annotation was
            found, or ``None`` if not annotated.
    """

    file: Path
    line: int
    pattern: str
    qualified_name: str
    end_line: Optional[int] = None
    annotation: EgressClass = "diagnostic"
    annotation_line: Optional[int] = None


# ------------------------------------------------------------------ #
# AST helpers
# ------------------------------------------------------------------ #


def _qualified_call_name(node: ast.Call) -> Optional[str]:
    """Reconstruct ``a.b.c`` from a Call's func chain, if static.

    Returns ``None`` for dynamic calls (callables stored in vars,
    invocations on subscripts, lambdas) — the scanner ignores
    those because they can't be classified by name alone.
    """
    func = node.func
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        parts.reverse()
        return ".".join(parts)
    return None


def _matches_pattern(qualified: str, pattern: str) -> bool:
    """Match a qualified call name against a pattern.

    Two pattern shapes:

    1. **Exact** (no ``*``): ``json.dumps`` matches ``json.dumps``
       only.
    2. **Suffix glob** with leading ``*.``: ``*.write_text``
       matches any qualified name whose final segment is
       ``write_text`` — e.g. ``p.write_text``,
       ``self.projection_path.write_text``,
       ``self.fs.handler.write_text``. The pattern is meant to
       audit "any call to a method named X regardless of how
       deeply nested the receiver is."
    3. **Per-segment glob** (legacy): a non-leading ``*`` matches
       a single attribute segment. ``a.*.b`` matches ``a.x.b``
       but not ``a.x.y.b``. Kept for backwards compatibility but
       rarely useful since real Python attribute chains can be
       arbitrarily deep.

    The previous behaviour required exact segment-count match for
    any pattern containing ``*``, so ``*.write_text`` (2
    segments) silently missed ``self.projection_path.write_text``
    (3 segments) — the PR #705 round 3 MED finding.
    """
    if "*" not in pattern:
        return qualified == pattern

    # Suffix-glob form: "*.foo.bar" matches any chain ending in
    # ".foo.bar". Strip the leading "*." and compare suffixes.
    #
    # PR #705 round 7 MED: ``qualified == suffix`` was previously
    # part of the match condition, but a bare ``write_text(data)``
    # call (no receiver — i.e. a local helper function with the
    # same name) is NOT a method call on a receiver; flagging it
    # as one inflates the Class P allowlist with false positives.
    # Require at least one dot before the suffix so ``*.write_text``
    # only matches genuine attribute access.
    if pattern.startswith("*."):
        suffix = pattern[2:]  # e.g. "write_text" or "fs.write"
        return qualified.endswith("." + suffix)

    # Per-segment glob (legacy): same number of segments,
    # ``*`` matches any single segment.
    pat_parts = pattern.split(".")
    real_parts = qualified.split(".")
    if len(pat_parts) != len(real_parts):
        return False
    return all(p == "*" or p == r for p, r in zip(pat_parts, real_parts))


def _is_read_only_open_call(node: ast.Call) -> bool:
    """Return True if a builtin ``open`` call opens for reading only.

    Read-only opens aren't egress events, so the audit filters them
    out (PR #705 round 4 MED — bare ``open`` previously matched
    every read). A call is treated as read-only if:

    - mode is absent (Python defaults to ``"r"``);
    - the second positional arg is a string literal whose first
      char is ``"r"`` and contains no ``+`` (so ``"r"``, ``"rb"``
      pass; ``"r+"`` does not — a read+write mode IS egress);
    - the ``mode=`` keyword arg is the same shape.

    Anything that the scanner can't statically prove is read-only
    (mode passed as a variable, computed string, etc.) is treated
    as potentially egress and kept in the inventory.
    """
    # Find a mode argument either positionally (open(path, mode)) or
    # via keyword (open(path, mode="r")).
    mode_node: Optional[ast.AST] = None
    if len(node.args) >= 2:
        mode_node = node.args[1]
    if mode_node is None:
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_node = kw.value
                break
    if mode_node is None:
        # ``open(path)`` defaults to "r" — read-only.
        return True
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        spec = mode_node.value
        # Read-only if starts with "r" and not a read+write
        # variant. ``open(path, "rb")`` is read-only; ``"r+"`` is
        # not.
        return spec.startswith("r") and "+" not in spec
    return False


def _annotation_comment_lines(
    text: str, filename: Optional[str] = None
) -> FrozenSet[int]:
    """Return the set of 1-indexed line numbers that carry the
    Class P annotation inside a real Python comment token.

    PR #705 round 7+4 MED: the round 7+3 simple ``"#" in line``
    heuristic admitted a false positive when a string literal
    contained both a ``#`` and the annotation text on a single
    line (e.g. ``DOC = "# egress-class: primary"``). Sprint 3
    flips ``WATERCOOLER_EGRESS_INVENTORY_STRICT=1`` and baselines
    the allowlist; a false positive there permanently grants Class
    P to a diagnostic call. Use Python's own ``tokenize`` module
    so only genuine ``COMMENT`` tokens count.

    ``tokenize.tokenize`` requires a binary stream and may raise
    ``tokenize.TokenError`` on malformed input. PR #705 round
    7+5+1 LOW: returning an empty set silently means every call
    in the affected file is classified ``"diagnostic"`` even if
    a real ``# egress-class: primary`` annotation is present
    (because the membership-test branch can't find the line in
    an empty set). Once Sprint 3 flips strict mode this becomes a
    quiet enforcement bypass — a tokenize-failed file with real
    Class P annotations would have those sites silently
    downgraded to diagnostic and skip the allowlist gate. Log
    a WARNING so the failure is visible in CI output, mirroring
    the OSError warning path in ``scan_package``.

    ``filename`` is optional — the tokenize stream itself doesn't
    need it, but the warning log does. Callers that have the
    source path available pass it.
    """
    try:
        stream = io.BytesIO(text.encode("utf-8", errors="replace"))
        return frozenset(
            tok.start[0]
            for tok in tokenize.tokenize(stream.readline)
            if tok.type == tokenize.COMMENT and CLASS_P_ANNOTATION in tok.string
        )
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        logger.warning(
            "egress_inventory: tokenize failed on %s (%s); Class P "
            "annotations in this file will be downgraded to diagnostic. "
            "Once WATERCOOLER_EGRESS_INVENTORY_STRICT=1 is flipped this "
            "is a quiet enforcement bypass — fix the file or extend the "
            "tokenize fallback before flipping strict mode.",
            filename or "<unknown>",
            exc,
        )
        return frozenset()


def _has_class_p_annotation(
    source_lines: Sequence[str],
    call_line: int,
    end_line: Optional[int] = None,
    *,
    annotation_lines: Optional[FrozenSet[int]] = None,
) -> Optional[int]:
    """Return the line number of the Class P annotation, or None.

    Proximity rule: the annotation may appear on the line
    immediately ABOVE the call (1 line above ``call_line``) OR
    anywhere within the call span ``[call_line, end_line]``.
    Multi-line calls commonly carry the trailing-paren annotation
    on their closing-paren line, which would otherwise be missed
    by a same-line-only check.

    The annotation must appear inside a real Python comment token
    (round 7+4 MED tightening): ``annotation_lines`` is the set of
    1-indexed line numbers ``_annotation_comment_lines`` resolved
    from the file's tokenize stream. When ``annotation_lines`` is
    None, the function falls back to the legacy line-text heuristic
    (used by tests that supply ``source_lines`` without a backing
    file).

    ``call_line`` and ``end_line`` are 1-indexed (matching
    :attr:`ast.Call.lineno` / :attr:`ast.Call.end_lineno`). If
    ``end_line`` is None or less than ``call_line``, only the
    single-line proximity rule applies (``call_line - 1`` and
    ``call_line``).
    """
    if call_line < 1 or call_line > len(source_lines):
        return None

    def _line_carries_annotation(line_no: int) -> bool:
        if annotation_lines is not None:
            return line_no in annotation_lines
        # Legacy path for callers without a tokenize-derived set.
        if not (1 <= line_no <= len(source_lines)):
            return False
        line = source_lines[line_no - 1]
        annotation_idx = line.find(CLASS_P_ANNOTATION)
        if annotation_idx == -1:
            return False
        hash_idx = line.find("#")
        return hash_idx != -1 and hash_idx < annotation_idx

    # Line immediately above (single-line and multi-line).
    if call_line >= 2 and _line_carries_annotation(call_line - 1):
        return call_line - 1
    # Lines within the call span (inclusive). For single-line
    # calls this is just ``call_line``; for multi-line calls it
    # extends to ``end_line``.
    span_end = end_line if end_line is not None and end_line >= call_line else call_line
    span_end = min(span_end, len(source_lines))
    for line_no in range(call_line, span_end + 1):
        if _line_carries_annotation(line_no):
            return line_no
    return None


# ------------------------------------------------------------------ #
# Scanning
# ------------------------------------------------------------------ #


def scan_module(
    file: Path,
    *,
    patterns: Iterable[str] = EGRESS_PATTERNS,
) -> List[EgressSite]:
    """Scan a single Python file and return classified egress sites.

    Each returned site already has its ``annotation`` /
    ``annotation_line`` resolved via :func:`classify_site` (which
    is called internally with the source lines that were already
    loaded for the AST parse — so the cost is one file read, not
    two). Callers do not need to invoke :func:`classify_site`
    themselves on the result.

    Returns an empty list if the file is unparseable (SyntaxError) or
    unreadable (OSError — EACCES, ENOENT-after-glob race, broken
    symlink, etc.). PR #705 round 7+3 LOW: the previous version only
    caught ``SyntaxError`` from ``ast.parse``; an ``OSError`` from
    ``read_text`` would propagate through ``scan_package`` and abort
    the full scan over a single unreadable file.

    PR #705 round 7+5+1 MED: ``scan_package`` needs to distinguish
    "no egress sites" from "file was skipped" so it can warn on
    skipped files. The distinction lives in the private
    ``_scan_module_with_status`` helper below; ``scan_module``
    remains the public empty-list contract.
    """
    sites, _status = _scan_module_with_status(file, patterns=patterns)
    return sites


_SCAN_OK = "ok"
_SCAN_UNREADABLE = "unreadable"
_SCAN_UNPARSEABLE = "unparseable"


def _scan_module_with_status(
    file: Path,
    *,
    patterns: Iterable[str] = EGRESS_PATTERNS,
) -> "tuple[List[EgressSite], str]":
    """Scan a single file, returning (sites, status).

    Status values:
        ``_SCAN_OK`` — file read + parsed cleanly. Sites may be empty
            simply because the file has no egress calls.
        ``_SCAN_UNREADABLE`` — ``read_text`` raised ``OSError``
            (EACCES, broken symlink, removed-after-walk, etc.).
        ``_SCAN_UNPARSEABLE`` — ``ast.parse`` raised ``SyntaxError``.

    PR #705 round 7+5+1 MED: ``scan_package``'s skip-warning
    accounting previously only flagged unreadable files because
    its readability check returned True for syntax-error files
    (they exist and are readable). The distinction has to come
    from the scanner itself; this helper exposes it. Sprint 3
    strict mode will warn on both classes so a syntax-error file
    can't silently remove its egress sites from the enforcement
    universe.
    """
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], _SCAN_UNREADABLE
    try:
        tree = ast.parse(text, filename=str(file))
    except SyntaxError:
        return [], _SCAN_UNPARSEABLE

    pattern_list = tuple(patterns)
    sites: list[EgressSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        qualified = _qualified_call_name(node)
        if qualified is None:
            continue
        for pattern in pattern_list:
            if not _matches_pattern(qualified, pattern):
                continue
            # Filter read-only ``open`` calls — not egress
            # (PR #705 round 4 MED).
            # Read-only filter applies to both bare ``open`` and
            # ``io.open`` — the latter is the same function object
            # in Python 3 and inherits the same mode semantics.
            if pattern in ("open", "io.open") and _is_read_only_open_call(node):
                break
            sites.append(
                EgressSite(
                    file=file,
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", None),
                    pattern=pattern,
                    qualified_name=qualified,
                )
            )
            break

    # Resolve Class P annotations by re-reading lines (cheap; same I/O
    # as the parse). Round 7+4 MED: pre-resolve the set of comment
    # lines via tokenize so a string literal containing the
    # annotation text is not mis-classified as an annotation.
    if not sites:
        return sites, _SCAN_OK
    source_lines = text.splitlines()
    annotation_lines = _annotation_comment_lines(text, filename=str(file))
    return (
        [
            classify_site(site, source_lines, annotation_lines=annotation_lines)
            for site in sites
        ],
        _SCAN_OK,
    )


def classify_site(
    site: EgressSite,
    source_lines: Sequence[str],
    *,
    annotation_lines: Optional[FrozenSet[int]] = None,
) -> EgressSite:
    """Resolve the Class P annotation for an egress site.

    When ``annotation_lines`` is provided (e.g. by ``scan_module``),
    only those line numbers count as carrying an annotation — the
    set comes from a real ``tokenize.COMMENT`` pass over the file
    so string literals containing the annotation text are excluded.

    When ``annotation_lines`` is ``None`` (test callers that fabricate
    ``source_lines`` without a backing file), a legacy line-text
    heuristic applies.
    """
    ann_line = _has_class_p_annotation(
        source_lines, site.line, site.end_line, annotation_lines=annotation_lines
    )
    if ann_line is None:
        return site
    return dataclasses.replace(site, annotation="primary", annotation_line=ann_line)


def scan_package(
    root: Path,
    *,
    patterns: Iterable[str] = EGRESS_PATTERNS,
    exclude_dirs: Optional[Set[str]] = None,
) -> List[EgressSite]:
    """Recursively walk *root*, returning all egress sites found.

    ``exclude_dirs`` is matched against directory *names* under the
    scan root, not against ancestor components of an absolute path.
    A repository installed under e.g. ``/home/user/build/my-project/``
    must not silently exclude all of its modules just because
    ``"build"`` appears in an ancestor path component.
    """
    # Distinguish ``None`` (use defaults) from ``set()`` (explicit
    # "no exclusions"). The previous ``exclude_dirs or {...}``
    # idiom treated the empty set as falsy and silently fell back
    # to defaults — surprising for callers who passed ``set()``
    # to mean "scan everything" (PR #705 round 4 LOW).
    if exclude_dirs is None:
        excludes = {
            "__pycache__",
            ".git",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".venv",
            "venv",
            "node_modules",
            "build",
            "dist",
            "_legacy",
            "tests",
        }
    else:
        excludes = set(exclude_dirs)
    root_resolved = root.resolve()
    out: list[EgressSite] = []
    # PR #705 round 7 LOW: ``Path.rglob`` follows directory symlinks
    # by default, and a symlink loop under the scan root would hang
    # the scanner indefinitely. ``Path.rglob(..., follow_symlinks=...)``
    # is 3.13-only; ``os.walk`` defaults to ``followlinks=False``
    # across all supported Python versions (3.10+) and lets us prune
    # excluded directories in-place to skip them entirely instead of
    # walking them and filtering after the fact.
    #
    # PR #705 round 7+4 LOW: track files scanned vs files skipped
    # (unreadable / unparseable) and warn loudly when any are
    # skipped. ``scan_module`` returns ``[]`` for both
    # ``OSError`` (round 7+3 LOW) and ``SyntaxError``; without
    # accounting at this layer the CI gate could pass on a
    # partial inventory if a transient permission / encoding issue
    # made one file unreadable. The warning is the
    # defense-in-depth signal — "files scanned: N, skipped: K"
    # — that lets a human notice when the gate ran on less than
    # the expected universe.
    #
    # PR #705 round 7+5+1 MED: distinguish unreadable from
    # unparseable via ``_scan_module_with_status``. The previous
    # readability re-check returned True for syntax-error files
    # (they exist + are readable), so syntax-error files were
    # silently dropped from the warning accounting and could slip
    # past the gate post-strict-mode. Both classes now appear in
    # the warning.
    files_total = 0
    files_unreadable: list[Path] = []
    files_unparseable: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(
        str(root_resolved), followlinks=False
    ):
        # Prune excluded directories in-place so ``os.walk`` does
        # not descend into them at all.
        dirnames[:] = [d for d in dirnames if d not in excludes]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            files_total += 1
            module_sites, status = _scan_module_with_status(
                path, patterns=patterns
            )
            if status == _SCAN_UNREADABLE:
                files_unreadable.append(path)
            elif status == _SCAN_UNPARSEABLE:
                files_unparseable.append(path)
            out.extend(module_sites)
    if files_unreadable or files_unparseable:
        logger.warning(
            "egress_inventory: scan_package(%s) skipped %d file(s) of %d "
            "(unreadable=%d, unparseable=%d); inventory may be incomplete. "
            "Unreadable examples: %s; unparseable examples: %s",
            root_resolved,
            len(files_unreadable) + len(files_unparseable),
            files_total,
            len(files_unreadable),
            len(files_unparseable),
            [str(p) for p in files_unreadable[:3]],
            [str(p) for p in files_unparseable[:3]],
        )
    return out


# ------------------------------------------------------------------ #
# Allowlist-based enforcement (strict mode)
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """A single permitted Class P site, identified by file + line.

    The path is relative to the scan root so allowlists are
    machine-portable. The line number is the call's first line
    (matches :attr:`EgressSite.line`).
    """

    rel_path: str
    line: int


def parse_allowlist(text: str) -> list[AllowlistEntry]:
    """Parse a Class P allowlist file.

    Format: one entry per line, ``<rel_path>:<line>``. Blank lines
    and lines starting with ``#`` are ignored. The path separator
    is forward-slash on every platform — backslashes (Windows
    paths) and absolute paths (drive letters, leading ``/``) are
    rejected at parse time so a Windows operator can't
    accidentally produce an allowlist that fails to compare
    against the POSIX-form ``relative_to(root).as_posix()``
    output that ``evaluate_inventory`` uses.

    Example:
        # commands.py thread-projection writes
        src/watercooler/commands.py:412
        # MCP tool returns
        src/watercooler_mcp/tools/thread_query.py:88
    """
    entries: list[AllowlistEntry] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(
                f"allowlist line {line_no}: expected '<rel_path>:<line>', got "
                f"{stripped!r}"
            )
        rel_path, _, line_part = stripped.rpartition(":")
        try:
            line = int(line_part)
        except ValueError as exc:
            raise ValueError(
                f"allowlist line {line_no}: line number {line_part!r} not int"
            ) from exc
        # PR #705 round 6 LOW: validate path shape so Windows /
        # absolute-path entries don't silently parse but never
        # match against the relative POSIX paths
        # ``evaluate_inventory`` produces.
        if "\\" in rel_path:
            raise ValueError(
                f"allowlist line {line_no}: rel_path {rel_path!r} contains "
                "backslash (use forward-slashes; allowlists are POSIX-form)"
            )
        if rel_path.startswith("/") or (len(rel_path) >= 2 and rel_path[1] == ":"):
            raise ValueError(
                f"allowlist line {line_no}: rel_path {rel_path!r} looks "
                "absolute (must be relative to scan root)"
            )
        if not rel_path:
            raise ValueError(f"allowlist line {line_no}: empty rel_path")
        entries.append(AllowlistEntry(rel_path=rel_path, line=line))
    return entries


def evaluate_inventory(
    sites: Iterable[EgressSite],
    *,
    root: Path,
    allowlist: Iterable[AllowlistEntry] = (),
) -> list[EgressSite]:
    """Return Class P sites that are NOT in the allowlist.

    The empty list means "every Class P site is permitted" (the
    typical CI-gate green case). A non-empty list means new
    Class P sites have appeared without explicit annotation +
    allowlist entry — the strict-mode enforcement signal.

    Args:
        sites: Output of :func:`scan_package`.
        root: Scan root for path normalisation. Site paths are
            converted to ``relative_to(root)`` before comparison
            against allowlist entries (which are relative by
            definition). **Required** — the previous signature
            allowed ``root=None`` and silently compared absolute
            site paths against relative allowlist entries, which
            never match. PR #705 round 4 HIGH fix removes that
            footgun by making ``root`` mandatory.
        allowlist: Permitted entries (typically loaded from
            ``ALLOWLIST_PATH`` via :func:`parse_allowlist`).
    """
    permit: set[tuple[str, int]] = {(entry.rel_path, entry.line) for entry in allowlist}
    root_resolved = root.resolve()
    unauthorised: list[EgressSite] = []
    for site in sites:
        if site.annotation != "primary":
            continue
        try:
            rel = site.file.relative_to(root_resolved)
        except ValueError:
            # Site outside the scan root — treat as unauthorised
            # since the allowlist's relative paths cannot describe
            # paths outside ``root``. Better to fail loudly than
            # to silently treat such a site as either OK or
            # unauthorised based on accidental absolute-path
            # collisions.
            unauthorised.append(site)
            continue
        key = (rel.as_posix(), site.line)
        if key not in permit:
            unauthorised.append(site)
    return unauthorised
