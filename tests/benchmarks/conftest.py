"""Session-scoped fixtures for benchmark tests.

Builds a rich benchmark graph with 6 threads and 32 entries covering:
- Multiple Decision entries for decision recall
- Cross-thread references for discovery
- Temporal spread for time-based queries
- Named agents and roles for entity search
- Superseded decisions for stale fact resolution
- Entries with and without custom summaries for quality tests
- code_branch and commit_refs for sync awareness
- File path references for specificity tests
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from .ground_truth import (
    RelevanceSet,
    SearchModePair,
    build_paraphrase_pairs,
    build_recall_goldens,
)


# ============================================================================
# Auto-skip / Auto-deselect Logic
# ============================================================================


def pytest_collection_modifyitems(config, items):
    """Auto-skip benchmark tests unless explicitly targeted."""
    # Check if benchmarks directory was directly targeted
    file_or_dir = config.option.file_or_dir or []
    targeting_benchmarks = any(
        "benchmark" in str(f) for f in file_or_dir
    )

    # Check if -m option explicitly includes benchmark
    markexpr = str(config.getoption("-m", default=""))
    selecting_benchmark = "benchmark" in markexpr

    if targeting_benchmarks or selecting_benchmark:
        return

    # Otherwise, skip benchmark tests so default `pytest tests/` excludes them
    skip_marker = pytest.mark.skip(
        reason="benchmark tests: use 'pytest tests/benchmarks/' or '-m benchmark'"
    )
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip_marker)


def pytest_runtest_setup(item):
    """Auto-skip tests whose markers require unavailable services."""
    if item.get_closest_marker("needs_embedding"):
        try:
            with socket.create_connection(("localhost", 8080), timeout=2):
                pass
        except OSError:
            pytest.skip("needs_embedding: embedding server not running on :8080")


# ============================================================================
# Graph Builder Helpers
# ============================================================================


def _make_entry(
    entry_id: str,
    thread_topic: str,
    title: str,
    body: str,
    agent: str,
    role: str,
    entry_type: str,
    index: int,
    timestamp: str,
    summary: str = "",
    code_branch: str | None = None,
    commit_refs: list[str] | None = None,
    file_refs: list[str] | None = None,
    pr_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Build an entry node dict for the benchmark graph."""
    node: dict[str, Any] = {
        "type": "entry",
        "entry_id": entry_id,
        "thread_topic": thread_topic,
        "index": index,
        "agent": agent,
        "role": role,
        "entry_type": entry_type,
        "title": title,
        "timestamp": timestamp,
        "summary": summary or body[:100],
        "body": body,
    }
    if code_branch is not None:
        node["code_branch"] = code_branch
    if commit_refs:
        node["commit_refs"] = commit_refs
    if file_refs:
        node["file_refs"] = file_refs
    if pr_refs:
        node["pr_refs"] = pr_refs
    return node


def _make_thread_meta(
    topic: str,
    title: str,
    entries: list[dict[str, Any]],
    status: str = "OPEN",
    ball: str = "Claude",
    summary: str = "",
) -> dict[str, Any]:
    """Build a thread meta node dict."""
    return {
        "type": "thread",
        "topic": topic,
        "title": title,
        "status": status,
        "ball": ball,
        "summary": summary or f"Thread about {title.lower()}",
        "entry_count": len(entries),
        "last_updated": entries[-1]["timestamp"] if entries else "",
    }


def _write_graph(base_dir: Path, threads: list[dict[str, Any]]) -> Path:
    """Write benchmark graph to disk in per-thread format."""
    graph_dir = base_dir / "graph" / "baseline"
    graph_dir.mkdir(parents=True, exist_ok=True)

    for thread_data in threads:
        topic = thread_data["meta"]["topic"]
        thread_dir = graph_dir / "threads" / topic
        thread_dir.mkdir(parents=True, exist_ok=True)

        # Write meta.json
        (thread_dir / "meta.json").write_text(json.dumps(thread_data["meta"]))

        # Write entries.jsonl
        if thread_data["entries"]:
            with open(thread_dir / "entries.jsonl", "w") as f:
                for entry in thread_data["entries"]:
                    f.write(json.dumps(entry) + "\n")

    return base_dir


# ============================================================================
# Benchmark Thread Corpus
# ============================================================================


def _build_auth_design() -> dict[str, Any]:
    """auth-design: 5 entries -- Plan, Decision, Note, Ack, Closure."""
    topic = "auth-design"
    entries = [
        _make_entry(
            entry_id="BMAD001",
            thread_topic=topic,
            title="Authentication Design Plan",
            body=(
                "Spec: planner\n\n"
                "Proposing authentication architecture for the project. "
                "We need secure token-based authentication with industry-standard "
                "signing algorithms. The authentication module should handle "
                "login, session management, and token refresh.\n\n"
                "Key files: `src/auth/handler.py`, `src/auth/tokens.py`"
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Plan",
            index=0,
            timestamp="2025-01-05T09:00:00Z",
            file_refs=["src/auth/handler.py", "src/auth/tokens.py"],
        ),
        _make_entry(
            entry_id="BMAD002",
            thread_topic=topic,
            title="JWT with RS256 for Token Signing",
            body=(
                "Spec: planner\n\n"
                "Decision: Use JWT tokens with RS256 signing for authentication. "
                "RS256 provides asymmetric key signing which allows token "
                "verification without sharing the private key. This is the "
                "standard approach for microservice authentication architectures.\n\n"
                "Implementation in `src/auth/tokens.py:15`"
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Decision",
            index=1,
            timestamp="2025-01-05T11:00:00Z",
            summary="Decided JWT with RS256 for authentication token signing",
            file_refs=["src/auth/tokens.py"],
        ),
        _make_entry(
            entry_id="BMAD003",
            thread_topic=topic,
            title="Auth Handler Implementation",
            body=(
                "Spec: implementer\n\n"
                "Implemented the authentication handler with JWT validation. "
                "The handler verifies RS256-signed tokens and extracts claims. "
                "Added middleware for route protection at `src/auth/handler.py:42`.\n\n"
                "Security note: CSRF protection added to callback handler. "
                "All authentication endpoints now return structured error responses."
            ),
            agent="Claude (implementer)",
            role="implementer",
            entry_type="Note",
            index=2,
            timestamp="2025-01-06T10:00:00Z",
            file_refs=["src/auth/handler.py"],
        ),
        _make_entry(
            entry_id="BMAD004",
            thread_topic=topic,
            title="Auth Implementation Reviewed",
            body=(
                "Spec: critic\n\n"
                "Reviewed the authentication implementation. "
                "LGTM -- RS256 signing is correctly configured, "
                "middleware chain looks solid. Approved."
            ),
            agent="User (reviewer)",
            role="critic",
            entry_type="Ack",
            index=3,
            timestamp="2025-01-06T14:00:00Z",
        ),
        _make_entry(
            entry_id="BMAD005",
            thread_topic=topic,
            title="Auth Design Complete",
            body=(
                "Spec: pm\n\n"
                "Authentication design and implementation complete. "
                "JWT with RS256 is deployed and tested. Thread closed."
            ),
            agent="Claude (pm)",
            role="pm",
            entry_type="Closure",
            index=4,
            timestamp="2025-01-07T09:00:00Z",
            summary="Authentication design completed with JWT RS256 signing",
        ),
    ]
    meta = _make_thread_meta(
        topic=topic,
        title="Authentication Design",
        entries=entries,
        status="CLOSED",
        summary="Authentication architecture using JWT with RS256 token signing",
    )
    return {"meta": meta, "entries": entries}


def _build_api_refactor() -> dict[str, Any]:
    """api-refactor: 6 entries -- Plan, Note, Decision(REST), Note, Decision(GraphQL), Closure.

    Contains a superseded decision: entry[2] decides REST, entry[4] overrides to GraphQL.
    """
    topic = "api-refactor"
    entries = [
        _make_entry(
            entry_id="BMAR001",
            thread_topic=topic,
            title="API Refactoring Plan",
            body=(
                "Spec: planner\n\n"
                "Planning the API layer refactoring. Current REST endpoints "
                "are inconsistent and need consolidation. Evaluating whether "
                "to stay with REST or migrate to GraphQL.\n\n"
                "Affected files: `src/api/routes.py`, `src/api/schemas.py`"
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Plan",
            index=0,
            timestamp="2025-01-08T09:00:00Z",
            file_refs=["src/api/routes.py", "src/api/schemas.py"],
        ),
        _make_entry(
            entry_id="BMAR002",
            thread_topic=topic,
            title="REST API Endpoint Analysis",
            body=(
                "Spec: implementer\n\n"
                "Analyzed existing REST API endpoints. Found 23 endpoints "
                "with inconsistent naming and 5 redundant routes. "
                "REST conventions are mostly followed but response schemas "
                "vary across controllers.\n\n"
                "See `src/api/routes.py:100` for the routing table."
            ),
            agent="Claude (implementer)",
            role="implementer",
            entry_type="Note",
            index=1,
            timestamp="2025-01-08T14:00:00Z",
            file_refs=["src/api/routes.py"],
        ),
        _make_entry(
            entry_id="BMAR003",
            thread_topic=topic,
            title="REST API Protocol Decision",
            body=(
                "Spec: planner\n\n"
                "Decision: Use REST for the API protocol layer. "
                "REST is simpler to implement and our team has more experience. "
                "The inconsistencies can be fixed with better conventions."
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Decision",
            index=2,
            timestamp="2025-01-09T10:00:00Z",
            summary="Initial decision to use REST for API endpoints",
        ),
        _make_entry(
            entry_id="BMAR004",
            thread_topic=topic,
            title="REST Implementation Progress",
            body=(
                "Spec: implementer\n\n"
                "Started implementing REST endpoint consolidation. "
                "Merged 5 redundant routes. Standardized response schemas "
                "across all controllers. Progress at `src/api/controllers/`."
            ),
            agent="Claude (implementer)",
            role="implementer",
            entry_type="Note",
            index=3,
            timestamp="2025-01-10T09:00:00Z",
            file_refs=["src/api/controllers/"],
        ),
        _make_entry(
            entry_id="BMAR005",
            thread_topic=topic,
            title="API Protocol Decision Updated to GraphQL",
            body=(
                "Spec: planner\n\n"
                "Decision: Switch to GraphQL for the API protocol layer. "
                "After further analysis, GraphQL provides better type safety "
                "and eliminates over-fetching issues. This supersedes the "
                "earlier REST decision. The API protocol is now GraphQL.\n\n"
                "Migration plan: `src/api/graphql/schema.graphql`"
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Decision",
            index=4,
            timestamp="2025-01-11T10:00:00Z",
            summary="Updated API protocol decision to GraphQL, superseding REST",
            file_refs=["src/api/graphql/schema.graphql"],
        ),
        _make_entry(
            entry_id="BMAR006",
            thread_topic=topic,
            title="API Refactor Complete",
            body=(
                "Spec: pm\n\n"
                "API refactoring complete. Migrated to GraphQL. "
                "All endpoints consolidated and type-safe."
            ),
            agent="Claude (pm)",
            role="pm",
            entry_type="Closure",
            index=5,
            timestamp="2025-01-12T09:00:00Z",
        ),
    ]
    meta = _make_thread_meta(
        topic=topic,
        title="API Refactoring",
        entries=entries,
        status="CLOSED",
        summary="API layer refactoring from REST to GraphQL",
    )
    return {"meta": meta, "entries": entries}


def _build_deployment_pipeline() -> dict[str, Any]:
    """deployment-pipeline: 5 entries -- code_branch metadata, commit refs."""
    topic = "deployment-pipeline"
    entries = [
        _make_entry(
            entry_id="BMDP001",
            thread_topic=topic,
            title="Deployment Pipeline Design",
            body=(
                "Spec: planner\n\n"
                "Designing the CI/CD deployment pipeline. "
                "Need automated testing, staging deployments, and "
                "production rollout with canary releases.\n\n"
                "Config: `deploy/pipeline.yml`"
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Plan",
            index=0,
            timestamp="2025-01-10T09:00:00Z",
            code_branch="main",
            file_refs=["deploy/pipeline.yml"],
        ),
        _make_entry(
            entry_id="BMDP002",
            thread_topic=topic,
            title="CI Configuration for Feature Branch",
            body=(
                "Spec: implementer\n\n"
                "Configured CI/CD for the feature/deploy branch. "
                "Added test stages, linting, and deployment to staging. "
                "Pipeline config at `deploy/ci.yml:25`."
            ),
            agent="Claude (implementer)",
            role="implementer",
            entry_type="Note",
            index=1,
            timestamp="2025-01-10T14:00:00Z",
            code_branch="feature/deploy",
            commit_refs=["abc1234"],
            file_refs=["deploy/ci.yml"],
        ),
        _make_entry(
            entry_id="BMDP003",
            thread_topic=topic,
            title="GitHub Actions CI Decision",
            body=(
                "Spec: planner\n\n"
                "Decision: Use GitHub Actions for CI/CD pipeline. "
                "GitHub Actions integrates natively with our repo, "
                "has generous free-tier minutes, and supports "
                "matrix builds for multi-Python testing."
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Decision",
            index=2,
            timestamp="2025-01-11T09:00:00Z",
            code_branch="feature/deploy",
            commit_refs=["def5678"],
        ),
        _make_entry(
            entry_id="BMDP004",
            thread_topic=topic,
            title="Pipeline Testing Results",
            body=(
                "Spec: tester\n\n"
                "Tested the deployment pipeline on feature/deploy branch. "
                "All stages pass: lint, test, build, deploy-staging. "
                "Average pipeline time: 4 minutes."
            ),
            agent="Claude (tester)",
            role="tester",
            entry_type="Note",
            index=3,
            timestamp="2025-01-11T14:00:00Z",
            code_branch="feature/deploy",
        ),
        _make_entry(
            entry_id="BMDP005",
            thread_topic=topic,
            title="Pipeline Deployment Approved",
            body=(
                "Spec: pm\n\n"
                "Deployment pipeline approved and merged to main. "
                "CI/CD is now active on all branches."
            ),
            agent="User (ops)",
            role="pm",
            entry_type="Ack",
            index=4,
            timestamp="2025-01-12T09:00:00Z",
            code_branch="main",
        ),
    ]
    meta = _make_thread_meta(
        topic=topic,
        title="Deployment Pipeline",
        entries=entries,
        summary="CI/CD pipeline design using GitHub Actions",
    )
    return {"meta": meta, "entries": entries}


def _build_database_migration() -> dict[str, Any]:
    """database-migration: 5 entries -- temporal spread Jan 1-15, named agents."""
    topic = "database-migration"
    entries = [
        _make_entry(
            entry_id="BMDB001",
            thread_topic=topic,
            title="Database Migration Strategy",
            body=(
                "Spec: planner\n\n"
                "Planning the database migration from PostgreSQL 14 to 16. "
                "Need to handle schema changes, data backfill, and "
                "zero-downtime migration strategy.\n\n"
                "Migration scripts: `db/migrations/`"
            ),
            agent="Alice (dev)",
            role="planner",
            entry_type="Plan",
            index=0,
            timestamp="2025-01-01T09:00:00Z",
            file_refs=["db/migrations/"],
        ),
        _make_entry(
            entry_id="BMDB002",
            thread_topic=topic,
            title="Schema Change Analysis",
            body=(
                "Spec: implementer\n\n"
                "Analyzed schema changes required for PostgreSQL 16. "
                "Three tables need column type updates, two indexes "
                "need rebuilding. See `db/migrations/002_schema_update.sql:10`."
            ),
            agent="Bob (ops)",
            role="implementer",
            entry_type="Note",
            index=1,
            timestamp="2025-01-05T10:00:00Z",
            file_refs=["db/migrations/002_schema_update.sql"],
        ),
        _make_entry(
            entry_id="BMDB003",
            thread_topic=topic,
            title="Migration Script Implementation",
            body=(
                "Spec: implementer\n\n"
                "Implemented migration scripts for all schema changes. "
                "Added rollback support and data validation checks. "
                "Scripts located at `db/migrations/003_rollback.sql` "
                "and `db/migrations/004_validate.py:30`."
            ),
            agent="Alice (dev)",
            role="implementer",
            entry_type="Note",
            index=2,
            timestamp="2025-01-08T14:00:00Z",
            file_refs=[
                "db/migrations/003_rollback.sql",
                "db/migrations/004_validate.py",
            ],
        ),
        _make_entry(
            entry_id="BMDB004",
            thread_topic=topic,
            title="PostgreSQL 16 Migration Decision",
            body=(
                "Spec: planner\n\n"
                "Decision: Proceed with PostgreSQL 16 migration using "
                "blue-green deployment. Rollback plan tested and verified. "
                "Migration window: Saturday 2am-6am UTC."
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Decision",
            index=3,
            timestamp="2025-01-12T09:00:00Z",
            summary="Decided to migrate PostgreSQL 14 to 16 via blue-green deploy",
        ),
        _make_entry(
            entry_id="BMDB005",
            thread_topic=topic,
            title="Database Migration Complete",
            body=(
                "Spec: pm\n\n"
                "Database migration completed successfully. PostgreSQL 16 "
                "is now live. All validation checks passed. Thread closed."
            ),
            agent="Alice (dev)",
            role="pm",
            entry_type="Closure",
            index=4,
            timestamp="2025-01-15T16:00:00Z",
        ),
    ]
    meta = _make_thread_meta(
        topic=topic,
        title="Database Migration",
        entries=entries,
        status="CLOSED",
        summary="PostgreSQL 14 to 16 migration with blue-green deployment",
    )
    return {"meta": meta, "entries": entries}


def _build_performance_optimization() -> dict[str, Any]:
    """performance-optimization: 6 entries -- cross-thread refs, coordination failures.

    Coordination failures seeded:
    - F1 (ball confusion): BMPO002 -> BMPO003 agent switch without handoff
    - F3 (unacked decision): BMPO004 Decision with no subsequent ack from different agent
    """
    topic = "performance-optimization"
    entries = [
        _make_entry(
            entry_id="BMPO001",
            thread_topic=topic,
            title="Performance Optimization Plan",
            body=(
                "Spec: planner\n\n"
                "Planning performance optimization for the application. "
                "Profiling indicates the API layer is the bottleneck. "
                "Need to optimize database queries and add caching.\n\n"
                "Profiler output: `reports/profiler_output.txt`"
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Plan",
            index=0,
            timestamp="2025-01-13T09:00:00Z",
            file_refs=["reports/profiler_output.txt"],
        ),
        _make_entry(
            entry_id="BMPO002",
            thread_topic=topic,
            title="API Layer Profiling Results",
            body=(
                "Spec: implementer\n\n"
                "Profiled the API layer (related to api-refactor thread). "
                "The GraphQL resolver for user queries takes 800ms avg. "
                "Database N+1 queries identified in `src/api/resolvers.py:55`. "
                "Cache miss rate is 85% on frequently accessed endpoints."
            ),
            agent="Claude (implementer)",
            role="implementer",
            entry_type="Note",
            index=1,
            timestamp="2025-01-13T14:00:00Z",
            file_refs=["src/api/resolvers.py"],
        ),
        # F1 trigger: agent switches from Claude to User without Ack/Handoff
        _make_entry(
            entry_id="BMPO003",
            thread_topic=topic,
            title="Cache Implementation Started",
            body=(
                "Spec: implementer\n\n"
                "Started implementing caching layer. Using in-memory cache "
                "initially, will evaluate Redis later. Changes in "
                "`src/cache/manager.py` and `src/api/middleware.py:20`."
            ),
            agent="User (dev)",
            role="implementer",
            entry_type="Note",
            index=2,
            timestamp="2025-01-14T10:00:00Z",
            file_refs=["src/cache/manager.py", "src/api/middleware.py"],
        ),
        # F3 trigger: Decision with no subsequent ack from different agent
        _make_entry(
            entry_id="BMPO004",
            thread_topic=topic,
            title="Redis Caching Layer Decision",
            body=(
                "Spec: planner\n\n"
                "Decision: Use Redis for the caching layer. "
                "Redis provides persistence, TTL support, and cluster mode "
                "for horizontal scaling. In-memory cache insufficient for "
                "production workloads."
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Decision",
            index=3,
            timestamp="2025-01-14T14:00:00Z",
        ),
        _make_entry(
            entry_id="BMPO005",
            thread_topic=topic,
            title="Redis Caching Benchmarks",
            body=(
                "Spec: tester\n\n"
                "Benchmarked Redis caching layer. P95 latency dropped "
                "from 800ms to 120ms. Cache hit rate improved to 78%. "
                "Results in `reports/cache_benchmarks.json`."
            ),
            agent="Claude (tester)",
            role="tester",
            entry_type="Note",
            index=4,
            timestamp="2025-01-15T09:00:00Z",
            file_refs=["reports/cache_benchmarks.json"],
        ),
        _make_entry(
            entry_id="BMPO006",
            thread_topic=topic,
            title="Performance Targets Met",
            body=(
                "Spec: pm\n\n"
                "Performance optimization targets achieved. "
                "API response times within SLA. Closing."
            ),
            agent="Claude (pm)",
            role="pm",
            entry_type="Ack",
            index=5,
            timestamp="2025-01-15T14:00:00Z",
        ),
    ]
    meta = _make_thread_meta(
        topic=topic,
        title="Performance Optimization",
        entries=entries,
        summary="API performance optimization with Redis caching layer",
    )
    return {"meta": meta, "entries": entries}


def _build_security_audit() -> dict[str, Any]:
    """security-audit: 5 entries -- entries with and without custom summaries."""
    topic = "security-audit"
    entries = [
        _make_entry(
            entry_id="BMSA001",
            thread_topic=topic,
            title="Security Audit Plan",
            body=(
                "Spec: planner\n\n"
                "Planning security audit of the authentication and API layers. "
                "Focus areas: input validation, authorization checks, "
                "dependency vulnerabilities, and secrets management.\n\n"
                "Audit checklist: `docs/security/audit-checklist.md`"
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Plan",
            index=0,
            timestamp="2025-01-16T09:00:00Z",
            summary=(
                "Security audit planned covering authentication, API input "
                "validation, dependency scanning, and secrets management"
            ),
            file_refs=["docs/security/audit-checklist.md"],
        ),
        _make_entry(
            entry_id="BMSA002",
            thread_topic=topic,
            title="Input Validation Findings",
            body=(
                "Spec: critic\n\n"
                "Found 3 input validation issues:\n"
                "1. SQL injection risk in `src/api/queries.py:88` -- "
                "raw string interpolation in WHERE clause\n"
                "2. XSS vulnerability in `src/web/templates/user.html:15` -- "
                "unescaped user input in template\n"
                "3. Path traversal in `src/api/files.py:42` -- "
                "user-controlled file path without sanitization\n\n"
                "Severity: HIGH for #1 and #3, MEDIUM for #2."
            ),
            agent="Claude (critic)",
            role="critic",
            entry_type="Note",
            index=1,
            timestamp="2025-01-16T14:00:00Z",
            summary=(
                "Three input validation vulnerabilities found: SQL injection, "
                "XSS, and path traversal in API and web layers"
            ),
            file_refs=[
                "src/api/queries.py",
                "src/web/templates/user.html",
                "src/api/files.py",
            ],
        ),
        _make_entry(
            entry_id="BMSA003",
            thread_topic=topic,
            title="Security Remediation Decision",
            body=(
                "Spec: planner\n\n"
                "Decision: Prioritize fixing all HIGH severity issues before "
                "next release. Use parameterized queries for SQL, escape all "
                "template output, and add path canonicalization. "
                "MEDIUM issues scheduled for following sprint."
            ),
            agent="Claude (planner)",
            role="planner",
            entry_type="Decision",
            index=2,
            timestamp="2025-01-17T09:00:00Z",
        ),
        _make_entry(
            entry_id="BMSA004",
            thread_topic=topic,
            title="Security Fixes Implemented",
            body=(
                "Spec: implementer\n\n"
                "Fixed all HIGH severity issues:\n"
                "- Parameterized queries in `src/api/queries.py:88`\n"
                "- Template auto-escaping in `src/web/templates/user.html:15`\n"
                "- Path canonicalization in `src/api/files.py:42`\n\n"
                "All fixes verified with regression tests."
            ),
            agent="Claude (implementer)",
            role="implementer",
            entry_type="Note",
            index=3,
            timestamp="2025-01-17T14:00:00Z",
            file_refs=[
                "src/api/queries.py",
                "src/web/templates/user.html",
                "src/api/files.py",
            ],
        ),
        _make_entry(
            entry_id="BMSA005",
            thread_topic=topic,
            title="Security Audit Complete",
            body=(
                "Spec: pm\n\n"
                "Security audit completed. All HIGH severity issues "
                "remediated and verified. MEDIUM issues tracked in backlog. "
                "Thread closed."
            ),
            agent="Claude (pm)",
            role="pm",
            entry_type="Closure",
            index=4,
            timestamp="2025-01-18T09:00:00Z",
            summary="Security audit completed, all HIGH issues fixed",
        ),
    ]
    meta = _make_thread_meta(
        topic=topic,
        title="Security Audit",
        entries=entries,
        status="CLOSED",
        summary="Security audit of authentication and API layers",
    )
    return {"meta": meta, "entries": entries}


def _build_benchmark_threads() -> list[dict[str, Any]]:
    """Build all 6 benchmark threads."""
    return [
        _build_auth_design(),
        _build_api_refactor(),
        _build_deployment_pipeline(),
        _build_database_migration(),
        _build_performance_optimization(),
        _build_security_audit(),
    ]


# ============================================================================
# Session-Scoped Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def benchmark_graph(tmp_path_factory) -> Path:
    """Seed a rich benchmark graph with 6 threads, 32 entries."""
    base = tmp_path_factory.mktemp("benchmark")
    threads = _build_benchmark_threads()
    return _write_graph(base, threads)


@pytest.fixture(scope="session")
def recall_goldens() -> list[RelevanceSet]:
    """Gold QA pairs for memory recall tests."""
    return build_recall_goldens()


@pytest.fixture(scope="session")
def superseded_decisions() -> list[dict[str, Any]]:
    """Pairs of older/newer decisions for stale fact tests."""
    return [
        {
            "query": "API protocol",
            "older_id": "BMAR003",
            "newer_id": "BMAR005",
        },
    ]


@pytest.fixture(scope="session")
def entries_with_summaries() -> list[dict[str, Any]]:
    """Entries that have both body and explicit summary for quality tests."""
    # These entries have manually-written summaries that reuse body keywords.
    # keyword_coverage measures exact token overlap (not stemming), so
    # summaries must use the same word forms as the body.
    return [
        {
            "id": "BMAD002",
            "body": (
                "Decision: Use JWT tokens with RS256 signing for authentication. "
                "RS256 provides asymmetric key signing which allows token "
                "verification without sharing the private key. This is the "
                "standard approach for microservice authentication architectures."
            ),
            "summary": "JWT tokens RS256 signing authentication key verification",
        },
        {
            "id": "BMSA001",
            "body": (
                "Planning security audit of the authentication and API layers. "
                "Focus areas: input validation, authorization checks, "
                "dependency vulnerabilities, and secrets management. "
                "Audit checklist covers all critical security controls."
            ),
            "summary": "Security audit authentication API input validation secrets",
        },
        {
            "id": "BMSA002",
            "body": (
                "Found 3 input validation issues: "
                "SQL injection risk in queries using raw string interpolation, "
                "XSS vulnerability in templates with unescaped user input, "
                "path traversal in files without sanitization. "
                "Severity HIGH for injection and traversal, MEDIUM for XSS."
            ),
            "summary": (
                "Input validation issues found: SQL injection queries, "
                "XSS vulnerability templates, path traversal files"
            ),
        },
        {
            "id": "BMSA005",
            "body": (
                "Security audit completed successfully. All HIGH severity issues "
                "have been remediated and verified through regression testing. "
                "MEDIUM severity issues are tracked in the backlog for next sprint."
            ),
            "summary": "Security audit completed HIGH severity issues remediated",
        },
        {
            "id": "BMDB004",
            "body": (
                "Decision: Proceed with PostgreSQL migration using blue-green "
                "deployment strategy. Rollback plan tested and verified. "
                "Migration window scheduled for Saturday maintenance period. "
                "All validation checks confirmed ready."
            ),
            "summary": "PostgreSQL migration blue-green deployment rollback plan verified",
        },
    ]


@pytest.fixture(scope="session")
def structured_thread() -> dict[str, Any]:
    """Thread with diverse entry types for communication tests.

    Uses auth-design which has good protocol structure:
    Plan -> Decision -> Note -> Ack -> Closure with multiple roles.
    """
    return _build_auth_design()


@pytest.fixture(scope="session")
def all_entries() -> list[dict[str, Any]]:
    """All entries across all benchmark threads."""
    entries = []
    for thread_data in _build_benchmark_threads():
        entries.extend(thread_data["entries"])
    return entries


@pytest.fixture(scope="session")
def branch_entries() -> dict[str, Any]:
    """Entry info for code_branch filtering tests."""
    return {
        "topic": "deployment-pipeline",
        "branch": "feature/deploy",
        # IDs of entries tagged with this branch
        "expected_ids": {"BMDP002", "BMDP003", "BMDP004"},
    }


@pytest.fixture(scope="session")
def entries_with_commits() -> list[dict[str, Any]]:
    """Entries with code_commit metadata for provenance tests."""
    return [
        {
            "entry_id": "BMDP002",
            "title": "CI Configuration for Feature Branch",
            "commit": "abc1234",
        },
        {
            "entry_id": "BMDP003",
            "title": "GitHub Actions CI Decision",
            "commit": "def5678",
        },
    ]


@pytest.fixture(scope="session")
def paraphrase_pairs() -> list[SearchModePair]:
    """Keyword/semantic comparison pairs for needs_embedding tests."""
    return build_paraphrase_pairs()
