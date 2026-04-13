"""Core analysis logic for Watercooler usage analysis.

Extracted from parse_analysis.py to enable in-process usage by
AnalysisSnapshotDaemon. All functions are pure (read files, return dicts)
with stdlib-only imports.

Public entry points:
    run_analysis(graph_dir, since_dt, include_closed, code_branch) -> dict
    classify_thread_shape(entries) -> dict
    evaluate_rules(metrics, contributors, ...) -> list[dict]
    build_pulse_block(recommendations, ...) -> dict
"""

import functools
import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants (from taxonomy_rules.md — authoritative)
# ---------------------------------------------------------------------------

CANONICAL_ROLES = {"implementer", "planner", "critic", "tester", "pm", "scribe"}
CANONICAL_TYPES = {"Note", "Decision", "Plan", "PR", "Closure"}

SHAPE_NAMES = {
  "S01": "Design clarification",
  "S02": "Documentation / model-positioning",
  "S03": "Protocol hardening",
  "S04": "Architecture rollout",
  "S05": "Operational coordination",
  "S06": "Unclassified / mixed",
}

# Behavioral profile descriptions from taxonomy_rules.md
BEHAVIORAL_PROFILES = {
  frozenset(["S01", "S02"]): "Ideation and ambiguity-disentangling; strong proposal/refinement and critique loops",
  frozenset(["S03", "S04"]): "Execution and stabilization; closure/governance and long protocol threads",
  frozenset(["S04", "S05"]): "Architecture delivery with operational coordination; phased rollout with PM hygiene",
  frozenset(["S01", "S04"]): "Design-to-architecture connector; drives from proposal through structured implementation",
  frozenset(["S02", "S05"]): "Documentation and coordination; scribe-heavy, focused on capturing and closing",
  frozenset(["S03", "S05"]): "Protocol governance; hardening + operational closeout",
}

STALE_DAYS = 14
SCHEMA_VERSION = "1.3"

# Recommendation rule thresholds (from recommendation_rules.md v1.1 — authoritative)
RULE_THRESHOLDS: dict[str, dict[str, Any]] = {
  "R01": {"closure_rate_max": 0.60, "confidence": 0.85, "priority": "actionable"},
  "R02": {"review_capture_rate_max": 0.30, "confidence": 0.80, "priority": "actionable"},
  "R03": {"stalled_min": 1, "confidence": 0.78, "priority": "actionable"},
  "R04": {"planner_tester_gap": 0.40, "confidence": 0.72, "priority": "monitor"},
  "R05": {"closure_rate_max": 0.40, "confidence": 0.90, "priority": "actionable"},
  "R06": {"implementer_min": 0.70, "critic_max": 0.10, "confidence": 0.70, "priority": "monitor"},
  "pairing": {"gap_threshold": 0.15, "fill_threshold": 0.20},
}


# ---------------------------------------------------------------------------
# JSONL streaming (mirrors parse_fingerprint.py)
# ---------------------------------------------------------------------------

def iter_topic_entries(
  graph_dir: Path,
  topic: str,
  parse_errors: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
  """Stream entries from a thread's entries.jsonl file."""
  entries_file = graph_dir / "threads" / topic / "entries.jsonl"
  if not entries_file.exists():
    return
  with open(entries_file, encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if line:
        try:
          yield json.loads(line)
        except json.JSONDecodeError:
          if parse_errors is not None:
            parse_errors.append(topic)
          continue


def list_topics(graph_dir: Path) -> list[str]:
  """List all thread topics with entries.jsonl files."""
  threads_dir = graph_dir / "threads"
  if not threads_dir.exists():
    return []
  return [
    d.name for d in threads_dir.iterdir()
    if d.is_dir() and (d / "entries.jsonl").exists()
  ]


def read_thread_meta(graph_dir: Path, topic: str) -> dict[str, Any]:
  """Read meta.json for a thread."""
  meta_file = graph_dir / "threads" / topic / "meta.json"
  if not meta_file.exists():
    return {}
  try:
    with open(meta_file, encoding="utf-8") as f:
      return json.load(f)
  except (json.JSONDecodeError, OSError):
    return {}


# ---------------------------------------------------------------------------
# Agent normalization (mirrors parse_fingerprint.py)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def normalize_agent(agent: str) -> str:
  """Strip platform prefixes from agent strings.

  Extracts the *innermost* parenthesized tag so that double-wrapped
  names like ``"Daemon ((system))"`` resolve to ``"system"`` rather
  than the malformed ``"(system"``.

  Examples:
    'Claude Code (jay)' -> 'jay'
    'Codex (jay)' -> 'jay'
    'Daemon ((system))' -> 'system'
    '(system' -> 'system'
    'jay' -> 'jay'
  """
  s = agent.strip()
  open_ = s.rfind('(')
  if open_ < 0:
    return s
  close = s.find(')', open_)
  if close < 0:
    # Unclosed paren — strip it and return the remainder.
    extracted = s[open_ + 1:].strip()
    return extracted if extracted else s
  extracted = s[open_ + 1:close].strip()
  return extracted if extracted else s


# ---------------------------------------------------------------------------
# Timestamp parsing (mirrors parse_fingerprint.py)
# ---------------------------------------------------------------------------

def parse_ts(ts: str) -> datetime:
  """Parse ISO 8601 timestamp to UTC-aware datetime."""
  ts = ts.replace("Z", "+00:00")
  try:
    dt = datetime.fromisoformat(ts)
  except ValueError:
    dt = datetime.fromisoformat(ts[:19] + "+00:00")
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  return dt


def ts_to_iso(dt: datetime) -> str:
  """Format datetime to ISO 8601 string."""
  return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Shape classification (thresholds from taxonomy_rules.md)
# ---------------------------------------------------------------------------

def classify_thread_shape(
  entries: list[dict[str, Any]],
) -> dict[str, Any]:
  """Classify a thread's workflow shape using S01-S06 taxonomy.

  Implements the scoring algorithm from taxonomy_rules.md exactly.

  Args:
    entries: All entries in the thread (full corpus, not window-filtered).

  Returns:
    Dict with shape_id, shape_name, confidence, qualifier.
  """
  if len(entries) < 3:
    return {"shape_id": "S06", "shape_name": SHAPE_NAMES["S06"], "confidence": 0.0, "qualifier": ""}

  role_counts: Counter = Counter()
  type_counts: Counter = Counter()
  for e in entries:
    r = e.get("role", "").lower().strip()
    if r in CANONICAL_ROLES:
      role_counts[r] += 1
    t = e.get("entry_type", "Note")
    if t in CANONICAL_TYPES:
      type_counts[t] += 1
    else:
      type_counts["Note"] += 1

  # Use canonical role count (excludes non-canonical) for role shares per taxonomy_rules.md
  total_roles = sum(role_counts.values()) or 1
  total_entries = len(entries) or 1

  def role_share(role: str) -> float:
    return role_counts.get(role, 0) / total_roles

  def type_share(etype: str) -> float:
    return type_counts.get(etype, 0) / total_entries

  has_closure = type_counts.get("Closure", 0) > 0
  has_critic = role_counts.get("critic", 0) > 0
  has_decision = type_counts.get("Decision", 0) > 0
  first_role = entries[0].get("role", "").lower().strip() if entries else ""
  entry_count = len(entries)

  scores: dict[str, float] = {}

  # S01 — Design clarification
  s01 = 0.0
  if has_critic and has_decision:
    s01 += 0.50
  if role_share("planner") > 0.20:
    s01 += 0.15
  if has_closure:
    s01 += 0.15
  if first_role == "planner":
    s01 += 0.10
  if role_share("critic") > 0.15:
    s01 += 0.10
  scores["S01"] = s01

  # S02 — Documentation / model-positioning
  s02 = 0.0
  if role_share("scribe") > 0.25 and type_share("Note") > 0.50:
    s02 += 0.50
  if has_critic:
    s02 += 0.15
  if entry_count >= 4:
    s02 += 0.10
  if type_counts.get("Plan", 0) == 0 and type_counts.get("Decision", 0) == 0:
    s02 += 0.10
  if role_share("critic") > 0.15:
    s02 += 0.10
  scores["S02"] = s02

  # S03 — Protocol hardening
  s03 = 0.0
  if type_share("Plan") > 0.10 and role_share("tester") > 0.10:
    s03 += 0.50
  if role_share("implementer") > 0.15:
    s03 += 0.15
  if has_critic:
    s03 += 0.15
  if has_closure:
    s03 += 0.10
  if entry_count >= 5:
    s03 += 0.10
  scores["S03"] = s03

  # S04 — Architecture rollout
  s04 = 0.0
  if role_share("planner") > 0.25 and role_share("implementer") > 0.20 and type_share("Plan") > 0.10:
    s04 += 0.50
  if entry_count >= 6:
    s04 += 0.15
  if has_closure:
    s04 += 0.15
  if has_critic:
    s04 += 0.10
  if type_share("Decision") > 0.05:
    s04 += 0.10
  scores["S04"] = s04

  # S05 — Operational coordination
  s05 = 0.0
  if role_share("pm") > 0.25 or role_share("scribe") > 0.35:
    s05 += 0.50
  if role_share("tester") > 0.10:
    s05 += 0.15
  if type_share("Note") > 0.60:
    s05 += 0.10
  if type_counts.get("Plan", 0) == 0:
    s05 += 0.10
  if entry_count >= 3:
    s05 += 0.10
  scores["S05"] = s05

  best_shape = max(scores, key=lambda k: scores[k])
  best_score = scores[best_shape]

  # Check for tie
  top_shapes = [k for k, v in scores.items() if v == best_score and v >= 0.50]
  if len(top_shapes) > 1 or best_score < 0.50:
    return {"shape_id": "S06", "shape_name": SHAPE_NAMES["S06"], "confidence": best_score, "qualifier": ""}

  qualifier = ""
  if best_score < 0.70:
    qualifier = "(probable)"

  return {
    "shape_id": best_shape,
    "shape_name": SHAPE_NAMES[best_shape],
    "confidence": round(best_score, 2),
    "qualifier": qualifier,
  }


# ---------------------------------------------------------------------------
# Behavioral profile lookup
# ---------------------------------------------------------------------------

def get_behavioral_profile(
  dominant_shapes: list[str],
  dominant_role: str,
  role_distribution: dict[str, Any],
  type_distribution: dict[str, Any],
) -> str:
  """Derive a behavioral profile description from dominant shapes or role/type mix."""
  if len(dominant_shapes) >= 2:
    key = frozenset(dominant_shapes[:2])
    if key in BEHAVIORAL_PROFILES:
      return BEHAVIORAL_PROFILES[key]

  # Fallback: describe role/type mix directly
  top_roles = sorted(
    [(r, v) for r, v in role_distribution.items() if v > 0],
    key=lambda x: -x[1]
  )[:2]
  top_types = sorted(
    [(t, v) for t, v in type_distribution.items() if v > 0],
    key=lambda x: -x[1]
  )[:2]

  role_desc = " and ".join(r for r, _ in top_roles) if top_roles else dominant_role
  type_desc = " and ".join(t for t, _ in top_types) if top_types else "Note"
  return f"Primarily {role_desc} contributor; dominant entry types: {type_desc}"


# ---------------------------------------------------------------------------
# ISO week helper
# ---------------------------------------------------------------------------

def iso_week_key(dt: datetime) -> str:
  """Return ISO week key as 'YYYY-Www'."""
  iso = dt.isocalendar()
  return f"{iso[0]}-W{iso[1]:02d}"


# ---------------------------------------------------------------------------
# Recommendation rule evaluation (thresholds from recommendation_rules.md)
# ---------------------------------------------------------------------------

def evaluate_rules(
  metrics: dict[str, Any],
  contributors: dict[str, Any],
  window_thread_records: list[dict[str, Any]],
  now: datetime,
  corpus_role_distribution: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
  """Evaluate R01-R06 recommendation rules against computed metrics.

  Args:
    metrics: Dict with closure_rate, review_capture_rate, stalled_thread_count.
    contributors: Per-contributor profile dicts.
    window_thread_records: Per-thread records within the analysis window.
    now: Current UTC datetime for recency checks.
    corpus_role_distribution: Corpus-level role distribution (for R06).

  Returns:
    List of recommendation dicts sorted by confidence descending.
  """
  recommendations: list[dict[str, Any]] = []
  closure_rate = metrics["closure_rate"]
  review_capture_rate = metrics["review_capture_rate"]
  stalled_count = metrics["stalled_thread_count"]

  # R05 and R01 are mutually exclusive — R05 takes precedence when < 0.40
  if closure_rate < RULE_THRESHOLDS["R05"]["closure_rate_max"]:
    recommendations.append({
      "rule_id": "R05",
      "text": f"Closure rate is critically low ({closure_rate * 100:.0f}%). "
              "Add a team norm: every PR merge triggers a Closure entry.",
      "confidence": RULE_THRESHOLDS["R05"]["confidence"],
      "priority": RULE_THRESHOLDS["R05"]["priority"],
      "affected_threads": [],
      "affected_contributors": [],
    })
  elif closure_rate < RULE_THRESHOLDS["R01"]["closure_rate_max"]:
    recommendations.append({
      "rule_id": "R01",
      "text": f"{closure_rate * 100:.0f}% of threads lack Closure entries. "
              "Post a Closure entry at PR merge and thread conclusion.",
      "confidence": RULE_THRESHOLDS["R01"]["confidence"],
      "priority": RULE_THRESHOLDS["R01"]["priority"],
      "affected_threads": [],
      "affected_contributors": [],
    })

  # R02 — low review capture rate
  if review_capture_rate < RULE_THRESHOLDS["R02"]["review_capture_rate_max"]:
    recommendations.append({
      "rule_id": "R02",
      "text": f"Only {review_capture_rate * 100:.0f}% of threads have a critic entry. "
              "Pair a critic pass before closing implementation threads.",
      "confidence": RULE_THRESHOLDS["R02"]["confidence"],
      "priority": RULE_THRESHOLDS["R02"]["priority"],
      "affected_threads": [],
      "affected_contributors": [],
    })

  # R03 — stalled threads (with triage gate)
  if stalled_count >= RULE_THRESHOLDS["R03"]["stalled_min"]:
    seven_days_ago = now - timedelta(days=7)
    stalled_topics = []
    for t in window_thread_records:
      if not t["stalled"]:
        continue
      # Triage gate: suppress if closed
      if t["has_closure"]:
        continue
      # Triage gate: suppress if Decision entry in last 7 days
      # Uses decision_timestamps (all entries, not just window) to avoid
      # missing Decisions posted before --since but within 7 days.
      has_recent_decision = False
      for ts_raw in t.get("decision_timestamps", []):
        try:
          e_dt = parse_ts(ts_raw)
          if e_dt >= seven_days_ago:
            has_recent_decision = True
            break
        except (ValueError, TypeError, AttributeError):
          pass
      if not has_recent_decision:
        stalled_topics.append(t["topic"])

    if stalled_topics:
      recommendations.append({
        "rule_id": "R03",
        "text": f"{len(stalled_topics)} threads have had no activity for > 14 days. "
                "Review whether they are stalled or can be closed.",
        "confidence": RULE_THRESHOLDS["R03"]["confidence"],
        "priority": RULE_THRESHOLDS["R03"]["priority"],
        "affected_threads": stalled_topics,
        "affected_contributors": [],
      })

  # R04 — per-contributor planner/tester gap
  for agent, profile in contributors.items():
    rd = profile.get("role_distribution", {})
    planner_share = rd.get("planner", 0.0)
    tester_share = rd.get("tester", 0.0)
    if planner_share - tester_share > RULE_THRESHOLDS["R04"]["planner_tester_gap"]:
      recommendations.append({
        "rule_id": "R04",
        "text": f"Contributor {agent} has high planner share but low tester coverage. "
                "Pair earlier in thread arcs.",
        "confidence": RULE_THRESHOLDS["R04"]["confidence"],
        "priority": RULE_THRESHOLDS["R04"]["priority"],
        "affected_threads": profile.get("threads_contributed", []),
        "affected_contributors": [agent],
      })

  # R06 — heavy implementer with minimal critic (corpus-level check)
  rd_corpus = corpus_role_distribution or {}
  corpus_implementer = rd_corpus.get("implementer", 0.0)
  corpus_critic = rd_corpus.get("critic", 0.0)
  if (corpus_implementer > RULE_THRESHOLDS["R06"]["implementer_min"]
      and corpus_critic < RULE_THRESHOLDS["R06"]["critic_max"]):
    # Identify contributors who are heavy implementers as affected
    heavy_implementers = [
      agent for agent, profile in contributors.items()
      if profile.get("role_distribution", {}).get("implementer", 0.0)
        > RULE_THRESHOLDS["R06"]["implementer_min"]
    ]
    recommendations.append({
      "rule_id": "R06",
      "text": f"Corpus is {corpus_implementer * 100:.0f}% implementer with only "
              f"{corpus_critic * 100:.0f}% critic coverage. "
              "Schedule structured review passes.",
      "confidence": RULE_THRESHOLDS["R06"]["confidence"],
      "priority": RULE_THRESHOLDS["R06"]["priority"],
      "affected_threads": [],
      "affected_contributors": heavy_implementers,
    })

  recommendations.sort(key=lambda r: -r["confidence"])
  return recommendations


def build_pulse_block(
  recommendations: list[dict[str, Any]],
  window_thread_records: list[dict[str, Any]],
  contributors: dict[str, Any],
  shape_distribution: dict[str, Any],
  stalled_topics: list[str] | None = None,
) -> dict[str, Any]:
  """Assemble the pulse_block contract from evaluated recommendations.

  Args:
    recommendations: Output of evaluate_rules().
    window_thread_records: Per-thread records within the analysis window.
    contributors: Per-contributor profile dicts.
    shape_distribution: Workflow shape distribution from metrics.
    stalled_topics: If provided, only include stalled threads whose topic is
      in this list (typically from R03 affected_threads). None = no filter.

  Returns:
    pulse_block dict conforming to pulse_block_schema.json.
  """
  # coordination_risks: actionable recommendations
  coordination_risks = [
    {
      "rule_id": r["rule_id"],
      "text": r["text"],
      "confidence": r["confidence"],
      "affected_threads": r["affected_threads"],
    }
    for r in recommendations
    if r["priority"] == "actionable"
  ]

  # stalled_threads — filter by R03 post-triage topics when available
  stalled_topics_set = set(stalled_topics) if stalled_topics is not None else None
  stalled_threads = [
    {
      "topic": t["topic"],
      "days_since_last": t["days_since_last"],
      "last_entry_timestamp": t.get("last_entry_timestamp"),
      "contributors": t.get("contributors", []),
    }
    for t in window_thread_records
    if t["stalled"] and (stalled_topics_set is None or t["topic"] in stalled_topics_set)
  ]

  # recommended_pairings: derive from R04/R06 flagged contributors
  flagged_map: dict[str, str] = {}
  for r in recommendations:
    if r["rule_id"] in ("R04", "R06"):
      for c in r["affected_contributors"]:
        flagged_map.setdefault(c, r["rule_id"])

  recommended_pairings: list[dict[str, Any]] = []
  contributor_names = list(contributors.keys())
  for flagged, rule_id in flagged_map.items():
    flagged_rd = contributors.get(flagged, {}).get("role_distribution", {})
    best_partner: str | None = None
    best_score = 0.0
    gap_thresh = RULE_THRESHOLDS["pairing"]["gap_threshold"]
    fill_thresh = RULE_THRESHOLDS["pairing"]["fill_threshold"]
    for candidate in contributor_names:
      if candidate == flagged:
        continue
      cand_rd = contributors.get(candidate, {}).get("role_distribution", {})
      # Complementarity score: sum of role differences where candidate fills gaps
      score = 0.0
      for role in CANONICAL_ROLES:
        gap = flagged_rd.get(role, 0.0)
        fill = cand_rd.get(role, 0.0)
        if gap < gap_thresh and fill > fill_thresh:
          score += fill - gap
      if score > best_score:
        best_score = score
        best_partner = candidate

    if best_partner is not None:
      reason = (
        f"{flagged} flagged by {rule_id}; "
        f"{best_partner} has complementary role coverage"
      )
    else:
      reason = f"{flagged} flagged by {rule_id}; no suitable partner found (single contributor)"

    recommended_pairings.append({
      "contributor": flagged,
      "recommended_partner": best_partner,
      "reason": reason,
      "rule_id": rule_id,
    })

  # top_actions: top 3 by confidence
  top_actions = [
    {
      "rule_id": r["rule_id"],
      "text": r["text"],
      "confidence": r["confidence"],
      "priority": r["priority"],
    }
    for r in recommendations[:3]
  ]

  return {
    "pulse_block_version": "1.0",
    "coordination_risks": coordination_risks,
    "stalled_threads": stalled_threads,
    "recommended_pairings": recommended_pairings,
    "top_actions": top_actions,
    "workflow_shape_distribution": shape_distribution,
  }


# ---------------------------------------------------------------------------
# Main analysis pass
# ---------------------------------------------------------------------------

def run_analysis(
  graph_dir: Path,
  since_dt: datetime,
  include_closed: bool,
  code_branch: str,
) -> dict[str, Any]:
  """Single-pass analysis over the baseline graph.

  Args:
    graph_dir: Baseline graph directory (graph/baseline).
    since_dt: UTC-aware datetime; entries before this are excluded from window.
    include_closed: If True, include closed threads.
    code_branch: Branch filter; '*' means all branches.

  Returns:
    Full analysis result dict matching the output schema.
  """
  enumeration_warnings: list[str] = []
  now = datetime.now(tz=timezone.utc)

  all_topics = list_topics(graph_dir)
  total_threads_all = len(all_topics)  # true total before status filter
  total_entries_scoped = 0
  total_threads_scoped = 0

  # Per-thread accumulation
  thread_records: list[dict[str, Any]] = []

  # Corpus-wide window metrics
  window_role_counts: Counter = Counter()
  window_type_counts: Counter = Counter()

  # Per-contributor accumulation
  contributor_entry_counts: Counter = Counter()
  contributor_role_counts: dict[str, Counter] = defaultdict(Counter)
  contributor_type_counts: dict[str, Counter] = defaultdict(Counter)
  contributor_threads: dict[str, set[str]] = defaultdict(set)
  contributor_weekly: dict[str, Counter] = defaultdict(Counter)
  # Map: contributor -> {topic: shape_id} for threads they contributed to
  contributor_thread_shapes: dict[str, dict[str, str]] = defaultdict(dict)

  for topic in all_topics:
    meta = read_thread_meta(graph_dir, topic)
    status = (meta.get("status") or "open").lower()

    if not include_closed and status not in ("open", "in_review"):
      continue

    total_threads_scoped += 1

    parse_errors: list[str] = []
    all_entries: list[dict[str, Any]] = []

    for entry in iter_topic_entries(graph_dir, topic, parse_errors):
      if code_branch != "*":
        entry_branch = entry.get("code_branch", "")
        if entry_branch != code_branch:
          continue
      all_entries.append(entry)

    if parse_errors:
      enumeration_warnings.append(
        f"{len(parse_errors)} JSONL parse error(s) in thread '{topic}'"
      )

    total_entries_scoped += len(all_entries)

    if not all_entries:
      continue

    # Sort by index for consistent ordering
    all_entries.sort(key=lambda e: e.get("index", 0))

    # Identify window entries
    window_entries = []
    for e in all_entries:
      ts_raw = e.get("timestamp", "")
      if not ts_raw:
        continue
      try:
        dt = parse_ts(ts_raw)
      except (ValueError, TypeError):
        enumeration_warnings.append(f"Bad timestamp in thread '{topic}': {ts_raw!r}")
        continue
      if dt >= since_dt:
        window_entries.append(e)

    # Thread is in-window if it has at least one window entry
    out_of_window = len(window_entries) == 0

    # Per-thread signals (computed over ALL entries, not just window)
    role_counts_thread: Counter = Counter()
    type_counts_thread: Counter = Counter()
    contributors_thread: set = set()
    decision_timestamps: list[str] = []
    last_ts: datetime | None = None

    for e in all_entries:
      r = e.get("role", "").lower().strip()
      if r in CANONICAL_ROLES:
        role_counts_thread[r] += 1
      t = e.get("entry_type", "Note")
      if t in CANONICAL_TYPES:
        type_counts_thread[t] += 1
      agent_raw = e.get("agent", "")
      if agent_raw:
        contributors_thread.add(normalize_agent(agent_raw))
      ts_raw = e.get("timestamp", "")
      if ts_raw:
        try:
          dt = parse_ts(ts_raw)
          if last_ts is None or dt > last_ts:
            last_ts = dt
        except (ValueError, TypeError):
          pass
      # Collect Decision timestamps for R03 triage gate
      if e.get("entry_type") == "Decision":
        ts_raw_d = e.get("timestamp", "")
        if ts_raw_d:
          decision_timestamps.append(ts_raw_d)

    has_closure = type_counts_thread.get("Closure", 0) > 0
    has_critic = role_counts_thread.get("critic", 0) > 0
    has_decision = type_counts_thread.get("Decision", 0) > 0

    days_since_last = 0
    last_ts_iso: str | None = None
    if last_ts is not None:
      last_ts_iso = ts_to_iso(last_ts)
      days_since_last = max(0, (now - last_ts).days)

    stalled = (
      status == "open"
      and days_since_last > STALE_DAYS
      and not has_closure
    )

    # Shape classification (over all entries)
    shape = classify_thread_shape(all_entries)

    # Entry summaries for this thread (window entries only for the entries list)
    entry_list = []
    for e in window_entries:
      entry_list.append({
        "index": e.get("index", 0),
        "role": e.get("role", ""),
        "type": e.get("entry_type", "Note"),
        "agent": normalize_agent(e.get("agent", "")),
        "timestamp": e.get("timestamp", ""),
        "title": e.get("title", ""),
      })

    # Build role and type distribution dicts for the thread
    total_r = sum(role_counts_thread.values()) or 1
    total_t = len(all_entries) or 1
    thread_role_dist = {r: round(role_counts_thread.get(r, 0) / total_r, 3) for r in CANONICAL_ROLES}
    thread_type_dist = {t: round(type_counts_thread.get(t, 0) / total_t, 3) for t in CANONICAL_TYPES}

    thread_records.append({
      "topic": topic,
      "status": status,
      "entry_count": len(window_entries),
      "entry_count_total": len(all_entries),
      "out_of_window": out_of_window,
      "has_closure": has_closure,
      "has_critic": has_critic,
      "has_decision": has_decision,
      "last_entry_timestamp": last_ts_iso,
      "days_since_last": days_since_last,
      "stalled": stalled,
      "workflow_shape": shape,
      "role_distribution": thread_role_dist,
      "entry_type_distribution": thread_type_dist,
      "contributors": sorted(contributors_thread),
      "entries": entry_list,
      "decision_timestamps": decision_timestamps,
    })

    # Accumulate window metrics
    if not out_of_window:
      for e in window_entries:
        r = e.get("role", "").lower().strip()
        if r in CANONICAL_ROLES:
          window_role_counts[r] += 1
        t = e.get("entry_type", "Note")
        if t in CANONICAL_TYPES:
          window_type_counts[t] += 1
        else:
          window_type_counts["Note"] += 1

        agent_raw = e.get("agent", "")
        if agent_raw:
          agent = normalize_agent(agent_raw)
          contributor_entry_counts[agent] += 1
          contributor_threads[agent].add(topic)
          r_for_contrib = e.get("role", "").lower().strip()
          if r_for_contrib in CANONICAL_ROLES:
            contributor_role_counts[agent][r_for_contrib] += 1
          t_for_contrib = e.get("entry_type", "Note")
          bucket = t_for_contrib if t_for_contrib in CANONICAL_TYPES else "Note"
          contributor_type_counts[agent][bucket] += 1
          ts_raw = e.get("timestamp", "")
          if ts_raw:
            try:
              dt = parse_ts(ts_raw)
              contributor_weekly[agent][iso_week_key(dt)] += 1
            except (ValueError, TypeError):
              pass

      # Record thread shape per contributor
      for contrib in contributors_thread:
        contributor_thread_shapes[contrib][topic] = shape["shape_id"]

  # ---------------------------------------------------------------------------
  # Window-scope aggregation
  # ---------------------------------------------------------------------------
  window_thread_records = [t for t in thread_records if not t["out_of_window"]]
  window_thread_count = len(window_thread_records)
  window_entry_count = sum(t["entry_count"] for t in window_thread_records)

  # Closure rate and review capture rate
  if window_thread_count > 0:
    closure_rate = round(
      sum(1 for t in window_thread_records if t["has_closure"]) / window_thread_count, 3
    )
    review_capture_rate = round(
      sum(1 for t in window_thread_records if t["has_critic"]) / window_thread_count, 3
    )
  else:
    closure_rate = 0.0
    review_capture_rate = 0.0

  stalled_count = sum(1 for t in window_thread_records if t["stalled"])

  # Shape distribution across window threads
  shape_counts: Counter = Counter()
  for t in window_thread_records:
    shape_counts[t["workflow_shape"]["shape_id"]] += 1

  shape_distribution: dict[str, Any] = {}
  for sid in ["S01", "S02", "S03", "S04", "S05", "S06"]:
    count = shape_counts.get(sid, 0)
    pct = round(count / window_thread_count, 3) if window_thread_count > 0 else 0.0
    shape_distribution[sid] = {"count": count, "pct": pct}

  # Role and type distributions (window scope, normalized)
  total_window_role = sum(window_role_counts.values()) or 1
  total_window_type = window_entry_count or 1
  role_dist = {r: round(window_role_counts.get(r, 0) / total_window_role, 3) for r in CANONICAL_ROLES}
  type_dist = {t: round(window_type_counts.get(t, 0) / total_window_type, 3) for t in CANONICAL_TYPES}

  # Entry volume by contributor week
  entry_volume: dict[str, dict[str, int]] = {
    agent: dict(weeks) for agent, weeks in contributor_weekly.items()
  }

  # ---------------------------------------------------------------------------
  # Contributor profiles
  # ---------------------------------------------------------------------------
  contributors: dict[str, Any] = {}
  for agent, entry_count in contributor_entry_counts.items():
    role_c = contributor_role_counts[agent]
    type_c = contributor_type_counts[agent]

    total_r = sum(role_c.values()) or 1
    total_t = sum(type_c.values()) or 1

    role_dist_contrib = {r: round(role_c.get(r, 0) / total_r, 3) for r in CANONICAL_ROLES}
    type_dist_contrib = {t: round(type_c.get(t, 0) / total_t, 3) for t in CANONICAL_TYPES}

    dominant_role = max(role_c, key=lambda k: role_c[k]) if role_c else ""
    dominant_type = max(type_c, key=lambda k: type_c[k]) if type_c else "Note"

    threads_list = sorted(contributor_threads[agent])

    # Shape distribution for threads this contributor contributed to
    shape_dist_contrib: Counter = Counter()
    for topic, sid in contributor_thread_shapes[agent].items():
      shape_dist_contrib[sid] += 1

    # Dominant shapes: top 2 by count, must appear in at least 1 thread
    dominant_shapes = [
      sid for sid, _ in shape_dist_contrib.most_common(2)
      if shape_dist_contrib[sid] > 0
    ]

    shape_dist_dict = {sid: shape_dist_contrib.get(sid, 0) for sid in ["S01", "S02", "S03", "S04", "S05", "S06"]}

    behavioral_profile = get_behavioral_profile(
      dominant_shapes, dominant_role, role_dist_contrib, type_dist_contrib
    )

    contributors[agent] = {
      "entry_count": entry_count,
      "role_distribution": role_dist_contrib,
      "type_distribution": type_dist_contrib,
      "threads_contributed": threads_list,
      "dominant_role": dominant_role,
      "dominant_type": dominant_type,
      "shape_distribution": shape_dist_dict,
      "dominant_shapes": dominant_shapes,
      "behavioral_profile": behavioral_profile,
    }

  # ---------------------------------------------------------------------------
  # Rule evaluation and pulse_block assembly
  # ---------------------------------------------------------------------------
  recommendations = evaluate_rules(
    metrics={
      "closure_rate": closure_rate,
      "review_capture_rate": review_capture_rate,
      "stalled_thread_count": stalled_count,
    },
    contributors=contributors,
    window_thread_records=window_thread_records,
    now=now,
    corpus_role_distribution=role_dist,
  )
  # Extract R03 post-triage stalled topics.
  # When stalled threads exist but all were suppressed by triage gates,
  # R03 won't fire — use [] (not None) so the filter excludes them.
  # None = no stalled threads at all, so no filtering needed.
  r03_matches = [r for r in recommendations if r["rule_id"] == "R03"]
  if r03_matches:
    r03_stalled_topics = r03_matches[0]["affected_threads"]
  elif stalled_count > 0:
    r03_stalled_topics = []  # all stalled threads triaged away
  else:
    r03_stalled_topics = None  # no stalled threads exist

  pulse_block = build_pulse_block(
    recommendations=recommendations,
    window_thread_records=window_thread_records,
    contributors=contributors,
    shape_distribution=shape_distribution,
    stalled_topics=r03_stalled_topics,
  )

  # ---------------------------------------------------------------------------
  # Output schema (schema_version 1.3)
  # ---------------------------------------------------------------------------
  return {
    "schema_version": SCHEMA_VERSION,
    "generated_at": ts_to_iso(now),
    "phase_complete": "metrics",
    "window": {
      "since": since_dt.date().isoformat(),
      "until": now.date().isoformat(),
      "include_closed": include_closed,
      "code_branch": code_branch,
    },
    "scope": {
      "included_statuses": ["open", "in_review"] if not include_closed else None,
      "include_closed": include_closed,
    },
    "corpus": {
      "total_threads_all": total_threads_all,
      "scoped_threads": total_threads_scoped,
      "scoped_entries": total_entries_scoped,
      "window_thread_count": window_thread_count,
      "window_entry_count": window_entry_count,
    },
    "window_threads": window_thread_records,
    "contributors": contributors,
    "metrics": {
      "role_distribution": role_dist,
      "entry_type_distribution": type_dist,
      "closure_rate": closure_rate,
      "review_capture_rate": review_capture_rate,
      "entry_volume_by_contributor_week": entry_volume,
      "workflow_shape_distribution": shape_distribution,
      "stalled_thread_count": stalled_count,
    },
    "recommendations": recommendations,
    "pulse_block": pulse_block,
    "enumeration_warnings": enumeration_warnings,
  }
