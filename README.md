# solidworks-mcp

<!-- mcp-name: io.github.matteobaisini/solidworks-mcp -->

[![PyPI](https://img.shields.io/pypi/v/solidworks-mcp-server)](https://pypi.org/project/solidworks-mcp-server/)
[![tests](https://github.com/MatteoBaisini/solidworks-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/MatteoBaisini/solidworks-mcp/actions/workflows/tests.yml)
[![M8ven Trust Score](https://m8ven.ai/badge/mcp/matteobaisini/solidworks-mcp)](https://m8ven.ai/mcp/matteobaisini/solidworks-mcp?s=readme)

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant model parts in SolidWorks. The assistant sketches, extrudes, cuts, measures and exports through the SolidWorks COM API, in the SolidWorks window you have open, so you can watch and take over at any point.

- Dimensions are always millimetres and degrees.
- Works with SolidWorks in any language.
- Starts SolidWorks if it is not running.
- Commands run one at a time; if SolidWorks stops answering you get an error instead of a hang.

## Requirements

- Windows with SolidWorks installed and licensed
- Python 3.11 or newer
- An MCP client (Claude Code, Claude Desktop, Cursor, ...)

Developed and tested on SolidWorks 2026. The API calls used go back many releases, but older versions have not been tested.

## Install

### One-click installer (Windows)

Download **[SolidWorks-MCP-Setup.exe](https://github.com/MatteoBaisini/solidworks-mcp/releases/latest/download/SolidWorks-MCP-Setup.exe)** and run it. It needs no administrator rights and sets up everything for [Claude Code](https://code.claude.com):

- [uv](https://docs.astral.sh/uv/) and this server, pinned to the latest release (no Python or Git needed beforehand)
- Claude Code, if it is not installed yet
- a work folder, `Documents\SolidWorks-MCP`, with `.mcp.json`, design instructions for the assistant (`CLAUDE.md`), the server's tools pre-approved, and a `projects` folder for your parts

Then double-click **SolidWorks-MCP** on the desktop: the control panel opens (see below); *Start Claude* opens Claude Code in the work folder, which starts the server. The first time, it asks you to log in (Pro, Max, Team or Enterprise plan) and to trust the folder. The shortcut checks for a new release once a day; *Start menu > SolidWorks-MCP > Repair* fixes a broken install, and uninstalling keeps your work folder.

The installer is not code-signed, so Windows SmartScreen may show *Windows protected your PC*: choose *More info > Run anyway*.

To build it yourself: `bash installer/build.sh` (needs [NSIS](https://nsis.sourceforge.io)). Pushing a `v*` tag builds it on GitHub Actions and attaches it to the release.

### Manual install

The package is on PyPI as **`solidworks-mcp-server`**. With [uv](https://docs.astral.sh/uv/), nothing needs installing by hand. Add the server to your MCP client configuration:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "uvx",
      "args": ["solidworks-mcp-server"]
    }
  }
}
```

With pip:

```
pip install solidworks-mcp-server
```

and use `"command": "solidworks-mcp"` with no arguments in the client configuration (`solidworks-mcp-server` works too).

The latest code from GitHub, before it is released: `"args": ["--from", "git+https://github.com/MatteoBaisini/solidworks-mcp", "solidworks-mcp"]`.

From a clone, run `install.bat` (it creates `.venv` and installs the server in it), then use the full path of `.venv\Scripts\solidworks-mcp.exe` as the command.

SolidWorks and the server must run with the same privileges: do not run either one as administrator.

### CLI Tools (`doctor` & `install`)

- **Diagnostics (`doctor`)**: verify SolidWorks COM connectivity, permissions, and default templates:
  ```bash
  solidworks-mcp doctor
  ```
- **Automated Client Setup (`install`)**: configure Claude Desktop, Cursor, or Windsurf with automatic backups of existing configurations:
  ```bash
  solidworks-mcp install --client claude   # or cursor, windsurf, all
  ```

### Control panel

`solidworks-mcp-panel` (or `solidworks-mcp panel --workspace <folder>`) opens a small window for people who are new to CAD or to Claude Code. In English or Italian, following the Windows language, it:

- starts Claude Code in the work folder, optionally with a first request;
- shows live status: whether SolidWorks is running and which document is active, and whether Claude is waiting, thinking, designing (with the current step in plain words) or asking for a permission. It also warns when SolidWorks stops answering;
- lists every step with its result and duration;
- fills in requests from part templates (plate, L bracket, flange, spacer, box) and offers one-click requests (check, show, export, save a new version, undo);
- has a short guide, the `doctor` checks, and buttons that open the projects folder and the last saved part.

The status comes from [Claude Code hooks](https://code.claude.com/docs/en/hooks) that the panel adds to `<work folder>\.claude\settings.local.json`, keeping your other settings. They append one line per event to `%LOCALAPPDATA%\solidworks-mcp\activity.jsonl`. The panel reads SolidWorks through COM read-only and never starts it. Nothing leaves the PC, and the MCP server itself is unchanged.

## Tools

Every tool declares standard MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), allowing clients to automatically authorize safe read and inspection calls while prompting for changes.

| Group | Tools |
|---|---|
| Information | `get_active_document_info`, `get_feature_tree`, `get_bounding_box`, `get_mass_properties`, `get_screenshot`, `measure`, `check_model` |
| Documents | `new_part`, `open_document`, `list_open_documents`, `activate_document`, `close_document`, `save_document`, `export_document` |
| Assemblies | `new_assembly`, `insert_component`, `add_mate`, `check_interference` |
| Drawings | `new_drawing`, `insert_model_dimensions` |
| Configurations | `list_configurations`, `activate_configuration`, `add_configuration` |
| Selection | `select_plane`, `select_face_at_point`, `select_edge_at_point`, `list_faces`, `list_edges` |
| Sketch | `start_sketch`, `end_sketch`, `sketch_line`, `sketch_rectangle`, `sketch_circle`, `sketch_arc`, `sketch_polygon`, `sketch_slot`, `sketch_ellipse`, `sketch_spline`, `sketch_fillet`, `sketch_offset`, `sketch_point`, `sketch_trim`, `sketch_mirror`, `sketch_linear_pattern`, `sketch_circular_pattern`, `delete_sketch_entity`, `edit_sketch`, `convert_entities`, `convert_point` |
| Sketch constraints | `fully_define_sketch`, `add_sketch_dimension`, `add_sketch_relation` |
| Features | `extrude`, `cut_extrude`, `revolve`, `rib`, `sweep`, `loft`, `create_plane`, `create_axis`, `draft` |
| Finishing | `fillet`, `chamfer`, `shell`, `hole` |
| Sheet Metal | `base_flange`, `edge_flange`, `export_flat_pattern` |
| Patterns | `linear_pattern`, `circular_pattern`, `mirror` |
| Material | `list_materials`, `set_material` |
| Parameters | `get_dimensions`, `set_dimension`, `suppress_feature`, `get_equations`, `add_equation`, `delete_equation`, `get_custom_properties`, `set_custom_property` |
| Editing | `edit_feature`, `rename_feature`, `undo`, `delete_feature`, `zoom_to_fit` |
| Optional | `execute_solidworks_macro` (off by default, see [Security](#security)) |

A typical part is `new_part` → `select_plane` → `start_sketch` → sketch tools → `extrude`, then `select_face_at_point` → `start_sketch` → `cut_extrude` for pockets, `hole` for drilled holes, `fillet` / `chamfer` for the edges and `set_material` for a real mass.

How the assistant points at things:

- Sketch tools take sketch coordinates (x, y). `start_sketch` reports how the sketch axes map onto the model, and `convert_point` converts single points.
- Faces, edges and vertices (fillet, chamfer, shell, hole, measure, ...) are picked by a point lying on them, in model coordinates [x, y, z]. The pick is geometric, so it also finds entities hidden in the current view. `list_faces` and `list_edges` list them with a point on each, their type, size and the feature that made them, so the assistant does not have to guess.
- Features and sketches are referred to by the names in the feature tree, which every tool that creates one returns.
- Extrusions and cuts can go blind, midplane, through all or up to a face, in two directions, with draft, and as thin walls.
- `hole` drills plain, counterbored or countersunk holes, sized by diameter or by metric screw size (ISO 273 clearance, tap drill, ISO 4762 / ISO 10642 counterbores and countersinks). Tapped holes can carry a cosmetic thread (`thread=True`); threads are not modelled as geometry.
- Reference planes can be offset from a plane, at an angle around an axis or an edge, or through three vertices; `create_axis` adds an axis through a cylindrical face, an edge or two planes.
- Existing parts: `open_document` opens one, `edit_sketch` reopens a sketch, `edit_feature` changes an extrusion's depth or end condition, and `save_document(new_version=True)` saves the next version without overwriting the previous one.
- Linear and circular patterns run along the model axes (a hidden reference axis is added the first time) or around an edge.

## Prompts & Resources

In addition to tools, the server exposes standard MCP prompts and live read-only resources:

- **Prompt `design_part`**: Step-by-step guidance and recommended workflow for mechanical design from initial plane selection to feature creation, inspection, and export.
- **Resource `solidworks://active/info`**: Live status, document title, file path, document type, and SolidWorks revision.
- **Resource `solidworks://active/feature-tree`**: Current design feature tree with names and types.
- **Resource `solidworks://materials`**: Configured material databases and categories.

## Configuration

Settings live in a TOML file; every key is optional. Copy [`config.example.toml`](config.example.toml), which documents all of them, and tell the server where it is in one of these ways (first match wins):

1. `--config <path>` on the command line
2. the `SOLIDWORKS_MCP_CONFIG` environment variable
3. `solidworks-mcp.toml` in the server's working directory
4. `%APPDATA%\solidworks-mcp\config.toml`

For example, to keep every part in a projects folder and export STEP and STL next to the models:

```toml
[projects]
directory = "projects"       # relative to this file, or an absolute path

[export]
formats = ["step", "stl"]
directory = "../{format}"
```

With a projects folder set, the assistant saves each part as `<project>/cad/<project>_v<N>_<description>.SLDPRT` inside it, creating the folders and numbering the versions, without being told where to save each time. `export_document` then writes `<project>/step/<part>.step` and `<project>/stl/<part>.stl`.

A clone of this repository comes with a [`projects/`](projects/README.md) folder ready for this; its contents are ignored by git, so your parts are not published.

### Command line

```
solidworks-mcp [--transport stdio|sse|streamable-http] [--port 8000] [--config PATH]
```

The default transport is `stdio`, which is what MCP clients expect when they start the server themselves. `sse` and `streamable-http` serve on `http://127.0.0.1:<port>` (`/sse` or `/mcp`) and are reachable from this PC only.

## Security

`execute_solidworks_macro` lets the assistant run arbitrary Python code on your PC, with your privileges. It exists because the dedicated tools do not cover everything (assemblies, drawings, sheet metal, ...), but it is disabled unless you turn it on:

```toml
[macro]
enabled = true
```

Enable it only if you trust the assistant and everything it reads. The `SOLIDWORKS_MCP_ALLOW_MACRO` environment variable (`1` or `0`) overrides the file.

The assistant works in your live SolidWorks session, on whichever document is active. Save your own work before letting it model, and keep an eye on which document is in front.

## Troubleshooting

- **"SolidWorks is not running (or cannot be reached)"** while it is open: SolidWorks or the server is running as administrator and the other is not.
- **"got no answer from SolidWorks within 90 s"**: a dialog is usually waiting in the SolidWorks window, or SolidWorks is still starting. Answer the dialog and retry; raise `command_timeout_s` if your models are slow to rebuild.
- **"Default part template not set"**: set one in SolidWorks under Options > File Locations / Default Templates.

## Development

```
git clone https://github.com/MatteoBaisini/solidworks-mcp
cd solidworks-mcp
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\pytest
```

The tests do not need SolidWorks; `tests/test_integration.py` also runs against SolidWorks when it is open. For an end-to-end check through the MCP protocol:

```
.venv\Scripts\python examples\smoke_test.py             # read only
.venv\Scripts\python examples\smoke_test.py draw        # creates a plate with a hole
.venv\Scripts\python examples\smoke_test.py all         # part, configurations, drawing, assembly, sheet metal
.venv\Scripts\python examples\smoke_test.py assembly    # or any of: part config drawing assembly sheetmetal
```

The groups save their files in a temporary folder and close every document they open. The exit code is the number of failed calls.

## License

[MIT](LICENSE)
