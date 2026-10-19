#!/usr/bin/env bash
# Builds installer/dist/SolidWorks-MCP-Setup.exe with NSIS (Linux, macOS or Git Bash).
#   Ubuntu/Debian: sudo apt-get install nsis      Windows: winget install NSIS.NSIS
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/solidworks_mcp/__init__.py")"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Cannot read a x.y.z version from src/solidworks_mcp/__init__.py (got '$version')" >&2
  exit 1
fi
mkdir -p "$here/dist"
makensis -V2 -INPUTCHARSET UTF8 -DVERSION="$version" "$here/SolidWorks-MCP.nsi"
echo "Built $here/dist/SolidWorks-MCP-Setup.exe (version $version)"
