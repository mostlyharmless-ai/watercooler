#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable


RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
RE_URL = re.compile(r"\bhttps?://[^\s)>\"]+\b")

# Common token/secret formats (conservative)
RE_GITHUB_TOKEN = re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
RE_SLACK_TOKEN = re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b")
RE_OPENAI_KEY = re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b")
RE_AWS_ACCESS_KEY = re.compile(r"\b(AKIA[0-9A-Z]{16})\b")
RE_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._=-]{20,}\b", flags=re.IGNORECASE)


def _sanitize_text(text: str) -> str:
  if not text:
    return text
  text = RE_EMAIL.sub("<REDACTED_EMAIL>", text)
  text = RE_URL.sub("<REDACTED_URL>", text)
  text = RE_GITHUB_TOKEN.sub("<REDACTED_GITHUB_TOKEN>", text)
  text = RE_SLACK_TOKEN.sub("<REDACTED_SLACK_TOKEN>", text)
  text = RE_OPENAI_KEY.sub("<REDACTED_API_KEY>", text)
  text = RE_AWS_ACCESS_KEY.sub("<REDACTED_AWS_ACCESS_KEY>", text)
  text = RE_BEARER.sub("Bearer <REDACTED_TOKEN>", text)
  return text


def _iter_jsonl(path: Path) -> Iterable[dict]:
  with path.open("r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      obj = json.loads(line)
      if isinstance(obj, dict):
        yield obj


def sanitize_entries_jsonl(path: Path) -> tuple[int, int]:
  changed = 0
  total = 0
  out_lines: list[str] = []
  for obj in _iter_jsonl(path):
    total += 1
    before = json.dumps(obj, ensure_ascii=False, sort_keys=True)

    # Keep IDs stable; redact only human text fields.
    for k in ("title", "summary", "body"):
      if isinstance(obj.get(k), str):
        obj[k] = _sanitize_text(obj[k])

    after = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    if after != before:
      changed += 1
    out_lines.append(json.dumps(obj, ensure_ascii=False))

  path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
  return changed, total


def sanitize_markdown(path: Path) -> bool:
  text = path.read_text(encoding="utf-8", errors="replace")
  sanitized = _sanitize_text(text)
  if sanitized != text:
    path.write_text(sanitized, encoding="utf-8")
    return True
  return False


def main() -> None:
  ap = argparse.ArgumentParser(description="Sanitize a Watercooler baseline-graph threads_dir export")
  ap.add_argument("--threads-dir", required=True, help="Path containing graph/ and threads/ directories")
  ap.add_argument("--no-md", action="store_true", help="Do not sanitize threads/*.md projections")
  args = ap.parse_args()

  threads_dir = Path(args.threads_dir).resolve()
  graph_dir = threads_dir / "graph" / "baseline" / "threads"
  md_dir = threads_dir / "threads"

  if not graph_dir.exists():
    raise SystemExit(f"Expected baseline graph at {graph_dir}")

  entries_files = list(graph_dir.glob("*/entries.jsonl"))
  if not entries_files:
    raise SystemExit(f"No entries.jsonl files under {graph_dir}")

  total_changed = 0
  total_entries = 0
  for p in entries_files:
    changed, total = sanitize_entries_jsonl(p)
    total_changed += changed
    total_entries += total

  md_changed = 0
  if not args.no_md and md_dir.exists():
    for md in md_dir.glob("**/*.md"):
      if sanitize_markdown(md):
        md_changed += 1

  print(
    json.dumps(
      {
        "threads_dir": str(threads_dir),
        "entries_files": len(entries_files),
        "entries_total": total_entries,
        "entries_changed": total_changed,
        "markdown_files_changed": md_changed,
      },
      indent=2,
    )
  )


if __name__ == "__main__":
  main()

