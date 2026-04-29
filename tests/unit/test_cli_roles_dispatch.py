"""CLI dispatch tests for ``watercooler roles`` (no subcommand)."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from watercooler import cli


def test_roles_no_subcommand_prints_help_and_exits_one(capsys):
    """``watercooler roles`` (bare) → prints subparser help, exits 1.

    Locks in the user-facing affordance: bare ``roles`` should not raise,
    should not silently no-op, and should exit non-zero so wrappers can react.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["roles"])

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    # argparse prints the usage line to stdout on print_help()
    assert "roles" in (captured.out + captured.err)


def test_roles_dispatch_handles_missing_dest_attribute(capsys):
    """If argparse omits the ``roles_cmd`` dest attribute, dispatch must not raise.

    Regression guard for issue #684: on some argparse configurations a
    subparsers group with ``dest="roles_cmd"`` doesn't set the attribute on
    the Namespace when no subcommand is provided. The dispatch code uses
    ``getattr(args, "roles_cmd", None)`` to handle this safely.
    """
    fake_ns = argparse.Namespace(cmd="roles")
    # Confirm the regression precondition: bare attribute access raises.
    with pytest.raises(AttributeError):
        _ = fake_ns.roles_cmd

    with patch.object(argparse.ArgumentParser, "parse_args", return_value=fake_ns):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["roles"])

    assert excinfo.value.code == 1
