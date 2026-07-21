"""`watercooler login` — hosted API-key acquisition (Phase B of the proxy default).

Covers the credential writer (`set_hosted_api_key`) and the CLI orchestration. The
key is read from the environment, stdin, or a hidden prompt — never a CLI argument —
and is written atomically at 0600, preserving other sections.
"""

from __future__ import annotations

import io
import os
import stat

import pytest

from watercooler.cli import main
from watercooler.credentials import set_hosted_api_key


class TestSetHostedApiKey:
    def test_creates_file_with_key_and_secure_perms(self, tmp_path):
        target = tmp_path / ".watercooler" / "credentials.toml"
        path = set_hosted_api_key("wc_abc123", path=target)
        assert path == target and target.exists()
        text = target.read_text(encoding="utf-8")
        assert "[hosted]" in text and 'api_key = "wc_abc123"' in text
        if os.name == "posix":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_updates_existing_and_preserves_other_sections(self, tmp_path):
        target = tmp_path / "credentials.toml"
        target.write_text('[github]\ntoken = "ghp_keep"\n\n[hosted]\napi_key = "wc_old"\n')
        set_hosted_api_key("wc_new", path=target)
        text = target.read_text(encoding="utf-8")
        assert 'api_key = "wc_new"' in text
        assert 'token = "ghp_keep"' in text  # unrelated section survives
        assert "wc_old" not in text

    def test_adds_hosted_section_when_absent(self, tmp_path):
        target = tmp_path / "credentials.toml"
        target.write_text('[github]\ntoken = "ghp_keep"\n')
        set_hosted_api_key("wc_new", path=target)
        text = target.read_text(encoding="utf-8")
        assert 'token = "ghp_keep"' in text and 'api_key = "wc_new"' in text

    def test_preserves_non_ascii_content_utf8(self, tmp_path):
        target = tmp_path / "credentials.toml"
        target.write_text('# clé — café\n[github]\ntoken = "ghp_keep"\n', encoding="utf-8")
        set_hosted_api_key("wc_new", path=target)
        text = target.read_text(encoding="utf-8")
        assert "clé — café" in text and 'api_key = "wc_new"' in text

    def test_no_temp_files_left_behind(self, tmp_path):
        target = tmp_path / ".watercooler" / "credentials.toml"
        set_hosted_api_key("wc_abc123", path=target)
        leftovers = [p.name for p in target.parent.iterdir() if p.name != "credentials.toml"]
        assert leftovers == []


def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows Path.home()
    monkeypatch.delenv("WATERCOOLER_HOSTED_API_KEY", raising=False)


def _cred_path(tmp_path):
    return tmp_path / ".watercooler" / "credentials.toml"


class TestLoginCommand:
    def test_login_with_env_key_writes_credentials(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("WATERCOOLER_HOSTED_API_KEY", "wc_livekey123")
        with pytest.raises(SystemExit) as exc:
            main(["login", "--no-browser"])
        assert exc.value.code == 0
        assert "wc_livekey123" in _cred_path(tmp_path).read_text(encoding="utf-8")
        out = capsys.readouterr().out
        assert "Saved your hosted API key" in out
        assert "not verified here" in out  # no false auth-success claim

    def test_login_reads_from_stdin(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.stdin", io.StringIO("wc_fromstdin9\n"))
        with pytest.raises(SystemExit) as exc:
            main(["login", "--stdin", "--no-browser"])
        assert exc.value.code == 0
        assert "wc_fromstdin9" in _cred_path(tmp_path).read_text(encoding="utf-8")

    def test_login_prompts_hidden_when_no_source(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: "wc_prompted99")
        with pytest.raises(SystemExit) as exc:
            main(["login", "--no-browser"])
        assert exc.value.code == 0
        assert "wc_prompted99" in _cred_path(tmp_path).read_text(encoding="utf-8")

    def test_login_rejects_non_wc_key_without_writing(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("WATERCOOLER_HOSTED_API_KEY", "not-a-key")
        with pytest.raises(SystemExit) as exc:
            main(["login", "--no-browser"])
        assert exc.value.code == 1
        assert not _cred_path(tmp_path).exists()

    def test_login_uses_dashboard_url_override_in_prompt(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: "wc_x0000000")
        with pytest.raises(SystemExit):
            main(["login", "--no-browser", "--dashboard-url", "https://dash.example"])
        assert "https://dash.example/settings" in capsys.readouterr().err

    def test_login_success_advises_restart(self, monkeypatch, tmp_path, capsys):
        # A running MCP chose its transport at startup, so login MUST advise a restart
        # unconditionally — a keyless-started server stays local and never refuses.
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("WATERCOOLER_HOSTED_API_KEY", "wc_livekey123")
        with pytest.raises(SystemExit):
            main(["login", "--no-browser"])
        assert "Restart your MCP server" in capsys.readouterr().out

    def test_stdin_empty_errors_and_never_prompts(self, monkeypatch, tmp_path):
        # --stdin is non-interactive: an empty pipe errors, it must not fall through
        # to the browser/getpass path.
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setattr("sys.stdin", io.StringIO(""))  # EOF / empty pipe

        def _boom(*a, **k):
            raise AssertionError("getpass must not be reached under --stdin")

        monkeypatch.setattr("getpass.getpass", _boom)
        with pytest.raises(SystemExit) as exc:
            main(["login", "--stdin", "--no-browser"])
        assert exc.value.code == 1
        assert not _cred_path(tmp_path).exists()
