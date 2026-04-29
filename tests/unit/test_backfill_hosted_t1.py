"""Unit tests for scripts/backfill_hosted_t1.py parsing helpers.

The end-to-end extract/push flow requires Docker + FalkorDB + MCP, which
is integration scope. These tests pin the small text-parsing helpers that
the script depends on so format drift in redis-cli output doesn't silently
corrupt the transport JSONL.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# Load the script as a module without going through pytest collection
# (it's a CLI script, not a package).
@pytest.fixture(scope="module")
def backfill_module():
    repo_root = Path(__file__).parent.parent.parent
    script_path = repo_root / "scripts" / "backfill_hosted_t1.py"
    spec = importlib.util.spec_from_file_location("backfill_hosted_t1", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_hosted_t1"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestParseEmbeddingStr:
    def test_falkor_vectorf32_shape(self, backfill_module):
        # FalkorDB renders Vectorf32 as <f1, f2, ...> via redis-cli
        s = "<-0.055360, 0.001273, -0.016418>"
        out = backfill_module._parse_embedding_str(s)
        assert out == pytest.approx([-0.055360, 0.001273, -0.016418])

    def test_python_list_shape(self, backfill_module):
        # Legacy / round-trip shape
        s = "[0.1, -0.2, 3.5e-4]"
        out = backfill_module._parse_embedding_str(s)
        assert out == pytest.approx([0.1, -0.2, 3.5e-4])

    def test_with_whitespace(self, backfill_module):
        s = "  < 0.1 , -0.2 , 3.5e-4 >  "
        out = backfill_module._parse_embedding_str(s)
        assert out == pytest.approx([0.1, -0.2, 3.5e-4])

    def test_empty_brackets(self, backfill_module):
        assert backfill_module._parse_embedding_str("[]") == []
        assert backfill_module._parse_embedding_str("<>") == []

    def test_not_a_list(self, backfill_module):
        assert backfill_module._parse_embedding_str("0.1, 0.2") is None

    def test_malformed_floats(self, backfill_module):
        assert backfill_module._parse_embedding_str("<abc, def>") is None
        assert backfill_module._parse_embedding_str("[abc, def]") is None

    def test_realistic_falkor_first_few(self, backfill_module):
        # First few values from real production data (sampled live during Phase 3)
        s = "<-0.055360, 0.001273, -0.016418>"
        out = backfill_module._parse_embedding_str(s)
        assert len(out) == 3
        assert out[0] == pytest.approx(-0.055360)
        assert out[2] == pytest.approx(-0.016418)


class TestParseSingleStringValueAcceptsBothVectorShapes:
    def test_angle_bracket_shape(self, backfill_module):
        raw = "n.embedding\n<-0.05, 0.001>\nCached execution: 0\n"
        assert backfill_module._parse_single_string_value(raw) == "<-0.05, 0.001>"

    def test_square_bracket_shape(self, backfill_module):
        raw = "emb\n[0.1, 0.2]\nCached execution: 0\n"
        assert backfill_module._parse_single_string_value(raw) == "[0.1, 0.2]"


class TestParseSingleStringValue:
    """Format: redis-cli non-TTY (subprocess) raw output, one value per line.

    Reproduced live during Phase 3 smoke test:
        emb
        [0.014611478, 0.0787507, ...]
        Cached execution: 0
        Query internal execution time: 0.6 milliseconds
    """

    def test_standard_redis_cli_output(self, backfill_module):
        raw = "emb\n[0.1, 0.2]\nCached execution: 0\n"
        out = backfill_module._parse_single_string_value(raw)
        assert out == "[0.1, 0.2]"

    def test_no_data(self, backfill_module):
        raw = "emb\n"
        out = backfill_module._parse_single_string_value(raw)
        assert out is None

    def test_returns_embedding_when_cached_execution_emitted_first(self, backfill_module):
        """Defensive: don't be fooled if 'Cached execution' precedes the embedding row."""
        raw = "emb\nCached execution: 0\n[0.1, 0.2]\n"
        out = backfill_module._parse_single_string_value(raw)
        assert out == "[0.1, 0.2]"

    def test_returns_none_when_no_embedding_shape_present(self, backfill_module):
        """If only stats lines are present (no [...] value), return None — don't grab a stats line."""
        raw = "emb\nCached execution: 0\nQuery internal execution time: 1.2 milliseconds\n"
        out = backfill_module._parse_single_string_value(raw)
        assert out is None

    def test_skips_blank_lines(self, backfill_module):
        raw = "\nemb\n\n[0.1]\n\n"
        out = backfill_module._parse_single_string_value(raw)
        assert out == "[0.1]"


class TestParseIdTopicPairs:
    """Pins the contract for the multi-row 2-column GRAPH.QUERY parser.

    Captured live during Phase 3 smoke test:
        n.entry_id
        n.thread_topic
        01KDWHAPSW61P3E2RSBS0MPR6G
        git-sync-refactor
        01KDWHGZTYTGDJ2SDH29M5XJE3
        git-sync-refactor
        ...
        Cached execution: 0
        Query internal execution time: 0.642016 milliseconds
    """

    def test_real_3_pair_response(self, backfill_module):
        raw = (
            "n.entry_id\n"
            "n.thread_topic\n"
            "01KDWHAPSW61P3E2RSBS0MPR6G\n"
            "git-sync-refactor\n"
            "01KDWHGZTYTGDJ2SDH29M5XJE3\n"
            "git-sync-refactor\n"
            "01KDWHS6ER4R8XKKN4V2JQFJPA\n"
            "git-sync-refactor\n"
            "Cached execution: 0\n"
            "Query internal execution time: 0.642016 milliseconds\n"
        )
        out = backfill_module._parse_id_topic_pairs(raw)
        assert out == [
            ("01KDWHAPSW61P3E2RSBS0MPR6G", "git-sync-refactor"),
            ("01KDWHGZTYTGDJ2SDH29M5XJE3", "git-sync-refactor"),
            ("01KDWHS6ER4R8XKKN4V2JQFJPA", "git-sync-refactor"),
        ]

    def test_zero_data_rows(self, backfill_module):
        raw = (
            "n.entry_id\n"
            "n.thread_topic\n"
            "Cached execution: 0\n"
            "Query internal execution time: 0.5 milliseconds\n"
        )
        out = backfill_module._parse_id_topic_pairs(raw)
        assert out == []

    def test_skips_blank_lines_in_middle(self, backfill_module):
        raw = "n.entry_id\nn.thread_topic\n\n01ABC\nt1\n\nCached execution: 0\n"
        out = backfill_module._parse_id_topic_pairs(raw)
        assert out == [("01ABC", "t1")]

    def test_drops_trailing_stats_lines(self, backfill_module):
        raw = (
            "n.entry_id\n"
            "n.thread_topic\n"
            "01ABC\n"
            "t1\n"
            "Cached execution: 0\n"
            "Query internal execution time: 0.5 milliseconds\n"
            "Graph removed, 0 keys\n"  # extra stats line shape
        )
        out = backfill_module._parse_id_topic_pairs(raw)
        assert out == [("01ABC", "t1")]

    def test_empty_input(self, backfill_module):
        assert backfill_module._parse_id_topic_pairs("") == []

    def test_only_headers(self, backfill_module):
        raw = "n.entry_id\nn.thread_topic\n"
        assert backfill_module._parse_id_topic_pairs(raw) == []

    def test_odd_trailing_token_logs_and_drops(self, backfill_module):
        # Trailing odd token without paired topic — log + break, don't include.
        raw = "n.entry_id\nn.thread_topic\n01ABC\nt1\n01DEF\n"
        out = backfill_module._parse_id_topic_pairs(raw)
        assert out == [("01ABC", "t1")]


class TestIsStatsLine:
    def test_cached_execution(self, backfill_module):
        assert backfill_module._is_stats_line("Cached execution: 0")
        assert backfill_module._is_stats_line("  Cached execution: 1  ")

    def test_query_internal(self, backfill_module):
        assert backfill_module._is_stats_line("Query internal execution time: 1.2 milliseconds")

    def test_graph_removed(self, backfill_module):
        assert backfill_module._is_stats_line("Graph removed, 5 keys")

    def test_data_row_is_not_stats(self, backfill_module):
        assert not backfill_module._is_stats_line("01KDWHAPSW61P3E2RSBS0MPR6G")
        assert not backfill_module._is_stats_line("[0.1, 0.2]")
        assert not backfill_module._is_stats_line("git-sync-refactor")


class TestUlidGuard:
    def test_real_ulid_accepted(self, backfill_module):
        # Sample from the production data we inspected this session.
        assert backfill_module._ULID_RE.match("01KDWHAPSW61P3E2RSBS0MPR6G")

    def test_quote_in_id_rejected(self, backfill_module):
        # Cypher injection vector — would close the string literal.
        assert not backfill_module._ULID_RE.match("01KD' DROP")

    def test_lowercase_rejected(self, backfill_module):
        # Crockford base32 uppercase only.
        assert not backfill_module._ULID_RE.match("01kdwhapsw61p3e2rsbs0mpr6g")

    def test_wrong_length_rejected(self, backfill_module):
        assert not backfill_module._ULID_RE.match("01KDWHAPSW")  # too short
        assert not backfill_module._ULID_RE.match("01KDWHAPSW61P3E2RSBS0MPR6GX")  # too long

    def test_excluded_chars_rejected(self, backfill_module):
        # Crockford base32 excludes I, L, O, U.
        assert not backfill_module._ULID_RE.match("0IKDWHAPSW61P3E2RSBS0MPR6G")
        assert not backfill_module._ULID_RE.match("0LKDWHAPSW61P3E2RSBS0MPR6G")
        assert not backfill_module._ULID_RE.match("0OKDWHAPSW61P3E2RSBS0MPR6G")
        assert not backfill_module._ULID_RE.match("0UKDWHAPSW61P3E2RSBS0MPR6G")


class TestResolveCanonicalTargets:
    def test_explicit_flags_passthrough(self, backfill_module, tmp_path):
        import argparse
        args = argparse.Namespace(
            target_database="my_db_t1",
            target_group_id="my_group",
            code_path=str(tmp_path),
        )
        db, gid = backfill_module._resolve_canonical_targets(args)
        assert db == "my_db_t1"
        assert gid == "my_group"


class TestLoadTransport:
    def test_loads_valid_jsonl(self, backfill_module, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps({"entry_id": "E1", "thread_topic": "t1", "embedding": [0.1]})
            + "\n"
            + json.dumps({"entry_id": "E2", "thread_topic": "t2", "embedding": [0.2]})
            + "\n"
        )
        out = backfill_module._load_transport(str(path))
        assert isinstance(out, list)
        assert [r["entry_id"] for r in out] == ["E1", "E2"]
        assert out[0]["embedding"] == [0.1]
        assert out[1]["thread_topic"] == "t2"

    def test_skips_blank_and_malformed_lines(self, backfill_module, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            "\n"
            + json.dumps({"entry_id": "E1", "thread_topic": "t1", "embedding": [0.1]})
            + "\n"
            + "this is not json\n"
            + json.dumps({"thread_topic": "no_id"})  # missing entry_id → skipped
            + "\n"
        )
        out = backfill_module._load_transport(str(path))
        assert [r["entry_id"] for r in out] == ["E1"]

    def test_dedups_by_entry_id_keeps_first(self, backfill_module, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps({"entry_id": "E1", "thread_topic": "t1", "embedding": [0.1]})
            + "\n"
            + json.dumps({"entry_id": "E1", "thread_topic": "t1-DUP", "embedding": [0.999]})
            + "\n"
        )
        out = backfill_module._load_transport(str(path))
        assert len(out) == 1
        assert out[0]["thread_topic"] == "t1"  # first wins
        assert out[0]["embedding"] == [0.1]


class TestCheckpoint:
    def test_load_checkpoint_empty_file(self, backfill_module, tmp_path):
        # Non-existent path → empty set.
        assert backfill_module._load_checkpoint(tmp_path / "nope.jsonl") == set()

    def test_load_checkpoint_skips_blank_lines(self, backfill_module, tmp_path):
        p = tmp_path / "cp.jsonl"
        p.write_text("E1\n\nE2\n  \nE3\n")
        out = backfill_module._load_checkpoint(p)
        assert out == {"E1", "E2", "E3"}

    def test_append_checkpoint_creates_parent(self, backfill_module, tmp_path):
        p = tmp_path / "deep" / "nested" / "cp.jsonl"
        backfill_module._append_checkpoint(p, "E_NEW")
        assert p.read_text() == "E_NEW\n"

    def test_append_is_additive(self, backfill_module, tmp_path):
        p = tmp_path / "cp.jsonl"
        p.write_text("E1\n")
        backfill_module._append_checkpoint(p, "E2")
        assert p.read_text() == "E1\nE2\n"


class TestCmdPushValidation:
    """Push iterates the transport file directly (no orphan-branch scan).

    Each transport record carries its own metadata (role/agent/etc.) and
    either an embedding (cache hit, fast path) or null (cache miss → API).
    """

    def _run_push(
        self,
        backfill_module,
        tmp_path,
        *,
        transport: list[dict],
        dry_run: bool,
    ):
        """Drive cmd_push with mocked stderr; return (rc, summary dict)."""
        import argparse
        import io
        import json as _json
        from unittest.mock import patch

        # Write the transport file on disk so the real loader is exercised.
        transport_path = tmp_path / "transport.jsonl"
        with open(transport_path, "w") as fh:
            for rec in transport:
                fh.write(_json.dumps(rec) + "\n")

        checkpoint_path = tmp_path / "cp.jsonl"

        args = argparse.Namespace(
            transport_file=str(transport_path),
            code_path="",
            threads_dir="",
            target_database="my_db_t1",
            target_group_id="my_group",
            checkpoint=str(checkpoint_path),
            dry_run=dry_run,
            limit=0,
        )

        # Capture summary written to stderr.
        stderr_buf = io.StringIO()

        with patch.object(backfill_module.sys, "stderr", stderr_buf):
            rc = backfill_module.cmd_push(args)

        # Last JSON object printed to stderr is the summary.
        out = stderr_buf.getvalue()
        summary_start = out.rfind('{\n  "summary"')
        assert summary_start >= 0, f"No summary in stderr: {out[-500:]}"
        summary = _json.loads(out[summary_start:])
        return rc, summary["summary"]

    def _full_record(self, eid: str, embedding=None) -> dict:
        """Build a transport record with full metadata."""
        return {
            "entry_id": eid,
            "thread_topic": "t1",
            "role": "implementer",
            "entry_type": "Note",
            "agent": "test",
            "timestamp": "",
            "title": "x",
            "body": "x",
            "embedding": embedding,
        }

    def test_dry_run_cached_with_good_embedding_counts_as_pushed(self, backfill_module, tmp_path):
        """Cache hit (good embedding present in transport) → counts as pushed in dry-run."""
        rc, summary = self._run_push(
            backfill_module,
            tmp_path,
            transport=[self._full_record("01KDWHAPSW61P3E2RSBS0MPR6G", embedding=[0.0] * 1024)],
            dry_run=True,
        )
        assert rc == 0
        assert summary["cache_hits"] == 1
        assert summary["api_calls"] == 0
        assert summary["pushed"] == 1
        assert summary["errored"] == 0

    def test_dry_run_null_embedding_is_api_path_counted_in_pushed(self, backfill_module, tmp_path):
        """Cache miss (null embedding in transport) → API path; dry-run still counts it as pushed."""
        rc, summary = self._run_push(
            backfill_module,
            tmp_path,
            transport=[self._full_record("01KDWHAPSW61P3E2RSBS0MPR6G", embedding=None)],
            dry_run=True,
        )
        assert rc == 0
        assert summary["cache_hits"] == 0
        assert summary["api_calls"] == 1
        assert summary["pushed"] == 1, (
            "Dry-run must count API-bound entries in `pushed` so the preview "
            "accurately represents total scope, not just cache hits."
        )

    def test_dry_run_missing_embedding_key_is_treated_as_cache_miss(self, backfill_module, tmp_path):
        """Transport record without an 'embedding' key at all → treated as cache miss (API path)."""
        rec = self._full_record("01KDWHAPSW61P3E2RSBS0MPR6G")
        del rec["embedding"]  # field missing entirely
        rc, summary = self._run_push(
            backfill_module,
            tmp_path,
            transport=[rec],
            dry_run=True,
        )
        # No KeyError. .get("embedding") returns None → API-path branch.
        assert rc == 0
        assert summary["cache_hits"] == 0
        assert summary["api_calls"] == 1
        assert summary["pushed"] == 1

    def test_cached_wrong_dim_is_errored_not_pushed(self, backfill_module, tmp_path):
        """Embedding present but wrong dim → errored, not silently pushed."""
        rc, summary = self._run_push(
            backfill_module,
            tmp_path,
            transport=[self._full_record("01KDWHAPSW61P3E2RSBS0MPR6G", embedding=[0.0] * 768)],
            dry_run=True,
        )
        assert summary["cache_hits"] == 0
        assert summary["api_calls"] == 0
        assert summary["errored"] == 1
        assert summary["pushed"] == 0
        assert rc == 2

    def test_cached_non_list_embedding_does_not_crash(self, backfill_module, tmp_path):
        """Pathological case: embedding field is a string instead of a list."""
        rc, summary = self._run_push(
            backfill_module,
            tmp_path,
            transport=[self._full_record("01KDWHAPSW61P3E2RSBS0MPR6G", embedding="not a list")],
            dry_run=True,
        )
        assert summary["errored"] == 1
        assert summary["pushed"] == 0
        assert rc == 2

    def test_checkpoint_skip_works(self, backfill_module, tmp_path):
        """Entries already in checkpoint are skipped (resume support)."""
        # Pre-populate checkpoint with one entry_id.
        cp = tmp_path / "cp.jsonl"
        cp.write_text("01KDWHAPSW61P3E2RSBS0MPR6G\n")

        # Use the standard helper but override checkpoint after.
        import argparse
        import io
        import json as _json
        from unittest.mock import patch

        transport_path = tmp_path / "transport.jsonl"
        with open(transport_path, "w") as fh:
            fh.write(_json.dumps(self._full_record("01KDWHAPSW61P3E2RSBS0MPR6G", embedding=[0.0] * 1024)) + "\n")
            fh.write(_json.dumps(self._full_record("01OTHERIDXXXXXXXXXXXXXXXXX", embedding=[0.0] * 1024)) + "\n")

        args = argparse.Namespace(
            transport_file=str(transport_path),
            code_path="",
            threads_dir="",
            target_database="my_db_t1",
            target_group_id="my_group",
            checkpoint=str(cp),
            dry_run=True,
            limit=0,
        )
        stderr_buf = io.StringIO()
        with patch.object(backfill_module.sys, "stderr", stderr_buf):
            backfill_module.cmd_push(args)
        out = stderr_buf.getvalue()
        summary_start = out.rfind('{\n  "summary"')
        summary = _json.loads(out[summary_start:])["summary"]
        assert summary["skipped_already_pushed"] == 1
        assert summary["pushed"] == 1  # only the second one
