# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.3.0] - 2026-10-19

### Added
- **Windows installer** (`installer/`): `SolidWorks-MCP-Setup.exe` installs, without administrator rights, uv, the server (pinned to the latest GitHub release) and Claude Code, and creates the `Documents\SolidWorks-MCP` work folder with `.mcp.json`, design instructions (`CLAUDE.md`), pre-approved server tools and a `projects` folder.
- **SolidWorks-MCP shortcut**: opens Claude Code in the work folder (Windows Terminal when available), repairs a broken install and checks for a new release once a day.
- **Release workflow** (`.github/workflows/installer.yml`): builds the installer with NSIS and attaches it to the GitHub release of every `v*` tag.
- `tests/test_installer.py`: checks the installer's settings, configuration template, tool names in `CLAUDE.md` and packaged files.

## [2.2.0] - 2026-10-19

### Added
- **Assembly Modeling (P8)**: 4 new assembly tools (`new_assembly`, `insert_component`, `add_mate`, `check_interference`) supporting coincident, concentric, parallel, perpendicular, tangent, distance, and angle mates.
- **2D Drawings & Annotation (P9)**: `new_drawing` generating standard 3rd-angle orthogonal views + Isometric view, `insert_model_dimensions` importing model dimensions, and support for DXF/DWG exports.
- **Configurations (P10)**: `list_configurations`, `activate_configuration`, and `add_configuration` for multi-variant part and assembly management.
- **Sheet Metal (P11)**: `base_flange`, `edge_flange`, and `export_flat_pattern` (DXF export with bend line controls).
- **Model Health Inspection (P2)**: `check_model` tool to inspect active documents for rebuild errors, multi-body structure, and geometry validity.
- **Prompts & Live Resources (P6)**: FastMCP prompt `design_part` guiding CAD workflow; read-only resources `solidworks://active/info`, `solidworks://active/feature-tree`, and `solidworks://materials`.
- **Read-Only Mode (P7)**: `[safety] read_only = true` configuration option and `SOLIDWORKS_MCP_READ_ONLY` environment variable to restrict the server strictly to inspection tools.
- **CLI Helper Commands (P12)**: `solidworks-mcp doctor` for COM connectivity, privileges, and template diagnostics; `solidworks-mcp install` for automated client configuration (Claude Desktop, Cursor, Windsurf) with automatic backups.
- **Integration Test Suite (P13)**: End-to-end `@pytest.mark.solidworks` integration test verifying 100x50x10 plate with Ø10 hole volume (49,214.6 mm³), gracefully skipped when offline.

### Changed
- **Error Handling (P1)**: Raised `SWError` so FastMCP properly sets `isError: true` on failures instead of returning error text strings as successes.
- **Rebuild Error Detection (P2)**: `finish_feature` interrogates `Feature.GetErrorCode2` immediately after creation and raises actionable errors on rebuild failure.
- **Structured Output (P3)**: `get_bounding_box`, `get_mass_properties`, `get_dimensions`, and `measure` return strongly typed dictionaries (`TypedDict`) for direct machine readability.
- **Input Validation (P4)**: Applied Pydantic `Annotated` + `Field` constraints across all 2D sketching and 3D modeling tools for positive dimensions, valid angles, and minimum side counts.
- **Secure File Saves (P5)**: Confined file operations to `projects_directory` (when configured), prohibited directory traversal (`..`), and added `overwrite: bool = True` requirement to `save_document`.

## [2.1.0] - 2026-10-18

### Added
- Standard literal MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) directly declared across all 57 tools.
- Cross-platform import support with resilient COM stubs, enabling server inspection and offline test execution on Linux and macOS without pywin32.
- Automated regression test `test_every_tool_declares_all_four_hints` to verify tool annotation consistency.
- GitHub Actions CI matrix running test suite and M8ven pre-flight audit across Windows and Ubuntu.
- Explicit documentation of overwrite and side-effect behaviors (`save_document`, `export_document`, `measure`, `set_custom_property`).
- Security docstring and privileges notice for `execute_solidworks_macro`.

### Changed
- `get_screenshot` uses `tempfile.TemporaryDirectory` to eliminate file deletion inside a view workflow.
- Updated M8ven trust score badge to canonical target.
- Corrected documentation typo in certification section and clarified tool hint semantics.

## [2.0.0] - 2026-10-18

### Added
- Comprehensive CAD modeling toolset with 57 tools covering 2D sketching, sketch constraints, 3D features, holes, finishing, patterns, materials, and parameters.
- Optional Python macro escape hatch `execute_solidworks_macro`.
- Dedicated STA COM thread executor with timeout and command queue safeguards.
- Auto-launch and configuration through TOML.
- Project folder management with automatic sequential part versioning.
