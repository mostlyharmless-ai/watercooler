**DEPRECATED** — This is v1 guidance, retained for benchmark reproducibility.
Current guidance is in `watercooler_usage.md`.

---

Spec: benchmark-guidance

You have access to Watercooler read-only tools via `wc-*` commands.

### When to use wc tools

- Use them when you are unsure what the codebase expects (patterns, pitfalls, decisions), or when you need fast recall before exploring files.
- Prefer **one** targeted retrieval call, then act (open files, inspect code, apply a fix).
- If a `wc-*` call returns an entry id, treat it as a citation you can rely on for constraints and decisions.

### Available commands (emit exactly one per step)

- `wc-search "<query>"`
  - Use for quick keyword-style retrieval of relevant org knowledge.
- `wc-smart-query "<question>"`
  - Use for natural-language questions; may consult multiple tiers depending on the configured ceiling.
- `wc-read-thread <topic>`
  - Use to list what’s in a thread (titles + entry ids).
- `wc-get-entry <topic> <index>`
  - Use to pull a specific entry body by index from `wc-read-thread`.

### Discipline (this matters for the benchmark)

- Keep wc calls under budget: do not spam retrieval.
- After a wc call, immediately do a bash action that uses the information:
  - open the file(s) that match the returned component/pattern
  - run the smallest test command to validate
  - make the minimal fix

### Example usage

1) Retrieve:

```bash
wc-search "failing test signature KeyError in serializer"
```

2) Act:

```bash
rg "SerializerClass" -n
```
