"""End-to-end check: starts the server over stdio, exactly as an MCP client does, and drives SolidWorks.

    python examples/smoke_test.py              -> read only (document info, feature tree, mass)
    python examples/smoke_test.py draw         -> also a 130x130x10 plate with a 50 mm hole
    python examples/smoke_test.py all          -> every group below
    python examples/smoke_test.py part config drawing assembly sheetmetal
                                               -> only the groups named

Groups other than the read-only one save their files in a temporary projects folder, which is
printed at the end, and every document they open is closed again: documents that were already
open before the test are left alone. The exit code is the number of failed calls.

Extra arguments after "--" are passed to the server, e.g.
    python examples/smoke_test.py all -- --config my.toml
(by default the groups use a temporary configuration pointing at the temporary folder).
"""
import asyncio
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GROUPS = ("part", "config", "drawing", "assembly", "sheetmetal")


def open_documents() -> set[str]:
    """Paths/titles of the documents open in SolidWorks, to close only the ones the test opens."""
    try:
        import win32com.client
        sw = win32com.client.GetActiveObject("SldWorks.Application")
    except Exception:
        return set()
    found = set()
    for doc in sw.GetDocuments or ():
        found.add(doc.GetTitle if isinstance(doc.GetTitle, str) else doc.GetTitle())
    return found


def close_documents(keep: set[str]) -> None:
    try:
        import win32com.client
        sw = win32com.client.GetActiveObject("SldWorks.Application")
    except Exception:
        return
    for title in open_documents() - keep:
        sw.CloseDoc(title)


async def run(groups: set[str], server_args: list[str]) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "solidworks_mcp", *server_args])
    failures: list[str] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"{len(tools.tools)} tools:", ", ".join(t.name for t in tools.tools), "\n")

            async def call(tool, **args):
                result = await session.call_tool(tool, args)
                texts = [getattr(item, "text", f"<{item.type}>") for item in result.content]
                status = "FAIL" if result.isError else "ok"
                if result.isError:
                    failures.append(tool)
                print(f"[{status:4}] {tool}: {' '.join(texts)[:300]}")
                return result

            await call("get_active_document_info")

            if "draw" in groups:
                await call("new_part")
                await call("select_plane", plane="top")
                await call("start_sketch")
                await call("sketch_rectangle", width_mm=130, height_mm=130)
                await call("extrude", depth_mm=10)
                await call("select_face_at_point", x_mm=0, y_mm=10, z_mm=0)
                await call("start_sketch")
                await call("sketch_circle", diameter_mm=50)
                await call("cut_extrude", through_all=True)
                await call("get_bounding_box")

            if groups & {"part", "config", "drawing", "assembly"}:
                print("\n--- part: 60x40x10 plate with two M5 holes, saved ---")
                await call("new_part")
                await call("select_plane", plane="top")
                await call("start_sketch")
                await call("sketch_rectangle", width_mm=60, height_mm=40)
                await call("extrude", depth_mm=10)
                await call("hole", centers=[[-20, 10, 0], [20, 10, 0]], size="M5", through_all=True)
                await call("fillet", radius_mm=2, edge_points=[[30, 5, 20]])
                await call("check_model")
                await call("get_mass_properties")
                await call("save_document", path="smoke/cad/smoke_v1_plate.SLDPRT")
                await call("export_document", formats=["step"])

            if "config" in groups:
                print("\n--- configurations ---")
                await call("add_configuration", name="Thin", description="smoke test")
                await call("get_dimensions")
                await call("list_configurations")
                await call("activate_configuration", name="Default")
                await call("list_configurations")

            if "drawing" in groups:
                print("\n--- drawing ---")
                await call("new_drawing")
                await call("insert_model_dimensions")
                await call("save_document", path="smoke/drw/smoke_v1_plate.SLDDRW")
                await call("export_document", formats=["pdf"], directory="../pdf")

            if "assembly" in groups:
                print("\n--- assembly: two plates stacked with a coincident mate ---")
                await call("new_assembly")
                await call("insert_component", path="smoke/cad/smoke_v1_plate.SLDPRT")
                await call("insert_component", path="smoke/cad/smoke_v1_plate.SLDPRT", y_mm=30)
                await call("add_mate", mate_type="coincident", entity1_point=[0, 10, 0], entity2_point=[0, 30, 0])
                await call("check_interference")
                await call("get_mass_properties")
                await call("save_document", path="smoke/cad/smoke_v1_stack.SLDASM")

            if "sheetmetal" in groups:
                print("\n--- sheet metal: L bracket with an edge flange, flat pattern ---")
                await call("new_part")
                await call("select_plane", plane="front")
                await call("start_sketch")
                await call("sketch_line", x1_mm=0, y1_mm=0, x2_mm=50, y2_mm=0)
                await call("sketch_line", x1_mm=50, y1_mm=0, x2_mm=50, y2_mm=30)
                await call("base_flange", thickness_mm=2, depth_mm=40, bend_radius_mm=2)
                await call("edge_flange", length_mm=20, edge_point_mm=[0, 0, 20])
                await call("check_model")
                await call("get_mass_properties")
                await call("save_document", path="smoke/cad/smoke_v1_bracket.SLDPRT")
                await call("export_flat_pattern")

            await call("get_feature_tree")
    return len(failures)


def main() -> int:
    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    own = set(argv[:argv.index("--")] if "--" in argv else argv)
    if "all" in own:
        own |= set(GROUPS)
    folder = None
    if own & set(GROUPS) and not extra:
        folder = tempfile.mkdtemp(prefix="sw_mcp_smoke_")
        config = os.path.join(folder, "solidworks-mcp.toml")
        with open(config, "w", encoding="utf-8") as fh:
            fh.write('[projects]\ndirectory = "projects"\n\n[export]\ndirectory = "../{format}"\n')
        extra = ["--config", config]
    before = open_documents()
    try:
        failed = asyncio.run(run(own, extra))
    finally:
        close_documents(before)
    if folder:
        print(f"\nFiles written to {folder}")
    print(f"{failed} failed call(s).")
    return failed


if __name__ == "__main__":
    sys.exit(main())
