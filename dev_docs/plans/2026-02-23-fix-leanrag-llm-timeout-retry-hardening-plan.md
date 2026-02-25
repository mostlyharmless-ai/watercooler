---
title: "fix: Add timeout/retry hardening to LeanRAG LLM calls"
type: fix
status: completed
date: 2026-02-23
thread: t3-memory-live-test
deepened: 2026-02-23
reviewed: 2026-02-22
---

# fix: Add timeout/retry hardening to LeanRAG LLM calls

## Enhancement Summary

**Deepened on:** 2026-02-23
**Agents used:** architecture-strategist, kieran-python-reviewer, performance-oracle, security-sentinel, pattern-recognition-specialist, code-simplicity-reviewer, best-practices-researcher, framework-docs-researcher, learnings-researcher

### Key Improvements from Research

1. **Simplified architecture**: Collapsed from 5 phases to 2 phases (~35% fewer LOC) — eager module-level client init replaces double-checked locking, env bridge replaces pass-through dataclass fields
2. **Default value alignment**: Resolved inconsistency between TOML default (60.0), LeanRAGConfig default (120.0), and config.yaml default (120) — standardize on 120.0 throughout
3. **Backoff formula consistency**: Aligned with canonical `_http_post_with_retry` pattern in `_utils.py:139` using `_BASE_DELAY * (2 ** attempt)` instead of ad-hoc `2 * (2 ** attempt)`
4. **Testability**: Added `_reset_clients()` for test isolation and shared `_client_kwargs()` to eliminate duplication between sync/async factories
5. **Lock contention quantified**: With 8 `ThreadPoolExecutor` workers retrying simultaneously, worst-case additional contention is ~44s during transient API degradation

### New Considerations Discovered

- OpenAI SDK `max_retries=2` means 3 total attempts by default (initial + 2 retries) — must set `max_retries=0` to get exactly our retry count
- `httpx.Timeout(120, connect=30.0)` is the correct API — positional first arg is the overall timeout, `connect` is keyword-only
- `LeanRAGConfig.llm_timeout`/`llm_max_retries` fields are unnecessary pass-throughs — env bridge in `_apply_config_to_env()` handles forwarding without dataclass fields
- Phase 5 provenance check is YAGNI for this fix — downgrade to `logger.debug()` in existing `_validate_config()` path

### Technical Review Findings (2026-02-22)

**Review agents:** architecture-strategist, kieran-python-reviewer, performance-oracle, security-sentinel, code-simplicity-reviewer, pattern-recognition-specialist, learnings-researcher

#### P1 — Must Fix Before Implementation

1. **`_apply_config_to_env()` scoping error**: The env bridge snippet references `llm.timeout` but `llm` is a local variable in `from_unified()`, not available in `_apply_config_to_env()`. Fix: call `resolve_llm_config("leanrag")` inside `_apply_config_to_env()` to get the resolved timeout value, or pass it in from `index()`.

2. **Bare `except Exception` catches non-retryable errors**: Auth failures (401), model not found (404), rate limits (429) all get retried identically. Fix: catch specific OpenAI exception types:
   ```python
   from openai import APITimeoutError, APIConnectionError, APIStatusError

   # In retry loop:
   except (APITimeoutError, APIConnectionError) as e:
       # Always retry — transient network/timeout errors
       ...
   except APIStatusError as e:
       if e.status_code in (429, 500, 502, 503):
           # Retry — server overload or transient errors
           ...
       else:
           raise  # 401, 403, 404 etc. — not retryable
   ```

3. **`response.choices[0].message.content` can be `None`**: The OpenAI API returns `content=None` for tool calls and some refusal responses. Fix: add null guard:
   ```python
   content = response.choices[0].message.content
   if content is None:
       raise LLMError("LLM returned null content (possible refusal or empty response)")
   return content
   ```

#### P2 — Should Fix

4. **Input validation for timeout/retry values**: `_DEFAULT_TIMEOUT = float(...)` accepts 0 or negative values from config, producing infinite hangs or immediate failures. Fix: add a guard after reading config:
   ```python
   if _DEFAULT_TIMEOUT <= 0:
       raise ValueError(f"deepseek.timeout must be positive, got {_DEFAULT_TIMEOUT}")
   if _DEFAULT_MAX_RETRIES < 1:
       raise ValueError(f"deepseek.max_retries must be >= 1, got {_DEFAULT_MAX_RETRIES}")
   ```

5. **API key exposure in exception logs**: `logger.warning("... %s", e)` may include API keys or request bodies in the exception string. Fix: use a helper to sanitize exception messages before logging:
   ```python
   def _safe_exc_str(e: Exception) -> str:
       """Sanitize exception for logging (strip potential API keys)."""
       s = str(e)
       # Mask anything that looks like an API key
       import re
       return re.sub(r'(sk-|key-)[a-zA-Z0-9]{8,}', r'\1***', s)
   ```

6. **Default timeout claim is incorrect**: Plan claims "no TOML override → 120s default" but `resolve_llm_config()` returns 60.0 (schema default in `memory_config.py:97`). When `_apply_config_to_env()` sets `DEEPSEEK_TIMEOUT=60.0`, it overrides `config.yaml`'s `:-120` fallback. Fix: either raise the schema default to 120.0 for LeanRAG contexts, or document that the effective default without TOML override is 60s, not 120s. **Recommended**: check whether `DEEPSEEK_TIMEOUT` is already set before overwriting, so `config.yaml`'s `:-120` default applies when no TOML override exists.

7. **Backoff off-by-one**: With `range(3)`, attempts are 0, 1, 2. Sleep happens on attempts 0 and 1 (when `attempt < max_retries - 1`). Attempt 2 raises. So total backoff is `2s + 4s = 6s`, not `2s + 4s + 8s = 14s` as documented in some places. Fix: update comments and documentation to accurately reflect the actual backoff timing.

#### P3 — Nice-to-Have

8. **Remove YAGNI helpers**: `_client_kwargs()` and `_reset_clients()` can be inlined. `_client_kwargs()` is called exactly twice (sync + async client init), and `_reset_clients()` is only needed for tests. Consider keeping the code simple: inline the kwargs dict and let tests re-import the module.

9. **Redundant `standard_to_leanrag` bridge entry**: If `_apply_config_to_env()` directly sets `DEEPSEEK_TIMEOUT` from `ResolvedLLMConfig.timeout`, there's no need to also bridge `LLM_TIMEOUT` → `DEEPSEEK_TIMEOUT` in the standard-to-leanrag mapping. Pick one path.

10. **Provenance log is YAGNI**: The debug-level provenance log may never be needed. Can be added later if "wrong package" becomes an actual debugging problem.

---

## Overview

The LeanRAG memory pipeline hung for 2h21m during a live T3 smoke test, then failed with `DeepSeek API call failed after 5 attempts: Request timed out.` The root cause is that the sync `generate_text()` in `external/LeanRAG/leanrag/core/llm.py` has no explicit timeout (600s OpenAI SDK default), no retry logic, and creates a new OpenAI client per call. This function is used by the hierarchical clustering phase (`build_native.py`) and the incremental index path (`incremental_update`), making it the single most critical unprotected code path in the memory pipeline.

## Problem Statement

### Current State (llm.py:108-151)

| Function | Timeout | Retry | Client | Used By |
|----------|---------|-------|--------|---------|
| `generate_text()` (sync) | **None** (600s SDK default) | **None** | New per call | `build_native.py` clustering, `incremental_update`, `query_graph` |
| `generate_text_async()` (async) | **None** (600s SDK default) | 5x hardcoded backoff | New per call | `triple_extraction` via `_tracked_llm` |
| `embedding()` (sync) | 120s hardcoded | **None** | N/A (raw requests) | Embedding generation |

### Why It Matters

- A single DeepSeek API hiccup during graph building kills the entire multi-hour pipeline
- The async path has retry protection but the sync path (used more widely) has zero
- Both paths create a new `OpenAI`/`AsyncOpenAI` client per call (connection pool waste)
- The config pipeline already has `ResolvedLLMConfig.timeout` but it's never forwarded to LeanRAG
- The upcoming submodule bump to `6e23231` adds `community_update.py` with more sync LLM calls, increasing the unprotected surface area

### Evidence

- Log: `~/.watercooler/logs/watercooler_2026-02-22_184810.log` — task ran 15:16-17:37 (2h21m)
- `curl` to DeepSeek API returned 200 in 1.07s — API was healthy, pipeline had no resilience
- Only $0.01 spent — pipeline failed at summarization step before completing many LLM calls
- All 3 task queue attempts exhausted; task dead-lettered

## Prerequisites (already applied)

### TOML Config: Point T3 at DeepSeek

`[memory.leanrag]` now has explicit LLM overrides so T3 uses DeepSeek instead of falling back to the local qwen3:1.7b from `[memory.llm]`:

```toml
[memory.leanrag]
llm_api_base = "https://api.deepseek.com/v1"
llm_model = "deepseek-chat"
```

API key auto-resolves from `~/.watercooler/credentials.toml` `[deepseek].api_key` via provider URL detection. Timeout inherits from `[memory.llm].timeout` (180.0s). The baseline graph summarizer stays on local qwen3:1.7b since it reads `[memory.llm]` directly.

## Proposed Solution

### Phase 1: LeanRAG Submodule Bump + LLM Hardening

#### 1a. Submodule Bump (prerequisite)

Bump `external/LeanRAG` from `c0f0f7c` to `6e23231` (or latest `main` at `e95bc70`) to pick up incremental clustering with `community_update.py`. This ensures our timeout/retry hardening covers the new sync LLM call sites.

**Files:**
- `external/LeanRAG` (submodule ref update)

#### 1b. Config Keys (`config.yaml`)

Add timeout/retry keys under `deepseek:` section:

```yaml
deepseek:
  model: "${DEEPSEEK_MODEL}"
  api_key: "${DEEPSEEK_API_KEY}"
  base_url: "${DEEPSEEK_BASE_URL}"
  timeout: "${DEEPSEEK_TIMEOUT:-120}"       # Per-call timeout in seconds
  max_retries: "${DEEPSEEK_MAX_RETRIES:-3}" # Manual retry count (SDK retries disabled)
```

**File:** `external/LeanRAG/config.yaml` (+2 lines)

#### 1c. Core LLM Hardening (`llm.py`)

Rewrite `external/LeanRAG/leanrag/core/llm.py` (~100 lines) with:

**Config-driven defaults:**

```python
import logging
import random
import time

logger = logging.getLogger(__name__)

# Read from config.yaml (which reads from env vars)
_DEFAULT_TIMEOUT = float(config.get('deepseek', {}).get('timeout', 120))
_DEFAULT_MAX_RETRIES = int(config.get('deepseek', {}).get('max_retries', 3))
_BASE_DELAY = 2  # seconds, exponential backoff: _BASE_DELAY * (2 ** attempt)

# Input validation (P2 review finding)
if _DEFAULT_TIMEOUT <= 0:
    raise ValueError(f"deepseek.timeout must be positive, got {_DEFAULT_TIMEOUT}")
if _DEFAULT_MAX_RETRIES < 1:
    raise ValueError(f"deepseek.max_retries must be >= 1, got {_DEFAULT_MAX_RETRIES}")
```

**Eager module-level clients (no locking needed):**

```python
from openai import Timeout

def _client_kwargs() -> dict:
    """Shared config for sync and async clients."""
    return dict(
        api_key=config['deepseek']['api_key'],
        base_url=config['deepseek']['base_url'],
        timeout=Timeout(_DEFAULT_TIMEOUT, connect=30.0),
        max_retries=0,  # We handle retries ourselves
    )

_sync_client = OpenAI(**_client_kwargs())
_async_client = AsyncOpenAI(**_client_kwargs())


def _reset_clients() -> None:
    """Reset clients for testing. Not for production use."""
    global _sync_client, _async_client
    _sync_client = OpenAI(**_client_kwargs())
    _async_client = AsyncOpenAI(**_client_kwargs())
```

> **Research insight (simplicity reviewer):** Double-checked locking with `threading.Lock()` is unnecessary here. `config` is already loaded at module import time, so clients can be eagerly created. OpenAI SDK httpx clients are thread-safe for concurrent use by `ThreadPoolExecutor` workers. The `_reset_clients()` function enables test isolation without complex locking machinery.

> **Research insight (framework docs):** `httpx.Timeout(120, connect=30.0)` is the correct API. The first positional arg sets the overall timeout; `connect` is keyword-only. OpenAI SDK wraps this transparently. Setting `max_retries=0` disables the SDK's default 2-retry behavior (which would mean 3 total attempts per our 1 attempt).

**Critical detail (Codex correction #1):** `max_retries=0` on the SDK client disables the OpenAI SDK's built-in 2-retry default. Our manual retry loop handles all retries, preventing retry multiplication (our 3 retries x SDK's 3 total attempts = 9 actual attempts).

**Retry-hardened sync `generate_text()`:**

```python
from openai import APITimeoutError, APIConnectionError, APIStatusError

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}

def generate_text(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict[str, str]] | None = None,
    **kwargs,
) -> str:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": prompt})

    for attempt in range(_DEFAULT_MAX_RETRIES):
        try:
            response = _sync_client.chat.completions.create(
                model=config['deepseek']['model'],
                messages=messages,
                **kwargs,
            )
            content = response.choices[0].message.content
            if content is None:
                raise LLMError("LLM returned null content (possible refusal or empty response)")
            return content
        except (APITimeoutError, APIConnectionError) as e:
            # Transient network/timeout — always retry
            if attempt < _DEFAULT_MAX_RETRIES - 1:
                delay = _BASE_DELAY * (2 ** attempt) + random.uniform(0, 1) + random.uniform(0, 1)  # ~2s, ~4s + jitter
                logger.warning(
                    "Sync LLM call failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1, _DEFAULT_MAX_RETRIES, type(e).__name__, delay,
                )
                time.sleep(delay)
            else:
                raise LLMError(
                    f"DeepSeek API call failed after {_DEFAULT_MAX_RETRIES} attempts: {e}"
                ) from e
        except APIStatusError as e:
            if e.status_code in _RETRYABLE_STATUS_CODES and attempt < _DEFAULT_MAX_RETRIES - 1:
                delay = _BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "Sync LLM call failed (attempt %d/%d): HTTP %d. Retrying in %ds...",
                    attempt + 1, _DEFAULT_MAX_RETRIES, e.status_code, delay,
                )
                time.sleep(delay)
            elif e.status_code in _RETRYABLE_STATUS_CODES:
                raise LLMError(
                    f"DeepSeek API call failed after {_DEFAULT_MAX_RETRIES} attempts: HTTP {e.status_code}"
                ) from e
            else:
                raise  # 401, 403, 404 etc. — not retryable, fail immediately
```

> **Review fix (P1):** Bare `except Exception` replaced with specific OpenAI exception types. Auth failures (401), model-not-found (404) now fail immediately instead of burning through all retries. Null content guard prevents `NoneType` downstream.

> **Review fix (P2):** Log messages use `type(e).__name__` or `e.status_code` instead of `str(e)` to avoid potential API key leakage in exception strings.

> **Note (backoff timing):** With `range(3)`, sleep fires on attempts 0 and 1 only (attempt 2 raises). Actual backoff is `2s + 4s = 6s` total, not `14s`.

> **Research insight (pattern reviewer):** The backoff formula `_BASE_DELAY * (2 ** attempt)` matches the canonical pattern in `_utils.py:139` (`_http_post_with_retry`).

> **Research insight (python reviewer):** `history_messages: list[dict[str, str]] | None` is tighter than `list | None`. The OpenAI API expects `{"role": str, "content": str}` dicts — this catches misuse at the type level.

**Unified async `generate_text_async()`:**

Same retry pattern as sync but with `await asyncio.sleep(delay)`, config-driven retry count (replacing hardcoded 5), `logger.warning()` instead of bare `print()`, and direct `_async_client` usage.

### Phase 2: Config Bridge (`leanrag.py`)

Bridge timeout/retry from unified TOML config into LeanRAG's env var space. No new dataclass fields needed — just env var bridging.

#### 2a. Bridge env vars in `_apply_config_to_env()` (leanrag.py:314-364)

Resolve LLM config inside `_apply_config_to_env()` and bridge timeout/retry into LeanRAG's env var space:

```python
def _apply_config_to_env(self) -> None:
    # ... existing code ...

    # Resolve LLM config for LeanRAG backend (respects [memory.leanrag] overrides)
    from watercooler.memory_config import resolve_llm_config
    llm = resolve_llm_config("leanrag")

    # Only set DEEPSEEK_TIMEOUT if TOML has an explicit override;
    # otherwise let config.yaml's ${DEEPSEEK_TIMEOUT:-120} default apply.
    # This preserves the 120s LeanRAG default when TOML uses the 60s schema default.
    if llm.timeout != 60.0:  # 60.0 is the schema default (no explicit override)
        env_mappings.append(("DEEPSEEK_TIMEOUT", str(llm.timeout)))
    env_mappings.append(("DEEPSEEK_MAX_RETRIES", "3"))

    standard_to_leanrag = [
        # ... existing mappings ...
        ("LLM_TIMEOUT", "DEEPSEEK_TIMEOUT"),           # NEW
        ("LLM_MAX_RETRIES", "DEEPSEEK_MAX_RETRIES"),   # NEW
    ]
```

> **Review fix (P1):** `llm` is now resolved inside `_apply_config_to_env()` via `resolve_llm_config("leanrag")`, fixing the `NameError` where it was referenced as a local from `from_unified()`.

> **Review fix (P2):** Only sets `DEEPSEEK_TIMEOUT` when TOML has an explicit override (not the 60.0 schema default). This prevents the TOML default from overriding `config.yaml`'s `:-120` fallback, preserving the correct 120s default for LeanRAG.

> **Research insight (simplicity reviewer):** No `llm_timeout`/`llm_max_retries` dataclass fields needed — the env bridge reads directly from `ResolvedLLMConfig.timeout` (already resolved) without intermediate fields.

#### 2b. Provenance log (debug-level, in existing path)

Add a single `logger.debug()` line in the existing `_validate_config()` or `_ensure_leanrag_available()` path:

```python
logger.debug(
    "LeanRAG loaded from: %s",
    getattr(leanrag, '__file__', 'unknown'),
)
```

> **Research insight (simplicity reviewer):** The original Phase 5 dedicated this to a separate implementation phase with `logger.info()`. For a fix PR, a debug-level log in an existing code path is sufficient. If import provenance becomes a real debugging problem, it can be promoted to info level later.

## Technical Considerations

### Retry Multiplication Prevention

The OpenAI Python SDK defaults to `max_retries=2` (meaning 3 total attempts: 1 initial + 2 retries). Our manual retry loops provide clearer logging and configurable behavior. Setting `max_retries=0` in the client constructor ensures only our retries fire.

**Worst case with fix:** 120s timeout x 3 attempts + backoff (2s+4s) = ~6.1 min per LLM call step
**Worst case without fix:** 600s timeout x 1 attempt = 10 min block, then crash

### Client Thread Safety

OpenAI SDK v1.x uses httpx internally, which is thread-safe. Module-level eager client creation is safe for `ThreadPoolExecutor` workers in `hierarchical.py`. No locking needed — httpx connection pooling handles concurrent requests internally.

> **Research insight (framework docs):** Confirmed via OpenAI SDK source: `httpx.Client` and `httpx.AsyncClient` are explicitly documented as thread-safe. Connection pool size defaults to 100 max connections / 20 max keepalive, which is more than sufficient for `max_workers=8` in hierarchical clustering.

### `_chdir_lock` Contention (known limitation)

`LeanRAGBackend.index()` holds `_chdir_lock` (an `RLock`) during both Stage 1 (triple extraction, line 611) and Stage 2 (graph building, line 664). With retry loops, contention duration could increase.

> **Research insight (performance reviewer):** Quantified impact: With 8 `ThreadPoolExecutor` workers and each retry adding `_BASE_DELAY * (2**attempt)` seconds of sleep, worst case during a transient API outage is:
> - Per-worker: 120s timeout x 3 attempts + 6s backoff = ~6.1 min
> - Lock held: Entire Stage 2 duration (which includes all worker retries)
> - Additional contention: ~44s of cumulative backoff sleep across 8 workers during transient degradation
>
> This is strictly better than the current 600s x 1 unbounded timeout. The lock protects `os.chdir()` and `sys.path`, not the LLM calls — narrowing the lock scope is a separate architectural change.

### Eager Client Init vs Lazy Init

> **Research insight (simplicity + architecture reviewers):** The original plan used double-checked locking for lazy client initialization. This is unnecessary because:
> 1. `config` is loaded at module import time via `load_config()` — all values are available immediately
> 2. LeanRAG modules are imported inside `_ensure_leanrag_available()` which runs after env vars are set
> 3. Eager init eliminates the `threading.Lock()`, the `global` declarations, and the double-check pattern (~15 LOC savings)
> 4. The only downside (client created even if never used) is irrelevant — if `llm.py` is imported, it will be used

### Default Value Alignment

> **Research insight (architecture reviewer):** The plan had an inconsistency in default timeout values:
> - `ResolvedLLMConfig.timeout` defaults to 60.0 (from `memory_config.py:97`)
> - `LeanRAGConfig.llm_timeout` proposed default of 120.0
> - `config.yaml` `${DEEPSEEK_TIMEOUT:-120}` defaults to 120
>
> **Resolution:** Standardize on 120.0 for LeanRAG's LLM timeout. The TOML default of 60.0 applies to the summarizer (which makes short, fast calls). LeanRAG's hierarchical clustering makes longer calls that need more headroom. The env bridge sets `DEEPSEEK_TIMEOUT` from `ResolvedLLMConfig.timeout` when explicitly configured in TOML, and `config.yaml`'s `:-120` default handles the unconfigured case. This means:
> - No TOML override: `config.yaml` default of 120s applies
> - TOML `[memory.llm].timeout = 180`: propagates through env to 180s

### Pipeline-Level Timeout (explicitly excluded)

Per Codex's guidance: `asyncio.wait_for(asyncio.to_thread(...))` does **not** stop the underlying worker thread. A pipeline-level timeout would require subprocess isolation with kill-on-timeout semantics. This is out of scope for this fix — per-call timeouts provide sufficient protection.

### `community_update.py` Coverage

After bumping the submodule to `6e23231`, the new `community_update.py` module calls `generate_text` (sync) for community re-summarization during incremental updates. Our hardening of `generate_text()` automatically covers these new call sites without additional changes.

### Embedding Path (out of scope)

`embedding()` (line 44-105) uses raw `requests.post` with a hardcoded 120s timeout and no retry. This is a separate concern — the embedding endpoint is local (BGE-M3) and responds quickly. Retry hardening for embeddings can be a follow-up.

## Acceptance Criteria

### Functional Requirements

- [x] `generate_text()` retries up to 3x with exponential backoff on failure
- [x] `generate_text_async()` uses config-driven retry count (not hardcoded 5)
- [x] Both functions use shared, reusable module-level clients with explicit 120s timeout
- [x] OpenAI SDK `max_retries=0` prevents retry multiplication
- [x] `config.yaml` supports `timeout` and `max_retries` under `deepseek:`
- [x] `_apply_config_to_env()` bridges `DEEPSEEK_TIMEOUT` and `DEEPSEEK_MAX_RETRIES` env vars
- [x] Retry attempts are logged with `logger.warning()` (not bare `print()`)
- [x] `_reset_clients()` available for test isolation

### Submodule

- [x] `external/LeanRAG` bumped to `f9807d7` (latest main, includes `6e23231` + `e95bc70`)
- [x] `community_update.py` sync LLM calls covered by hardened `generate_text()`

### Quality Gates

- [x] Existing tests pass (`pytest tests/`) — 1994 passed, 29 skipped, 0 failed
- [x] No retry multiplication: verify SDK `max_retries=0` in client init
- [x] Config flow verified: TOML `[memory.llm].timeout` -> env `DEEPSEEK_TIMEOUT` -> `config.yaml` -> `_DEFAULT_TIMEOUT`
- [x] Backoff formula uses `_BASE_DELAY * (2 ** attempt)` consistent with `_utils.py` pattern

### Review-Mandated Checks (from technical review)

- [x] Retry catches specific `APITimeoutError`/`APIConnectionError`/`APIStatusError` — NOT bare `Exception`
- [x] Non-retryable status codes (401, 403, 404) raise immediately without retry
- [x] `response.choices[0].message.content` null-guarded before return
- [x] `_DEFAULT_TIMEOUT` and `_DEFAULT_MAX_RETRIES` validated at module load (>0 and >=1)
- [x] `DEEPSEEK_TIMEOUT` only set in env when TOML has explicit override (not schema default 60.0)
- [x] Log messages use `type(e).__name__` or `e.status_code`, not raw `str(e)` (API key safety)

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `external/LeanRAG` | Submodule bump to `6e23231`+ | ref only |
| `external/LeanRAG/config.yaml` | Add `timeout`, `max_retries` keys | +2 |
| `external/LeanRAG/leanrag/core/llm.py` | Eager clients, timeout, retry for sync, unify async, `_reset_clients()` | ~80 rewritten |
| `src/watercooler_memory/backends/leanrag.py` | Bridge `DEEPSEEK_TIMEOUT`/`DEEPSEEK_MAX_RETRIES` env vars, debug provenance log | ~6 added |

## Config Flow Diagram

```
[memory.llm].timeout (TOML, default 60.0)
        |
        v
ResolvedLLMConfig.timeout (memory_config.py:97)
        |
        v
_apply_config_to_env(): sets DEEPSEEK_TIMEOUT env var
        |
        v
config.yaml: deepseek.timeout: "${DEEPSEEK_TIMEOUT:-120}"
        |
        v
llm.py: _DEFAULT_TIMEOUT = float(config['deepseek']['timeout'])
        |
        v
OpenAI(timeout=Timeout(_DEFAULT_TIMEOUT, connect=30.0), max_retries=0)
```

> **Note:** The `LeanRAGConfig` dataclass is NOT in this flow — the env bridge writes directly to `DEEPSEEK_TIMEOUT` from `ResolvedLLMConfig.timeout`, bypassing intermediate fields.

## Dependencies & Risks

### Dependencies
- LeanRAG submodule bump must succeed cleanly (potential merge conflicts in `config.yaml`)
- OpenAI SDK v1.x API (`Timeout` class, `max_retries` param) — already in use

### Risks
- **Low:** Client reuse could theoretically leak state between calls (mitigated: OpenAI SDK httpx clients are stateless per-request)
- **Low:** Retry loops extend `_chdir_lock` hold time (mitigated: max 6.1 min vs previous 10 min unbounded; ~44s additional contention quantified)
- **Medium:** Submodule bump may introduce unrelated changes from commits between `c0f0f7c` and `6e23231`/`e95bc70`

### Security Notes

> **Research insight (security reviewer):** No critical or high findings. The API key is already held in the `config` dict at module level — the singleton client doesn't expand the exposure surface. Overall security posture improves: explicit timeouts prevent resource exhaustion from hung connections.

## References

### Internal
- Thread: `t3-memory-live-test` (entries 0-3) — full investigation + Codex review
- `src/watercooler_memory/_utils.py:139` — canonical `_http_post_with_retry` pattern (backoff formula source)
- `src/watercooler_memory/summarizer.py:67-93` — `SummarizerConfig` timeout/retry precedent
- `src/watercooler/memory_config.py:87-116` — `ResolvedLLMConfig` with `timeout` field
- `docs/solutions/logic-errors/federation-phase1-code-review-fixes.md` — cross-field validation pattern

### External
- [OpenAI Python SDK timeout docs](https://github.com/openai/openai-python#configuring-the-http-client) — `Timeout` class, `max_retries` param
- LeanRAG PR #6: `6e23231` — incremental clustering + `community_update.py`
- OpenAI SDK source: `max_retries=2` default confirmed (3 total attempts)
- httpx docs: `Client`/`AsyncClient` are thread-safe, connection pool defaults 100 max / 20 keepalive
