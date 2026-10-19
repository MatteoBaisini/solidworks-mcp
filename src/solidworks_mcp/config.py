"""Settings for the SolidWorks MCP server, read from an optional TOML file.

Lookup order (first match wins):
  1. the path passed with --config
  2. the SOLIDWORKS_MCP_CONFIG environment variable
  3. solidworks-mcp.toml in the current directory
  4. %APPDATA%\\solidworks-mcp\\config.toml
With no file at all the defaults below apply.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_CONFIG = "SOLIDWORKS_MCP_CONFIG"
ENV_ALLOW_MACRO = "SOLIDWORKS_MCP_ALLOW_MACRO"
ENV_READ_ONLY = "SOLIDWORKS_MCP_READ_ONLY"
LOCAL_NAME = "solidworks-mcp.toml"

EXPORT_FORMATS = ("step", "stp", "iges", "igs", "x_t", "stl", "3mf", "pdf", "png")


class ConfigError(ValueError):
    """The configuration file is missing or invalid."""


@dataclass
class Config:
    auto_launch: bool = True          # start SolidWorks when it is not running
    executable: str = ""              # SLDWORKS.exe path; empty = find it in the registry
    startup_timeout_s: float = 180.0  # how long to wait for SolidWorks to start
    command_timeout_s: float = 90.0   # how long a single command may take
    export_formats: list[str] = field(default_factory=lambda: ["step"])
    export_directory: str = "."       # relative to the document folder; {format} is replaced
    projects_directory: str = ""      # where the assistant saves parts; empty = not set
    allow_macro: bool = False         # expose execute_solidworks_macro (runs arbitrary code)
    read_only: bool = False           # expose only READ tools (inspection/review)
    source: str = "(defaults)"


# TOML [section] key -> (Config attribute, expected type)
_SCHEMA: dict[str, dict[str, tuple[str, type | tuple[type, ...]]]] = {
    "solidworks": {
        "auto_launch": ("auto_launch", bool),
        "executable": ("executable", str),
        "startup_timeout_s": ("startup_timeout_s", (int, float)),
        "command_timeout_s": ("command_timeout_s", (int, float)),
    },
    "export": {
        "formats": ("export_formats", list),
        "directory": ("export_directory", str),
    },
    "projects": {
        "directory": ("projects_directory", str),
    },
    "macro": {
        "enabled": ("allow_macro", bool),
    },
    "safety": {
        "read_only": ("read_only", bool),
    },
}


def normalize_format(fmt: str) -> str:
    """'STEP', '.step' -> 'step'; raises ConfigError for formats the server does not export."""
    ext = str(fmt).strip().lower().lstrip(".")
    if ext not in EXPORT_FORMATS:
        raise ConfigError(f"Unsupported export format '{fmt}'. Supported: {', '.join(EXPORT_FORMATS)}.")
    return ext


def _find_file(explicit: str | os.PathLike[str] | None) -> Path | None:
    for candidate in (explicit, os.environ.get(ENV_CONFIG)):
        if candidate:
            path = Path(candidate).expanduser()
            if not path.is_file():
                raise ConfigError(f"Configuration file not found: {path}")
            return path
    local = Path.cwd() / LOCAL_NAME
    if local.is_file():
        return local
    appdata = os.environ.get("APPDATA")
    if appdata:
        user = Path(appdata) / "solidworks-mcp" / "config.toml"
        if user.is_file():
            return user
    return None


def _apply(cfg: Config, data: dict[str, Any], origin: Path) -> None:
    for section, values in data.items():
        if section not in _SCHEMA:
            raise ConfigError(f"{origin}: unknown section [{section}].")
        if not isinstance(values, dict):
            raise ConfigError(f"{origin}: [{section}] must be a table.")
        for key, value in values.items():
            if key not in _SCHEMA[section]:
                raise ConfigError(f"{origin}: unknown key '{key}' in [{section}].")
            attr, expected = _SCHEMA[section][key]
            if isinstance(value, bool) and expected is not bool or not isinstance(value, expected):
                raise ConfigError(f"{origin}: [{section}] {key} has the wrong type.")
            setattr(cfg, attr, value)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg = Config()
    file = _find_file(path)
    if file is not None:
        try:
            with open(file, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{file}: invalid TOML: {e}") from e
        _apply(cfg, data, file)
        cfg.source = str(file)
        if cfg.projects_directory:  # a relative folder is relative to the configuration file
            cfg.projects_directory = str((file.parent / Path(cfg.projects_directory).expanduser()).resolve())

    cfg.export_formats = [normalize_format(f) for f in cfg.export_formats]
    if cfg.startup_timeout_s <= 0 or cfg.command_timeout_s <= 0:
        raise ConfigError("Timeouts must be positive.")

    env_macro = os.environ.get(ENV_ALLOW_MACRO)
    if env_macro is not None:
        cfg.allow_macro = env_macro.strip().lower() in ("1", "true", "yes", "on")

    env_ro = os.environ.get(ENV_READ_ONLY)
    if env_ro is not None:
        cfg.read_only = env_ro.strip().lower() in ("1", "true", "yes", "on")
    return cfg
