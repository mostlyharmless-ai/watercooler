"""Packaging tests for the canonical JSON schemas.

The schemas live under ``src/watercooler/schemas/`` and must ship inside the
built wheel so that ``schema_validation.load_schema`` resolves them from an
installed package (uvx / pip install), not just a dev checkout.

A plain ``importlib.resources`` test would pass under an editable install even
if ``package-data`` were misconfigured — the files are reachable in the source
tree either way. So the authoritative check builds a real wheel and asserts the
schemas are members of it.
"""

import os
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MEMBERS = (
    "watercooler/schemas/thread_entry.schema.json",
    "watercooler/schemas/watercooler_thread.schema.json",
)


def test_schemas_resolve_via_importlib_resources():
    """Both schemas load through the packaged-resource path."""
    from watercooler.schema_validation import load_schema

    entry = load_schema("thread_entry.schema.json")
    thread = load_schema("watercooler_thread.schema.json")
    assert entry.get("$schema", "").startswith("http")
    assert thread.get("$schema", "").startswith("http")


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """Build a wheel from the repo and return its path.

    Drives the setuptools build backend directly (``build_meta.build_wheel``)
    rather than the ``build`` / ``pip`` CLIs, so the test runs hermetically
    against the always-present build backend with no extra tooling or network.
    """
    from setuptools import build_meta

    outdir = tmp_path_factory.mktemp("wheel")
    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        wheel_name = build_meta.build_wheel(str(outdir))
    finally:
        os.chdir(cwd)
    wheel_path = outdir / wheel_name
    assert wheel_path.is_file(), f"wheel not produced: {wheel_path}"
    return wheel_path


def test_schemas_are_packaged_in_wheel(built_wheel: Path):
    """The schema JSON files are present inside the built wheel."""
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    missing = [m for m in SCHEMA_MEMBERS if m not in names]
    assert not missing, (
        f"schemas missing from wheel {built_wheel.name}: {missing}. "
        f"Check [tool.setuptools.package-data] in pyproject.toml."
    )
