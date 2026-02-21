---
name: pydantic-hardener
description: Audit Pydantic v2 models for missing cross-field validators, type-narrowing gaps, empty-value guards, frozen enforcement, and missing numeric bounds. Based on patterns from PR #190 (14-round review).
allowed-tools:
  - Read
  - Glob
  - Grep
  - Edit
  - Write
  - Bash(pytest *)
  - Bash(mypy *)
  - AskUserQuestion
---

# Pydantic Config Hardener

Audit Pydantic v2 models for five bug classes discovered during 14 rounds of code review on PR #190. Each class caused merge-blocking defects that slipped through initial implementation.

**Prerequisites:** Pydantic v2 (`pydantic >= 2.0`). All fix patterns use v2 syntax (`model_validator`, `ConfigDict`, `Field`). If mypy is installed, bug class 2 detection uses `mypy --strict`; otherwise that check is skipped with a warning.

Optional arguments via `$ARGUMENTS`:
- A file path or glob to scope the audit (e.g., `/pydantic-hardener src/watercooler/config_schema.py`)
- `--fix` to apply fixes automatically (default is report-only)
- `--tests` to also generate test cases for discovered gaps

## Bug Classes Checked

### 1. Missing Cross-Field Validators

**Pattern:** Two or more numeric fields with an ordering invariant (e.g., `timeout_a <= timeout_b`) but no `model_validator(mode="after")` to enforce it.

**Detection:**
- Find all Pydantic models (classes inheriting `BaseModel`)
- Identify pairs of numeric fields with semantically related names (e.g., `*_timeout`, `min_*`/`max_*`, `*_floor`/`*_ceiling`)
- Check for existing `@model_validator` decorators
- Flag models with related numeric fields but no cross-field validation

**Fix pattern:**
```python
@model_validator(mode="after")
def check_field_ordering(self) -> "ModelName":
    if self.field_a > self.field_b:
        raise ValueError(
            f"field_a ({self.field_a}) must be <= field_b ({self.field_b})"
        )
    return self
```

### 2. Type-Narrowing Gaps After Guard Clauses

**Pattern:** A function returns `tuple[T | None, Error | None]`. Callers guard with `if error: return` but then use the `T | None` value without narrowing, causing `mypy --strict` failures.

**Detection:**
- Find functions returning `tuple[..., ... | None]` or `Optional[...]`
- Find callsites that guard on one element but use the other without `assert`
- Run `mypy --strict` on target files and parse output for `[arg-type]` or `[union-attr]` errors

**Fix pattern:**
```python
error, result = fallible_call()
if error:
    return handle_error(error)
assert result is not None  # guaranteed by <function> contract
```

### 3. Empty/Sentinel Value Dedup Keys

**Pattern:** A collection is deduped by a string key field. If that field can be empty (`""`), all empty-keyed items collapse into one.

**Detection:**
- Find `set()` or `dict` used for dedup (patterns: `if x not in seen: seen.add(x)`)
- Scope to functions whose parameters or local types involve Pydantic models (ignore unrelated dedup loops)
- Trace the dedup key back to its source field
- Check if the source field type allows empty strings or None
- Flag if there's no guard before the dedup check

**Fix pattern:**
```python
if not entry.dedup_key:
    continue  # skip entries without usable dedup key
```

### 4. Frozen Model Enforcement

**Pattern:** Config models that should be immutable use `model_config = ConfigDict(frozen=True)` but some fields use mutable default values (`list`, `dict`) that bypass freezing.

**Note:** Pydantic v2 already rejects bare mutable defaults (`= []`, `= {}`) with `PydanticSchemaGenerationError` at class-definition time. The residual risk this catches is `Optional[list]` fields with `= None` that get mutated after construction, or `field_default` bypass patterns. On a healthy v2 codebase this check may produce few findings.

**Detection:**
- Find models with `frozen=True` in their `ConfigDict`
- Check for `Optional[list]`/`Optional[dict]` fields with `= None` that may be mutated post-construction
- Verify `default_factory` is used instead of mutable literals where applicable

**Fix pattern:**
```python
class MyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[str] = Field(default_factory=list)  # not = []
```

### 5. Missing Numeric Bounds

**Pattern:** Numeric fields that represent physical quantities (timeouts, weights, counts) lack `ge=`, `le=`, `gt=`, `lt=` constraints, allowing nonsensical values.

**Detection:**
- Find `float` and `int` fields in Pydantic models
- Check if they have `Field(ge=..., le=...)` or `@field_validator` bounds
- Flag fields named `*_timeout`, `*_weight`, `*_count`, `max_*`, `min_*` without bounds

**Fix pattern:**
```python
namespace_timeout: float = Field(default=0.4, gt=0, le=30.0)
max_namespaces: int = Field(default=5, ge=1, le=20)
```

## Execution Steps

### Step 1: Discover target models

```
# If $ARGUMENTS has a path, use it. Otherwise scan the project.
Glob: **/config*.py, **/schema*.py, **/models.py
Grep: "class.*BaseModel" in target files
```

Collect all Pydantic model classes and their file locations.

### Step 2: Audit each model

For each model found:

1. **Read the model** and all its fields
2. **Check bug class 1:** Identify related numeric field pairs, check for `model_validator`
3. **Check bug class 3:** Find any dedup logic using fields from this model
4. **Check bug class 4:** If `frozen=True`, check for mutable defaults
5. **Check bug class 5:** Check numeric fields for missing bounds

### Step 3: Audit callsites (bug class 2)

For functions that return models or `Optional[Model]`:

1. **Find callsites** using Grep
2. **Check for assert guards** after None-returning paths
3. **Run mypy** if available: `mypy --strict <file>` and parse errors

### Step 4: Report findings

Present findings grouped by bug class:

```
## Audit Results

### Cross-Field Validators (Bug Class 1)
- [ ] `FederationConfig`: namespace_timeout vs max_total_timeout — FIXED (model_validator exists)
- [ ] `NewModel`: field_a vs field_b — MISSING validator

### Type-Narrowing Gaps (Bug Class 2)
- [ ] `federation.py:137`: primary_ctx after _require_context — FIXED (assert exists)

### Empty Dedup Keys (Bug Class 3)
- [ ] `federation.py:282`: sr.node_id used as dedup key — FIXED (skip guard exists)

### Frozen Model Defaults (Bug Class 4)
- (none found)

### Missing Numeric Bounds (Bug Class 5)
- [ ] `SomeConfig.retry_count`: int with no upper bound — NEEDS FIX
```

### Step 5: Apply fixes (if `--fix`)

For each finding, show the proposed diff and confirm with the user before applying. Then apply the fix pattern from the relevant bug class. Stage only the modified files.

**Note:** Detection is heuristic (name-based field matching). Review all findings before applying fixes — some may be false positives where application-level guarantees make validation unnecessary.

### Step 6: Generate tests (if `--tests`)

For each fix applied, generate a test following the pattern:

```python
def test_<model>_<field>_cross_field_validation():
    """<field_a> must not exceed <field_b>."""
    with pytest.raises(ValidationError, match="<field_a>"):
        ModelName(field_a=10.0, field_b=1.0)

def test_<model>_<field>_boundary_accepted():
    """Equal values are valid."""
    cfg = ModelName(field_a=5.0, field_b=5.0)
    assert cfg.field_a == 5.0
```

**Note:** Verify that `match=` values in generated tests match the actual exception text from the validator. The templates above use placeholder field names — substitute the real error message substring.

### Step 7: Verify

```bash
pytest <test_files> -v
mypy --strict <source_files>
```

## Example Invocations

- `/pydantic-hardener` — audit all Pydantic models in the project
- `/pydantic-hardener src/watercooler/config_schema.py` — audit a specific file
- `/pydantic-hardener --fix` — audit and apply fixes
- `/pydantic-hardener --fix --tests` — audit, fix, and generate tests
- `/pydantic-hardener src/watercooler_mcp/` — audit all models in MCP server

## Reference

Based on findings from PR #190 (Federation Phase 1). Full documentation:
- `docs/solutions/logic-errors/federation-phase1-code-review-fixes.md`
