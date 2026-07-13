"""Unit tests for the Codex plugin + marketplace adapter (plan 1c, goal G3).

Covers ``render_codex_package``/``render_codex_marketplace`` in
``scripts/gen_client_packages.py``: schema validation of the generated
``.codex-plugin/plugin.json`` and ``.agents/plugins/marketplace.json``,
server-name coupling, release-ref pinning, version lockstep, exactly-7-skills
shape, absence of the private ``mcp__watercooler-cloud__`` prefix, no
absolute paths in marketplace metadata, and clean re-run semantics — mirrors
``tests/unit/test_client_package_render.py``'s Claude coverage. Renders to
``tmp_path`` only; no generated package tree is committed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# scripts/gen_client_packages.py is private dev tooling (not exported by
# copy.bara.sky), so this test file is excluded from the public build; the
# importorskip lets it skip cleanly if ever collected without it present.
pytest.importorskip("gen_client_packages")

from gen_client_packages import (  # noqa: E402
    load_manifest,
    render_codex_marketplace,
    render_codex_package,
)
from _client_package_render import load_pyproject_version, resolve_source_ref  # noqa: E402

CODEX_PLUGIN_JSON_SCHEMA = {
    "type": "object",
    "required": ["name", "version", "description"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "skills": {"type": "string", "minLength": 1},
        "mcpServers": {"type": "string", "minLength": 1},
    },
    "additionalProperties": True,
}

MCP_JSON_SCHEMA = {
    "type": "object",
    "required": ["mcpServers"],
    "properties": {
        "mcpServers": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {
                "type": "object",
                "required": ["command", "args"],
                "properties": {
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

MARKETPLACE_JSON_SCHEMA = {
    "type": "object",
    "required": ["name", "plugins"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "interface": {"type": "object"},
        "plugins": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "source"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "source": {
                        "type": "object",
                        "required": ["source", "path"],
                        "properties": {
                            "source": {"type": "string"},
                            "path": {"type": "string", "pattern": r"^\./"},
                        },
                    },
                    "policy": {"type": "object"},
                    "category": {"type": "string"},
                },
            },
        },
    },
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return load_manifest()


# --- schema validation ------------------------------------------------------ #


def test_codex_plugin_json_validates_against_schema(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    data = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text())
    jsonschema.validate(data, CODEX_PLUGIN_JSON_SCHEMA)
    assert data["skills"] == "./skills/"
    assert data["mcpServers"] == "./.mcp.json"


def test_codex_mcp_json_validates_against_schema(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    data = json.loads((plugin_root / ".mcp.json").read_text())
    jsonschema.validate(data, MCP_JSON_SCHEMA)


def test_codex_marketplace_json_validates_against_schema(manifest, tmp_path) -> None:
    render_codex_package(manifest, tmp_path, "1.2.3", "release")
    marketplace_path = render_codex_marketplace(manifest, tmp_path)
    data = json.loads(marketplace_path.read_text())
    jsonschema.validate(data, MARKETPLACE_JSON_SCHEMA)


# --- server-name coupling --------------------------------------------------- #


def test_codex_server_name_matches_skill_mcp_prefix(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    mcp = json.loads((plugin_root / ".mcp.json").read_text())
    servers = list(mcp["mcpServers"].keys())
    assert servers == ["watercooler"]
    assert manifest["invocation"]["server_name"] == "watercooler"
    assert manifest["invocation"]["mcp_prefix"] == "mcp__watercooler__"

    body = "".join(
        f.read_text(encoding="utf-8")
        for f in (plugin_root / "skills").rglob("*")
        if f.is_file()
    )
    assert "mcp__watercooler__" in body
    assert "mcp__watercooler-cloud__" not in body


def test_codex_marketplace_plugin_name_matches_plugin_json(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    plugin_data = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text())
    marketplace_path = render_codex_marketplace(manifest, tmp_path)
    marketplace_data = json.loads(marketplace_path.read_text())
    assert marketplace_data["plugins"][0]["name"] == plugin_data["name"] == "watercooler"


# --- release-ref pinning ----------------------------------------------------- #


def test_codex_release_channel_pins_version_ref(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    mcp = json.loads((plugin_root / ".mcp.json").read_text())
    args = mcp["mcpServers"]["watercooler"]["args"]
    from_arg = args[args.index("--from") + 1]
    assert "@v1.2.3" in from_arg
    assert "@main" not in from_arg


def test_codex_release_channel_rejects_floating_ref_end_to_end(manifest, tmp_path) -> None:
    with pytest.raises(ValueError):
        render_codex_package(manifest, tmp_path, "1.2.3-dev", "release")


def test_codex_dev_channel_may_float(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3-dev", "dev")
    mcp = json.loads((plugin_root / ".mcp.json").read_text())
    args = mcp["mcpServers"]["watercooler"]["args"]
    from_arg = args[args.index("--from") + 1]
    assert "@main" in from_arg


def test_resolve_source_ref_shared_helper_reused(manifest) -> None:
    # Codex adapter must reuse the shared resolve_source_ref, not reimplement it.
    assert resolve_source_ref("release", "1.2.3") == "@v1.2.3"


# --- version lockstep --------------------------------------------------------- #


def test_codex_generated_version_matches_pyproject(manifest, tmp_path) -> None:
    version = load_pyproject_version()
    plugin_root = render_codex_package(manifest, tmp_path, version, "dev")
    data = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text())
    assert data["version"] == version


# --- skills tree shape -------------------------------------------------------- #


def test_codex_package_has_exactly_seven_skills_with_skill_md(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    skill_dirs = sorted(p.name for p in (plugin_root / "skills").iterdir() if p.is_dir())
    assert skill_dirs == sorted(manifest["identity"])
    assert len(skill_dirs) == 7
    for name in skill_dirs:
        assert (plugin_root / "skills" / name / "SKILL.md").is_file()


# --- no leaked private prefix ------------------------------------------------- #


def test_no_private_mcp_prefix_anywhere_in_rendered_codex_package(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    all_text = "".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in plugin_root.rglob("*")
        if f.is_file()
    )
    assert "mcp__watercooler-cloud__" not in all_text


# --- marketplace: no absolute paths ------------------------------------------- #


def test_codex_marketplace_source_path_has_no_absolute_paths(manifest, tmp_path) -> None:
    marketplace_path = render_codex_marketplace(manifest, tmp_path)
    data = json.loads(marketplace_path.read_text())
    source_path = data["plugins"][0]["source"]["path"]
    assert source_path.startswith("./")
    assert not Path(source_path).is_absolute()
    assert ".." not in Path(source_path).parts


def test_codex_marketplace_rejects_absolute_source_path(manifest, tmp_path) -> None:
    with pytest.raises(ValueError, match="repo-relative"):
        render_codex_marketplace(manifest, tmp_path, source_path="/abs/plugins/codex/watercooler")


def test_codex_marketplace_rejects_parent_escape_source_path(manifest, tmp_path) -> None:
    with pytest.raises(ValueError, match="repo-relative"):
        render_codex_marketplace(manifest, tmp_path, source_path="./plugins/../../etc")


def test_codex_marketplace_default_source_path(manifest, tmp_path) -> None:
    marketplace_path = render_codex_marketplace(manifest, tmp_path)
    data = json.loads(marketplace_path.read_text())
    assert data["plugins"][0]["source"]["path"] == "./plugins/codex/watercooler"


# --- re-run safety (clean-slate render) --------------------------------------- #


def _tree_shape(plugin_root: Path) -> list[str]:
    return sorted(str(p.relative_to(plugin_root)) for p in plugin_root.rglob("*"))


def test_codex_rerun_into_same_out_dir_is_clean(manifest, tmp_path) -> None:
    first = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    first_shape = _tree_shape(first)
    second = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    assert _tree_shape(second) == first_shape
    skill_dirs = sorted(p.name for p in (second / "skills").iterdir() if p.is_dir())
    assert skill_dirs == sorted(manifest["identity"])
    for name in skill_dirs:
        assert (second / "skills" / name / "SKILL.md").is_file()
        assert not (second / "skills" / name / name).exists()  # no nesting
    assert not list(second.rglob("_render"))


def test_codex_rerun_removes_stale_skill_from_previous_render(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    stale = plugin_root / "skills" / "stale-skill"
    stale.mkdir()
    (stale / "SKILL.md").write_text("stale\n", encoding="utf-8")
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    assert not (plugin_root / "skills" / "stale-skill").exists()


def test_codex_release_failure_leaves_existing_tree_intact(manifest, tmp_path) -> None:
    plugin_root = render_codex_package(manifest, tmp_path, "1.2.3", "release")
    shape_before = _tree_shape(plugin_root)
    with pytest.raises(ValueError):
        render_codex_package(manifest, tmp_path, "1.2.4-dev", "release")
    assert _tree_shape(plugin_root) == shape_before


def test_codex_marketplace_rerun_overwrites_cleanly(manifest, tmp_path) -> None:
    first = render_codex_marketplace(manifest, tmp_path, source_path="./plugins/codex/watercooler")
    second = render_codex_marketplace(manifest, tmp_path, source_path="./plugins/codex/watercooler2")
    assert first == second  # same output path
    data = json.loads(second.read_text())
    assert data["plugins"][0]["source"]["path"] == "./plugins/codex/watercooler2"
