"""Consistency checks for the Windows installer in installer/ (no Windows or NSIS needed)."""
import json
import re
from pathlib import Path

from solidworks_mcp.config import load_config

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer"
WORKSPACE = INSTALLER / "workspace"


def test_workspace_settings_approve_the_server_and_its_tools():
    settings = json.loads((WORKSPACE / "claude" / "settings.json").read_text(encoding="utf-8"))
    assert "solidworks" in settings["enabledMcpjsonServers"]
    assert "mcp__solidworks" in settings["permissions"]["allow"]
    # The macro tool runs arbitrary code: it must still ask, even when enabled.
    assert "mcp__solidworks__execute_solidworks_macro" in settings["permissions"]["ask"]


def test_workspace_config_template_is_valid_and_safe():
    config = load_config(WORKSPACE / "solidworks-mcp.toml.in")
    assert Path(config.projects_directory) == (WORKSPACE / "projects").resolve()
    assert config.allow_macro is False
    assert config.export_formats == ["step"]


def test_workspace_instructions_use_existing_tools():
    from solidworks_mcp import server

    import asyncio

    tools = {t.name for t in asyncio.run(server.mcp.list_tools())} | {"execute_solidworks_macro"}
    text = (WORKSPACE / "CLAUDE.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text))
    assert named, "CLAUDE.md should name the tools it relies on"
    assert named <= tools, f"CLAUDE.md names unknown tools: {sorted(named - tools)}"


def test_nsis_script_packages_files_that_exist():
    script = (INSTALLER / "SolidWorks-MCP.nsi").read_text(encoding="utf-8")
    packaged = re.findall(r'^\s*File\s+"([^"]+)"', script, flags=re.MULTILINE)
    assert packaged
    for rel in packaged:
        assert (INSTALLER / rel).is_file(), f"SolidWorks-MCP.nsi packages a missing file: {rel}"
    for rel in re.findall(r'^\s*!insertmacro MUI_PAGE_LICENSE "([^"]+)"', script, flags=re.MULTILINE):
        assert (INSTALLER / rel).is_file()


def test_powershell_scripts_are_ascii():
    # Windows PowerShell 5.1 reads scripts without a byte-order mark as ANSI.
    for script in (INSTALLER / "app").glob("*.ps1"):
        script.read_bytes().decode("ascii")
