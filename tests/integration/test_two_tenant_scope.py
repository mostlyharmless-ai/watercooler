"""Two-tenant isolation + provenance fixture (V1 of the completion plan).

Plan of record: thread ``audit-transport-modes-hosted-db-2026-07``,
completion plan v3 (01KWXZEXDZ7787DZ7859VV1YWH) item V1 and completion
sequence 01KWZFP6RCYTBAY3JHE63JKJH0 wave 1.

Runs against a REAL FalkorDB (the point of the fixture — the class of
failure it guards shipped precisely because no test exercised a real
multi-tenant graph). Only the LLM and embedder are stubbed, at the
graphiti-core client-class level, so ingestion, graph writes, tenant
partitioning, and the provenance heal all execute their production code
paths deterministically with no external API.

Asserts the four user-visible capabilities from plan v3:
1. a tenant's write lands in that tenant's graph and no other;
2. a cross-tenant caller hint is refused in strict mode (the policy
   core every write path calls — ``auth.scope.enforce_caller_hint``);
3. ``resolve_episode_entry_ids`` (the same-request heal behind
   ``smart_query(resolve_provenance=true)``) resolves a tenant's
   episodes from its graph's durable ``entry:<ULID>`` markers;
4. resolution survives a simulated redeploy (mapping-cache wipe) and
   heals the cache back — and one tenant's resolver cannot see another
   tenant's episodes.

Mark: @pytest.mark.integration_falkor (excluded from the default CI
gate; run by the dedicated two-tenant FalkorDB job — V2).
"""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration_falkor

FALKOR_HOST = "localhost"
FALKOR_PORT = 6379

TENANT_A_DB = "pytest__two_tenant_scope_a_t2"
TENANT_B_DB = "pytest__two_tenant_scope_b_t2"

ENTRY_A = "01TESTAAAAAAAAAAAAAAAAAAAA"
ENTRY_B = "01TESTBBBBBBBBBBBBBBBBBBBB"

EMBEDDING_DIM = 64


def _falkordb_reachable() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((FALKOR_HOST, FALKOR_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


requires_falkordb = pytest.mark.skipif(
    not _falkordb_reachable(),
    reason=f"FalkorDB not reachable at {FALKOR_HOST}:{FALKOR_PORT}",
)


# ---------------------------------------------------------------------------
# Deterministic stubs — patched at the graphiti-core CLASS level so the
# clients Graphiti bundles at construction are already stubbed. Everything
# else (drivers, graph writes, search, provenance parsing) is real.
# ---------------------------------------------------------------------------


def _hash_vector(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        out.extend(b / 255.0 for b in h)
        counter += 1
    return out[:dim]


@pytest.fixture
def stubbed_graphiti_clients(monkeypatch):
    """Stub LLM + embedder deterministically; keep the graph real."""
    # Never attempt llama-server auto-start from tests: on CI runners the
    # attempt spawns a startup worker thread that is still alive at
    # interpreter exit, and Python 3.12 aborts the whole pytest process
    # ("FATAL: exception not rethrown", exit 134) AFTER all tests pass.
    monkeypatch.setenv("WATERCOOLER_AUTO_START_SERVICES", "0")
    from graphiti_core.embedder.openai import OpenAIEmbedder
    from graphiti_core.llm_client.client import LLMClient
    from graphiti_core.llm_client.openai_generic_client import (
        OpenAIGenericClient,
    )

    async def fake_generate_response(
        self, messages, response_model=None, *args, **kwargs
    ):
        name = getattr(response_model, "__name__", "")
        if name == "ExtractedEntities":
            return {"extracted_entities": []}
        if name == "ExtractedEdges":
            return {"edges": []}
        # Zero-entity episodes should not need any other LLM call; a loud
        # failure here beats a silently-wrong canned answer.
        raise AssertionError(
            f"stub LLM got unexpected response_model={name!r} — extend the "
            "stub deliberately if the pipeline legitimately needs it"
        )

    async def fake_create(self, input_data):
        text = input_data if isinstance(input_data, str) else json.dumps(
            input_data, default=str
        )
        return _hash_vector(text)

    async def fake_create_batch(self, input_data_list):
        return [_hash_vector(str(t)) for t in input_data_list]

    # OpenAIGenericClient overrides generate_response, so the base-class
    # patch alone never intercepts — patch both.
    monkeypatch.setattr(LLMClient, "generate_response", fake_generate_response)
    monkeypatch.setattr(
        OpenAIGenericClient, "generate_response", fake_generate_response
    )
    monkeypatch.setattr(OpenAIEmbedder, "create", fake_create)
    monkeypatch.setattr(OpenAIEmbedder, "create_batch", fake_create_batch)


def _make_backend(database: str, index_path: Path):
    from watercooler_memory.backends.graphiti import (
        GraphitiBackend,
        GraphitiConfig,
    )

    config = GraphitiConfig(
        falkordb_host=FALKOR_HOST,
        falkordb_port=FALKOR_PORT,
        database=database,
        llm_api_base="http://stub.invalid/v1",
        llm_api_key="stub-key",
        llm_model="stub-model",
        embedding_api_base="http://stub.invalid/v1",
        embedding_api_key="stub-key",
        embedding_dim=EMBEDDING_DIM,
        entry_episode_index_path=index_path,
        auto_save_index=True,
    )
    return GraphitiBackend(config)


def _drop_graph(database: str) -> None:
    from falkordb import FalkorDB

    db = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT)
    try:
        db.select_graph(database).delete()
    except Exception:
        pass  # graph may not exist


def _episode_count(database: str) -> int:
    from falkordb import FalkorDB

    db = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT)
    try:
        res = db.select_graph(database).query(
            "MATCH (e:Episodic) RETURN count(e)"
        )
        return int(res.result_set[0][0])
    except Exception:
        return 0


@pytest.fixture
async def two_tenants(stubbed_graphiti_clients, tmp_path):
    """Two isolated tenant backends against the same real FalkorDB.

    Async fixture so the backends' drivers are closed INSIDE the test's
    event loop. Leaking them to interpreter exit aborts the whole pytest
    process on CI runners ("FATAL: exception not rethrown", SIGABRT 134)
    even after every test passed — first CI run of PR #1090.
    """
    _drop_graph(TENANT_A_DB)
    _drop_graph(TENANT_B_DB)

    backend_a = _make_backend(TENANT_A_DB, tmp_path / "home_a" / "index.json")
    backend_b = _make_backend(TENANT_B_DB, tmp_path / "home_b" / "index.json")
    (tmp_path / "home_a").mkdir(exist_ok=True)
    (tmp_path / "home_b").mkdir(exist_ok=True)

    yield backend_a, backend_b

    for backend in (backend_a, backend_b):
        try:
            await backend.aclose()
        except Exception:
            pass
    _drop_graph(TENANT_A_DB)
    _drop_graph(TENANT_B_DB)


async def _ingest(backend, *, entry_id: str, topic: str, body: str,
                  with_metadata: bool = True, with_markers: bool = True):
    """Ingest one episode through the production direct-add path.

    ``with_metadata`` controls the A1-full first-class provenance
    properties (episode_metadata → fork-persisted node properties);
    ``with_markers`` controls the legacy durable text markers. Both on is
    the production shape; the split lets tests isolate each signal.
    """
    return await backend.add_episode_direct(
        name=f"{entry_id}: {topic}" if with_markers else topic,
        episode_body=body,
        source_description=(
            f"thread:{topic} | hybrid_handoff | entry:{entry_id}"
            if with_markers
            else "no markers here"
        ),
        reference_time=datetime.now(timezone.utc),
        group_id=backend.config.database,
        episode_metadata=(
            {
                "entry_id": entry_id,
                "thread_id": topic,
                "chunk_index": 1,
                "total_chunks": 1,
            }
            if with_metadata
            else None
        ),
    )


# ---------------------------------------------------------------------------
# 1 + 4b — tenant partitioning of writes and reads
# ---------------------------------------------------------------------------


@requires_falkordb
class TestTenantIsolation:
    @pytest.mark.anyio
    async def test_write_lands_in_own_tenant_graph_only(self, two_tenants):
        backend_a, backend_b = two_tenants

        result = await _ingest(
            backend_b,
            entry_id=ENTRY_B,
            topic="tenant-b-topic",
            body="Tenant B decided to use OAuth2 for service auth.",
        )
        assert result.get("episode_uuid")

        assert _episode_count(TENANT_B_DB) == 1
        assert _episode_count(TENANT_A_DB) == 0

    @pytest.mark.anyio
    async def test_resolver_cannot_see_other_tenants_episodes(self, two_tenants):
        backend_a, backend_b = two_tenants

        result = await _ingest(
            backend_b,
            entry_id=ENTRY_B,
            topic="tenant-b-topic",
            body="Tenant B private fact.",
        )
        uuid_b = result["episode_uuid"]

        resolved_by_a = await backend_a.resolve_episode_entry_ids([uuid_b])
        assert resolved_by_a == {}, (
            "tenant A's resolver must not resolve tenant B's episode"
        )


# ---------------------------------------------------------------------------
# 2 — cross-tenant caller hints are refused in strict mode
# ---------------------------------------------------------------------------


class TestCrossTenantWriteRefusal:
    """Pins the policy core every scoped write path calls
    (``tools/memory.py`` imports ``enforce_caller_hint`` for exactly this).
    No database needed — this is the authorization decision itself."""

    def test_mismatched_hint_refused_in_strict_mode(self, monkeypatch):
        from watercooler_mcp.auth.scope import (
            ScopeResolutionError,
            enforce_caller_hint,
        )

        monkeypatch.setenv("WATERCOOLER_STRICT_SCOPE", "1")
        with pytest.raises(ScopeResolutionError):
            enforce_caller_hint(
                derived="tenant_a_group", caller_supplied="tenant_b_group"
            )

    def test_matching_hint_passes(self, monkeypatch):
        from watercooler_mcp.auth.scope import enforce_caller_hint

        monkeypatch.setenv("WATERCOOLER_STRICT_SCOPE", "1")
        enforce_caller_hint(
            derived="tenant_a_group", caller_supplied="tenant_a_group"
        )

    def test_strict_mode_defaults_on(self, monkeypatch):
        from watercooler_mcp.auth.scope import strict_mode

        monkeypatch.delenv("WATERCOOLER_STRICT_SCOPE", raising=False)
        assert strict_mode() is True


# ---------------------------------------------------------------------------
# 3 + 4 — provenance resolves from the graph and survives a redeploy
# ---------------------------------------------------------------------------


@requires_falkordb
class TestProvenanceHealAndRedeploySurvival:
    @pytest.mark.anyio
    async def test_resolves_from_durable_graph_markers(self, two_tenants):
        _, backend_b = two_tenants

        result = await _ingest(
            backend_b,
            entry_id=ENTRY_B,
            topic="tenant-b-topic",
            body="Tenant B fact with durable provenance markers.",
        )
        uuid_b = result["episode_uuid"]

        resolved = await backend_b.resolve_episode_entry_ids([uuid_b])
        assert resolved == {uuid_b: ENTRY_B}

    @pytest.mark.anyio
    async def test_survives_simulated_redeploy_and_heals_cache(
        self, two_tenants
    ):
        """The Jay scenario: the mapping cache is gone (redeploy wipe /
        never built for this tenant); resolution must still succeed from
        the graph and write the cache back through."""
        _, backend_b = two_tenants

        result = await _ingest(
            backend_b,
            entry_id=ENTRY_B,
            topic="tenant-b-topic",
            body="Tenant B fact that must survive a redeploy.",
        )
        uuid_b = result["episode_uuid"]

        # Simulated redeploy: wipe the ephemeral mapping cache.
        index_path = backend_b.config.entry_episode_index_path
        if index_path.exists():
            index_path.unlink()

        resolved = await backend_b.resolve_episode_entry_ids([uuid_b])
        assert resolved == {uuid_b: ENTRY_B}, (
            "provenance must resolve from the graph with no cache present"
        )

        # Write-through: the heal repopulates the cache for future requests.
        assert index_path.exists(), (
            "heal must write the recovered mapping back to the cache"
        )
        cache = json.loads(index_path.read_text())
        assert ENTRY_B in json.dumps(cache), (
            "healed cache must contain the recovered entry mapping"
        )


# ---------------------------------------------------------------------------
# A1-full — first-class provenance properties (fork-persisted), property-first
# resolution, and the per-tenant backfill
# ---------------------------------------------------------------------------


def _episode_props(database: str, uuid: str) -> dict:
    from falkordb import FalkorDB

    db = FalkorDB(host=FALKOR_HOST, port=FALKOR_PORT)
    res = db.select_graph(database).query(
        "MATCH (e:Episodic {uuid: $uuid}) "
        "RETURN e.entry_id, e.thread_id, e.chunk_index, e.total_chunks",
        {"uuid": uuid},
    )
    row = res.result_set[0]
    return {
        "entry_id": row[0],
        "thread_id": row[1],
        "chunk_index": row[2],
        "total_chunks": row[3],
    }


@requires_falkordb
class TestFirstClassProvenance:
    @pytest.mark.anyio
    async def test_ingest_stamps_provenance_properties(self, two_tenants):
        """A1-full: episodes are BORN with first-class provenance."""
        _, backend_b = two_tenants
        result = await _ingest(
            backend_b, entry_id=ENTRY_B, topic="tenant-b-topic",
            body="born stamped",
        )
        props = _episode_props(TENANT_B_DB, result["episode_uuid"])
        assert props == {
            "entry_id": ENTRY_B,
            "thread_id": "tenant-b-topic",
            "chunk_index": 1,
            "total_chunks": 1,
        }

    @pytest.mark.anyio
    async def test_resolution_works_from_property_alone(self, two_tenants):
        """No markers anywhere — the first-class property alone resolves.
        This is the post-A1 world where the cache and markers are both
        merely auxiliary."""
        _, backend_b = two_tenants
        result = await _ingest(
            backend_b, entry_id=ENTRY_B, topic="tenant-b-topic",
            body="property only", with_markers=False,
        )
        uuid_b = result["episode_uuid"]

        index_path = backend_b.config.entry_episode_index_path
        if index_path.exists():
            index_path.unlink()

        resolved = await backend_b.resolve_episode_entry_ids([uuid_b])
        assert resolved == {uuid_b: ENTRY_B}

    @pytest.mark.anyio
    async def test_backfill_stamps_marker_episodes_and_reports_orphans(
        self, two_tenants
    ):
        """Pre-A1 episodes (markers, no properties) get stamped by the
        per-tenant backfill; unmarked episodes are reported as orphans,
        never guessed. Idempotent on re-run."""
        _, backend_b = two_tenants
        legacy = await _ingest(
            backend_b, entry_id=ENTRY_B, topic="tenant-b-topic",
            body="legacy marker episode", with_metadata=False,
        )
        orphan = await _ingest(
            backend_b, entry_id=ENTRY_A, topic="tenant-b-topic",
            body="no provenance at all", with_metadata=False,
            with_markers=False,
        )

        report = await __import__("asyncio").to_thread(
            backend_b.backfill_provenance_properties
        )
        assert report["stamped"] == 1
        assert [o["uuid"] for o in report["orphans"]] == [
            orphan["episode_uuid"]
        ]

        props = _episode_props(TENANT_B_DB, legacy["episode_uuid"])
        assert props["entry_id"] == ENTRY_B
        assert props["thread_id"] == "tenant-b-topic"

        # Idempotent: the stamped episode is not re-examined.
        report2 = await __import__("asyncio").to_thread(
            backend_b.backfill_provenance_properties
        )
        assert report2["stamped"] == 0
        assert len(report2["orphans"]) == 1

    @pytest.mark.anyio
    async def test_backfill_repairs_partial_provenance_rows(self, two_tenants):
        """PR #1101 review finding 1: an episode born with entry_id but
        missing thread/chunk fields must be repairable — partial provenance
        must never become permanent."""
        _, backend_b = two_tenants
        # Simulate a partial-stamping caller: metadata bypassing the
        # boundary normalization, entry_id only.
        result = await backend_b.add_episode_direct(
            name=f"{ENTRY_B}: partial",
            episode_body="partial provenance",
            source_description=f"thread:tenant-b-topic | entry:{ENTRY_B}",
            reference_time=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            group_id=backend_b.config.database,
            episode_metadata={"entry_id": ENTRY_B, "chunk_index": None,
                              "total_chunks": None, "thread_id": None},
        )

        report = await __import__("asyncio").to_thread(
            backend_b.backfill_provenance_properties
        )
        assert report["stamped"] == 1

        props = _episode_props(TENANT_B_DB, result["episode_uuid"])
        assert props == {
            "entry_id": ENTRY_B,
            "thread_id": "tenant-b-topic",
            "chunk_index": 1,
            "total_chunks": 1,
        }

        report2 = await __import__("asyncio").to_thread(
            backend_b.backfill_provenance_properties
        )
        assert report2["stamped"] == 0
