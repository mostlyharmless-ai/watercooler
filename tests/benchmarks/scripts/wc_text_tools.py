"""Host-side dispatcher for prompt-only Watercooler tool usage.

This module implements a strict, text-command interface (`wc-*`) that can be
emitted by models without native tool/function-calling support. The runner
intercepts these commands and executes them on the host, returning concise,
token-budgeted text back to the agent as an observation.

Supported commands (strict):
- wc-search "<query>"
- wc-smart-query "<query>"
- wc-read-thread <topic>
- wc-get-entry <topic> <index>
- wc-t2-facts [--active-only] [--start-time <iso>] [--end-time <iso>] "<query>"
- wc-provenance <episode_uuid>
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence


TierCeiling = Literal["T1", "T2", "T3"]


@dataclass
class WcCommandResult:
  tool: str
  output: str
  ok: bool = True
  entry_ids: list[str] = field(default_factory=list)
  meta: dict[str, object] = field(default_factory=dict)


def _estimate_tokens(text: str) -> int:
  # Rough approximation (matches existing patterns in benchmarks).
  return int(len(text.split()) / 0.75) if text else 0


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
  if token_budget <= 0:
    return ""
  if _estimate_tokens(text) <= token_budget:
    return text

  # Cheap truncation: keep head+tail.
  target_chars = max(200, int(token_budget * 4))  # heuristic: ~4 chars/token
  if len(text) <= target_chars:
    return text
  head = text[: int(target_chars * 0.65)]
  tail = text[-int(target_chars * 0.35) :]
  return head.rstrip() + "\n\n... (truncated) ...\n\n" + tail.lstrip()


def _format_search_results(results: object, token_budget: int) -> tuple[str, list[str]]:
  # Import lazily to keep module import cost low.
  from watercooler.baseline_graph.search import SearchResults

  if not isinstance(results, SearchResults):
    return "No results (search failed).", []

  lines: list[str] = []
  entry_ids: list[str] = []
  for i, r in enumerate(results.results[: results.query.limit]):
    if r.entry:
      entry_ids.append(r.entry.entry_id)
      snippet = (r.entry.summary or r.entry.body or "").strip().replace("\n", " ")
      snippet = snippet[:220] + ("…" if len(snippet) > 220 else "")
      lines.append(
        f"{i+1}. [{r.score:.3f}] {r.entry.title} (entry_id={r.entry.entry_id}, topic={r.entry.thread_topic})\n"
        f"   {snippet}"
      )
    elif r.thread:
      snippet = (r.thread.summary or "").strip().replace("\n", " ")
      snippet = snippet[:220] + ("…" if len(snippet) > 220 else "")
      lines.append(f"{i+1}. [{r.score:.3f}] {r.thread.topic}\n   {snippet}")

  if not lines:
    return "No matches.", []

  text = "\n".join(lines)
  return _truncate_to_token_budget(text, token_budget), entry_ids


def _format_thread_summary(topic: str, entries: list[object], token_budget: int) -> str:
  # Entries are GraphEntry objects; avoid importing their type.
  lines: list[str] = [f"Thread: {topic}", f"Entries: {len(entries)}", ""]
  for i, e in enumerate(entries[:25]):
    title = getattr(e, "title", "") or ""
    entry_id = getattr(e, "entry_id", "") or ""
    summary = (getattr(e, "summary", "") or "").strip().replace("\n", " ")
    if summary:
      summary = summary[:200] + ("…" if len(summary) > 200 else "")
      lines.append(f"{i}. {title} (entry_id={entry_id})\n   {summary}")
    else:
      lines.append(f"{i}. {title} (entry_id={entry_id})")

  text = "\n".join(lines)
  return _truncate_to_token_budget(text, token_budget)


def _format_smart_query(result: object, token_budget: int) -> tuple[str, list[str], dict[str, Any]]:
  from watercooler_memory.tier_strategy import TierResult

  if not isinstance(result, TierResult):
    return "No results (smart-query failed).", [], {}

  lines: list[str] = [
    f"tiers_queried={','.join(t.value for t in result.tiers_queried)}",
    f"primary_tier={(result.primary_tier.value if result.primary_tier else '')}",
    f"sufficient={result.sufficient}",
    f"result_count={result.result_count}",
    f"message={result.message}",
    "",
  ]

  entry_ids: list[str] = []
  t3_results = 0
  t3_with_source = 0
  top_evidence: list[dict[str, Any]] = []
  for i, e in enumerate(result.top_results(8)):
    eid = str(e.id or "")
    if eid:
      entry_ids.append(eid)
    source = ""
    if isinstance(getattr(e, "provenance", None), dict):
      source = str(e.provenance.get("source") or "")
    if e.tier.value == "T3":
      t3_results += 1
      if source:
        t3_with_source += 1
    snippet = (e.content or "").strip().replace("\n", " ")
    snippet = snippet[:220] + ("…" if len(snippet) > 220 else "")
    source_part = f" source={source}" if source else ""
    lines.append(
      f"{i+1}. [{e.tier.value}] [{e.score:.3f}] {e.name or ''} (id={eid}{source_part})\n"
      f"   {snippet}"
    )
    top_evidence.append(
      {
        "tier": e.tier.value,
        "id": eid,
        "score": float(e.score or 0.0),
        "name": e.name or "",
        "source": source,
      }
    )

  text = "\n".join(lines)
  meta = {
    "tiers_queried": [t.value for t in result.tiers_queried],
    "primary_tier": result.primary_tier.value if result.primary_tier else None,
    "result_count": int(result.result_count),
    "t3_results": int(t3_results),
    "t3_with_source": int(t3_with_source),
    "top_evidence": top_evidence,
  }
  return _truncate_to_token_budget(text, token_budget), entry_ids, meta


@dataclass
class WcToolSession:
  threads_dir: Path
  default_topic: str
  code_path: Path | None = None
  group_ids: Sequence[str] | None = None
  graphiti_entry_episode_index_path: Path | None = None
  tier_ceiling: TierCeiling = "T1"
  max_calls: int = 5
  token_budget: int = 900
  allow_write: bool = False
  calls_made: int = 0

  def execute(self, command: str) -> WcCommandResult:
    """Execute a single wc-* text command."""
    if self.calls_made >= self.max_calls:
      return WcCommandResult(
        tool="wc-budget",
        ok=False,
        output=f"Error: wc tool budget exceeded (max_calls={self.max_calls}).",
      )

    self.calls_made += 1

    try:
      argv = shlex.split(command.strip())
    except ValueError as e:
      return WcCommandResult(tool="wc-parse", ok=False, output=f"Error: could not parse wc command: {e}")

    if not argv:
      return WcCommandResult(tool="wc-parse", ok=False, output="Error: empty wc command.")

    tool = argv[0]

    if tool == "wc-search":
      query = " ".join(argv[1:]).strip()
      if not query:
        return WcCommandResult(tool=tool, ok=False, output='Usage: wc-search "<query>"')
      from watercooler.baseline_graph import SearchQuery, search_graph

      sq = SearchQuery(
        query=query,
        semantic=True,
        limit=10,
        include_threads=False,
        include_entries=True,
        thread_topic=self.default_topic,
      )
      results = search_graph(self.threads_dir, sq)
      out, entry_ids = _format_search_results(results, self.token_budget)
      return WcCommandResult(tool=tool, output=out, entry_ids=entry_ids, meta={"query": query})

    if tool == "wc-smart-query":
      query = " ".join(argv[1:]).strip()
      if not query:
        return WcCommandResult(tool=tool, ok=False, output='Usage: wc-smart-query "<query>"')

      # Enforce tier ceilings by selectively configuring orchestrator.
      from watercooler_memory.tier_strategy import TierOrchestrator, load_tier_config

      max_tiers = {"T1": 1, "T2": 2, "T3": 3}[self.tier_ceiling]
      code_path = self.code_path if self.tier_ceiling in ("T2", "T3") else None
      config = load_tier_config(threads_dir=self.threads_dir, code_path=code_path)
      config.max_tiers = max_tiers
      config.t3_enabled = self.tier_ceiling == "T3"

      orchestrator = TierOrchestrator(config)
      result = orchestrator.query(query, group_ids=self.group_ids)
      out, entry_ids, meta = _format_smart_query(result, self.token_budget)
      return WcCommandResult(tool=tool, output=out, entry_ids=entry_ids, meta={"query": query, **meta})

    if tool == "wc-read-thread":
      if len(argv) != 2:
        return WcCommandResult(tool=tool, ok=False, output="Usage: wc-read-thread <topic>")
      topic = argv[1].strip()
      from watercooler.baseline_graph.reader import read_thread_from_graph

      result = read_thread_from_graph(self.threads_dir, topic)
      if result is None:
        return WcCommandResult(tool=tool, ok=False, output=f"Thread not found: {topic}")
      _, entries = result
      out = _format_thread_summary(topic, entries, self.token_budget)
      entry_ids = [getattr(e, "entry_id", "") for e in entries if getattr(e, "entry_id", "")]
      return WcCommandResult(tool=tool, output=out, entry_ids=entry_ids[:10])

    if tool == "wc-get-entry":
      if len(argv) != 3:
        return WcCommandResult(tool=tool, ok=False, output="Usage: wc-get-entry <topic> <index>")
      topic = argv[1].strip()
      try:
        idx = int(argv[2])
      except ValueError:
        return WcCommandResult(tool=tool, ok=False, output="Error: <index> must be an integer.")

      from watercooler.baseline_graph.reader import read_thread_from_graph

      result = read_thread_from_graph(self.threads_dir, topic)
      if result is None:
        return WcCommandResult(tool=tool, ok=False, output=f"Thread not found: {topic}")
      _, entries = result
      if idx < 0 or idx >= len(entries):
        return WcCommandResult(tool=tool, ok=False, output=f"Error: index out of range (0..{len(entries)-1}).")
      e = entries[idx]
      title = getattr(e, "title", "") or ""
      entry_id = getattr(e, "entry_id", "") or ""
      body = (getattr(e, "body", "") or getattr(e, "summary", "") or "").strip()
      out = f"{title}\nentry_id={entry_id}\n\n{body}"
      out = _truncate_to_token_budget(out, self.token_budget)
      return WcCommandResult(tool=tool, output=out, entry_ids=[entry_id] if entry_id else [])

    if tool == "wc-t2-facts":
      # Usage:
      #   wc-t2-facts --active-only --start-time <iso> --end-time <iso> "<query>"
      active_only = False
      start_time = ""
      end_time = ""
      rest = argv[1:]
      i = 0
      while i < len(rest):
        a = rest[i]
        if a == "--active-only":
          active_only = True
          i += 1
          continue
        if a == "--start-time" and i + 1 < len(rest):
          start_time = rest[i + 1]
          i += 2
          continue
        if a == "--end-time" and i + 1 < len(rest):
          end_time = rest[i + 1]
          i += 2
          continue
        break
      query = " ".join(rest[i:]).strip()
      if not query:
        return WcCommandResult(
          tool=tool,
          ok=False,
          output='Usage: wc-t2-facts [--active-only] [--start-time <iso>] [--end-time <iso>] "<query>"',
        )

      if not self.code_path:
        return WcCommandResult(tool=tool, ok=False, output="Error: wc-t2-facts requires code_path.")

      try:
        from watercooler_mcp.memory import get_graphiti_backend, load_graphiti_config
      except Exception as e:
        return WcCommandResult(tool=tool, ok=False, output=f"Error: Graphiti backend unavailable: {e}")

      config = load_graphiti_config(code_path=self.code_path)
      if config is None:
        return WcCommandResult(tool=tool, ok=False, output="Error: Graphiti config not available/enabled.")

      backend = get_graphiti_backend(config)
      if backend is None or isinstance(backend, dict):
        return WcCommandResult(tool=tool, ok=False, output=f"Error: Graphiti backend init failed: {backend}")

      try:
        facts = backend.search_memory_facts(
          query=query,
          group_ids=list(self.group_ids) if self.group_ids else None,
          max_facts=15,
          start_time=start_time,
          end_time=end_time,
        )
      except Exception as e:
        return WcCommandResult(tool=tool, ok=False, output=f"Error: Graphiti fact search failed: {e}")

      # active_only filter: keep only currently-valid facts
      filtered = []
      stale = 0
      for f in facts:
        invalid_at = (f.get("invalid_at") if isinstance(f, dict) else None)
        if invalid_at:
          stale += 1
        if active_only and invalid_at:
          continue
        filtered.append(f)

      lines = [
        f"active_only={active_only}",
        f"returned={len(filtered)}",
        f"stale_count={stale}",
        "",
      ]
      entry_ids: list[str] = []
      for j, f in enumerate(filtered[:10]):
        if not isinstance(f, dict):
          continue
        uuid = str(f.get("uuid") or f.get("id") or "")
        if uuid:
          entry_ids.append(uuid)
        fact = (str(f.get("fact") or f.get("content") or "")).strip().replace("\n", " ")
        fact = fact[:240] + ("…" if len(fact) > 240 else "")
        lines.append(
          f"{j+1}. uuid={uuid} score={float(f.get('score', 0.0) or 0.0):.3f} "
          f"valid_at={f.get('valid_at')} invalid_at={f.get('invalid_at')}\n"
          f"   {fact}"
        )

      out = _truncate_to_token_budget("\n".join(lines), self.token_budget)
      meta: dict[str, Any] = {
        "query": query,
        "active_only": active_only,
        "start_time": start_time,
        "end_time": end_time,
        "total": len(facts),
        "returned": len(filtered),
        "stale_count": stale,
        "facts": filtered[:15],
      }
      return WcCommandResult(tool=tool, output=out, entry_ids=entry_ids, meta=meta)

    if tool == "wc-provenance":
      if len(argv) != 2:
        return WcCommandResult(tool=tool, ok=False, output="Usage: wc-provenance <episode_uuid>")
      episode_uuid = argv[1].strip()
      if not episode_uuid:
        return WcCommandResult(tool=tool, ok=False, output="Usage: wc-provenance <episode_uuid>")

      try:
        from watercooler_memory.entry_episode_index import EntryEpisodeIndex, IndexConfig
      except Exception as e:
        return WcCommandResult(tool=tool, ok=False, output=f"Error: EntryEpisodeIndex unavailable: {e}")

      cfg = IndexConfig(backend="graphiti", index_path=self.graphiti_entry_episode_index_path)
      idx = EntryEpisodeIndex(cfg, auto_load=True)
      entry_id = idx.get_entry(episode_uuid)
      if not entry_id:
        return WcCommandResult(
          tool=tool,
          ok=True,
          output="provenance_available=false",
          meta={"episode_uuid": episode_uuid, "provenance_available": False},
        )
      return WcCommandResult(
        tool=tool,
        ok=True,
        output=f"provenance_available=true\nentry_id={entry_id}",
        entry_ids=[entry_id],
        meta={"episode_uuid": episode_uuid, "provenance_available": True, "entry_id": entry_id},
      )

    if tool == "wc-say":
      if not self.allow_write:
        return WcCommandResult(tool=tool, ok=False, output="Error: wc-say is disabled for this run.")
      if len(argv) < 3:
        return WcCommandResult(tool=tool, ok=False, output='Usage: wc-say "<title>" "<body>"')

      title = argv[1].strip()
      body = " ".join(argv[2:]).strip()
      if not title or not body:
        return WcCommandResult(tool=tool, ok=False, output='Usage: wc-say "<title>" "<body>"')

      from ulid import ULID
      from watercooler.commands_graph import say

      entry_id = str(ULID())
      say(
        self.default_topic,
        threads_dir=self.threads_dir,
        agent="BenchAgent (wc-say)",
        role="implementer",
        title=title,
        body=body,
        entry_type="Note",
        entry_id=entry_id,
      )
      return WcCommandResult(
        tool=tool,
        ok=True,
        output=f"OK: wrote entry_id={entry_id} to topic={self.default_topic}",
        entry_ids=[entry_id],
      )

    return WcCommandResult(
      tool=tool,
      ok=False,
      output=(
        "Error: unknown wc tool. Supported: wc-search, wc-smart-query, wc-read-thread, wc-get-entry, wc-t2-facts, wc-provenance, wc-say."
      ),
    )

