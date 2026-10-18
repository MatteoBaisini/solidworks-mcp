# solidworks-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant model parts in SolidWorks. The assistant sketches, extrudes, cuts, measures and exports through the SolidWorks COM API, in the SolidWorks window you have open, so you can watch and take over at any point.

- Dimensions are always millimetres and degrees.
- Works with SolidWorks in any language.
- Starts SolidWorks if it is not running.
- Commands run one at a time; if SolidWorks stops answering you get an error instead of a hang.

# M8ven ceritification
[![M8ven Verified](https://m8ven.ai/badge/mcp/matteobaisini-solidworks-mcp-12f1ym?variant=verified&v=ee9fa8badf81fac9ed8ebe64e6ed319c)](https://m8ven.ai/mcp/matteobaisini-solidworks-mcp-12f1ym?s=readme)

## Requirements

- Windows with SolidWorks installed and licensed
- Python 3.11 or newer
- An MCP client (Claude Code, Claude Desktop, Cursor, ...)

Developed and tested on SolidWorks 2026. The API calls used go back many releases, but older versions have not been tested.

## Install

With [uv](https://docs.astral.sh/uv/), nothing needs installing by hand. Add the server to your MCP client configuration:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/MatteoBaisini/solidworks-mcp", "solidworks-mcp"]
    }
  }
}
```

With pip:

```
pip install git+https://github.com/MatteoBaisini/solidworks-mcp
```

and use `"command": "solidworks-mcp"` with no arguments in the client configuration.

From a clone, run `install.bat` (it creates `.venv` and installs the server in it), then use the full path of `.venv\Scripts\solidworks-mcp.exe` as the command.

SolidWorks and the server must run with the same privileges: do not run either one as administrator.

## Tools

| Group | Tools |
|---|---|
| Information | `get_active_document_info`, `get_feature_tree`, `get_bounding_box`, `get_mass_properties`, `get_screenshot` |
| Documents | `new_part`, `save_document`, `export_document` |
| Selection | `select_plane`, `select_face_at_point` |
| Sketch | `start_sketch`, `end_sketch`, `sketch_line`, `sketch_rectangle`, `sketch_circle`, `sketch_arc`, `sketch_polygon` |
| Features | `extrude`, `cut_extrude`, `revolve` |
| Editing | `undo`, `delete_feature`, `zoom_to_fit` |
| Optional | `execute_solidworks_macro` (off by default, see [Security](#security)) |

A typical part is `new_part` → `select_plane` → `start_sketch` → sketch tools → `extrude`, then `select_face_at_point` → `start_sketch` → `cut_extrude` for holes and pockets.

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

`execute_solidworks_macro` lets the assistant run arbitrary Python code on your PC, with your privileges. It exists because the dedicated tools do not cover everything (fillets, chamfers, patterns, assemblies), but it is disabled unless you turn it on:

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

The tests do not need SolidWorks. For an end-to-end check against a running SolidWorks:

```
.venv\Scripts\python examples\smoke_test.py         # read only
.venv\Scripts\python examples\smoke_test.py draw    # creates a plate with a hole
```

## License

[MIT](LICENSE)
