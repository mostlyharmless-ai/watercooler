Spec: benchmark-guidance-v3

You have access to Watercooler read-only tools via `wc-*` commands.

## Objective

Use Watercooler only when it improves execution quality under fixed step/cost budgets.
Your goal is to solve the task, not maximize tool usage.

## Decision flow (follow strictly)

1) Start local first:
- Read failing test output and likely target files before retrieval.
- Use local inspection (`rg`, focused file reads) to form the first hypothesis.

2) Use wc tools only when blocked by missing context:
- Known topic/thread: `wc-read-thread <topic>` then `wc-get-entry <topic> <index>`
- Unknown location or keyword recall: one `wc-search "<query>"`
- Conceptual or policy question: one `wc-smart-query "<question>"`

2a) Retrieval mode priority:
- First retrieval should be `wc-smart-query` unless you already know the exact topic.
- Use `wc-search` only for keyword lookup when smart-query is clearly unsuitable.
- If any retrieval returns entry IDs or a concrete topic, switch to thread mode:
  - `wc-read-thread <topic>`
  - `wc-get-entry <topic> <index>`

3) Retrieval discipline:
- Max 1 wc call before first edit or test run.
- Do not repeat the same wc command pattern twice in a row.
- Do not run more than one `wc-search` unless the previous one yielded no actionable clue.
- After each wc call, immediately do one concrete action:
  - open likely file(s),
  - apply minimal edit, or
  - run smallest validating test command.

4) Citation/evidence handling:
- If wc returns entry IDs, treat them as constraints.
- Prefer thread + entry retrieval over repeated broad search when citations are available.
- Before the next command, explicitly bind retrieval output to a file/symbol hypothesis.

5) Stop condition:
- If 2 wc calls do not produce actionable target clues, stop retrieval and continue direct code/debug workflow.
- If a patch is possible, attempt the patch and run the smallest relevant test instead of additional retrieval.

## Available commands (emit exactly one per step)

- `wc-search "<query>"`
  - Quick keyword retrieval.
- `wc-smart-query "<question>"`
  - Natural-language retrieval across tiers.
- `wc-read-thread <topic>`
  - Lists entries (titles + entry IDs).
- `wc-get-entry <topic> <index>`
  - Pulls one entry body by index.

## Example sequence

1) Local triage:

```bash
rg "RidgeClassifierCV|store_cv_values" -n
```

2) One targeted retrieval (if needed):

```bash
wc-search "ridge classifier cv values known pitfalls"
```

3) Immediate action:

```bash
pytest -q tests/test_linear_model.py -k RidgeClassifierCV
```

