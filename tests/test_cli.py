"""Tests for the solidworks-mcp CLI (doctor and install)."""
import json
import os
import pytest

import sys

from solidworks_mcp import cli, server


def test_doctor_runs_and_reports(capsys):
    ret = cli.run_doctor()
    captured = capsys.readouterr().out
    assert "System Diagnostics (Doctor)" in captured
    assert "Operating System" in captured
    if sys.platform == "win32":
        assert "Process Elevation" in captured
        assert ret in (0, 1)
    else:
        assert "Windows required" in captured
        assert ret == 1


def test_doctor_windows_simulation(capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "check_privileges", lambda: False)
    ret = cli.run_doctor()
    captured = capsys.readouterr().out
    assert "Process Elevation" in captured
    assert ret in (0, 1)


def test_install_client_config(tmp_path, capsys):
    config_file = str(tmp_path / "claude_desktop_config.json")

    # Initial install into empty / non-existent file
    ok = cli.install_client_config("claude", config_file, "solidworks-mcp")
    assert ok is True
    assert os.path.exists(config_file)

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "mcpServers" in data
    assert data["mcpServers"]["solidworks"]["command"] == "solidworks-mcp"

    # Second install triggers backup
    ok2 = cli.install_client_config("claude", config_file, "solidworks-mcp-v2")
    assert ok2 is True

    backups = [f for f in os.listdir(tmp_path) if f.startswith("claude_desktop_config.json.bak.")]
    assert len(backups) == 1

    with open(config_file, "r", encoding="utf-8") as f:
        data2 = json.load(f)
    assert data2["mcpServers"]["solidworks"]["command"] == "solidworks-mcp-v2"


def test_run_install_all(monkeypatch, tmp_path):
    mock_paths = {
        "claude": str(tmp_path / "claude.json"),
        "cursor": str(tmp_path / "cursor.json"),
        "windsurf": str(tmp_path / "windsurf.json"),
    }
    monkeypatch.setattr(cli, "CLIENT_CONFIG_PATHS", mock_paths)

    ret = cli.run_install("all")
    assert ret == 0
    for path in mock_paths.values():
        assert os.path.exists(path)


def test_main_cli_dispatch(monkeypatch):
    called = []

    def mock_doctor():
        called.append("doctor")
        return 0

    def mock_install(client="claude"):
        called.append(f"install:{client}")
        return 0

    monkeypatch.setattr(cli, "run_doctor", mock_doctor)
    monkeypatch.setattr(cli, "run_install", mock_install)

    with pytest.raises(SystemExit) as exc_info:
        server.main(["doctor"])
    assert exc_info.value.code == 0
    assert "doctor" in called

    with pytest.raises(SystemExit) as exc_info:
        server.main(["install", "--client", "cursor"])
    assert exc_info.value.code == 0
    assert "install:cursor" in called
