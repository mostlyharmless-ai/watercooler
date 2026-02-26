#!/usr/bin/env python3
"""Build org knowledge packs for SWE-bench repos via watercooler threads.

Mines "repo-native" sources (docs, docstrings, contributor guides, git history)
and uses LLM distillation to produce structured KG entities. Publishes entities
as watercooler thread entries using say() for proper graph-backed storage.

Output: a watercooler threads_dir per repo with baseline graph structure,
readable via read_thread_from_graph().

Usage:
    # Build pack for pilot repos
    python tests/benchmarks/scripts/build_knowledge_pack.py \
        --instance-ids sympy__sympy-20590 django__django-11099 \
                       pytest-dev__pytest-5103 scikit-learn__scikit-learn-13142 \
        --output-dir logs/knowledge-packs

    # Build with specific model
    python tests/benchmarks/scripts/build_knowledge_pack.py \
        --instance-ids django__django-11099 \
        --model minimax/MiniMax-M2.5 \
        --output-dir logs/knowledge-packs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import docker
import litellm

# Add project root for imports
repo_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repo_root))
# Ensure src-layout package imports work without editable install.
sys.path.insert(0, str(repo_root / "src"))

from tests.benchmarks.adapters.knowledge_pack import (
    Entity,
    Relation,
    check_leakage,
    publish_to_watercooler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials (reuse from run_swebench.py)
# ---------------------------------------------------------------------------

def setup_api_keys() -> None:
    creds_path = Path.home() / ".watercooler" / "credentials.toml"
    if not creds_path.exists():
        return
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(creds_path, "rb") as f:
        creds = tomllib.load(f)
    mapping = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
        "minimax": "MINIMAX_API_KEY",
    }
    for section, env_var in mapping.items():
        if section in creds and "api_key" in creds[section]:
            if env_var not in os.environ:
                os.environ[env_var] = creds[section]["api_key"]


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def exec_in_container(container, cmd: str, workdir: str = "/testbed") -> tuple[int, str]:
    """Execute a command in a running container."""
    try:
        result = container.exec_run(["bash", "-c", cmd], workdir=workdir, demux=True)
        stdout = result.output[0].decode("utf-8", errors="replace") if result.output[0] else ""
        stderr = result.output[1].decode("utf-8", errors="replace") if result.output[1] else ""
        output = stdout
        if stderr:
            output += "\n" + stderr
        if len(output) > 30000:
            output = output[:10000] + f"\n... ({len(output) - 20000} chars truncated) ...\n" + output[-10000:]
        return result.exit_code, output
    except Exception as e:
        return -1, f"Error: {e}"


# ---------------------------------------------------------------------------
# Repo mining: extract raw knowledge from Docker container
# ---------------------------------------------------------------------------

# Files to look for in each repo
KNOWLEDGE_SOURCES = [
    # Contributor/developer docs
    "cat README.md 2>/dev/null | head -200",
    "cat CONTRIBUTING.md 2>/dev/null | head -300",
    "cat CONTRIBUTING.rst 2>/dev/null | head -300",
    "cat docs/contributing.rst 2>/dev/null | head -300",
    "cat docs/CONTRIBUTING.md 2>/dev/null | head -300",
    "cat HACKING.rst 2>/dev/null | head -200",
    # Architecture / design docs
    "find docs/ -name '*.rst' -o -name '*.md' | head -20",
    "cat docs/internals/*.rst 2>/dev/null | head -500",
    "cat docs/internals/*.md 2>/dev/null | head -500",
    # Module docstrings (top-level __init__.py files, 2 levels deep)
    "find . -maxdepth 3 -name '__init__.py' -exec head -30 {} + 2>/dev/null | head -500",
    # Code style / conventions
    "cat .editorconfig 2>/dev/null",
    "cat setup.cfg 2>/dev/null | head -50",
    "cat pyproject.toml 2>/dev/null | head -80",
    # CI / test configuration hints
    "cat tox.ini 2>/dev/null | head -50",
    "cat .github/workflows/*.yml 2>/dev/null | head -100",
    # Recent git log (architecture-relevant commits, not specific bug fixes)
    "git log --oneline --since='1 year ago' --no-merges -- '*.py' | head -50",
]

# Targeted extraction for specific areas (run after discovering repo structure)
TARGETED_EXTRACTION = """
# Find the most-edited files (architecture hotspots)
git log --pretty=format: --name-only --since='2 years ago' -- '*.py' | sort | uniq -c | sort -rn | head -20

# Module-level docstrings for top directories
for dir in $(ls -d */ 2>/dev/null | head -10); do
    if [ -f "$dir/__init__.py" ]; then
        echo "=== $dir/__init__.py ==="
        head -40 "$dir/__init__.py"
    fi
done

# Key base classes and mixins (common patterns)
grep -rn 'class.*Mixin\\|class.*Base\\|class.*Abstract' --include='*.py' -l | head -10
"""


def mine_repo(container, repo: str) -> dict[str, str]:
    """Extract raw knowledge from a repo inside a Docker container.

    Returns dict mapping source label -> extracted text.
    """
    raw: dict[str, str] = {}

    # Run standard knowledge source commands
    for cmd in KNOWLEDGE_SOURCES:
        label = cmd.split()[0:2]
        label_str = " ".join(label)
        exit_code, output = exec_in_container(container, cmd)
        if exit_code == 0 and output.strip():
            raw[label_str] = output.strip()

    # Targeted extraction
    exit_code, output = exec_in_container(container, TARGETED_EXTRACTION)
    if exit_code == 0 and output.strip():
        raw["targeted_extraction"] = output.strip()

    # Repo-specific mining based on known structure
    repo_name = repo.split("/")[-1] if "/" in repo else repo

    # Find and read key module docstrings for the main package
    exit_code, output = exec_in_container(
        container,
        f"find . -maxdepth 2 -path './{repo_name}*/__init__.py' "
        f"-exec head -50 {{}} + 2>/dev/null || "
        f"find . -maxdepth 2 -name '__init__.py' "
        f"-exec head -30 {{}} + 2>/dev/null | head -1000"
    )
    if exit_code == 0 and output.strip():
        raw["package_docstrings"] = output.strip()

    log.info(f"  Mined {len(raw)} knowledge sources from {repo}")
    return raw


# ---------------------------------------------------------------------------
# LLM distillation: raw text -> structured entities
# ---------------------------------------------------------------------------

DISTILL_SYSTEM = """You are a knowledge engineer extracting structured org memory from a software repository.

Given raw text from a repo's docs, docstrings, and history, produce structured entities.

Entity types:
- Component: A code module, class, or subsystem (e.g., "django.contrib.auth.validators")
- Pattern: A fix recipe or refactoring pattern (e.g., "Use \\A/\\Z instead of ^/$ for regex validators")
- Pitfall: A common mistake + symptoms (e.g., "Regex anchors allow multiline bypass in validators")
- Decision: An ADR or design choice (e.g., "Validators use @deconstructible for migration serialization")
- TestSignal: A failure signature -> likely cause (e.g., "ValidationError with newline in username -> regex anchor issue")
- ExampleFix: A high-level fix pattern, NOT a specific patch (e.g., "Replace ^ with \\A and $ with \\Z in regex patterns")

Rules:
- Each entity must have: id, type, name, summary (1-3 sentences), tags (list of keywords)
- Entity IDs: use slug format like "comp-django-auth-validators" or "pitfall-regex-anchors"
- Keep summaries concise and actionable
- For ExampleFix: describe the PATTERN, not a specific commit/PR
- Do NOT reference specific issue numbers or PR numbers
- Do NOT include verbatim diffs or patches
- Focus on knowledge that would help someone working on this codebase for the first time

Also produce relations between entities:
- Pitfall -> affects -> Component
- TestSignal -> suggests -> Component or Pattern
- Pattern -> applies_to -> Component
- Decision -> constrains -> Pattern
- ExampleFix -> demonstrates -> Pattern

Output valid JSON with two arrays: "entities" and "relations".
"""

DISTILL_USER_TEMPLATE = """Repository: {repo}

Here is raw knowledge extracted from this repo's docs, docstrings, and history:

{raw_text}

Produce 10-25 structured entities and their relations. Focus on the most useful org knowledge
that would help a developer fix bugs in this codebase. Output JSON:

{{
  "entities": [
    {{"id": "...", "type": "Component|Pattern|Pitfall|Decision|TestSignal|ExampleFix",
      "name": "...", "summary": "...", "detail": "...", "tags": ["..."]}}
  ],
  "relations": [
    {{"source_id": "...", "relation": "affects|suggests|applies_to|constrains|demonstrates",
      "target_id": "..."}}
  ]
}}"""


def distill_entities(raw_sources: dict[str, str], repo: str,
                     model: str = "minimax/MiniMax-M2.5") -> tuple[list[Entity], list[Relation]]:
    """Use LLM to distill raw text into structured entities and relations."""

    # Combine raw sources into a single text block, with labels
    parts = []
    for label, text in raw_sources.items():
        # Truncate each source to keep prompt manageable
        truncated = text[:3000]
        if len(text) > 3000:
            truncated += f"\n... ({len(text) - 3000} chars truncated)"
        parts.append(f"### {label}\n{truncated}")

    raw_text = "\n\n".join(parts)

    # Truncate total to ~12k chars to stay well within context
    if len(raw_text) > 12000:
        raw_text = raw_text[:12000] + "\n\n... (truncated for context limit)"

    user_msg = DISTILL_USER_TEMPLATE.format(repo=repo, raw_text=raw_text)

    try:
        response = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": DISTILL_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        log.error(f"  LLM distillation failed: {e}")
        return [], []

    content = response.choices[0].message.content or ""

    # Parse JSON response
    try:
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(content)
    except json.JSONDecodeError as e:
        log.error(f"  Failed to parse LLM response as JSON: {e}")
        log.debug(f"  Response: {content[:500]}")
        return [], []

    # Convert to dataclasses
    entities = []
    for e_data in data.get("entities", []):
        try:
            entity = Entity(
                id=e_data["id"],
                type=e_data["type"],
                name=e_data["name"],
                summary=e_data.get("summary", ""),
                detail=e_data.get("detail", ""),
                source=f"mined:{repo}",
                tags=e_data.get("tags", []),
            )
            entities.append(entity)
        except (KeyError, TypeError) as err:
            log.warning(f"  Skipping malformed entity: {err}")

    relations = []
    entity_ids = {e.id for e in entities}
    for r_data in data.get("relations", []):
        try:
            rel = Relation(
                source_id=r_data["source_id"],
                relation=r_data["relation"],
                target_id=r_data["target_id"],
            )
            if rel.source_id in entity_ids and rel.target_id in entity_ids:
                relations.append(rel)
        except (KeyError, TypeError) as err:
            log.warning(f"  Skipping malformed relation: {err}")

    # Track cost
    usage = response.usage
    cost = 0.0
    if usage:
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            pass

    log.info(f"  Distilled {len(entities)} entities, {len(relations)} relations "
             f"(cost: ${cost:.4f})")

    return entities, relations


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build_pack_for_repo(
    instances: list[dict[str, Any]],
    model: str,
    output_dir: Path,
) -> int:
    """Build a knowledge pack for a repo and publish as watercooler thread.

    Uses the first instance's Docker image to mine the repo, then writes
    entities as watercooler thread entries via say().

    Returns number of entities published.
    """
    from swebench.harness.docker_build import build_instance_images, make_test_spec

    instance = instances[0]
    repo = instance["repo"]
    instance_id = instance["instance_id"]

    log.info(f"Building knowledge pack for {repo}")
    log.info(f"  Using instance {instance_id} as mining source")

    client = docker.from_env()

    # Build Docker image
    spec = make_test_spec(instance)
    image_name = spec.instance_image_key
    log.info(f"  Building image: {image_name}")
    build_instance_images(client=client, dataset=[spec])

    # Start container
    container = client.containers.run(
        image_name, command="sleep infinity", detach=True, remove=False,
    )

    try:
        # Mine raw knowledge
        raw_sources = mine_repo(container, repo)

        # LLM distillation
        entities, relations = distill_entities(raw_sources, repo, model=model)

        if not entities:
            log.warning(f"  No entities distilled for {repo}")
            return 0

        # Anti-leakage filter: remove entities too close to any instance's answer
        entity_ids_to_remove = set()
        for inst in instances:
            issue_text = inst.get("problem_statement", "")
            patch_text = inst.get("patch", "")
            for entity in entities:
                combined = f"{entity.name} {entity.summary} {entity.detail}"
                if check_leakage(combined, inst["instance_id"], issue_text, patch_text):
                    entity_ids_to_remove.add(entity.id)
                    log.info(f"  Anti-leakage: removing {entity.id} "
                             f"(too similar to {inst['instance_id']})")

        if entity_ids_to_remove:
            entities = [e for e in entities if e.id not in entity_ids_to_remove]
            relations = [r for r in relations
                         if r.source_id not in entity_ids_to_remove
                         and r.target_id not in entity_ids_to_remove]
            log.info(f"  After anti-leakage: {len(entities)} entities, "
                     f"{len(relations)} relations")

        # Publish as watercooler thread entries
        repo_slug = repo.replace("/", "__")
        threads_dir = output_dir / repo_slug
        threads_dir.mkdir(parents=True, exist_ok=True)
        topic = f"{repo_slug}-knowledge"

        count = publish_to_watercooler(
            entities, relations, threads_dir, topic,
            agent="Builder (system)",
        )

        return count

    finally:
        container.stop(timeout=5)
        container.remove(force=True)


def main():
    parser = argparse.ArgumentParser(description="Build org knowledge packs for SWE-bench repos")
    parser.add_argument("--instance-ids", nargs="+", required=True,
                        help="SWE-bench instance IDs to build packs for")
    parser.add_argument("--model", default="minimax/MiniMax-M2.5",
                        help="LiteLLM model for distillation")
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite",
                        help="HuggingFace dataset name")
    parser.add_argument("--split", default="test", help="Dataset split")
    parser.add_argument("--output-dir", default="logs/knowledge-packs",
                        help="Output directory for knowledge packs (watercooler threads)")
    args = parser.parse_args()

    setup_api_keys()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load dataset
    from datasets import load_dataset
    ds = load_dataset(args.dataset, split=args.split)
    log.info(f"Loaded {len(ds)} instances from {args.dataset}")

    # Filter to requested instances
    target_ids = set(args.instance_ids)
    instances_by_repo: dict[str, list] = {}
    for inst in ds:
        if inst["instance_id"] in target_ids:
            repo = inst["repo"]
            instances_by_repo.setdefault(repo, []).append(inst)

    log.info(f"Found {sum(len(v) for v in instances_by_repo.values())} instances "
             f"across {len(instances_by_repo)} repos")

    # Build pack for each repo
    start_time = time.time()
    total_entities = 0

    for repo, repo_instances in instances_by_repo.items():
        count = build_pack_for_repo(repo_instances, args.model, output_dir)
        total_entities += count

    elapsed = time.time() - start_time
    log.info(f"\nDone: {total_entities} entities "
             f"across {len(instances_by_repo)} repos in {elapsed:.0f}s")
    log.info(f"Packs saved to: {output_dir}")


if __name__ == "__main__":
    main()
