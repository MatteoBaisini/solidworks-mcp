"""End-to-end check: starts the server over stdio, exactly as an MCP client does, and drives SolidWorks.

    python examples/smoke_test.py              -> read only (document info, feature tree, mass)
    python examples/smoke_test.py draw         -> also a 130x130x10 plate with a 50 mm hole
    python examples/smoke_test.py all          -> every group below
    python examples/smoke_test.py part config drawing assembly sheetmetal sketch solid files
                                               -> only the groups named

Groups other than the read-only one save their files in a temporary projects folder, which is
printed at the end, and every document they open is closed again: documents that were already
open before the test are left alone. The exit code is the number of failed calls.

Extra arguments after "--" are passed to the server, e.g.
    python examples/smoke_test.py all -- --config my.toml
(by default the groups use a temporary configuration pointing at the temporary folder).
"""
import asyncio
import math
import os
import re
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GROUPS = ("part", "config", "drawing", "assembly", "sheetmetal", "sketch", "solid", "files")


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

            async def call(tool, expect_error=False, **args):
                result = await session.call_tool(tool, args)
                texts = [getattr(item, "text", f"<{item.type}>") for item in result.content]
                failed = bool(result.isError) != expect_error
                status = "FAIL" if failed else ("ok*" if expect_error else "ok")
                if failed:
                    failures.append(tool)
                print(f"[{status:4}] {tool}: {' '.join(texts)[:300]}")
                return result, " ".join(texts)

            def check(ok, what):
                print(f"[{'ok' if ok else 'FAIL':4}] check: {what}")
                if not ok:
                    failures.append(f"check: {what}")

            def quoted(text, n=0):
                names = re.findall(r"'([^']+)'", text)
                return names[n] if len(names) > n else ""

            async def data(tool, **args):
                result, _ = await call(tool, **args)
                return result.structuredContent or {}

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

            if groups & {"sketch", "solid", "files"}:
                print("\n--- sketch: 100x60 plate, holes by linear / mirror / circular pattern, trim, delete ---")
                await call("new_part")
                await call("select_plane", plane="top")
                await call("start_sketch")
                await call("sketch_rectangle", width_mm=100, height_mm=60)
                await call("sketch_point", x_mm=0, y_mm=0)
                await call("sketch_circle", diameter_mm=6, center_x_mm=-35, center_y_mm=0)
                await call("sketch_linear_pattern", entity_points=[[-35, 3]], count_x=2, spacing_x_mm=12)
                await call("sketch_line", x1_mm=0, y1_mm=-10, x2_mm=0, y2_mm=10, construction=True)
                await call("sketch_mirror", entity_points=[[-35, 3], [-23, 3]], mirror_line_point=[0, 5])
                await call("sketch_circle", diameter_mm=4, center_x_mm=15, center_y_mm=-20)
                await call("sketch_circular_pattern", entity_points=[[15, -18]], count=4)
                await call("sketch_line", x1_mm=40, y1_mm=-40, x2_mm=40, y2_mm=40)
                await call("sketch_trim", x_mm=40, y_mm=35)
                await call("sketch_trim", x_mm=40, y_mm=-35)
                await call("delete_sketch_entity", points=[[40, 0]])
                _, text = await call("extrude", depth_mm=10)
                boss, sketch = quoted(text, 0), quoted(text, 1)
                await call("check_model")
                holes = math.pi * (4 * 3 ** 2 + 4 * 2 ** 2)
                v = (await data("get_mass_properties")).get("volume_mm3", 0)
                check(abs(v - (100 * 60 - holes) * 10) < 1, f"volume {v:.1f} = {(100 * 60 - holes) * 10:.1f} mm3 (8 holes)")

                print("\n--- selection: list_faces / list_edges ---")
                cyl = await data("list_faces", face_type="cylinder")
                check(cyl.get("total") == 8 and all(f.get("concave") for f in cyl.get("faces", [])),
                      f"8 concave cylinders (got {cyl.get('total')})")
                planes = await data("list_faces", face_type="plane")
                top = [f for f in planes.get("faces", []) if f.get("normal") and f["normal"][1] > 0.99]
                check(len(top) == 1, "one planar face pointing +Y")
                edges = await data("list_edges", edge_type="line")
                vert = [e for e in edges.get("edges", []) if abs(e.get("length_mm", 0) - 10) < 1e-6]
                check(len(vert) == 4, f"4 vertical edges 10 mm long (got {len(vert)})")
                if top:
                    x, y, z = top[0]["point_mm"]
                    await call("select_face_at_point", x_mm=x, y_mm=y, z_mm=z)
                if vert:
                    await call("fillet", radius_mm=3, edge_points=[vert[0]["point_mm"]])
                    await call("undo")

                if groups & {"solid", "files"}:
                    print("\n--- solid features: rename, edit, edit sketch, axis, planes, threaded hole, draft ---")
                    await call("rename_feature", name=boss, new_name="Base plate")
                    await call("rename_feature", name=sketch, new_name="Base plate", expect_error=True)
                    await call("edit_feature", name="Base plate", depth_mm=15)
                    size = (await data("get_bounding_box")).get("size_mm", [])
                    check([round(c, 3) for c in size] == [100, 15, 60], f"size 100 x 15 x 60 ({size})")
                    await call("edit_sketch", name=sketch)
                    await call("sketch_circle", diameter_mm=8, center_x_mm=0, center_y_mm=-20)
                    await call("end_sketch")
                    await call("check_model")
                    v = (await data("get_mass_properties")).get("volume_mm3", 0)
                    expected = (100 * 60 - holes - math.pi * 16) * 15
                    check(abs(v - expected) < 1, f"volume after sketch edit {v:.1f} = {expected:.1f} mm3")
                    await call("create_axis", planes=["front", "right"], name="Vertical axis")
                    await call("create_plane", reference="front", angle_deg=30, axis="Vertical axis")
                    await call("create_plane", through_points=[[-50, 15, -30], [50, 15, -30], [50, 15, 30]])
                    await call("hole", centers=[[0, 15, 0]], size="M5", fit="tapped", depth_mm=10, thread=True)
                    await call("draft", angle_deg=3, neutral_face_point=[0, 0, 0],
                               face_points=[[50, 7, 0], [-50, 7, 0]])
                    await call("check_model")
                    await call("get_feature_tree")

                if "files" in groups:
                    print("\n--- files: save, new version, list, close, open, activate ---")
                    await call("save_document", path="smoke/cad/smoke_v1_tools.SLDPRT")
                    await call("save_document", new_version=True)
                    docs = await data("list_open_documents")
                    titles = [d.get("title", "") for d in docs.get("documents", [])]
                    check(any("smoke_v2_tools" in t for t in titles), f"v2 is open ({titles})")
                    await call("close_document")
                    await call("open_document", path="smoke/cad/smoke_v1_tools.SLDPRT", read_only=True)
                    await call("open_document", path="smoke/cad/smoke_v2_tools.SLDPRT")
                    await call("activate_document", name="smoke_v1_tools.SLDPRT")
                    await call("get_active_document_info")
                    await call("close_document", name="smoke_v1_tools.SLDPRT")
                    await call("activate_document", name="smoke_v2_tools")
                    await call("rename_feature", name="Base plate", new_name="Plate")
                    await call("close_document", expect_error=True)   # unsaved changes: refused
                    await call("close_document", discard_changes=True)
                    await call("activate_document", name="smoke_v2_tools", expect_error=True)

            if not groups & {"files"}:
                await call("get_feature_tree")
    if failures:
        print("Failed:", ", ".join(failures))
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
