"""Unit tests for the T2 supersession layer in the triage-owner-threads skill.

The helper lives at ``.claude/skills/triage-owner-threads/triage.py`` — a file
whose directory name has hyphens and is not on ``pythonpath`` — so it is loaded
by file path via ``importlib``. These tests touch no MCP and no git: they
exercise the pure, importable helpers (``load_t2_states``, ``rollup_states``,
``t2_suggestion``, ``t2_rank``, ``t2_annotation``, ``passes_t2_filter``,
``repo_tip_stale``) plus the argparse mutual-exclusion at the CLI boundary.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HELPER = (Path(__file__).resolve().parents[2]
          / ".claude" / "skills" / "triage-owner-threads" / "triage.py")

_spec = importlib.util.spec_from_file_location("triage_helper", HELPER)
triage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(triage)


# --- Test 8: import path (proves the helper is importable without packaging) ---

def test_helper_importable_by_path():
    assert HELPER.exists()
    for fn in ("load_t2_states", "rollup_states", "t2_suggestion", "t2_rank",
               "t2_annotation", "passes_t2_filter", "repo_tip_stale"):
        assert callable(getattr(triage, fn))


# --- Test 1: byte-identical-without-flag guard (the load-bearing regression) ---

def test_no_map_loads_empty():
    assert triage.load_t2_states("") == {}
    assert triage.load_t2_states("/nonexistent/triage-map.json") == {}


def test_empty_map_file_is_not_probed(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"schema": "t2-states/1", "states": {},
                             "run_state": "ok", "matched": 0, "candidates": 0}))
    m = triage.load_t2_states(str(p))
    # candidates == 0 ⇒ t2_probed is False ⇒ no diagnostic, no annotation, no banner.
    assert (m.get("candidates") or 0) == 0


def test_unknown_rows_preserve_stale_order():
    # All-unknown state ⇒ the (rank, stale) key must reduce to today's stale order,
    # stably — including rows that carry a blocking flag (which must NOT reorder
    # when there is no T2 state to override).
    rows = [("a", 30, []), ("b", 10, ["openish"]),
            ("c", 10, []), ("d", 200, ["pending-candidate"])]
    by_stale = sorted(rows, key=lambda r: r[1])
    by_t2 = sorted(rows, key=lambda r: (triage.t2_rank("unknown", r[2]), r[1]))
    assert by_stale == by_t2


def test_annotation_empty_for_unknown_or_absent():
    assert triage.t2_annotation(None, []) == ""
    assert triage.t2_annotation({"state": "unknown", "breakdown": {},
                                 "decision_count": 0}, ["openish"]) == ""


# --- Test 2: rollup precedence (B2) ----------------------------------------

def test_rollup_in_force_dominates():
    r = triage.rollup_states([{"state": "superseded"}, {"state": "in_force"},
                              {"state": "superseded"}])
    assert r["state"] == "in_force"
    assert r["decision_count"] == 3
    assert r["breakdown"]["superseded"] == 2
    assert r["breakdown"]["in_force"] == 1


def test_rollup_all_known_superseded():
    assert triage.rollup_states(
        [{"state": "superseded"}, {"state": "superseded"}])["state"] == "superseded"
    # an unknown alongside all-superseded does not count as "known" ⇒ still superseded
    assert triage.rollup_states(
        [{"state": "superseded"}, {"state": "unknown"}])["state"] == "superseded"


def test_rollup_superseded_partial_mix_is_partial():
    assert triage.rollup_states(
        [{"state": "superseded"},
         {"state": "partially_superseded"}])["state"] == "partially_superseded"
    assert triage.rollup_states(
        [{"state": "partially_superseded"}])["state"] == "partially_superseded"


def test_rollup_unknown_only_and_empty():
    assert triage.rollup_states([{"state": "unknown"}, {}])["state"] == "unknown"
    empty = triage.rollup_states([])
    assert empty["state"] == "unknown"
    assert empty["decision_count"] == 0
    assert empty["as_of"] is None


def test_rollup_as_of_is_max_non_null():
    r = triage.rollup_states([{"state": "superseded", "as_of": "2026-01-01T00:00:00Z"},
                              {"state": "superseded", "as_of": "2026-06-01T00:00:00Z"},
                              {"state": "superseded", "as_of": None}])
    assert r["as_of"] == "2026-06-01T00:00:00Z"


# --- Test 3: stable secondary sort -----------------------------------------

def test_sort_band_order():
    rows = [("u", "unknown", []), ("if", "in_force", []),
            ("sup", "superseded", []), ("part", "partially_superseded", [])]
    order = sorted(rows, key=lambda r: (triage.t2_rank(r[1], r[2]), 0))
    assert [r[0] for r in order] == ["sup", "part", "u", "if"]


def test_sort_stale_within_band():
    rows = [("a", "superseded", [], 50), ("b", "superseded", [], 10)]
    order = sorted(rows, key=lambda r: (triage.t2_rank(r[1], r[2]), r[3]))
    assert [r[0] for r in order] == ["b", "a"]


def test_blocking_flag_keeps_in_force_in_review_band():
    assert triage.t2_rank("in_force", []) == 3
    assert triage.t2_rank("in_force", ["post-closure-activity"]) == 1
    assert triage.t2_rank("unknown", ["pending-candidate"]) == 2  # no state to override
    rows = [("clean", "in_force", []),
            ("flagged", "in_force", ["post-closure-activity"])]
    order = sorted(rows, key=lambda r: (triage.t2_rank(r[1], r[2]), 0))
    assert [r[0] for r in order] == ["flagged", "clean"]


# --- Test 4: suggestion gate (H2 ✅-suppressed / H3 flag-dominant) ----------

def test_suggestion_superseded_never_clean():
    assert triage.t2_suggestion("superseded", None, []) == "❓ review"
    assert triage.t2_suggestion("partially_superseded", None, []) == "❓ review"


def test_suggestion_in_force_demotes():
    assert triage.t2_suggestion("in_force", None, []) == "🚫 likely keep"


def test_suggestion_blocking_flag_forces_review():
    assert triage.t2_suggestion("in_force", None, ["pending-candidate"]) == "❓ review"
    assert triage.t2_suggestion("superseded", None, ["closed-refs-other-thread"]) == "❓ review"


def test_suggestion_unknown_is_empty():
    assert triage.t2_suggestion("unknown", None, []) == ""
    # blocking flag never resurrects a suggestion when there is no usable T2 state
    assert triage.t2_suggestion("unknown", "t2_unavailable", ["pending-candidate"]) == ""


def test_superseder_thread_is_never_marked_clean():
    # superseded + done-signal + no open language ⇒ must NOT be ✅; carries ❓.
    entry = triage.rollup_states([{"state": "superseded"}])
    ann = triage.t2_annotation(entry, [])
    assert "✅" not in ann
    assert "❓ review" in ann
    assert ann.startswith(" · t2:superseded(1/1)")


# --- Test 5: degrade --------------------------------------------------------

def test_degrade_missing_corrupt_nondict(tmp_path):
    assert triage.load_t2_states("/nope/missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    assert triage.load_t2_states(str(bad)) == {}
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]")
    assert triage.load_t2_states(str(arr)) == {}  # non-dict ⇒ {}


def test_degrade_wrong_typed_fields_do_not_crash(tmp_path):
    # A malformed-but-JSON map must normalize, never crash the consumer (P2).
    # main() does: (candidates or 0) > 0  and  states.get(...)  — both must be safe.
    def loaded(obj):
        p = tmp_path / "m.json"
        p.write_text(json.dumps(obj))
        m = triage.load_t2_states(str(p))
        probed = (m.get("candidates") or 0) > 0          # must not raise
        states = m.get("states") or {}
        states.get("watercooler-cloud::x")               # must not raise (dict)
        return m, probed

    # candidates as a numeric string → coerced to int (probe still works)
    m, probed = loaded({"candidates": "5", "states": {}})
    assert m["candidates"] == 5 and probed is True
    # candidates as garbage → 0 (degrade, no probe)
    m, probed = loaded({"candidates": "abc"})
    assert m["candidates"] == 0 and probed is False
    # candidates as bool → 0 (True must not count as a probe)
    m, probed = loaded({"candidates": True})
    assert m["candidates"] == 0 and probed is False
    # states as a list → {} ; tips as a list → {}
    m, _ = loaded({"states": [], "tips": [1, 2], "candidates": 1})
    assert m["states"] == {} and m["tips"] == {}
    # non-dict state entries are dropped
    m, _ = loaded({"states": {"a::t": "notadict", "b::t": {"state": "superseded"}},
                   "candidates": 2})
    assert "a::t" not in m["states"] and m["states"]["b::t"]["state"] == "superseded"
    # run_state wrong type → "ok"
    m, _ = loaded({"run_state": 7, "candidates": 1})
    assert m["run_state"] == "ok"


def test_repo_tip_stale_tolerates_non_dict_tips():
    # tips normalized to {} upstream; repo_tip_stale must not crash on a bare map.
    assert triage.repo_tip_stale({"tip": "abc", "tips": {}}, "r", "def") is True


def test_malformed_inner_breakdown_normalizes_at_boundary(tmp_path):
    # The consumer (t2_annotation) must never crash on a hostile per-entry shape;
    # load_t2_states normalizes each entry to the canonical shape (boundary guard).
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"candidates": 1, "states": {
        "watercooler-cloud::a": {"state": "superseded", "breakdown": "notadict",
                                 "decision_count": "5"},
        "watercooler-cloud::b": {"state": "partially_superseded",
                                 "breakdown": {"superseded": None, "partially_superseded": 1}},
        "watercooler-cloud::c": {"state": "bogus-state"}}}))
    m = triage.load_t2_states(str(p))
    a = m["states"]["watercooler-cloud::a"]
    assert isinstance(a["breakdown"], dict) and a["decision_count"] == 5
    # garbage state coerced to unknown; None count coerced to 0 — both render, no raise
    assert m["states"]["watercooler-cloud::c"]["state"] == "unknown"
    for key in m["states"]:
        triage.t2_annotation(m["states"][key], [])  # must not raise
    assert triage.t2_annotation(a, []).startswith(" · t2:superseded(0/5)")
    # partial: n = superseded(0, was None) + partial(1) = 1
    assert " · t2:partially_superseded(1/0)" in triage.t2_annotation(
        m["states"]["watercooler-cloud::b"], [])


def test_rollup_as_of_mixed_types_does_not_crash():
    # an agent/MCP scratchpad with a non-string as_of must not blow up max()
    r = triage.rollup_states([{"state": "superseded", "as_of": "2026-06-01T00:00:00Z"},
                              {"state": "superseded", "as_of": 5},
                              {"state": "superseded", "as_of": None}])
    assert r["as_of"] == "2026-06-01T00:00:00Z"  # non-str dropped


def test_repo_label_strips_trailing_slash():
    # the <repo>::<topic> key must be slash-robust and match the SKILL's basename
    assert triage.repo_label("/x/watercooler-cloud") == "watercooler-cloud"
    assert triage.repo_label("/x/watercooler-cloud/") == "watercooler-cloud"
    assert triage.repo_label("/x/watercooler-site///") == "watercooler-site"


def test_stale_tip_drops_repo():
    m = {"tip": "aaaaaaa", "states": {"watercooler-cloud::t": {"state": "superseded"}}}
    assert triage.repo_tip_stale(m, "watercooler-cloud", "bbbbbbb") is True
    assert triage.repo_tip_stale(m, "watercooler-cloud", "aaaaaaa") is False
    # per-repo tips override the top-level tip
    m2 = {"tips": {"watercooler-cloud": "ccc"}, "tip": "zzz", "states": {}}
    assert triage.repo_tip_stale(m2, "watercooler-cloud", "ccc") is False
    assert triage.repo_tip_stale(m2, "watercooler-cloud", "zzz") is True
    # missing current tip or empty map ⇒ best-effort: not stale
    assert triage.repo_tip_stale(m, "watercooler-cloud", "") is False
    assert triage.repo_tip_stale({}, "watercooler-cloud", "abc") is False


def test_unavailable_run_state_disables_annotation(tmp_path):
    p = tmp_path / "unavail.json"
    p.write_text(json.dumps({
        "states": {"watercooler-cloud::t": {"state": "unknown", "reason": "t2_unavailable"}},
        "run_state": "unavailable", "matched": 0, "candidates": 5}))
    m = triage.load_t2_states(str(p))
    # main() computes: t2_probed = candidates>0 ; t2_annotate = probed and not unavailable.
    probed = (m.get("candidates") or 0) > 0
    annotate = probed and m.get("run_state") != "unavailable"
    assert probed is True          # diagnostic/banner prints
    assert annotate is False       # no per-row annotations (column dropped)


# --- Test 6: multi-repo keying (B3) ----------------------------------------

def test_multi_repo_keying_no_cross_contamination():
    states = {
        "watercooler-cloud::roles": {
            "state": "superseded", "decision_count": 1,
            "breakdown": {"superseded": 1, "in_force": 0,
                          "partially_superseded": 0, "unknown": 0}},
        "watercooler-site::roles": {
            "state": "in_force", "decision_count": 1,
            "breakdown": {"superseded": 0, "in_force": 1,
                          "partially_superseded": 0, "unknown": 0}},
    }
    cloud = states["watercooler-cloud::roles"]
    site = states["watercooler-site::roles"]
    assert cloud["state"] == "superseded"
    assert site["state"] == "in_force"
    assert "t2:superseded(1/1)" in triage.t2_annotation(cloud, [])
    assert "🚫 likely keep" in triage.t2_annotation(site, [])


# --- Test 7: flag filters ---------------------------------------------------

def test_filter_superseded_keeps_review_band():
    assert triage.passes_t2_filter("superseded", True, False) is True
    assert triage.passes_t2_filter("partially_superseded", True, False) is True
    assert triage.passes_t2_filter("in_force", True, False) is False
    assert triage.passes_t2_filter("unknown", True, False) is False


def test_filter_in_force_only():
    assert triage.passes_t2_filter("in_force", False, True) is True
    assert triage.passes_t2_filter("superseded", False, True) is False
    assert triage.passes_t2_filter("unknown", False, True) is False


def test_filter_none_passes_all():
    for s in triage.T2_STATES:
        assert triage.passes_t2_filter(s, False, False) is True


def test_filter_flags_mutually_exclusive_at_cli():
    # argparse rejects both flags before any git/MCP work happens.
    r = subprocess.run(
        [sys.executable, str(HELPER), "triage", "/tmp/nonexistent-wt",
         "--owner", "x", "--t2-superseded", "--t2-in-force"],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert "not allowed with" in r.stderr or "mutually exclusive" in r.stderr.lower()


# --- Test 9: decision_bearing (the t2-plan candidate filter) ----------------

def test_decision_bearing():
    has = [{"entry_type": "Note"}, {"entry_type": "Decision"}, {"entry_type": "Closure"}]
    none = [{"entry_type": "Note"}, {"entry_type": "Closure"}]
    assert triage.decision_bearing(has) is True
    assert triage.decision_bearing(none) is False
    assert triage.decision_bearing([]) is False


# --- Test 10: _decisions_from_response (MCP response shape tolerance) --------

def test_decisions_from_response_result_wrapper():
    # The MCP tool result may arrive wrapped as {"result": "<json-str>"}.
    inner = json.dumps({"decisions": [{"supersession": {"state": "superseded"}}]})
    ds = triage._decisions_from_response({"result": inner})
    assert len(ds) == 1 and ds[0]["supersession"]["state"] == "superseded"


def test_decisions_from_response_raw_dict():
    ds = triage._decisions_from_response({"decisions": [{"x": 1}, {"y": 2}]})
    assert len(ds) == 2


def test_decisions_from_response_degrades_to_empty():
    assert triage._decisions_from_response({}) == []                       # no key
    assert triage._decisions_from_response({"decisions": "nope"}) == []    # wrong type
    assert triage._decisions_from_response([1, 2, 3]) == []                # non-dict
    assert triage._decisions_from_response({"result": "{not json"}) == []  # bad inner
    # a present 'decisions' key wins over the result-wrapper path
    assert triage._decisions_from_response({"decisions": [{"a": 1}], "result": "x"}) == [{"a": 1}]


# --- Test 11: build_map_from_responses (the --t2-responses assembler) -------

def _write_plan(d, planned, **extra):
    plan = {"schema": "t2-plan/1", "session_anchor": "2026-06-28T00:00:00+00:00",
            "tips": {"watercooler-cloud": "abc123"}, "tip": "abc123",
            "run_state": "ok", "planned": planned}
    plan.update(extra)
    (d / "plan.json").write_text(json.dumps(plan))
    return plan


def _resp(d, name, decisions):
    (d / name).write_text(json.dumps({"decisions": decisions}))


def test_build_map_happy_path(tmp_path):
    _write_plan(tmp_path, [
        {"repo": "watercooler-cloud", "topic": "roles", "code_path": "/x",
         "outfile": "watercooler-cloud__roles.json"},
        {"repo": "watercooler-cloud", "topic": "infra", "code_path": "/x",
         "outfile": "watercooler-cloud__infra.json"}])
    _resp(tmp_path, "watercooler-cloud__roles.json",
          [{"supersession": {"state": "superseded", "as_of": "2026-05-01T00:00:00Z"}}])
    _resp(tmp_path, "watercooler-cloud__infra.json",
          [{"supersession": {"state": "in_force", "as_of": None}}])
    m = triage.build_map_from_responses(str(tmp_path))
    assert m["candidates"] == 2 and m["matched"] == 2 and m["run_state"] == "ok"
    assert m["states"]["watercooler-cloud::roles"]["state"] == "superseded"
    assert m["states"]["watercooler-cloud::infra"]["state"] == "in_force"
    # session_anchor is carried through for the Step-2b provenance gate
    assert m["session_anchor"] == "2026-06-28T00:00:00+00:00"
    # tips carried through so the stale guard can fire downstream
    assert m["tips"]["watercooler-cloud"] == "abc123"


def test_build_map_missing_response_is_unknown_and_partial(tmp_path):
    _write_plan(tmp_path, [
        {"repo": "watercooler-cloud", "topic": "ok", "code_path": "/x",
         "outfile": "watercooler-cloud__ok.json"},
        {"repo": "watercooler-cloud", "topic": "gone", "code_path": "/x",
         "outfile": "watercooler-cloud__gone.json"}])
    _resp(tmp_path, "watercooler-cloud__ok.json", [{"supersession": {"state": "superseded"}}])
    # 'gone' response file intentionally absent
    m = triage.build_map_from_responses(str(tmp_path))
    gone = m["states"]["watercooler-cloud::gone"]
    assert gone["state"] == "unknown" and gone["reason"] == "response_missing_or_unreadable"
    assert m["run_state"] == "partial"          # one resolved < two valid
    assert m["matched"] == 1


def test_build_map_no_decisions_is_unknown(tmp_path):
    _write_plan(tmp_path, [
        {"repo": "watercooler-cloud", "topic": "empty", "code_path": "/x",
         "outfile": "watercooler-cloud__empty.json"}])
    _resp(tmp_path, "watercooler-cloud__empty.json", [])
    m = triage.build_map_from_responses(str(tmp_path))
    v = m["states"]["watercooler-cloud::empty"]
    assert v["state"] == "unknown" and v["reason"] == "no_decisions_in_response"
    assert m["run_state"] == "unavailable"      # resolved == 0


def test_build_map_no_plan_or_malformed_degrades_to_empty(tmp_path):
    assert triage.build_map_from_responses(str(tmp_path)) == {}      # no plan.json
    (tmp_path / "plan.json").write_text("{not json")
    assert triage.build_map_from_responses(str(tmp_path)) == {}      # corrupt plan
    (tmp_path / "plan.json").write_text(json.dumps({"planned": "nope"}))
    assert triage.build_map_from_responses(str(tmp_path)) == {}      # planned wrong type


def test_build_map_skips_invalid_planned_rows(tmp_path):
    _write_plan(tmp_path, [
        {"repo": "watercooler-cloud", "topic": "good", "code_path": "/x",
         "outfile": "watercooler-cloud__good.json"},
        {"repo": "watercooler-cloud"},          # missing topic — dropped from 'valid'
        "notadict"])
    _resp(tmp_path, "watercooler-cloud__good.json", [{"supersession": {"state": "in_force"}}])
    m = triage.build_map_from_responses(str(tmp_path))
    assert m["candidates"] == 1                 # only the valid row counts
    assert list(m["states"]) == ["watercooler-cloud::good"]


def test_build_map_keying_and_result_wrapper(tmp_path):
    # response saved in the {"result": "<json-str>"} wrapper form must still parse,
    # and the key must be <repo>::<topic> (cross-repo collision-safe).
    _write_plan(tmp_path, [
        {"repo": "watercooler-site", "topic": "roles", "code_path": "/x",
         "outfile": "watercooler-site__roles.json"}])
    inner = json.dumps({"decisions": [{"supersession": {"state": "superseded"}}]})
    (tmp_path / "watercooler-site__roles.json").write_text(json.dumps({"result": inner}))
    m = triage.build_map_from_responses(str(tmp_path))
    assert "watercooler-site::roles" in m["states"]
    assert m["states"]["watercooler-site::roles"]["state"] == "superseded"


# --- Test 12: write_t2_plan (manifest emission + candidate filtering) -------

def test_write_t2_plan_filters_and_caps(tmp_path, monkeypatch, capsys):
    # Exercise the planner without touching git/MCP: stub the git-facing helpers
    # and feed synthetic owned-open threads. Only Decision-bearing Tier-1/2 topics
    # should be planned; the cap splits the remainder into 'omitted'.
    monkeypatch.setattr(triage, "fetch", lambda wt: None)
    monkeypatch.setattr(triage, "freshness", lambda wt: True)
    monkeypatch.setattr(triage, "git", lambda wt, *a: "deadbeef0\n")
    dec = {"entry_type": "Decision", "agent": "Codex (caleb)", "title": "d", "body": "x"}
    clo = {"entry_type": "Closure", "agent": "Codex (caleb)", "title": "c", "body": "done"}
    note = {"entry_type": "Note", "agent": "Codex (caleb)", "title": "n",
            "body": "todo next steps"}

    def fake_owned_open(wt, owner):
        yield "A-dec-tier1", {"status": "OPEN"}, [dec, clo]   # decision-bearing, tier1
        yield "B-nodec-tier1", {"status": "OPEN"}, [clo]      # tier1 but NOT decision-bearing
        yield "C-dec-tier2", {"status": "OPEN"}, [dec, note]  # decision-bearing, tier2
    monkeypatch.setattr(triage, "owned_open", fake_owned_open)

    out = tmp_path / "plan"
    triage.write_t2_plan(["/x/watercooler-cloud"], "caleb", ["/repo/root"], str(out), cap=40)
    plan = json.loads((out / "plan.json").read_text())
    topics = sorted(p["topic"] for p in plan["planned"])
    assert topics == ["A-dec-tier1", "C-dec-tier2"]          # B excluded (no Decision)
    assert all(p["code_path"] == "/repo/root" for p in plan["planned"])
    assert plan["session_anchor"] and plan["tips"]["watercooler-cloud"] == "deadbeef0"
    # printed call lines + re-run hint are present for the agent to follow
    cap = capsys.readouterr().out
    assert "watercooler_list_decisions(topic=\"A-dec-tier1\"" in cap
    assert "--t2-responses" in cap

    # cap=1 pushes the second candidate into 'omitted', still enumerated
    triage.write_t2_plan(["/x/watercooler-cloud"], "caleb", ["/repo/root"], str(out), cap=1)
    plan = json.loads((out / "plan.json").read_text())
    assert len(plan["planned"]) == 1 and len(plan["omitted"]) == 1
    assert plan["run_state"] == "partial"


def test_write_t2_plan_code_path_default_is_worktree(tmp_path, monkeypatch):
    # With no --code-path, code_path defaults to the worktree path (per-worktree).
    monkeypatch.setattr(triage, "fetch", lambda wt: None)
    monkeypatch.setattr(triage, "freshness", lambda wt: True)
    monkeypatch.setattr(triage, "git", lambda wt, *a: "deadbeef0\n")
    dec = {"entry_type": "Decision", "agent": "Codex (caleb)", "title": "d", "body": "x"}
    clo = {"entry_type": "Closure", "agent": "Codex (caleb)", "title": "c", "body": "done"}
    monkeypatch.setattr(triage, "owned_open",
                        lambda wt, owner: iter([("t", {"status": "OPEN"}, [dec, clo])]))
    out = tmp_path / "plan"
    triage.write_t2_plan(["/x/watercooler-cloud"], "caleb", [], str(out))
    plan = json.loads((out / "plan.json").read_text())
    assert plan["planned"][0]["code_path"] == "/x/watercooler-cloud"


# --- Test 13: CLI guards for the new modes/flags ---------------------------

def test_t2_plan_requires_out():
    r = subprocess.run([sys.executable, str(HELPER), "t2-plan", "/tmp/nonexistent-wt",
                        "--owner", "x"], capture_output=True, text=True)
    assert r.returncode != 0
    assert "--out" in r.stdout + r.stderr


def test_t2_states_and_responses_mutually_exclusive_at_cli():
    r = subprocess.run([sys.executable, str(HELPER), "triage", "/tmp/nonexistent-wt",
                        "--owner", "x", "--t2-states", "/a", "--t2-responses", "/b"],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "mutually exclusive" in (r.stdout + r.stderr).lower()
