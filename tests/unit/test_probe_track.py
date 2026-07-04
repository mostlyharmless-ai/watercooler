"""Pure-Python unit coverage for the probe benchmark track (no Docker).

Guards the deterministic scorer (word-boundary matching — #913 review), the probe
loader's required-key validation, and the provenance manifest's threads-ref
detection that drives the isolation abort.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.benchmarks.wcbench.tracks.probe import (
    _BRIDGE_MARKER,
    _MEMORY_BRIDGE,
    _load_probe,
    _provenance_manifest,
    _score_answer,
    _term_present,
    _write_memory_bridge,
)


# --------------------------------------------------------------------------- #
# _term_present / _score_answer — word-boundary scoring
# --------------------------------------------------------------------------- #

def test_term_present_word_boundary_rejects_substring():
    # The whole point: "View" must NOT be satisfied by "overview"/"review".
    assert _term_present("View", "the View base class")
    assert not _term_present("View", "an async overview of the change")
    assert not _term_present("View", "a quick review and preview")


def test_term_present_matches_punctuated_token():
    assert _term_present("#2080", "fixed in #2080.")
    assert _term_present("async", "made it async)")
    assert not _term_present("async", "asynchronous helper")  # boundary, not prefix


def test_score_answer_substring_term_is_not_credited():
    gt = {"required_substrings": ["async", "View", "migration"]}
    # Contains async + migration, but "View" only inside "overview" -> NOT correct.
    res = _score_answer("An async overview of the migration.", gt)
    assert res["correct"] is False
    assert "View" in res["missing_substrings"]
    assert set(res["matched_substrings"]) == {"async", "migration"}


def test_score_answer_all_terms_present_is_correct():
    gt = {
        "required_substrings": ["async", "View", "migration"],
        "cited_entries": ["#2080"],
    }
    res = _score_answer(
        "The async View base class drove the migration (see #2080).", gt
    )
    assert res["correct"] is True
    assert res["missing_substrings"] == []
    assert res["cited"] is True
    assert "#2080" in res["citations_found"]


# --------------------------------------------------------------------------- #
# _load_probe — required-key validation
# --------------------------------------------------------------------------- #

_VALID = {
    "name": "p", "question": "q?", "subject_repo": "https://x/y",
    "threads_ref": "watercooler/threads",
    "ground_truth": {"required_substrings": ["a"]},
}


def test_load_probe_accepts_valid(tmp_path):
    p = tmp_path / "probe.json"
    p.write_text(json.dumps(_VALID), encoding="utf-8")
    assert _load_probe(p)["name"] == "p"


@pytest.mark.parametrize("missing", ["name", "question", "subject_repo", "threads_ref", "ground_truth"])
def test_load_probe_rejects_missing_key(tmp_path, missing):
    bad = {k: v for k, v in _VALID.items() if k != missing}
    p = tmp_path / "probe.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match=missing):
        _load_probe(p)


# --------------------------------------------------------------------------- #
# _provenance_manifest — threads-ref detection (drives the isolation abort)
# --------------------------------------------------------------------------- #

def _init_repo(d: Path):
    from git import Repo

    repo = Repo.init(d)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    (d / "f.txt").write_text("x", encoding="utf-8")
    repo.git.add("-A")
    repo.index.commit("init")
    return repo


def test_manifest_clean_control_clone_has_no_threads_ref(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir)
    m = _provenance_manifest(
        condition="control", repo_dir=repo_dir,
        threads_mounted=False, mcp_available=False, image="img",
    )
    assert m["watercooler_threads_ref_present"] is False


def test_manifest_detects_leaked_threads_ref(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = _init_repo(repo_dir)
    # A leaked threads ref is exactly what the isolation abort must catch.
    repo.git.branch("watercooler/threads")
    m = _provenance_manifest(
        condition="control", repo_dir=repo_dir,
        threads_mounted=False, mcp_available=False, image="img",
    )
    assert m["watercooler_threads_ref_present"] is True


# --------------------------------------------------------------------------- #
# _write_memory_bridge — onboarding agent-context bridge (treatment only)
# --------------------------------------------------------------------------- #

def test_memory_bridge_writes_single_harness_file(tmp_path):
    # openhands consumes AGENTS.md; we write ONLY that (no duplicate CLAUDE.md).
    rec = _write_memory_bridge(tmp_path, "openhands")
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MEMORY_BRIDGE
    assert not (tmp_path / "CLAUDE.md").exists()
    assert rec["file"] == "AGENTS.md" and rec["mode"] == "created"
    import hashlib
    assert rec["sha256"] == hashlib.sha256(_MEMORY_BRIDGE.encode()).hexdigest()


def test_memory_bridge_claude_code_writes_claude_md(tmp_path):
    rec = _write_memory_bridge(tmp_path, "claude-code")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _MEMORY_BRIDGE
    assert not (tmp_path / "AGENTS.md").exists()
    assert rec["file"] == "CLAUDE.md"


def test_memory_bridge_appends_not_clobbers_subject_file(tmp_path):
    # A subject repo that ships its OWN AGENTS.md must keep its content.
    native = "# datasette agents\n\nRun the tests with pytest.\n"
    (tmp_path / "AGENTS.md").write_text(native, encoding="utf-8")
    rec = _write_memory_bridge(tmp_path, "openhands")
    merged = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert native.strip() in merged          # subject content preserved
    assert _MEMORY_BRIDGE in merged          # our section appended
    assert rec["mode"] == "appended"
    # Idempotent: a second write is a no-op (marker already present).
    assert _write_memory_bridge(tmp_path, "openhands")["mode"] == "already-present"


def test_bridge_marker_anchors_isolation_check():
    # The marker must be IN our bridge (so the control-isolation check can match
    # it) and is a plain HTML comment carrying no answer tokens.
    assert _BRIDGE_MARKER in _MEMORY_BRIDGE
    assert _BRIDGE_MARKER.startswith("<!--") and _BRIDGE_MARKER.endswith("-->")
    # A subject's own agent-context file (no marker) must NOT look like our bridge.
    assert _BRIDGE_MARKER not in "# some repo's own AGENTS.md\nrun pytest\n"


def test_memory_bridge_is_answer_free():
    """The bridge is the sign on the cabinet, never the answer.

    It must name no PR/entry/segment/rationale token that could leak ground
    truth into treatment — only generic 'consult the threads' guidance.
    """
    low = _MEMORY_BRIDGE.lower()
    # No issue/PR citations.
    import re
    assert not re.search(r"#\d{2,}", _MEMORY_BRIDGE), "bridge must not cite PR/issue numbers"
    # No datasette-specific or answer-specific tokens from any probe ground truth.
    for forbidden in ("datasette", "baseview", "dataview", "async view", "#2080", "manytomany"):
        assert forbidden not in low, f"bridge leaks answer token: {forbidden!r}"
    # It DOES point at the watercooler tools (its whole job).
    assert "watercooler_" in _MEMORY_BRIDGE
    assert "/data/threads" in _MEMORY_BRIDGE


def test_manifest_records_bridge_presence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    rec = _write_memory_bridge(repo)
    m = _provenance_manifest(
        condition="treatment", repo_dir=repo,
        threads_mounted=True, mcp_available=True, image="img",
        harness="openhands", memory_bridge=rec,
    )
    assert m["memory_bridge_present"] is True
    assert m["memory_bridge"]["sha256"] == rec["sha256"]
    assert m["harness"] == "openhands"


def test_manifest_no_bridge_when_absent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    m = _provenance_manifest(
        condition="control", repo_dir=repo,
        threads_mounted=False, mcp_available=False, image="img",
    )
    assert m["memory_bridge_present"] is False
    assert m["memory_bridge"] is None


# --------------------------------------------------------------------------- #
# A3-raw arm (entry 53 §4): custody stripper + parity-of-access pointer
# --------------------------------------------------------------------------- #

from tests.benchmarks.wcbench.tracks.probe import (  # noqa: E402
    _RAW_POINTER,
    _RAW_POINTER_MARKER,
    _strip_custody_scaffolding,
    _write_raw_pointer,
)


def _fake_threads_clone(root: Path, topic: str, rows: list[dict]) -> Path:
    """Build a minimal orphan-branch checkout shape for the stripper."""
    tdir = root / "graph" / "baseline" / "threads" / topic
    tdir.mkdir(parents=True)
    (tdir / "entries.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )
    # Custody artifacts that must NOT be exported.
    (tdir / "edges.jsonl").write_text(
        json.dumps({"source": "e1", "target": "e2", "type": "superseded_by"}),
        encoding="utf-8",
    )
    (tdir / "meta.json").write_text(
        json.dumps({"topic": topic, "status": "OPEN", "ball": "alice"}),
        encoding="utf-8",
    )
    return root


def _entry_row(i: int, **over) -> dict:
    row = {
        "id": f"entry:{i}",
        "type": "entry",
        "entry_id": f"01FAKEENTRY{i:014d}",
        "entry_type": "Decision" if i == 2 else "Note",
        "agent": "Alice (dev)",
        "role": "planner",
        "timestamp": f"2026-01-0{i + 1}T00:00:00+00:00",
        "thread_topic": "topic-a",
        "index": i,
        "title": f"step {i} of the design",
        "body": f"Spec: planner-architecture\n\nWe considered approach {i}.\n\n<!-- Entry-ID: 01FAKEENTRY{i:014d} -->",
        "code_branch": "main",
    }
    row.update(over)
    return row


def test_stripper_drops_custody_scaffolding_keeps_prose(tmp_path):
    threads = _fake_threads_clone(
        tmp_path / "threads", "topic-a", [_entry_row(0), _entry_row(1), _entry_row(2)]
    )
    dest = tmp_path / "raw"
    rec = _strip_custody_scaffolding(threads, dest)

    out = (dest / "topic-a.md").read_text(encoding="utf-8")
    # Prose, titles, speakers: KEPT.
    assert "We considered approach 1." in out
    assert "step 1 of the design" in out
    assert "Alice (dev)" in out
    # Custody scaffolding: STRIPPED.
    assert "Spec:" not in out
    assert "Entry-ID" not in out
    assert "01FAKEENTRY" not in out          # provenance ids
    assert "planner" not in out              # roles
    assert "2026-01-0" not in out            # timestamps
    assert "superseded_by" not in out        # edges.jsonl not exported
    assert "OPEN" not in out                 # meta.json not exported
    # Entry typing must not survive as a label (the body/titles here don't
    # contain the word, so any occurrence would be a leaked type field).
    assert "Decision" not in out
    # Audit record pins the export.
    assert rec["topic_count"] == 1
    assert rec["entry_count"] == 3
    assert rec["files"] == ["topic-a.md"]
    assert len(rec["sha256"]) == 64


def test_stripper_preserves_chronological_order(tmp_path):
    rows = [_entry_row(2), _entry_row(0), _entry_row(1)]  # shuffled on disk
    threads = _fake_threads_clone(tmp_path / "threads", "topic-a", rows)
    out = ""
    _strip_custody_scaffolding(threads, tmp_path / "raw")
    out = (tmp_path / "raw" / "topic-a.md").read_text(encoding="utf-8")
    assert out.index("approach 0") < out.index("approach 1") < out.index("approach 2")


def test_stripper_refuses_empty_export(tmp_path):
    threads = tmp_path / "threads"
    (threads / "graph" / "baseline" / "threads").mkdir(parents=True)
    with pytest.raises(SystemExit):
        _strip_custody_scaffolding(threads, tmp_path / "raw")


def test_raw_pointer_writes_single_harness_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _write_raw_pointer(repo, "openhands")
    assert rec["file"] == "AGENTS.md"
    assert (repo / "AGENTS.md").exists()
    assert not (repo / "CLAUDE.md").exists()
    assert _RAW_POINTER_MARKER in (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_raw_pointer_appends_not_clobbers_subject_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# subject's own context\nrun pytest\n", encoding="utf-8")
    rec = _write_raw_pointer(repo, "openhands")
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert rec["mode"] == "appended"
    assert text.startswith("# subject's own context")
    assert _RAW_POINTER_MARKER in text
    # Idempotent on second write.
    rec2 = _write_raw_pointer(repo, "openhands")
    assert rec2["mode"] == "already-present"


def test_raw_pointer_marker_distinct_from_bridge_marker():
    # The isolation checks key on markers; they must never alias each other.
    assert _RAW_POINTER_MARKER != _BRIDGE_MARKER
    assert _RAW_POINTER_MARKER not in _MEMORY_BRIDGE
    assert _BRIDGE_MARKER not in _RAW_POINTER


def test_raw_pointer_is_answer_free_and_custody_free():
    """Parity-of-access without structure leakage.

    The pointer must (a) leak no probe ground-truth tokens, exactly like the
    bridge, and (b) carry NO custody vocabulary — no MCP tools, no entry
    typing/status/supersession words — because the raw arm exists to measure
    the marginal value of precisely that structure.
    """
    low = _RAW_POINTER.lower()
    import re as _re
    assert not _re.search(r"#\d{2,}", _RAW_POINTER), "pointer must not cite PR/issue numbers"
    for forbidden in ("datasette", "baseview", "dataview", "async view", "#2080", "manytomany"):
        assert forbidden not in low, f"pointer leaks answer token: {forbidden!r}"
    for custody_term in (
        "watercooler_", "mcp", "decision", "candidate", "ratified",
        "supersession", "supersede", "provenance", "entry_type", "graph",
    ):
        assert custody_term not in low, f"pointer leaks custody vocabulary: {custody_term!r}"
    # It DOES point at the transcripts (its whole job) with bridge-equivalent
    # directive strength (the same intent-first sentence, modulo line wrap).
    assert "/data/transcripts" in _RAW_POINTER
    _flat = lambda s: " ".join(s.split())  # noqa: E731 - collapse line wraps
    parity_sentence = "Do not infer design constraints from code alone."
    assert parity_sentence in _flat(_RAW_POINTER)
    assert parity_sentence in _flat(_MEMORY_BRIDGE)


def test_manifest_records_raw_fields(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    threads = _fake_threads_clone(tmp_path / "threads", "topic-a", [_entry_row(0)])
    raw_rec = _strip_custody_scaffolding(threads, tmp_path / "raw")
    ptr_rec = _write_raw_pointer(repo, "openhands")
    m = _provenance_manifest(
        condition="raw", repo_dir=repo,
        threads_mounted=False, mcp_available=False, image="img",
        raw_transcripts=raw_rec, raw_pointer=ptr_rec,
    )
    assert m["raw_transcripts_mounted_at_/data/transcripts"] is True
    assert m["raw_transcripts"]["sha256"] == raw_rec["sha256"]
    assert m["raw_pointer_present"] is True
    assert m["watercooler_mcp_available"] is False
    assert m["threads_mounted_at_/data/threads"] is False


# --------------------------------------------------------------------------- #
# PR #945 review fixes: arm precedence, typed-title normalization, skip count
# --------------------------------------------------------------------------- #

from tests.benchmarks.wcbench.tracks.probe import _resolve_arms  # noqa: E402


def test_resolve_arms_cli_beats_probe_file():
    # Review finding 2: the operator's explicit flag must win — the inverse
    # silently turns a requested 2-arm run into a 3-arm (50% more LLM cost).
    assert _resolve_arms("control,treatment", "control,raw,treatment") == [
        "control", "treatment",
    ]


def test_resolve_arms_file_then_default():
    assert _resolve_arms(None, "control,raw,treatment") == ["control", "raw", "treatment"]
    assert _resolve_arms(None, None) == ["control", "treatment"]


def test_resolve_arms_canonical_order_and_validation():
    assert _resolve_arms("treatment,raw,control", None) == ["control", "raw", "treatment"]
    with pytest.raises(SystemExit):
        _resolve_arms("control,bogus", None)
    with pytest.raises(SystemExit):
        _resolve_arms(" , ", None)


def test_stripper_normalizes_typed_title_prefixes(tmp_path):
    # Review finding 3: "Decision: …" / "[Candidate] …" titles are entry typing
    # rendered into prose; the prefix is dropped, the rest kept verbatim.
    rows = [
        _entry_row(0, title="Decision: adopt approach zero"),
        _entry_row(1, title="[Candidate] maybe approach one"),
        _entry_row(2, title="plan: sketch for approach two"),
    ]
    threads = _fake_threads_clone(tmp_path / "threads", "topic-a", rows)
    _strip_custody_scaffolding(threads, tmp_path / "raw")
    out = (tmp_path / "raw" / "topic-a.md").read_text(encoding="utf-8")
    assert "adopt approach zero" in out
    assert "maybe approach one" in out
    assert "sketch for approach two" in out
    assert "Decision:" not in out
    assert "[Candidate]" not in out
    assert "plan:" not in out.lower().replace("sketch for approach two", "")
    # The team's own vocabulary INSIDE prose is kept (normalization, not
    # laundering): a body that says "supersedes" survives.
    rows2 = [_entry_row(0, body="This supersedes the older sketch.")]
    threads2 = _fake_threads_clone(tmp_path / "t2", "topic-b", rows2)
    _strip_custody_scaffolding(threads2, tmp_path / "raw2")
    out2 = (tmp_path / "raw2" / "topic-b.md").read_text(encoding="utf-8")
    assert "supersedes" in out2


def test_stripper_counts_skipped_malformed_rows(tmp_path):
    # Review finding 4 / no-silent-caps: malformed rows are dropped but COUNTED
    # so the manifest discloses any prose asymmetry vs the treatment arm.
    tdir = tmp_path / "threads" / "graph" / "baseline" / "threads" / "topic-a"
    tdir.mkdir(parents=True)
    good = json.dumps(_entry_row(0))
    (tdir / "entries.jsonl").write_text(
        good + "\n{not json}\n" + json.dumps(_entry_row(1)) + "\n{also bad",
        encoding="utf-8",
    )
    rec = _strip_custody_scaffolding(tmp_path / "threads", tmp_path / "raw")
    assert rec["entry_count"] == 2
    assert rec["rows_skipped"] == 2
