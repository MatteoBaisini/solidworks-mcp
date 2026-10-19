"""CLI support commands for SolidWorks MCP: doctor and client installer."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
from typing import Any


def check_privileges() -> bool:
    """Returns True if running as elevated administrator on Windows."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_doctor() -> int:
    """Checks SolidWorks COM connectivity, privileges, and default templates."""
    print("=" * 60)
    print(" SolidWorks MCP - System Diagnostics (Doctor)")
    print("=" * 60)

    # 1. Platform
    is_win = sys.platform == "win32"
    status_win = "OK" if is_win else "FAIL"
    print(f"[{status_win}] Operating System: {sys.platform} (Windows required)")
    if not is_win:
        print("     SolidWorks COM API requires Windows.")
        return 1

    # 2. Privileges
    is_admin = check_privileges()
    if is_admin:
        print("[WARN] Process Elevation: Running as Administrator.")
        print("       SolidWorks and this server must share privileges.")
        print("       Running as admin usually prevents connecting to a non-admin SolidWorks.")
    else:
        print("[OK] Process Elevation: Running with standard user privileges.")

    # 3. pywin32
    try:
        import win32com.client
        print("[OK] COM Support: pywin32 library is installed and loadable.")
    except ImportError:
        print("[FAIL] COM Support: pywin32 library is NOT installed.")
        return 1

    # 4. SolidWorks COM Connection
    sw = None
    try:
        sw = win32com.client.GetActiveObject("SldWorks.Application")
        rev = sw.RevisionNumber
        print(f"[OK] SolidWorks Instance: Active session found (Revision {rev}).")
    except Exception:
        print("[WARN] SolidWorks Instance: SolidWorks is not currently running.")
        print("       (Can be auto-launched on demand if auto_launch = true in config).")

    # 5. Templates check (if connected)
    if sw is not None:
        templates = [
            ("Part Template", 0),      # SW_DEFAULT_TEMPLATE_PART
            ("Assembly Template", 1),  # SW_DEFAULT_TEMPLATE_ASSEMBLY
            ("Drawing Template", 2),   # SW_DEFAULT_TEMPLATE_DRAWING
        ]
        for name, pref_id in templates:
            try:
                tmpl_path = sw.GetUserPreferenceStringValue(pref_id)
                if tmpl_path and os.path.isfile(tmpl_path):
                    print(f"[OK] {name}: Configured ({tmpl_path})")
                else:
                    print(f"[WARN] {name}: Not found or not set ({tmpl_path or 'none'})")
                    print("       Set in SolidWorks: Options > System Options > Default Templates.")
            except Exception as e:
                print(f"[WARN] {name}: Check failed ({e})")

    print("=" * 60)
    print("Diagnostics complete.")
    print("=" * 60)
    return 0


CLIENT_CONFIG_PATHS = {
    "claude": os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "Claude",
        "claude_desktop_config.json"
    ),
    "cursor": os.path.join(
        os.path.expanduser("~"),
        ".cursor",
        "mcp.json"
    ),
    "windsurf": os.path.join(
        os.path.expanduser("~"),
        ".codeium",
        "windsurf",
        "mcp_config.json"
    ),
}


def install_client_config(client_name: str, config_path: str, command: str) -> bool:
    """Configures a client JSON file with solidworks-mcp, creating a backup."""
    target_dir = os.path.dirname(config_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    backup_path = None
    existing_data: dict[str, Any] = {}
    if os.path.exists(config_path):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{config_path}.bak.{timestamp}"
        try:
            shutil.copy2(config_path, backup_path)
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    existing_data = json.loads(content)
        except Exception as e:
            print(f"[FAIL] Error reading existing config {config_path}: {e}")
            return False

    servers = existing_data.setdefault("mcpServers", {})
    servers["solidworks"] = {
        "command": command,
        "args": [],
    }

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, indent=2)
            f.write("\n")
    except Exception as e:
        print(f"[FAIL] Error writing configuration to {config_path}: {e}")
        return False

    print(f"[OK] Configured {client_name.capitalize()}: {config_path}")
    if backup_path:
        print(f"     Backup saved to: {backup_path}")
    return True


def run_install(client: str = "claude") -> int:
    """Installs solidworks-mcp into the target client configuration."""
    print("=" * 60)
    print(" SolidWorks MCP - Client Configuration Installer")
    print("=" * 60)

    # Determine command
    cmd = shutil.which("solidworks-mcp")
    if not cmd:
        cmd = "solidworks-mcp"

    targets = list(CLIENT_CONFIG_PATHS.keys()) if client == "all" else [client.lower()]
    success = True
    for target in targets:
        path = CLIENT_CONFIG_PATHS.get(target)
        if not path:
            print(f"[FAIL] Unknown client: '{target}'. Supported: {', '.join(CLIENT_CONFIG_PATHS.keys())}, all.")
            success = False
            continue
        ok = install_client_config(target, path, cmd)
        if not ok:
            success = False

    print("=" * 60)
    return 0 if success else 1
