"""MCP tool — watercooler_promote_candidate.

Wraps the Phase 1b promotion helper at ``watercooler.promotion`` with the
canonical write path (``_say_impl``) so a human-authorized promotion produces:

1. A promoted Decision or durable ``## Lesson`` Note on the candidate's thread,
   carrying forward target-specific provenance and human authorization fields.
2. A ``CandidateDisposition`` ``Note`` on the same thread marking the
   candidate as ``promoted`` and referencing the promoted entry's ULID.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastmcp import Context
from fastmcp.exceptions import ToolError

from watercooler.baseline_graph.storage import get_graph_dir
from watercooler.baseline_graph.writer import get_entry_node_from_graph
from watercooler.decision_extraction import reverify_quotes_against_source
from watercooler.promotion import (
    PromotionError,
    build_promotion_authority_fields,
    format_candidate_disposition_body,
    parse_candidate_body,
    plan_promotion,
)

from .thread_write import _say_impl

log = logging.getLogger(__name__)

# `Entry-ID: <ULID>` appears in the _say_impl response on the line right after
# the ball flip / status lines. ULIDs are 26 chars of base32 Crockford.
_ENTRY_ID_RE = re.compile(r"Entry-ID:\s*([0-9A-HJKMNP-TV-Z]{26})", re.MULTILINE)


_SUPERSEDED_ENTRY_RE = re.compile(r"^Superseded-Entry:\s*(\S+)", re.MULTILINE)
_SUPERSEDED_BY_ENTRY_RE = re.compile(r"^Superseded-By-Entry:\s*(\S+)", re.MULTILINE)
_PROMOTION_SOURCE_EARNED_RE = re.compile(
    r"^Promotion-Source:\s*earned_edge\s*$", re.MULTILINE
)


def _promote_supersession_candidate(
    *,
    meta,
    candidate_entry_id: str,
    topic: str,
    candidate_body: str,
    human_authorized_by: str,
    existing_thread_entries,
    ctx: Context,
    code_path: str,
    agent_func: str,
) -> str:
    """Ratify an earned_edge supersession candidate (RFC P3, Level 3 human act).

    Runs the SAME ``validate_candidate_for_promotion`` gate as Decision/Learning promotion
    (authorizer scrub, ``needs_human_confirmation`` status, and the append-only
    double-promotion guard keyed on ``Disposition-Target``), then writes the append-only
    ``xref_supersedes`` annotation (superseded entry → successor) — the authored record that
    flips ``list_decisions``' ``superseded_by_ratified`` afforded→authored — plus a
    ``CandidateDisposition`` Note. No Decision/Learning entry is produced. The candidate body
    (daemon-emitted) must carry ``Superseded-Entry:`` and ``Superseded-By-Entry:``.
    """
    from watercooler.promotion import (
        PromotionError,
        validate_candidate_for_promotion,
    )

    from .graph import _annotate_impl

    # Same L3 guards as Decision/Learning: empty-authorizer scrub, needs_human_confirmation,
    # and the append-only prior-disposition guard (Disposition-Target). Skipping these was
    # the authority bypass in review #1041.
    try:
        validate_candidate_for_promotion(
            meta,
            "Supersession",
            human_authorized_by,
            existing_thread_entries=existing_thread_entries,
        )
    except PromotionError as exc:
        return f"❌ watercooler_promote_candidate: {exc}"

    # Enforce the earned-edge candidate contract — only a daemon-emitted
    # needs_human_confirmation earned_edge candidate may be ratified as a supersession,
    # not an arbitrary Note that merely carries the two entry markers (review #1041
    # re-review). validate_candidate_for_promotion only rejects Candidate-Status when the
    # marker is PRESENT, so require it present + correct here, plus the earned_edge source.
    status = (meta.candidate_status or "").strip().lower().replace(" ", "_")
    if status != "needs_human_confirmation":
        return (
            "❌ watercooler_promote_candidate: Supersession candidate "
            f"{candidate_entry_id} is not a promotable candidate — it must carry "
            "'Candidate-Status: needs_human_confirmation'."
        )
    if not _PROMOTION_SOURCE_EARNED_RE.search(candidate_body):
        return (
            "❌ watercooler_promote_candidate: Supersession candidate "
            f"{candidate_entry_id} must be an earned_edge candidate "
            "('Promotion-Source: earned_edge')."
        )

    a = _SUPERSEDED_ENTRY_RE.search(candidate_body)
    b = _SUPERSEDED_BY_ENTRY_RE.search(candidate_body)
    if not a or not b:
        return (
            "❌ watercooler_promote_candidate: Supersession candidate "
            f"{candidate_entry_id} must carry 'Superseded-Entry:' and "
            "'Superseded-By-Entry:' lines."
        )
    superseded_entry, successor_entry = a.group(1), b.group(1)

    xref_result = _annotate_impl(
        topic, superseded_entry, "entry", "xref_supersedes",
        successor_entry, code_path, human_authorized_by,
    )
    parsed = None
    if isinstance(xref_result, str):
        try:
            parsed = json.loads(xref_result)
        except ValueError:
            parsed = None
    if not (isinstance(parsed, dict) and parsed.get("status") == "ok"):
        return (
            "❌ watercooler_promote_candidate: xref_supersedes append failed: "
            f"{xref_result}"
        )

    disposition_body = (
        "Spec: general-purpose\n"
        "CandidateDisposition: promoted\n"
        f"Disposition-Target: {candidate_entry_id}\n"
        "Promotion-Source: earned_edge\n"
        f"Superseded-Entry: {superseded_entry}\n"
        f"Superseded-By-Entry: {successor_entry}\n"
        f"Authorized-By: {human_authorized_by.strip()}\n\n"
        f"Ratified supersession {superseded_entry} → {successor_entry} as an authored "
        "xref_supersedes annotation (earned→authored, RFC P3)."
    )
    disposition_response = _say_impl(
        topic=topic,
        title=f"CandidateDisposition: ratified supersession {superseded_entry}",
        body=disposition_body,
        ctx=ctx,
        role="implementer",
        entry_type="Note",
        code_path=code_path,
        agent_func=agent_func,
    )
    disposition_entry_id = _parse_entry_id(disposition_response)
    return (
        f"✅ Ratified supersession candidate {candidate_entry_id} on thread '{topic}'.\n"
        f"xref_supersedes: {superseded_entry} → {successor_entry}\n"
        f"CandidateDisposition Entry-ID: {disposition_entry_id or '(write failed — verify)'}\n"
        f"Authorized by: {human_authorized_by.strip()}"
    )


def _parse_entry_id(say_response: str) -> Optional[str]:
    """Extract the Entry-ID written by _say_impl. Returns None on parse failure."""
    m = _ENTRY_ID_RE.search(say_response)
    return m.group(1) if m else None


def _promote_candidate_impl(
    candidate_entry_id: str,
    topic: str,
    target_type: str,
    human_authorized_by: str,
    ctx: Context,
    code_path: str,
    agent_func: str,
    edits: Optional[dict] = None,
) -> str:
    """Promote a candidate Note to a supported durable entry.

    Reads the candidate body from the baseline graph, validates that it is in
    the ``needs_human_confirmation`` state, plans the target-specific promoted
    entry, writes it via the canonical write path, then writes a
    ``CandidateDisposition`` Note referencing the promoted entry's ULID. The
    candidate Note itself is never edited (append-only).

    Args:
        candidate_entry_id: ULID of the candidate Note to promote.
        topic: Thread topic the candidate lives on.
        target_type: Target entry type. Supported values are ``"Decision"``,
            ``"Learning"``, and ``"Supersession"`` (ratifies an earned_edge
            supersession candidate into an append-only ``xref_supersedes`` annotation).
        human_authorized_by: Identifier of the authorizing human (required —
            promotion is a Level 3 act).
        ctx: MCP request context.
        code_path: Path to the repo root (required).
        agent_func: Agent identity (``<platform>:<model>:<role>``).
        edits: Optional dict with keys ``decision_statement`` / ``rationale``
            / ``scope`` to override or extend the carried-forward candidate
            content. Unknown keys are ignored.

    Returns:
        Confirmation string with both new entry IDs.
    """
    if not candidate_entry_id or not candidate_entry_id.strip():
        return "❌ watercooler_promote_candidate: candidate_entry_id is required."
    if not topic or not topic.strip():
        return "❌ watercooler_promote_candidate: topic is required."

    # Resolve the context (_say_impl resolves the same way for the writes below).
    from ..validation import _require_context, is_hosted_context

    error, context = _require_context(code_path)
    if error:
        return f"❌ watercooler_promote_candidate: {error}"
    if context is None or context.threads_dir is None:
        return (
            "❌ watercooler_promote_candidate: could not resolve threads_dir "
            f"for code_path={code_path!r}."
        )

    # Read the candidate body + the thread's entry list (the latter feeds the
    # #886 double-promotion guard). Hosted mode has no local filesystem baseline
    # graph — `threads_dir` is the `/hosted` sentinel and the graph lives behind
    # the GitHub-backed hosted path — so branch on the mode the same way the read
    # tools (thread_query, decisions) do. Without this branch the local path's
    # `get_graph_dir("/hosted")` → `/hosted/graph/baseline` never exists and the
    # promote fails before it can find the candidate. The write path (_say_impl,
    # below) is already hosted-aware, which is why reject/disposition succeed.
    if is_hosted_context(context):
        from ..hosted_ops import load_thread_entries_hosted

        load_err, hosted_entries = load_thread_entries_hosted(topic)
        if load_err:
            # Fail closed (parity with the local except below): the double-
            # promotion guard depends on this list, so refuse rather than risk a
            # duplicate promoted entry.
            return (
                f"❌ watercooler_promote_candidate: could not load thread "
                f"{topic!r} entries in hosted mode to verify candidate "
                f"{candidate_entry_id} ({load_err}). Promotion is refused rather "
                f"than risk a duplicate promoted entry."
            )
        candidate_obj = next(
            (e for e in hosted_entries if e.entry_id == candidate_entry_id), None
        )
        if candidate_obj is None:
            return (
                f"❌ watercooler_promote_candidate: candidate entry "
                f"{candidate_entry_id} not found on thread {topic!r}."
            )
        candidate_body = candidate_obj.body or ""
        # The planner's guard reads dict-shaped entries (`entry.get(...)`);
        # ThreadEntry is an object, so project to the shape get_entries_for_thread
        # yields in local mode.
        existing_thread_entries = [
            {
                "entry_id": e.entry_id,
                "entry_type": e.entry_type,
                "body": e.body or "",
                "title": e.title,
            }
            for e in hosted_entries
        ]
    else:
        graph_dir = get_graph_dir(context.threads_dir)
        if graph_dir is None or not Path(graph_dir).exists():
            return (
                f"❌ watercooler_promote_candidate: baseline graph not found at "
                f"{graph_dir}. Has the thread been read at least once?"
            )

        candidate_entry = get_entry_node_from_graph(
            context.threads_dir, candidate_entry_id, topic
        )
        if candidate_entry is None:
            return (
                f"❌ watercooler_promote_candidate: candidate entry "
                f"{candidate_entry_id} not found on thread {topic!r}."
            )

        candidate_body = candidate_entry.get("body", "") or ""

        # Load existing thread entries so the planner can refuse double-
        # promotion. Append-only candidates never transition their own status, so
        # the guards scan the thread for a prior disposition or a prior promoted
        # Decision (#886).
        try:
            from watercooler.baseline_graph.writer import get_entries_for_thread

            existing_thread_entries = list(
                get_entries_for_thread(context.threads_dir, topic)
            )
        except (OSError, KeyError, ValueError) as exc:
            # Fail closed: promotion is a rare, human-authorized act, and the
            # double-promotion guards (disposition + promoted entry, #886) depend
            # on this list. Skipping the check on a flaky read is exactly when a
            # prior write may have half-failed — refuse rather than risk a
            # duplicate promoted entry.
            log.warning(
                "promote_candidate: could not load thread entries for the "
                "double-promotion check (%s); refusing promotion.", exc
            )
            return (
                f"❌ watercooler_promote_candidate: could not load thread entries "
                f"to verify candidate {candidate_entry_id} was not already "
                f"promoted ({exc}). Promotion is refused rather than risk a "
                f"duplicate promoted entry; retry once the thread graph is "
                f"readable."
            )

    if not candidate_body:
        return (
            f"❌ watercooler_promote_candidate: candidate {candidate_entry_id} "
            f"has empty body — cannot promote."
        )

    meta = parse_candidate_body(candidate_body, candidate_entry_id, topic)

    # A Supersession candidate ratifies an earned edge into an append-only
    # xref_supersedes annotation (earned→authored, RFC P3) rather than a
    # Decision/Learning entry — but through the SAME L3 guards (authorizer scrub,
    # needs_human_confirmation, append-only double-promotion). Branch after the
    # shared parse; the helper runs validate_candidate_for_promotion itself.
    if target_type == "Supersession":
        return _promote_supersession_candidate(
            meta=meta,
            candidate_entry_id=candidate_entry_id,
            topic=topic,
            candidate_body=candidate_body,
            human_authorized_by=human_authorized_by,
            existing_thread_entries=existing_thread_entries,
            ctx=ctx,
            code_path=code_path,
            agent_func=agent_func,
        )

    # #887 quote re-validation builds the §6 source/record_state warrant — a
    # Decision-promotion concern. A learning candidate carries no Source-Entry and
    # its promoted lesson renders no warrant, so skip the source-chain check for it.
    quote_verified = None
    quote_reverification_reason = None
    source_entry_type = None
    if target_type == "Decision":
        # Re-validate the candidate's evidence quotes against the LIVE source entry
        # so the promoted Decision's warrant reflects the real source — not the
        # candidate's self-asserted Quote-Evidence-Status, which a hand-forged
        # candidate could fake. The source entry may live on another thread.
        # In hosted mode there is no local baseline graph to read the source
        # from (`threads_dir` is the `/hosted` sentinel), so withhold source
        # support — the same graceful degrade as an unreadable source below,
        # rather than a doomed filesystem probe.
        source_node = None
        if meta.source_entry_id and not is_hosted_context(context):
            try:
                source_node = get_entry_node_from_graph(
                    context.threads_dir, meta.source_entry_id
                )
            except (OSError, KeyError, ValueError) as exc:
                # Unlike the double-promotion guard (which fails CLOSED), an
                # unreadable *source* only means we cannot confirm the quotes — so
                # we withhold source/record_state support (quote_verified stays
                # False) and let the human-authorized promotion proceed.
                log.warning(
                    "promote_candidate: source entry %s unreadable for quote "
                    "re-validation (%s); withholding source support.",
                    meta.source_entry_id, exc,
                )
        quote_reverification = reverify_quotes_against_source(
            meta.evidence_quotes,
            source_node.get("body") if source_node else None,
        )
        quote_verified = quote_reverification.verified
        quote_reverification_reason = quote_reverification.reason
        # Live source entry type — record_state must reflect what the source
        # actually is, not the candidate's self-asserted marker (#887).
        source_entry_type = source_node.get("entry_type") if source_node else None

    try:
        plan = plan_promotion(
            candidate_body=candidate_body,
            candidate_entry_id=candidate_entry_id,
            candidate_topic=topic,
            target_type=target_type,
            human_authorized_by=human_authorized_by,
            edits=edits,
            existing_thread_entries=existing_thread_entries,
            quote_verified=quote_verified,
            quote_reverification_reason=quote_reverification_reason,
            source_entry_type=source_entry_type,
        )
    except PromotionError as exc:
        return f"❌ watercooler_promote_candidate: {exc}"

    # Persist structured authority metadata alongside the body markers so human
    # ownership is queryable in graph metadata, not only in prose. actor_class="agent"
    # because an agent executes this MCP write under human instruction —
    # distinguishable from a direct human-authored write. authority_source (a ULID
    # field) is intentionally left unset: the body marker's "Authority-Source:
    # human" is a human-readable label, not a ULID. Shared with the CLI promote path.
    decision_authority_fields = build_promotion_authority_fields(
        human_authorized_by=human_authorized_by,
        source_entry_id=candidate_entry_id,
        actor_class="agent",
        target_type=target_type,
    )

    # Write the promoted entry — a Decision, or a durable ## Lesson Note for a
    # learning candidate (plan.decision_entry_type carries the right type).
    decision_response = _say_impl(
        topic=topic,
        title=plan.decision_title,
        body=plan.decision_body,
        ctx=ctx,
        role="implementer",
        entry_type=plan.decision_entry_type,
        code_path=code_path,
        agent_func=agent_func,
        authority_fields=decision_authority_fields,
        support_fields=plan.decision_support_fields,
    )
    decision_entry_id = _parse_entry_id(decision_response)
    if decision_entry_id is None:
        # The promoted-entry write failed or returned an unexpected format.
        return (
            f"❌ watercooler_promote_candidate: {target_type} write did not "
            f"produce a parseable Entry-ID. Response:\n{decision_response}"
        )

    # Re-build the disposition body now that we have the real promoted-entry ID
    # (reusing the candidate meta parsed above).
    disposition_body = format_candidate_disposition_body(
        meta,
        promoted_entry_id=decision_entry_id,
        human_authorized_by=human_authorized_by,
        promoted_kind=target_type,
    )

    disposition_response = _say_impl(
        topic=topic,
        title=plan.disposition_title,
        body=disposition_body,
        ctx=ctx,
        role="implementer",
        entry_type="Note",
        code_path=code_path,
        agent_func=agent_func,
    )
    disposition_entry_id = _parse_entry_id(disposition_response)

    lines = [
        f"✅ Promoted candidate {candidate_entry_id} to {target_type} on "
        f"thread '{topic}'.",
        f"{target_type} Entry-ID: {decision_entry_id}",
        f"CandidateDisposition Entry-ID: {disposition_entry_id or '(write failed — verify)'}",
        f"Authorized by: {human_authorized_by.strip()}",
        "",
        "Decision write response:",
        decision_response,
        "",
        "Disposition write response:",
        disposition_response,
    ]
    return "\n".join(lines)


def _promote_candidate_tool(
    candidate_entry_id: str,
    topic: str,
    target_type: str,
    human_authorized_by: str,
    ctx: Context,
    code_path: str,
    agent_func: str,
    edits: Optional[dict] = None,
) -> str:
    """MCP boundary over :func:`_promote_candidate_impl`.

    ``_promote_candidate_impl`` signals failure by *returning* a ``❌``-prefixed
    string (the repo-wide tool convention). Returned that way, the MCP result
    carries ``isError: false`` — so a client that only inspects ``isError`` (and
    promotion is an L3 authoritative write) reads the failure as success and
    reports a 200 while nothing was written. Promotion is exactly where a silent
    false-success is dangerous, so here — and only here — convert a ``❌`` result
    into a raised :class:`ToolError`. FastMCP then marks the tool result
    ``isError: true`` and surfaces the message verbatim, so the real reason
    reaches the caller. Success strings (``✅ …``) pass through unchanged.
    """
    result = _promote_candidate_impl(
        candidate_entry_id=candidate_entry_id,
        topic=topic,
        target_type=target_type,
        human_authorized_by=human_authorized_by,
        ctx=ctx,
        code_path=code_path,
        agent_func=agent_func,
        edits=edits,
    )
    if isinstance(result, str) and result.lstrip().startswith("❌"):
        raise ToolError(result.strip())
    return result


# Preserve the rich impl docstring as the MCP tool description (args, semantics).
_promote_candidate_tool.__doc__ = _promote_candidate_impl.__doc__


def register_promotion_tools(mcp) -> None:
    """Register watercooler_promote_candidate with the MCP server."""
    mcp.tool(name="watercooler_promote_candidate")(_promote_candidate_tool)
