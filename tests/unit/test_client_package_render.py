"""Unit tests for the shared client-package renderer + Claude plugin adapter.

Covers ``scripts/gen_client_packages.py`` and
``scripts/_client_package_render.py`` (plan 1a + 1b, goal G2): schema
validation of the generated ``plugin.json``/``.mcp.json``, server-name
coupling, release-ref pinning, ZIP shape, and version lockstep with
``pyproject.toml``. Renders to ``tmp_path`` only — no generated package tree
is committed.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# scripts/gen_client_packages.py and scripts/_client_package_render.py are
# private dev tooling (not exported by copy.bara.sky), so this test file is
# excluded from the public build; the importorskip lets it skip cleanly if
# ever collected without those private scripts present.
pytest.importorskip("gen_client_packages")

from gen_client_packages import (  # noqa: E402
    CLAUDE_MARKETPLACE_SOURCE_PATH_DEFAULT,
    build_zip,
    load_manifest,
    render_claude_marketplace,
    render_claude_package,
)
from _client_package_render import (  # noqa: E402
    load_pyproject_version,
    resolve_source_ref,
)

MANIFEST_PATH = REPO_ROOT / "src" / "watercooler" / "open_core_skills.json"

PLUGIN_JSON_SCHEMA = {
    "type": "object",
    "required": ["name", "version", "description"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
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


@pytest.fixture(scope="module")
def manifest() -> dict:
    return load_manifest()


def _write_pyproject(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "watercooler"\nversion = "{version}"\n', encoding="utf-8")
    return path


# --- schema validation ------------------------------------------------------ #


def test_plugin_json_validates_against_schema(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    data = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
    jsonschema.validate(data, PLUGIN_JSON_SCHEMA)


def test_mcp_json_validates_against_schema(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    data = json.loads((plugin_root / ".mcp.json").read_text())
    jsonschema.validate(data, MCP_JSON_SCHEMA)


# --- server-name coupling --------------------------------------------------- #


def test_server_name_matches_skill_mcp_prefix(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
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


# --- release-ref pinning ----------------------------------------------------- #


def test_release_channel_pins_version_ref(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    mcp = json.loads((plugin_root / ".mcp.json").read_text())
    args = mcp["mcpServers"]["watercooler"]["args"]
    from_arg = args[args.index("--from") + 1]
    assert "@v1.2.3" in from_arg
    assert "@main" not in from_arg


def test_release_channel_rejects_dev_version() -> None:
    with pytest.raises(ValueError, match="release channel requires"):
        resolve_source_ref("release", "1.2.3-dev")


def test_release_channel_rejects_floating_ref_end_to_end(manifest, tmp_path) -> None:
    with pytest.raises(ValueError):
        render_claude_package(manifest, tmp_path, "1.2.3-dev", "release")


def test_dev_channel_may_float(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3-dev", "dev")
    mcp = json.loads((plugin_root / ".mcp.json").read_text())
    args = mcp["mcpServers"]["watercooler"]["args"]
    from_arg = args[args.index("--from") + 1]
    assert "@main" in from_arg


# --- ZIP shape --------------------------------------------------------------- #


def test_zip_shape(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    zip_path = tmp_path / "watercooler-claude-plugin.zip"
    build_zip(plugin_root, zip_path, "1.2.3")

    extract_dir = tmp_path / "extracted"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    root = extract_dir / "watercooler"
    assert (root / ".claude-plugin" / "plugin.json").is_file()
    assert (root / ".mcp.json").is_file()
    skill_dirs = sorted(p.name for p in (root / "skills").iterdir() if p.is_dir())
    assert skill_dirs == sorted(manifest["identity"])
    assert len(skill_dirs) == 7
    for name in skill_dirs:
        assert (root / "skills" / name / "SKILL.md").is_file()


def test_zip_contains_checksum_and_version_stamp(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    zip_path = tmp_path / "watercooler-claude-plugin.zip"
    build_zip(plugin_root, zip_path, "1.2.3")

    with zipfile.ZipFile(zip_path) as zf:
        stamp = json.loads(zf.read("watercooler/VERSION.json"))
    assert stamp["version"] == "1.2.3"
    assert len(stamp["sha256"]) == 64


# --- re-run safety (clean-slate render) --------------------------------------- #


def _tree_shape(plugin_root: Path) -> list[str]:
    return sorted(str(p.relative_to(plugin_root)) for p in plugin_root.rglob("*"))


def test_rerun_into_same_out_dir_is_clean(manifest, tmp_path) -> None:
    first = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    first_shape = _tree_shape(first)
    second = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    assert _tree_shape(second) == first_shape
    skill_dirs = sorted(p.name for p in (second / "skills").iterdir() if p.is_dir())
    assert skill_dirs == sorted(manifest["identity"])
    for name in skill_dirs:
        assert (second / "skills" / name / "SKILL.md").is_file()
        assert not (second / "skills" / name / name).exists()  # no nesting
    assert not list(second.rglob("_render"))


def test_rerun_removes_stale_skill_from_previous_render(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    stale = plugin_root / "skills" / "stale-skill"
    stale.mkdir()
    (stale / "SKILL.md").write_text("stale\n", encoding="utf-8")
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    assert not (plugin_root / "skills" / "stale-skill").exists()


def test_release_failure_leaves_existing_tree_intact(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    shape_before = _tree_shape(plugin_root)
    with pytest.raises(ValueError):
        render_claude_package(manifest, tmp_path, "1.2.4-dev", "release")
    assert _tree_shape(plugin_root) == shape_before


# --- version lockstep --------------------------------------------------------- #


def test_generated_version_matches_pyproject(manifest, tmp_path) -> None:
    version = load_pyproject_version()
    plugin_root = render_claude_package(manifest, tmp_path, version, "dev")
    data = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
    assert data["version"] == version


def test_load_pyproject_version_reads_custom_path(tmp_path) -> None:
    custom = _write_pyproject(tmp_path, "9.9.9")
    assert load_pyproject_version(custom) == "9.9.9"


# --- no leaked private prefix anywhere in the package ------------------------- #


def test_no_private_mcp_prefix_anywhere_in_rendered_package(manifest, tmp_path) -> None:
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    zip_path = tmp_path / "watercooler-claude-plugin.zip"
    build_zip(plugin_root, zip_path, "1.2.3")

    all_text = "".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in plugin_root.rglob("*")
        if f.is_file()
    )
    assert "mcp__watercooler-cloud__" not in all_text

    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith((".md", ".json")):
                content = zf.read(name).decode("utf-8", errors="replace")
                assert "mcp__watercooler-cloud__" not in content


# --- Claude self-hosted marketplace manifest (G5, 1f) ------------------------- #

CLAUDE_MARKETPLACE_SCHEMA = {
    "type": "object",
    "required": ["name", "description", "owner", "plugins"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "owner": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string", "minLength": 1}},
        },
        "plugins": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "source", "description"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "source": {"type": "string", "pattern": "^\\./"},
                    "description": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


def test_claude_marketplace_validates_against_schema(manifest, tmp_path) -> None:
    path = render_claude_marketplace(manifest, tmp_path)
    assert path == tmp_path / ".claude-plugin" / "marketplace.json"
    data = json.loads(path.read_text())
    jsonschema.validate(data, CLAUDE_MARKETPLACE_SCHEMA)


def test_claude_marketplace_entry_matches_plugin_and_default_source(manifest, tmp_path) -> None:
    data = json.loads(render_claude_marketplace(manifest, tmp_path).read_text())
    (entry,) = data["plugins"]
    assert entry["name"] == "watercooler"
    assert entry["source"] == CLAUDE_MARKETPLACE_SOURCE_PATH_DEFAULT == "./plugins/claude/watercooler"
    plugin_root = render_claude_package(manifest, tmp_path, "1.2.3", "release")
    plugin_json = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
    assert plugin_json["name"] == data["name"] == entry["name"]


def test_claude_marketplace_rejects_absolute_source_path(manifest, tmp_path) -> None:
    with pytest.raises(ValueError, match="repo-relative"):
        render_claude_marketplace(manifest, tmp_path, source_path="/abs/watercooler")


def test_claude_marketplace_rejects_parent_escape_source_path(manifest, tmp_path) -> None:
    with pytest.raises(ValueError, match="repo-relative"):
        render_claude_marketplace(manifest, tmp_path, source_path="./plugins/../../evil")


def test_claude_marketplace_rejects_backslash_and_bare_root_paths(manifest, tmp_path) -> None:
    for bad in ("./plugins\\..\\..\\evil", "./"):
        with pytest.raises(ValueError, match="repo-relative"):
            render_claude_marketplace(manifest, tmp_path, source_path=bad)


def test_claude_marketplace_rerun_overwrites_cleanly(manifest, tmp_path) -> None:
    first = render_claude_marketplace(manifest, tmp_path).read_text()
    second = render_claude_marketplace(manifest, tmp_path).read_text()
    assert first == second


# Pinned (release) mode — E1 ref-model findings:
# dev_docs/research/2026-07-09-claude-marketplace-ref-model.md

PIN_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def test_claude_marketplace_pinned_emits_git_subdir_source(manifest, tmp_path) -> None:
    path = render_claude_marketplace(
        manifest, tmp_path, pin_sha=PIN_SHA, pin_ref="v1.2.3"
    )
    (entry,) = json.loads(path.read_text())["plugins"]
    source = entry["source"]
    assert source["source"] == "git-subdir"
    assert source["url"] == "https://github.com/mostlyharmless-ai/watercooler.git"
    assert source["path"] == "plugins/claude/watercooler"  # no './' in git-subdir path
    assert source["sha"] == PIN_SHA
    assert source["ref"] == "v1.2.3"


def test_claude_marketplace_pin_sha_alone_is_valid(manifest, tmp_path) -> None:
    path = render_claude_marketplace(manifest, tmp_path, pin_sha=PIN_SHA)
    (entry,) = json.loads(path.read_text())["plugins"]
    assert entry["source"]["sha"] == PIN_SHA
    assert "ref" not in entry["source"]


def test_claude_marketplace_rejects_ref_without_sha(manifest, tmp_path) -> None:
    with pytest.raises(ValueError, match="not a strict pin"):
        render_claude_marketplace(manifest, tmp_path, pin_ref="v1.2.3")


def test_claude_marketplace_rejects_malformed_sha(manifest, tmp_path) -> None:
    for bad in ("abc123", PIN_SHA.upper(), PIN_SHA[:39], f"{PIN_SHA}0"):
        with pytest.raises(ValueError, match="40-hex"):
            render_claude_marketplace(manifest, tmp_path, pin_sha=bad)
