# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
