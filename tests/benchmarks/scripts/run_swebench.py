#!/usr/bin/env python3
"""Run SWE-bench tasks with a minimal LLM agent and evaluate results.

Supports four runner modes:
  - baseline: bash only
  - inject: bash only + static org-knowledge pre-injected into the system prompt
  - tools: bash + read-only Watercooler text tools via `wc-*` commands
  - tools_guided: tools + injected usage guidance (prompt-only “skills”)

Produces predictions.jsonl compatible with SWE-bench's evaluator.

Usage:
    # Validate setup with 1 instance (gold patch)
    python tests/benchmarks/scripts/run_swebench.py --validate

    # Baseline run (no WC)
    python tests/benchmarks/scripts/run_swebench.py \
        --model minimax/MiniMax-M2.5 \
        --instance-ids sympy__sympy-20590 django__django-11099 \
        --output-dir logs/swebench-baseline

    # WC-augmented run (static inject)
    python tests/benchmarks/scripts/run_swebench.py \
        --model minimax/MiniMax-M2.5 \
        --instance-ids sympy__sympy-20590 django__django-11099 \
        --wc-pack logs/knowledge-packs \
        --wc-mode inject \
        --output-dir logs/swebench-wc

    # WC tool run (prompt-only models can use Watercooler via `wc-*`)
    python tests/benchmarks/scripts/run_swebench.py \
        --model minimax/MiniMax-M2.5 \
        --instance-ids sympy__sympy-20590 django__django-11099 \
        --wc-pack logs/knowledge-packs \
        --wc-mode tools \
        --wc-max-calls 5 \
        --output-dir logs/swebench-wc-tools
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
from typing import Any, Literal

import docker
import litellm

# Add project root + src/ for imports (src-layout package)
_repo_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def setup_api_keys() -> None:
    """Load API keys from ~/.watercooler/credentials.toml into env."""
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

def exec_in_container(container: docker.models.containers.Container, cmd: str,
                      workdir: str = "/testbed",
                      timeout: int = 120) -> tuple[int, str]:
    """Execute a command in a running container. Returns (exit_code, output)."""
    try:
        result = container.exec_run(
            ["bash", "-c", cmd],
            workdir=workdir,
            demux=True,
        )
        stdout = result.output[0].decode("utf-8", errors="replace") if result.output[0] else ""
        stderr = result.output[1].decode("utf-8", errors="replace") if result.output[1] else ""
        output = stdout
        if stderr:
            output += "\n" + stderr
        # Truncate very long output
        if len(output) > 15000:
            output = output[:5000] + f"\n\n... ({len(output) - 10000} chars truncated) ...\n\n" + output[-5000:]
        return result.exit_code, output
    except Exception as e:
        return -1, f"Error executing command: {e}"


# ---------------------------------------------------------------------------
# Agent (supports both baseline and WC-inject modes)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_BASELINE = """You are a skilled software engineer. You will be given a GitHub issue and access to the repository via bash commands.

Your goal is to produce a minimal patch that resolves the issue. Follow this workflow:

1. Read the issue carefully and understand what needs to change
2. Explore the relevant source files (use grep, find, cat)
3. Make the minimal code changes needed to fix the issue
4. Verify your changes are correct

Rules:
- Make ONLY the changes necessary to fix the issue
- Do NOT modify test files
- Do NOT add new dependencies
- Keep changes minimal and focused
- Do NOT spend the entire run only exploring. Within your first ~10 commands, you must attempt an edit and run a relevant test command.
- Prefer simple, explicit edits (e.g. use python/perl/sed to modify files), then run tests, then inspect `git diff`.
- When done, respond with EXACTLY: SUBMIT

Each response must contain exactly ONE bash command in a code block:
```bash
your_command_here
```"""

SYSTEM_PROMPT_WC_INJECT = """You are a skilled software engineer. You will be given a GitHub issue and access to the repository via bash commands.

Your team has accumulated organizational knowledge about this codebase. Relevant knowledge for this issue is provided below — use it to guide your approach.

{knowledge_context}

Your goal is to produce a minimal patch that resolves the issue. Follow this workflow:

1. Read the issue carefully and understand what needs to change
2. Review the org knowledge above for relevant patterns and pitfalls
3. Explore the relevant source files using bash commands (grep, find, cat)
4. Make the minimal code changes needed to fix the issue
5. Verify your changes are correct

Rules:
- Make ONLY the changes necessary to fix the issue
- Do NOT modify test files
- Do NOT add new dependencies
- Keep changes minimal and focused
- Do NOT spend the entire run only exploring. Within your first ~10 commands, you must attempt an edit and run a relevant test command.
- Prefer simple, explicit edits (e.g. use python/perl/sed to modify files), then run tests, then inspect `git diff`.
- When done, respond with EXACTLY: SUBMIT

Each response must contain exactly ONE bash command in a code block:
```bash
your_command_here
```"""

WC_TOOLS_APPENDIX = """

You may also use read-only Watercooler tools by emitting a `wc-*` command as your ONE command.
These run on the host and return an observation. Use them to retrieve org knowledge on demand.

Available tools:
- wc-search "<query>"
- wc-smart-query "<query>"
- wc-read-thread <topic>
- wc-get-entry <topic> <index>

Important:
- You still must output EXACTLY ONE command inside a single ```bash``` code block per step.
- When you use a wc tool, you will receive text back; then proceed with bash commands.
- Do not submit without using at least one `wc-*` retrieval command.

Recommended:
- Early in the run (often as step 1), do one `wc-search` using 3–8 concrete keywords from the issue.
"""


WcMode = Literal["baseline", "inject", "tools", "tools_guided"]


def build_knowledge_context(
    problem_statement: str,
    knowledge_pack,
    leakage_filter=None,
    max_queries: int = 3,
    max_tokens: int = 3000,
) -> str:
    """Search the knowledge pack and build a context string for prompt injection.

    Extracts key terms from the problem statement, runs multiple searches,
    deduplicates results, and formats them as a concise knowledge block.
    """
    if knowledge_pack is None:
        return ""

    # Extract candidate search queries from the problem statement
    queries = _extract_search_queries(problem_statement)[:max_queries]
    if not queries:
        formatted = knowledge_pack.format_results([])
        return f"--- ORG KNOWLEDGE BASE ---\n{formatted}\n--- END ORG KNOWLEDGE ---"

    # Run searches and collect unique results
    seen_ids: set[str] = set()
    all_results = []
    per_query_budget = max_tokens // max(len(queries), 1)

    for query in queries:
        results = knowledge_pack.search(
            query,
            top_k=3,
            max_tokens=per_query_budget,
            exclude_fn=leakage_filter,
        )
        for r in results:
            if r.entry_id not in seen_ids:
                seen_ids.add(r.entry_id)
                all_results.append(r)

    if not all_results:
        formatted = knowledge_pack.format_results([])
        return f"--- ORG KNOWLEDGE BASE ---\n{formatted}\n--- END ORG KNOWLEDGE ---"

    # Format as a knowledge block
    formatted = knowledge_pack.format_results(all_results)
    return f"--- ORG KNOWLEDGE BASE ---\n{formatted}\n--- END ORG KNOWLEDGE ---"


def _extract_search_queries(problem_statement: str) -> list[str]:
    """Extract 2-3 search queries from a problem statement.

    Strategy: pull out class names, function names, and error signatures
    that are likely to match knowledge pack entries.
    """
    queries: list[str] = []

    # Extract class/function names (CamelCase and snake_case identifiers)
    identifiers = re.findall(r'\b([A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]*)*)\b', problem_statement)
    # Filter out common non-class words
    skip = {"The", "This", "That", "When", "Where", "What", "How", "For",
            "With", "From", "Should", "Could", "Would", "Does", "Has",
            "Not", "But", "And", "Are", "Was", "Were", "Been", "Being",
            "Some", "Any", "All", "Each", "Every", "Other", "Error",
            "None", "True", "False", "Python", "TypeError", "ValueError",
            "AttributeError", "RuntimeError", "KeyError", "IndexError"}
    identifiers = [i for i in identifiers if i not in skip and len(i) > 2]

    # Build query from top identifiers, splitting CamelCase for better matching
    if identifiers:
        # Deduplicate preserving order
        seen = set()
        unique = []
        for ident in identifiers:
            if ident not in seen:
                seen.add(ident)
                unique.append(ident)
        # Split CamelCase: "UsernameValidator" -> "Username Validator"
        expanded = []
        for ident in unique:
            parts = re.findall(r'[A-Z][a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)', ident)
            if len(parts) > 1:
                expanded.extend(parts)
            expanded.append(ident)
        # First query: expanded terms from top identifiers
        queries.append(" ".join(expanded[:6]))
        # Second query: remaining identifiers if any
        if len(unique) > 4:
            queries.append(" ".join(unique[4:8]))

    # Extract snake_case function/method names
    func_names = re.findall(r'\b([a-z_][a-z0-9_]{2,})\b', problem_statement)
    # Filter to likely method/function names (not common English words)
    code_like = [f for f in func_names if '_' in f or f.startswith('__')]
    if code_like:
        seen_funcs = set()
        unique_funcs = []
        for f in code_like:
            if f not in seen_funcs:
                seen_funcs.add(f)
                unique_funcs.append(f)
        queries.append(" ".join(unique_funcs[:4]))

    # Fallback: use first 8 significant words from the problem
    if not queries:
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]+', problem_statement[:500])
        significant = [w for w in words if len(w) > 3][:8]
        if significant:
            queries.append(" ".join(significant))

    return queries


def run_agent(
    container: docker.models.containers.Container,
    problem_statement: str,
    model_name: str,
    max_steps: int = 40,
    cost_limit: float = 1.00,
    knowledge_context: str = "",
    wc_session=None,
    wc_guidance_text: str = "",
    min_wc_commands: int = 0,
    workdir: str = "/testbed",
) -> dict[str, Any]:
    """Run the LLM agent loop.

    Args:
        knowledge_context: Pre-built knowledge context string (from
            build_knowledge_context). If non-empty, uses the WC inject prompt.

    Returns dict with: patch, cost, steps, and detailed metrics.
    """
    use_wc_inject = bool(knowledge_context)
    if use_wc_inject:
        system_prompt = SYSTEM_PROMPT_WC_INJECT.format(
            knowledge_context=knowledge_context,
        )
    else:
        system_prompt = SYSTEM_PROMPT_BASELINE

    if wc_session is not None:
        system_prompt = system_prompt + WC_TOOLS_APPENDIX
        if wc_guidance_text.strip():
            system_prompt = system_prompt + "\n\n# Watercooler usage guidance\n\n" + wc_guidance_text.strip() + "\n"

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Please resolve this GitHub issue:\n\n{problem_statement}"},
    ]

    # Metrics tracking
    metrics = {
        "total_cost": 0.0,
        "steps": 0,
        "bash_commands": 0,
        "wc_injected": use_wc_inject,  # whether knowledge was pre-injected
        "wc_commands": 0,
        "wc_tools_used": {},           # tool -> count
        "wc_entry_ids_returned": [],   # for attribution
        "wc_tokens_returned": 0,
        "test_runs": 0,            # commands that look like test invocations
        "file_edits": 0,           # commands that modify files (sed, patch, etc.)
        "duplicate_commands": 0,   # repeated identical commands
        "commands_seen": set(),    # for duplicate detection
    }

    for step in range(max_steps):
        metrics["steps"] += 1

        try:
            response = litellm.completion(
                model=model_name,
                messages=messages,
                temperature=0.0,
                max_tokens=4096,
            )
        except Exception as e:
            log.error(f"  LLM call failed at step {step}: {e}")
            break

        # Track cost
        usage = response.usage
        if usage:
            try:
                cost = litellm.completion_cost(completion_response=response)
                metrics["total_cost"] += cost
            except Exception:
                pass

        choice = response.choices[0]
        content = choice.message.content or ""
        messages.append({"role": "assistant", "content": content})

        # Extract command (bash or wc-*)
        cmd = _extract_bash_command(content)
        if not cmd:
            messages.append({
                "role": "user",
                "content": (
                    "Please provide exactly ONE command in a ```bash``` code block "
                    "(bash command or a wc-* tool command), or say SUBMIT if done."
                ),
            })
            continue

        # Allow SUBMIT either bare or inside the command block, but enforce WC
        # retrieval minimum in tools modes so process metrics are meaningful.
        if cmd.strip().upper() == "SUBMIT":
            if min_wc_commands > 0 and metrics["wc_commands"] < min_wc_commands:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Before SUBMIT you must run at least {min_wc_commands} wc-* retrieval command(s). "
                        "Run a wc-search or wc-smart-query now."
                    ),
                })
                continue
            log.info(f"  Agent submitted at step {metrics['steps']}")
            break

        # Track command metrics
        cmd_lower = cmd.lower().strip()

        if cmd_lower in metrics["commands_seen"]:
            metrics["duplicate_commands"] += 1
        metrics["commands_seen"].add(cmd_lower)

        is_wc_cmd = cmd_lower.startswith("wc-")

        if (
            min_wc_commands > 0
            and not is_wc_cmd
            and metrics["wc_commands"] < min_wc_commands
            and metrics["bash_commands"] >= 2
        ):
            messages.append({
                "role": "user",
                "content": (
                    f"Run at least {min_wc_commands} wc-* retrieval command before continuing with more bash commands. "
                    "Use wc-search or wc-smart-query."
                ),
            })
            continue

        if is_wc_cmd:
            metrics["wc_commands"] += 1
            if wc_session is None:
                exit_code, output = 1, "Error: wc tools are not enabled for this run mode."
            else:
                result = wc_session.execute(cmd)
                tool_name = getattr(result, "tool", "wc-unknown")
                output = getattr(result, "output", "") or ""
                ok = bool(getattr(result, "ok", True))
                exit_code = 0 if ok else 1

                metrics["wc_tools_used"][tool_name] = metrics["wc_tools_used"].get(tool_name, 0) + 1
                entry_ids = list(getattr(result, "entry_ids", []) or [])
                if entry_ids:
                    metrics["wc_entry_ids_returned"].extend(entry_ids[:25])
                metrics["wc_tokens_returned"] += int(len(output.split()) / 0.75) if output else 0
        else:
            metrics["bash_commands"] += 1

            if any(kw in cmd_lower for kw in ["pytest", "runtests", "test_", "python -m test"]):
                metrics["test_runs"] += 1

            if any(kw in cmd_lower for kw in ["sed -i", "patch", "cat >", "cat >>", "echo >",
                                               "echo >>", "tee ", "python -c.*open.*write"]):
                metrics["file_edits"] += 1

            # Execute bash in-container
            exit_code, output = exec_in_container(container, cmd, workdir=workdir)

        observation = f"Exit code: {exit_code}\n{output}" if output else f"Exit code: {exit_code}"
        messages.append({"role": "user", "content": observation})

        log.info(f"  Step {metrics['steps']}: {cmd[:80]}... -> exit={exit_code} ({len(output)} chars)")

        # Cost guard
        if metrics["total_cost"] >= cost_limit:
            log.warning(f"  Cost limit reached: ${metrics['total_cost']:.4f}")
            break

    # Extract patch (diff from HEAD)
    _, patch = exec_in_container(container, "git diff", workdir=workdir)

    # Clean up non-serializable set
    metrics["commands_seen"] = list(metrics["commands_seen"])

    return {
        "patch": patch.strip(),
        **metrics,
    }


def _extract_bash_command(text: str) -> str | None:
    """Extract a bash command from markdown code block."""
    match = re.search(r"```(?:bash|sh)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Knowledge pack loading (watercooler-backed)
# ---------------------------------------------------------------------------

def load_knowledge_pack(wc_pack_dir: Path, repo: str):
    """Load a watercooler-backed knowledge pack for a specific repo.

    Expects the pack at wc_pack_dir/{repo_slug}/ with a watercooler
    baseline graph structure (created by build_knowledge_pack.py).
    """
    from tests.benchmarks.adapters.knowledge_pack import WatercoolerKnowledgePack

    repo_slug = repo.replace("/", "__")
    threads_dir = wc_pack_dir / repo_slug
    topic = f"{repo_slug}-knowledge"

    if not threads_dir.exists():
        log.warning(f"  No knowledge pack found for {repo} at {threads_dir}")
        return None

    pack = WatercoolerKnowledgePack(threads_dir, topic)
    log.info(f"  Loaded knowledge pack for {repo}: {pack.entry_count} entries")
    return pack


# ---------------------------------------------------------------------------
# SWE-bench integration
# ---------------------------------------------------------------------------

def build_and_run_instance(
    instance: dict[str, Any],
    model_name: str,
    output_dir: Path,
    max_steps: int = 40,
    cost_limit: float = 1.00,
    wc_pack_dir: Path | None = None,
    wc_mode: WcMode = "baseline",
    wc_guidance_text: str = "",
    wc_tier_ceiling: str = "T1",
    wc_max_calls: int = 5,
    wc_token_budget: int = 900,
    wc_code_path: Path | None = None,
) -> dict[str, Any]:
    """Build Docker image for one SWE-bench instance and run agent."""
    from swebench.harness.docker_build import (
        build_instance_images,
        make_test_spec,
    )

    instance_id = instance["instance_id"]
    repo = instance["repo"]
    log.info(f"Processing {instance_id}")

    # Pre-build knowledge context (search upfront, inject into prompt)
    knowledge_context = ""
    if wc_pack_dir and wc_mode == "inject":
        from tests.benchmarks.adapters.knowledge_pack import make_leakage_filter
        knowledge_pack = load_knowledge_pack(wc_pack_dir, repo)
        if knowledge_pack:
            leakage_filter = make_leakage_filter(
                instance_id,
                instance.get("problem_statement", ""),
                instance.get("patch", ""),
            )
            knowledge_context = build_knowledge_context(
                instance["problem_statement"],
                knowledge_pack,
                leakage_filter=leakage_filter,
                max_tokens=3000,
            )
            if knowledge_context:
                log.info(f"  Injected {len(knowledge_context)} chars of org knowledge")

    wc_session = None
    if wc_pack_dir and wc_mode in ("tools", "tools_guided"):
        knowledge_pack = load_knowledge_pack(wc_pack_dir, repo)
        if knowledge_pack:
            try:
                from tests.benchmarks.scripts.wc_text_tools import WcToolSession
            except Exception as e:
                log.warning(f"  Failed to import wc_text_tools dispatcher: {e}")
                WcToolSession = None  # type: ignore[assignment]

            if WcToolSession is not None:
                wc_session = WcToolSession(
                    threads_dir=knowledge_pack.threads_dir,
                    default_topic=knowledge_pack.topic,
                    code_path=wc_code_path if wc_tier_ceiling in ("T2", "T3") else None,
                    group_ids=None,
                    tier_ceiling=wc_tier_ceiling,
                    max_calls=wc_max_calls,
                    token_budget=wc_token_budget,
                )

    client = docker.from_env()

    # Build the instance Docker image
    spec = make_test_spec(instance)
    image_name = spec.instance_image_key

    log.info(f"  Building image: {image_name}")
    build_instance_images(client=client, dataset=[spec])
    try:
        container = client.containers.run(
            image_name,
            command="sleep infinity",
            detach=True,
            remove=False,
        )
    except Exception as e:
        log.error(f"  Failed to start container: {e}")
        return {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": "",
            "error": str(e),
            "mode": wc_mode,
        }

    try:
        # Run agent
        agent_result = run_agent(
            container,
            instance["problem_statement"],
            model_name,
            max_steps=max_steps,
            cost_limit=cost_limit,
            knowledge_context=knowledge_context,
            wc_session=wc_session,
            wc_guidance_text=(wc_guidance_text if wc_mode == "tools_guided" else ""),
            min_wc_commands=(1 if wc_mode in ("tools", "tools_guided") else 0),
        )

        result = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": agent_result["patch"],
            "cost": agent_result["total_cost"],
            "steps": agent_result["steps"],
            "mode": wc_mode,
            "metrics": {
                "bash_commands": agent_result["bash_commands"],
                "wc_injected": agent_result["wc_injected"],
                "wc_commands": agent_result.get("wc_commands", 0),
                "wc_tools_used": agent_result.get("wc_tools_used", {}),
                "wc_entry_ids_returned": agent_result.get("wc_entry_ids_returned", []),
                "wc_tokens_returned": agent_result.get("wc_tokens_returned", 0),
                "test_runs": agent_result["test_runs"],
                "file_edits": agent_result["file_edits"],
                "duplicate_commands": agent_result["duplicate_commands"],
            },
        }
        log.info(f"  Done: {agent_result['steps']} steps, "
                 f"${agent_result['total_cost']:.4f}, "
                 f"patch={len(agent_result['patch'])} chars")

    except Exception as e:
        log.error(f"  Agent error: {e}")
        result = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": "",
            "error": str(e),
            "mode": wc_mode,
        }
    finally:
        container.stop(timeout=5)
        container.remove(force=True)

    # Save individual result
    result_path = output_dir / f"{instance_id.replace('/', '__')}.json"
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)

    return result


def evaluate_predictions(predictions_path: Path, run_id: str, *, cwd: Path | None = None) -> dict:
    """Run SWE-bench evaluation on collected predictions."""
    import subprocess
    predictions_arg = str(predictions_path.resolve())
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--predictions_path", predictions_arg,
        "--max_workers", "4",
        "--run_id", run_id,
    ]
    log.info(f"Running evaluation: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    log.info(result.stdout)
    if result.returncode != 0:
        log.error(f"Evaluation failed:\n{result.stderr}")
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run SWE-bench with LLM agent")
    parser.add_argument("--validate", action="store_true",
                        help="Validate setup with gold patch on 1 instance")
    parser.add_argument("--model", default="minimax/MiniMax-M2.5",
                        help="LiteLLM model string")
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite",
                        help="HuggingFace dataset name")
    parser.add_argument("--split", default="test",
                        help="Dataset split")
    parser.add_argument("--instance-ids", nargs="+", default=None,
                        help="Specific instance IDs to run")
    parser.add_argument("--max-instances", type=int, default=None,
                        help="Maximum instances to run")
    parser.add_argument("--max-steps", type=int, default=40,
                        help="Max agent steps per instance")
    parser.add_argument("--cost-limit", type=float, default=1.00,
                        help="Cost limit per instance in USD")
    parser.add_argument("--output-dir", type=str, default="logs/swebench-test",
                        help="Output directory for results")
    parser.add_argument("--eval-only", action="store_true",
                        help="Only run evaluation on existing predictions")
    # WC knowledge pack options
    parser.add_argument("--wc-pack", type=str, default=None,
                        help="Path to knowledge packs directory (enables WC mode)")
    parser.add_argument(
        "--wc-mode",
        type=str,
        default=None,
        choices=["baseline", "inject", "tools", "tools_guided"],
        help="Runner mode (default: baseline, or inject if --wc-pack provided)",
    )
    parser.add_argument(
        "--wc-guidance-file",
        type=str,
        default=None,
        help="Usage guidance markdown to inject (tools_guided only)",
    )
    parser.add_argument(
        "--wc-max-calls",
        type=int,
        default=5,
        help="Max wc-* tool calls per instance (tools modes)",
    )
    parser.add_argument(
        "--wc-token-budget",
        type=int,
        default=900,
        help="Approx output token budget per wc-* call (tools modes)",
    )
    parser.add_argument(
        "--wc-tier-ceiling",
        type=str,
        default="T1",
        choices=["T1", "T2", "T3"],
        help="Max memory tier available to wc-smart-query (tools modes)",
    )
    parser.add_argument(
        "--wc-code-path",
        type=str,
        default="",
        help="Code path for T2/T3 backends (optional; tools modes)",
    )
    args = parser.parse_args()

    if args.validate:
        log.info("Validating SWE-bench setup with gold patch...")
        import subprocess
        result = subprocess.run([
            sys.executable, "-m", "swebench.harness.run_evaluation",
            "--predictions_path", "gold",
            "--max_workers", "1",
            "--instance_ids", "sympy__sympy-20590",
            "--run_id", "validate-gold",
        ], capture_output=False, timeout=300)
        sys.exit(result.returncode)

    setup_api_keys()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    wc_pack_dir = Path(args.wc_pack) if args.wc_pack else None
    if args.wc_mode is None:
        args.wc_mode = "inject" if wc_pack_dir else "baseline"
    mode: WcMode = args.wc_mode
    if mode != "baseline" and wc_pack_dir is None:
        raise SystemExit("--wc-mode requires --wc-pack (knowledge packs directory).")

    wc_guidance_text = ""
    if mode == "tools_guided":
        guidance_path = Path(args.wc_guidance_file) if args.wc_guidance_file else (
            Path(__file__).resolve().parents[1] / "guidance" / "watercooler_usage.md"
        )
        if guidance_path.exists():
            wc_guidance_text = guidance_path.read_text(encoding="utf-8")
        else:
            log.warning(f"Guidance file not found: {guidance_path} (continuing without guidance)")

    wc_code_path = Path(args.wc_code_path) if args.wc_code_path else None

    log.info(f"Mode: {mode}")

    predictions_path = output_dir / "predictions.jsonl"

    if not args.eval_only:
        # Load dataset
        from datasets import load_dataset
        ds = load_dataset(args.dataset, split=args.split)
        log.info(f"Loaded {len(ds)} instances from {args.dataset}")

        # Filter to specific instances
        if args.instance_ids:
            ds = ds.filter(lambda x: x["instance_id"] in args.instance_ids)
            log.info(f"Filtered to {len(ds)} instances")

        if args.max_instances:
            ds = ds.select(range(min(args.max_instances, len(ds))))
            log.info(f"Limited to {len(ds)} instances")

        # Run agent on each instance
        results = []
        start_time = time.time()

        for i, instance in enumerate(ds):
            log.info(f"\n{'='*60}")
            log.info(f"Instance {i+1}/{len(ds)}: {instance['instance_id']} [{mode}]")
            log.info(f"{'='*60}")

            result = build_and_run_instance(
                instance,
                args.model,
                output_dir,
                max_steps=args.max_steps,
                cost_limit=args.cost_limit,
                wc_pack_dir=wc_pack_dir,
                wc_mode=mode,
                wc_guidance_text=wc_guidance_text,
                wc_tier_ceiling=args.wc_tier_ceiling,
                wc_max_calls=args.wc_max_calls,
                wc_token_budget=args.wc_token_budget,
                wc_code_path=wc_code_path,
            )
            results.append(result)

            # Append to predictions file incrementally
            with open(predictions_path, "a") as f:
                pred = {
                    "instance_id": result["instance_id"],
                    "model_name_or_path": result["model_name_or_path"],
                    "model_patch": result.get("model_patch", ""),
                }
                f.write(json.dumps(pred) + "\n")

        elapsed = time.time() - start_time

        # Summary with enhanced metrics
        total_cost = sum(r.get("cost", 0) for r in results)
        total_patches = sum(1 for r in results if r.get("model_patch"))
        total_errors = sum(1 for r in results if r.get("error"))
        total_test_runs = sum(r.get("metrics", {}).get("test_runs", 0) for r in results)
        total_duplicates = sum(r.get("metrics", {}).get("duplicate_commands", 0) for r in results)
        total_bash = sum(r.get("metrics", {}).get("bash_commands", 0) for r in results)
        wc_injected = sum(1 for r in results if r.get("metrics", {}).get("wc_injected"))
        total_wc_cmds = sum(r.get("metrics", {}).get("wc_commands", 0) for r in results)

        summary = {
            "model": args.model,
            "dataset": args.dataset,
            "mode": mode,
            "instances": len(results),
            "patches_produced": total_patches,
            "errors": total_errors,
            "total_cost": total_cost,
            "elapsed_seconds": elapsed,
            "aggregate_metrics": {
                "total_bash_commands": total_bash,
                "total_wc_commands": total_wc_cmds,
                "total_test_runs": total_test_runs,
                "total_duplicate_commands": total_duplicates,
                "wc_injected_count": wc_injected,
                "avg_cost_per_instance": total_cost / len(results) if results else 0,
                "avg_steps": sum(r.get("steps", 0) for r in results) / len(results) if results else 0,
            },
            "results": results,
        }

        summary_path = output_dir / "summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)

        log.info(f"\n{'='*60}")
        log.info(f"COMPLETE [{mode}]: {total_patches}/{len(results)} patches produced")
        log.info(f"Cost: ${total_cost:.4f} | Time: {elapsed:.0f}s")
        log.info(f"Bash commands: {total_bash} | WC commands: {total_wc_cmds} | Test runs: {total_test_runs}")
        if wc_injected:
            log.info(f"WC knowledge injected: {wc_injected}/{len(results)} instances")
        log.info(f"Predictions: {predictions_path}")
        log.info(f"Summary: {summary_path}")

    # Evaluate
    if predictions_path.exists():
        log.info("\nRunning SWE-bench evaluation...")
        run_id = output_dir.name
        # swebench writes its report JSON to CWD; keep artifacts contained.
        evaluate_predictions(predictions_path, run_id, cwd=output_dir)
    else:
        log.warning(f"No predictions found at {predictions_path}")


if __name__ == "__main__":
    main()
