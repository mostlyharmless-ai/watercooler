# Watercooler Canonical Schemas

This directory contains the **canonical JSON Schemas** for Watercooler thread and entry data structures. These schemas serve as the single source of truth for data interchange between:

- **watercooler-cloud** (Python) - MCP server and CLI
- **watercooler-site** (TypeScript/Next.js) - Web dashboard

## Schema Files

### `thread_entry.schema.json`
Defines the structure of a single thread entry.

**Key Fields:**
- `agent` (not `author`) - Agent name extracted from "Entry: ..." line
- `entry_type` (not `type`) - Entry type (Note, Plan, Decision, PR, Closure)
- `timestamp` - ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ)
- `role` - Agent role (planner, critic, implementer, tester, pm, scribe)
- `entry_id` - ULID identifier (26 chars, base32)

**Optionality:**
- Required: `index`, `header`, `body`, `start_line`, `end_line`, `start_offset`, `end_offset`
- Optional: `agent`, `timestamp`, `role`, `entry_type`, `title`, `entry_id`

### `watercooler_thread.schema.json`
Defines the structure of a complete thread with metadata and entries.

**Key Fields:**
- `id` - Thread identifier (filename without .md)
- `topic` - Thread topic slug (same as id)
- `original_topic` - Original name if renamed (null if never renamed)
- `status` - OPEN, IN_REVIEW, BLOCKED, CLOSED, etc.
- `ball` - Current ball owner (e.g., "Claude (user)")
- `priority` - P0-P5 or null
- `entries` - Array of ThreadEntry objects

## Field Naming Conventions

### Python ↔ TypeScript Alignment

| Concept | Python (snake_case) | TypeScript (camelCase) | Schema (snake_case) |
|---------|---------------------|------------------------|---------------------|
| Agent name | `agent` | `agent` | `agent` |
| Entry type | `entry_type` | `entryType` | `entry_type` |
| Entry ID | `entry_id` | `entryId` | `entry_id` |
| Line numbers | `start_line`, `end_line` | `startLine`, `endLine` | `start_line`, `end_line` |
| Offsets | `start_offset`, `end_offset` | `startOffset`, `endOffset` | `start_offset`, `end_offset` |

**Rule:** Schema uses snake_case (Python convention). TypeScript implementations should map to camelCase automatically.

## Usage

### Python (watercooler-cloud)

The Python `ThreadEntry` dataclass in `src/watercooler/thread_entries.py` **must** match this schema exactly:

```python
@dataclass(frozen=True)
class ThreadEntry:
    index: int
    header: str
    body: str
    agent: Optional[str]          # matches schema
    timestamp: Optional[str]
    role: Optional[str]
    entry_type: Optional[str]     # matches schema
    title: Optional[str]
    entry_id: Optional[str]
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
```

### TypeScript (watercooler-site)

The TypeScript `ThreadEntry` class in `lib/threadStructure.ts` should use camelCase but map to schema fields:

```typescript
export class ThreadEntry {
  // Required fields
  index: number;                  // Required by schema
  header: string;                 // Required by schema
  body: string;                   // Required by schema
  startLine: number;              // Required, maps to 'start_line' in schema
  endLine: number;                // Required, maps to 'end_line' in schema
  startOffset: number;            // Required, maps to 'start_offset' in schema
  endOffset: number;              // Required, maps to 'end_offset' in schema

  // Optional fields (nullable in schema)
  agent: string | null;           // matches schema (was 'author')
  timestamp: string | null;
  role: string | null;
  entryType: string | null;       // maps to 'entry_type' in schema
  title: string | null;
  entryId: string | null;         // maps to 'entry_id' in schema
}
```

## Schema Validation

### Python

Use `jsonschema` package:

```python
import json
from jsonschema import validate
from pathlib import Path

schema_path = Path(__file__).parent.parent / "schemas" / "thread_entry.schema.json"
schema = json.loads(schema_path.read_text())

# Validate entry
entry_dict = {
    "index": 0,
    "header": "...",
    "body": "...",
    "agent": "Claude (user)",
    # ... other fields
}
validate(instance=entry_dict, schema=schema)
```

### TypeScript

Use `ajv` package:

```typescript
import Ajv from 'ajv';
import schema from '../../../watercooler-cloud/schemas/thread_entry.schema.json';

const ajv = new Ajv();
const validate = ajv.compile(schema);

const entry = {
  index: 0,
  header: "...",
  body: "...",
  agent: "Claude (user)",
  // ... other fields (camelCase)
};

if (!validate(entry)) {
  console.error(validate.errors);
}
```

## Updating Schemas

**CRITICAL:** When updating these schemas, you MUST update both implementations:

1. **Update the JSON Schema** in this directory
2. **Update Python dataclass** in `src/watercooler/thread_entries.py`
3. **Update TypeScript class** in `watercooler-site/lib/threadStructure.ts`
4. **Run validation tests** in both repos
5. **Update this README** with any new field conventions

### Schema Update Checklist

- [ ] Update `thread_entry.schema.json` or `watercooler_thread.schema.json`
- [ ] Update Python dataclass (`src/watercooler/thread_entries.py`)
- [ ] Update TypeScript class (`watercooler-site/lib/threadStructure.ts`)
- [ ] Add/update examples in schema file
- [ ] Run Python tests: `pytest tests/`
- [ ] Run TypeScript tests: `npm test`
- [ ] Update field mapping in API handlers if needed
- [ ] Update this README

## Testing Schema Parity

### Automated Tests

Both repos should have tests that validate schema parity:

**Python:**
```bash
pytest tests/test_schema_validation.py
```

**TypeScript:**
```bash
npm run test:schema
```

These tests should:
1. Load the canonical JSON Schema
2. Validate sample data against the schema
3. Ensure all required fields are present
4. Verify enum values match
5. Check pattern matching (timestamps, ULIDs)

## Common Pitfalls

### ❌ Wrong: Using `author` instead of `agent`
```typescript
// WRONG - old field name
const entry = { author: "Claude (user)", ... };
```

```typescript
// CORRECT - matches schema
const entry = { agent: "Claude (user)", ... };
```

### ❌ Wrong: Using `type` instead of `entryType`
```typescript
// WRONG - conflicts with built-in 'type' keyword
const entry = { type: "Note", ... };
```

```typescript
// CORRECT - matches schema
const entry = { entryType: "Note", ... };
```

### ❌ Wrong: Making optional fields required
```python
# WRONG - agent is optional in schema
@dataclass
class ThreadEntry:
    agent: str  # Missing Optional[]
```

```python
# CORRECT - matches schema
@dataclass
class ThreadEntry:
    agent: Optional[str]
```

## Schema Sync Process

### Syncing Schemas Between Repos

The canonical schemas live in `watercooler-cloud/schemas/` and are copied to `watercooler-site/schemas/` when needed:

```bash
# From watercooler-site root:
cp ../watercooler-cloud/schemas/*.json schemas/
```

**When to sync:**
- After any schema updates in watercooler-cloud
- Before adding new validation or tests
- When field names or optionality rules change

### Maintaining Schema Parity

**Automated validation:**
- Python tests: `pytest tests/test_schema_validation.py`
- TypeScript tests: `npx tsx test-schema-validation.ts`

**Manual checks:**
1. Compare field names in both repos
2. Verify optionality rules match (Optional in Python ↔ `| null` in TypeScript)
3. Check enum values are identical
4. Validate ULID/timestamp patterns

**CI/CD Integration:**
Add schema validation to your CI pipeline to catch drift early:

```yaml
# .github/workflows/test.yml
- name: Validate schema parity
  run: |
    pytest tests/test_schema_validation.py
    cd ../watercooler-site && npx tsx test-schema-validation.ts
```

### Field Mapping During Transition

**Backward Compatibility Layer:**
`watercooler-site/lib/threadMapper.ts` provides backward compatibility for old database records:

```typescript
agent: entry.agent || entry.author || 'unknown',  // Support both
entryType: entry.entryType || entry.type || 'Note',  // Support both
```

**Timeline:**
1. **Phase 1 (Complete)**: Schema defined, field names aligned, validation added
2. **Phase 2 (Current)**: Parser creates new field names, old records supported via mapper
3. **Phase 3 (Future)**: After all DB records migrated, remove backward compatibility

**Migration Status:**
- ✅ Canonical schemas created
- ✅ Field names aligned (author→agent, type→entryType)
- ✅ Optionality rules matched
- ✅ Validation utilities added
- ✅ Tests added
- 🔄 Database records gradually migrating (via normal sync process)
- ⏳ Backward compatibility can be removed once all records migrated

## Version History

- **2025-01-26** - Initial canonical schema creation
  - Established `agent` (not `author`)
  - Established `entry_type` (not `type`)
  - Defined optionality rules
  - Created cross-repo validation process
  - Added schema validation utilities and tests
  - Documented schema sync process
