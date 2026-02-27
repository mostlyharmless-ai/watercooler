"""Knowledge pack schema, watercooler-backed storage, and search for SWE-bench experiments.

Entities model the structured "org memory" a team accumulates:
- Component: code modules/classes the team knows about
- Pattern: fix recipes and refactor patterns
- Pitfall: common mistakes + symptoms
- Decision: ADRs explaining why something is done a certain way
- TestSignal: failure signature -> likely cause mapping
- ExampleFix: diff snippet / commit-like summary

Build phase: mine repos -> distill into Entity objects -> publish as watercooler thread entries
Run phase: read thread via baseline graph -> keyword search -> return results to agent

Storage: watercooler baseline graph (entries.jsonl, edges.jsonl, meta.json) written
via say() and read via read_thread_from_graph().
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Entity types (used during mining/distillation phase)
# ---------------------------------------------------------------------------

EntityType = Literal[
    "Component", "Pattern", "Pitfall", "Decision", "TestSignal", "ExampleFix"
]

RelationType = Literal[
    "affects",       # Pitfall -> Component
    "suggests",      # TestSignal -> Component/Pattern
    "applies_to",    # Pattern -> Component
    "constrains",    # Decision -> Pattern
    "demonstrates",  # ExampleFix -> Pattern
]


@dataclass
class Entity:
    """A knowledge graph entity representing org memory."""
    id: str
    type: EntityType
    name: str
    summary: str  # 1-3 sentence description
    detail: str = ""  # optional longer content (code snippet, checklist, etc.)
    source: str = ""  # where this was mined from (file, commit, doc URL)
    tags: list[str] = field(default_factory=list)

    def token_estimate(self) -> int:
        """Rough token count (words / 0.75)."""
        text = f"{self.name} {self.summary} {self.detail}"
        return int(len(text.split()) / 0.75)


@dataclass
class Relation:
    """A directed edge between two entities."""
    source_id: str
    relation: RelationType
    target_id: str
    weight: float = 1.0  # relevance weight


@dataclass
class SearchResult:
    """A single search result from the watercooler knowledge thread."""
    title: str
    body: str
    score: float
    entry_id: str = ""


# ---------------------------------------------------------------------------
# Publishing entities as watercooler thread entries
# ---------------------------------------------------------------------------

def publish_to_watercooler(
    entities: list[Entity],
    relations: list[Relation],
    threads_dir: Path,
    topic: str,
    agent: str = "Builder (system)",
) -> int:
    """Write entities as watercooler thread entries via say().

    Each entity becomes one thread entry. Relations are embedded in the
    entry body text so they're discoverable via keyword search.

    Args:
        entities: Distilled entities to publish.
        relations: Relations between entities.
        threads_dir: Isolated watercooler threads directory (NOT the main repo's).
        topic: Thread topic slug (e.g., "django__django-knowledge").
        agent: Agent name for entry attribution.

    Returns:
        Number of entries written.
    """
    from ulid import ULID
    from watercooler.commands_graph import say

    # Index for embedding relations and resolving target names
    entities_by_id = {e.id: e for e in entities}
    outgoing: dict[str, list[Relation]] = {}
    incoming: dict[str, list[Relation]] = {}
    for rel in relations:
        outgoing.setdefault(rel.source_id, []).append(rel)
        incoming.setdefault(rel.target_id, []).append(rel)

    count = 0
    for entity in entities:
        body = _entity_to_body(
            entity,
            outgoing.get(entity.id, []),
            incoming.get(entity.id, []),
            entities_by_id,
        )
        say(
            topic,
            threads_dir=threads_dir,
            agent=agent,
            role="scribe",
            title=f"{entity.type}: {entity.name}",
            body=body,
            entry_type="Note",
            entry_id=str(ULID()),
        )
        count += 1

    log.info(f"Published {count} entities to watercooler thread '{topic}' "
             f"at {threads_dir}")
    return count


def _entity_to_body(
    entity: Entity,
    outgoing_rels: list[Relation],
    incoming_rels: list[Relation],
    entities_by_id: dict[str, Entity],
) -> str:
    """Convert entity + relations to a watercooler entry body."""
    parts = [
        "Spec: knowledge-engineer",
        "",
        f"**Entity ID:** {entity.id}",
        f"**Type:** {entity.type}",
    ]
    if entity.tags:
        parts.append(f"**Tags:** {', '.join(entity.tags)}")
    if entity.source:
        parts.append(f"**Source:** {entity.source}")

    parts.extend(["", entity.summary])

    if entity.detail:
        parts.extend(["", "### Detail", "", entity.detail])

    # Embed relations in body for keyword searchability
    all_rels = []
    for rel in outgoing_rels:
        target = entities_by_id.get(rel.target_id)
        target_name = target.name if target else rel.target_id
        all_rels.append(f"- **{rel.relation}** -> {target_name}")
    for rel in incoming_rels:
        source = entities_by_id.get(rel.source_id)
        source_name = source.name if source else rel.source_id
        all_rels.append(f"- **{rel.relation}** <- {source_name}")

    if all_rels:
        parts.extend(["", "### Relations", ""])
        parts.extend(all_rels)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Watercooler-backed knowledge pack (read + search)
# ---------------------------------------------------------------------------

class WatercoolerKnowledgePack:
    """Read-only knowledge pack backed by a watercooler thread.

    Reads entries via read_thread_from_graph() and provides keyword search
    with token-budget-aware ranking.
    """

    def __init__(self, threads_dir: Path, topic: str):
        self.threads_dir = threads_dir
        self.topic = topic
        self._entries: Optional[list] = None  # lazy-loaded GraphEntry list

    def _load_entries(self) -> list:
        """Load entries from graph, caching the result."""
        if self._entries is not None:
            return self._entries
        from watercooler.baseline_graph.reader import read_thread_from_graph

        result = read_thread_from_graph(self.threads_dir, self.topic)
        if result is None:
            log.warning(f"Thread '{self.topic}' not found at {self.threads_dir}")
            self._entries = []
        else:
            _, self._entries = result
            log.info(f"Loaded {len(self._entries)} entries from thread '{self.topic}'")
        return self._entries

    @property
    def entry_count(self) -> int:
        return len(self._load_entries())

    def search(
        self,
        query: str,
        top_k: int = 5,
        max_tokens: int = 2000,
        exclude_fn=None,
    ) -> list[SearchResult]:
        """Keyword search over thread entries with token budget.

        Args:
            query: Natural language search query.
            top_k: Max results to return.
            max_tokens: Approximate token budget for all results.
            exclude_fn: Optional filter function(title, body) -> bool.
                        Returns True to EXCLUDE an entry (e.g., anti-leakage).
        """
        entries = self._load_entries()
        query_terms = _tokenize(query)
        if not query_terms:
            return []

        # Score each entry by keyword overlap
        scored: list[tuple[float, object]] = []
        for entry in entries:
            title = entry.title or ""
            body = entry.body or ""

            # Apply exclusion filter (anti-leakage)
            if exclude_fn and exclude_fn(title, body):
                continue

            searchable = f"{title} {body}"
            all_tokens = set(_tokenize(searchable))
            title_tokens = set(_tokenize(title))

            if not all_tokens:
                continue

            score = 0.0
            for qt in query_terms:
                if qt in title_tokens:
                    score += 3.0  # title matches weighted higher
                elif qt in all_tokens:
                    score += 1.0

            if score > 0:
                scored.append((score / len(query_terms), entry))

        scored.sort(key=lambda x: -x[0])

        # Build results respecting token budget
        results: list[SearchResult] = []
        token_budget = max_tokens

        for score, entry in scored[:top_k * 2]:
            body = entry.body or ""
            est = int(len(f"{entry.title} {body}".split()) / 0.75)
            if est > token_budget:
                continue

            token_budget -= est
            results.append(SearchResult(
                title=entry.title,
                body=body,
                score=score,
                entry_id=entry.entry_id,
            ))

            if len(results) >= top_k or token_budget <= 0:
                break

        return results

    def format_results(self, results: list[SearchResult]) -> str:
        """Format search results as a concise text block for the LLM."""
        if not results:
            return "No relevant org knowledge found."

        parts = []
        for i, r in enumerate(results, 1):
            block = f"[{i}] {r.title}"
            body = r.body
            # Strip the Spec: header line from display
            body = re.sub(r'^Spec:.*\n\n?', '', body)
            # Truncate for display
            if len(body) > 500:
                body = body[:500] + "..."
            block += f"\n{body}"
            parts.append(block)

        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Anti-leakage filters
# ---------------------------------------------------------------------------

def check_leakage(text: str, instance_id: str, issue_text: str,
                  patch_text: str = "", threshold: float = 0.6) -> bool:
    """Return True if text likely leaks the answer for this instance.

    Checks:
    1. Text mentions the exact instance ID or PR number
    2. Text is too similar to the patch (Jaccard on tokens)
    3. Text mentions the exact failing test + function + file combo
    """
    text_lower = text.lower()

    # Check 1: Direct mention of instance ID
    parts = instance_id.split("-")
    issue_num = parts[-1] if parts else ""
    if instance_id.lower() in text_lower:
        return True
    if issue_num and f"#{issue_num}" in text_lower:
        return True

    # Check 2: Jaccard similarity with patch
    if patch_text:
        text_tokens = set(_tokenize(text_lower))
        patch_tokens = set(_tokenize(patch_text.lower()))
        if text_tokens and patch_tokens:
            jaccard = len(text_tokens & patch_tokens) / len(text_tokens | patch_tokens)
            if jaccard > threshold:
                return True

    # Check 3: Exact test+function+file combo from issue text
    test_patterns = re.findall(r'test_\w+', issue_text.lower())
    file_patterns = re.findall(r'[\w/]+\.py', issue_text.lower())
    if test_patterns and file_patterns:
        matches = sum(1 for t in test_patterns if t in text_lower)
        matches += sum(1 for f in file_patterns if f in text_lower)
        if matches >= len(test_patterns) + len(file_patterns):
            return True

    return False


def make_leakage_filter(instance_id: str, issue_text: str,
                        patch_text: str = "", threshold: float = 0.6):
    """Return an exclude_fn for WatercoolerKnowledgePack.search().

    Usage:
        pack = WatercoolerKnowledgePack(threads_dir, topic)
        exclude = make_leakage_filter(instance_id, issue_text, patch_text)
        results = pack.search(query, exclude_fn=exclude)
    """
    def _exclude(title: str, body: str) -> bool:
        combined = f"{title} {body}"
        return check_leakage(combined, instance_id, issue_text, patch_text, threshold)
    return _exclude


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "and", "but", "or", "nor",
    "not", "so", "yet", "both", "each", "few", "more", "most", "other",
    "some", "such", "no", "only", "own", "same", "than", "too", "very",
    "this", "that", "these", "those", "it", "its",
    "spec", "knowledge", "engineer", "entity", "type", "tags", "source",
    "detail", "relations", "mined",
})


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens, removing stop words."""
    tokens = re.findall(r'[a-z_][a-z0-9_]*', text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]
