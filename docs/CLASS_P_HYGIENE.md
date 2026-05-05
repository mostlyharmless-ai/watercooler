# Class P storage hygiene

Reference for the egress-class data model from Move 6 of the
security consolidation plan v5.1. Every place watercooler emits
data crossing a process boundary — log emit, JSON dump, HTTP call,
subprocess, file write, MCP tool return — is classified into one
of two categories:

| Class | Means | Hygiene |
|---|---|---|
| **Class P** (primary) | Primary user data the system is *supposed* to retain unredacted (thread entries, decision records, tool return payloads, memory queue bodies) | Storage controls (0600 perms, scope-tagged paths, bounded retention); NO diagnostic copies |
| **Class D** (diagnostic) | Logs, traces, telemetry, error messages — emit to operators, not users; subject to redaction | Pattern-based redaction (`Secret` wrapper, `redact_value`, `redact_object`); structured snapshots routed through `SecretJSONEncoder` |

The *same call shape* (`Path.write_text`, `json.dumps`, etc.) can be
either class depending on intent — a `write_text` writing a thread
entry projection is Class P; the same call writing a daemon
snapshot is Class D. The author knows which; the audit scanner
can't infer it. So Class P sites must carry an explicit
annotation.

## Class P annotation convention

```python
# egress-class: primary
projection_path.write_text(thread_body)
```

or

```python
projection_path.write_text(thread_body)  # egress-class: primary
```

**Proximity rule**: the annotation must appear on the same line as
the call **or** the line immediately above. Anything further away
is rejected by `watercooler_mcp.audit.egress_inventory.classify_site`
because comments separated from their call rot — moving code or
inserting lines silently invalidates the label.

Anything not annotated is Class D by default. This errs on the
side of redaction.

## Class P sites today

This is the *intended* inventory. **None of these surfaces carry
the `# egress-class: primary` annotation in code yet** — Sprint 3
will add the annotations and baseline the allowlist. The egress-
inventory CI gate runs in observation mode today (scans,
classifies, asserts the data shape; does not yet fail on
unannotated Class P).

| Surface | File | Annotation | Storage hygiene |
|---|---|---|---|
| Thread entry projection writes | `commands.py` (and downstream `fs.py`) | not annotated yet (Sprint 3) | git's own perms apply (`.git/`) |
| MCP tool return payloads (read tools) | `tools/thread_query.py`, `tools/semantic.py` | not annotated yet (Sprint 3) | in-memory only — no at-rest hygiene applies |
| Git commit body / footer writes | `sync/primitives.py` | not annotated yet (Sprint 3) | git's own perms apply (`.git/`) |
| Memory queue payload bodies | `memory_queue/queue.py` | not annotated yet (Sprint 3) | explicit `0o600` in `_atomic_write` + dead-letter / receipt writes |
| Daemon findings JSONL + checkpoints | `daemons/state.py` | not annotated yet (Sprint 3) | explicit `0o600` in `append_findings` + `save_checkpoint` |

## Class P storage controls

All five controls from plan v5.1:

| Control | Where | Enforcement |
|---|---|---|
| **Scope isolation** | All Class P paths | Covered by Move 1 (`auth/scope.py:ResolvedScope.namespace`) + Move 3 (`derive_stdio_namespace`) |
| **0600 permissions** | Memory queue files, projection working files, findings JSONL | `tests/integration/test_class_p_permissions.py` (no-world-bits today; Sprint 3 tightens to exact 0600) |
| **Bounded retention** | Queue intermediate state | Existing TTL in `MemoryTaskQueue`; documented here |
| **No diagnostic copies** | No `logger.debug(entry_body)`, no `print(payload)` | Egress-inventory CI gate (`tests/integration/test_egress_inventory.py`) catches via Class D scan |
| **Scope-tagged paths** | Class P at-rest files | Path encodes scope; Move 3 contract |

## Class D adapters

Class D sites must redact secret-shaped substrings before egress.
The Move 4 secrets gateway provides the primitives:

| Use case | Tool |
|---|---|
| Secret value in code | `Secret(value, label=...)` — fails loudly under naive serialization; only `.reveal()` returns the raw bytes |
| Secret in nested log payload | `redact_object(payload)` (recursive) |
| Free-form string with embedded tokens | `redact_value(s)` (pattern-based) |
| Structured JSON output | `json.dumps(obj, cls=SecretJSONEncoder)` |

## Egress-inventory CI gate

`tests/integration/test_egress_inventory.py` runs the AST scanner
against `src/watercooler` + `src/watercooler_mcp` on every CI run.
Today it runs in **observation mode** — it counts sites and
verifies classification produces a deterministic answer. Sprint 3
work will:

1. Annotate every existing Class P site with the canonical comment.
2. Baseline the allowlist of permitted Class P sites in the test
   fixture.
3. Flip the `WATERCOOLER_EGRESS_INVENTORY_STRICT` flag default to
   `1` so a new unannotated Class P site fails CI.

## NFS / network-filesystem caveat

Some POSIX permission semantics (notably `fcntl` advisory locks)
behave differently on NFS. The Class P contract assumes a local
filesystem; deployments running watercooler state on NFS should
treat the 0600 invariant as best-effort and add filesystem-level
ACLs as a belt-and-suspenders measure.

## References

- Plan v5.1: thread `security-audit-2026-04-28`
- Move 1 (scope authority): `src/watercooler_mcp/auth/scope.py`
- Move 3 (canonical-stdio-namespace): `src/watercooler_mcp/auth/scope.py:derive_stdio_namespace`
- Move 4 (secrets gateway): `src/watercooler_mcp/secrets/gateway.py`
- Move 5 (file lock): `src/watercooler_mcp/sync/file_lock.py`
- Move 6 (this doc + egress inventory): `src/watercooler_mcp/audit/egress_inventory.py`
