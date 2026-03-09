#!/usr/bin/env python3
"""Extract watercooler MCP tool metadata from source files.

Usage:
    python3 extract_tools.py [tools_dir]

    tools_dir defaults to src/watercooler_mcp/tools/ relative to git root.

Output: JSON with summary and per-tool metadata records.
"""

import ast
import json
import sys
from pathlib import Path
import subprocess


def get_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return Path.cwd()
    return Path(result.stdout.strip())


def extract_docstring(func_node: ast.FunctionDef) -> str:
    if (
        func_node.body
        and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, ast.Constant)
    ):
        return func_node.body[0].value.value.strip()
    return ""


def extract_first_line(docstring: str) -> str:
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def extract_params(func_node: ast.FunctionDef) -> list:
    params = []
    args = func_node.args
    defaults = args.defaults
    n_args = len(args.args)
    n_defaults = len(defaults)
    default_start = n_args - n_defaults

    for i, arg in enumerate(args.args):
        if arg.arg in ("self", "ctx"):
            continue

        param = {"name": arg.arg}

        if arg.annotation:
            try:
                param["type"] = ast.unparse(arg.annotation)
            except Exception:
                param["type"] = "?"

        if i >= default_start:
            default_node = defaults[i - default_start]
            try:
                param["default"] = ast.unparse(default_node)
            except Exception:
                param["default"] = "?"
            param["required"] = False
        else:
            param["required"] = True

        params.append(param)

    return params


def infer_rw_class(name: str, docstring: str) -> str:
    name_lower = name.lower()
    doc_lower = docstring.lower()

    mutating = [
        "say", "ack", "handoff", "set_status", "add_episode", "clear_graph",
        "migrate_to", "graph_enrich", "bulk_index", "reindex", "leanrag_run",
    ]
    for kw in mutating:
        if kw in name_lower:
            return "write"

    admin = ["health", "whoami", "daemon", "migration_preflight", "graph_recover", "memory_task_status"]
    for kw in admin:
        if kw in name_lower:
            return "admin"

    return "read"


def infer_tier(name: str, docstring: str) -> str:
    name_lower = name.lower()
    doc_lower = docstring.lower()[:500]

    if "leanrag" in name_lower or "t3" in doc_lower:
        return "T3"
    if any(kw in name_lower for kw in ["graphiti", "entity_edge", "get_entity"]):
        return "T2"
    if any(kw in doc_lower for kw in ["graphiti", "falkordb", "t2"]):
        return "T2"
    return "T1"


def categorize(module_name: str) -> str:
    return {
        "thread_write": "Thread Write",
        "thread_query": "Thread Read",
        "graph": "Graph & Search",
        "memory": "Memory",
        "diagnostic": "Diagnostic",
        "sync": "Sync",
        "migration": "Migration",
        "federation": "Federation",
        "daemon": "Daemon",
    }.get(module_name, "Other")


def extract_tools_from_file(path: Path) -> list:
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [{"error": str(e), "file": str(path)}]

    module_name = path.stem

    # Map impl function name -> MCP tool name
    tool_names: dict[str, str] = {}

    for node in ast.walk(tree):
        # Pattern: mcp.tool(name="watercooler_...")(impl_fn)
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Call)
                and isinstance(func.func, ast.Attribute)
                and func.func.attr == "tool"
            ):
                for kw in func.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        tool_name = kw.value.value
                        if tool_name.startswith("watercooler_"):
                            if node.args:
                                arg0 = node.args[0]
                                if isinstance(arg0, ast.Name):
                                    tool_names[arg0.id] = tool_name
                                elif isinstance(arg0, ast.Attribute):
                                    tool_names[arg0.attr] = tool_name

    tools = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in tool_names:
                tool_name = tool_names[node.name]
                docstring = extract_docstring(node)
                params = extract_params(node)

                tools.append({
                    "name": tool_name,
                    "impl": node.name,
                    "module": module_name,
                    "category": categorize(module_name),
                    "description": extract_first_line(docstring),
                    "docstring_snippet": docstring[:600] if docstring else "",
                    "parameters": params,
                    "param_names": [p["name"] for p in params],
                    "required_params": [p["name"] for p in params if p.get("required")],
                    "rw_class": infer_rw_class(tool_name, docstring),
                    "min_tier": infer_tier(tool_name, docstring),
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                })

    return tools


def main():
    repo_root = get_repo_root()

    tools_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else repo_root / "src" / "watercooler_mcp" / "tools"

    if not tools_dir.exists():
        print(json.dumps({"error": f"Tools directory not found: {tools_dir}"}))
        sys.exit(1)

    all_tools = []
    for path in sorted(tools_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        all_tools.extend(extract_tools_from_file(path))

    all_tools.sort(key=lambda t: (t.get("category", ""), t.get("name", "")))

    summary: dict = {
        "total_tools": len(all_tools),
        "by_category": {},
        "by_rw_class": {},
        "by_tier": {},
    }
    for t in all_tools:
        for key, field in [("by_category", "category"), ("by_rw_class", "rw_class"), ("by_tier", "min_tier")]:
            v = t.get(field, "?")
            summary[key][v] = summary[key].get(v, 0) + 1  # type: ignore[index]

    print(json.dumps({"summary": summary, "tools": all_tools}, indent=2))


if __name__ == "__main__":
    main()
