"""Offline checks: the server loads and queues commands correctly. SolidWorks is not needed."""
import asyncio
import threading
import time

import pytest

server = pytest.importorskip("solidworks_mcp.server", reason="needs Windows with pywin32")

EXPECTED_TOOLS = {
    "get_active_document_info", "get_mass_properties", "get_feature_tree", "get_bounding_box",
    "get_screenshot", "new_part", "save_document", "export_document", "select_plane",
    "select_face_at_point", "select_edge_at_point", "start_sketch", "end_sketch", "convert_point",
    "sketch_rectangle", "sketch_circle", "sketch_line", "sketch_polygon", "sketch_arc", "sketch_slot",
    "sketch_ellipse", "sketch_spline", "sketch_fillet", "sketch_offset", "convert_entities",
    "fully_define_sketch", "add_sketch_dimension", "add_sketch_relation", "extrude", "cut_extrude",
    "revolve", "rib", "sweep", "loft", "create_plane", "fillet", "chamfer", "shell", "linear_pattern",
    "circular_pattern", "mirror", "hole", "list_materials", "set_material", "get_dimensions",
    "set_dimension", "suppress_feature", "get_equations", "add_equation", "delete_equation",
    "get_custom_properties", "set_custom_property", "measure", "undo", "delete_feature", "zoom_to_fit",
}


def test_tools_are_registered_and_macro_is_off_by_default():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == EXPECTED_TOOLS
    assert "execute_solidworks_macro" not in names


def test_commands_run_one_at_a_time_in_order():
    running, overlaps, order = [], [], []

    def make(i):
        def work():
            overlaps.append(len(running))
            running.append(i)
            time.sleep(0.05)
            running.remove(i)
            order.append(i)
            return i
        return work

    async def go():
        return await asyncio.gather(*(server.com_tool(make(i)) for i in range(5)))

    assert asyncio.run(go()) == [0, 1, 2, 3, 4]
    assert order == [0, 1, 2, 3, 4]
    assert overlaps == [0, 0, 0, 0, 0]


def test_stuck_command_times_out_and_later_ones_fail_fast(monkeypatch):
    monkeypatch.setattr(server.CONFIG, "command_timeout_s", 0.2)
    monkeypatch.setattr(server.CONFIG, "auto_launch", False)  # no startup allowance
    release = threading.Event()

    def hang():
        release.wait(5)
        return "late"

    def quick():
        return "ok"

    async def go():
        first, queued = await asyncio.gather(server.com_tool(hang), server.com_tool(quick))
        started = time.monotonic()
        refused = await server.com_tool(quick)
        refused_in = time.monotonic() - started
        release.set()
        for _ in range(50):          # the queue reopens once SolidWorks answers
            await asyncio.sleep(0.05)
            if not server._stuck["name"]:
                break
        return first, queued, refused, refused_in, await server.com_tool(quick)

    first, queued, refused, refused_in, after = asyncio.run(go())
    assert first.startswith("ERROR:") and "no answer" in first
    assert queued.startswith("ERROR:") and "still busy" in queued
    assert refused.startswith("ERROR:") and "still busy" in refused
    assert refused_in < 0.1
    assert after == "ok"


def test_save_paths_resolve_under_the_projects_folder(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(server.CONFIG, "projects_directory", str(root))

    target = server.resolve_save_path("bracket/cad/bracket_v1_base.SLDPRT")
    assert target == str(root / "bracket" / "cad" / "bracket_v1_base.SLDPRT")
    assert (root / "bracket" / "cad").is_dir()          # created inside the projects folder

    outside = tmp_path / "other" / "part.SLDPRT"
    with pytest.raises(server.SWError):                  # never created outside it
        server.resolve_save_path(str(outside))
    assert not outside.parent.exists()
    with pytest.raises(server.SWError):                  # escaping with .. is outside too
        server.resolve_save_path("../escaped/part.SLDPRT")
    assert not (tmp_path / "escaped").exists()

    existing = tmp_path / "existing"
    existing.mkdir()
    assert server.resolve_save_path(str(existing / "part.step")) == str(existing / "part.step")


def test_relative_save_path_needs_a_projects_folder(monkeypatch):
    monkeypatch.setattr(server.CONFIG, "projects_directory", "")
    with pytest.raises(server.SWError):
        server.resolve_save_path("bracket/cad/part.SLDPRT")


def test_hole_sizes_come_from_the_metric_table():
    assert server.hole_sizes("M6", "normal", 0, "simple", 0, 0, 0)[0] == 6.6
    assert server.hole_sizes("m6", "tapped", 0, "simple", 0, 0, 0)[0] == 5.0
    assert server.hole_sizes("M6", "close", 0, "counterbore", 0, 0, 0) == (6.4, 11.0, 6.4, 0)
    assert server.hole_sizes("M6", "normal", 0, "countersink", 0, 0, 0)[3] == 13.0
    assert server.hole_sizes("", "normal", 7, "counterbore", 12, 5, 0) == (7, 12, 5, 0)
    assert server.hole_sizes("M6", "normal", 6.2, "simple", 0, 0, 0)[0] == 6.2   # explicit wins


@pytest.mark.parametrize("args", [
    ("M7", "normal", 0, "simple", 0, 0, 0),          # unknown size
    ("", "normal", 0, "simple", 0, 0, 0),            # no size, no diameter
    ("", "normal", 8, "counterbore", 6, 3, 0),       # counterbore smaller than the hole
    ("", "normal", 8, "countersink", 0, 0, 0),       # countersink without a diameter
])
def test_hole_sizes_reject_bad_input(args):
    with pytest.raises(server.SWError):
        server.hole_sizes(*args)


def test_extrusion_end_conditions_are_checked_before_touching_solidworks():
    for args in ((0, "blind", 0), (10, "midplane", 5), (0, "up_to_surface", 0)):
        with pytest.raises(server.SWError):
            server.extrusion_ends(*args, None, doc=None)
    assert server.extrusion_ends(10, "blind", 0, None, doc=None) == (True, 0, 0, 10 * server.MM, 0.0)
    assert server.extrusion_ends(10, "blind", 4, None, doc=None) == (False, 0, 0, 10 * server.MM, 4 * server.MM)


def test_sketch_coordinates_follow_the_sketch_transform(monkeypatch):
    # Top plane as SolidWorks reports it (row vectors: sketch = model * R + T), moved up 10 mm:
    # sketch X = model X, sketch Y = model -Z, sketch normal = model Y.
    rows = [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]]
    monkeypatch.setattr(server, "_sketch_transform", lambda doc: (rows, [0.0, 0.0, -0.01], 1.0))
    assert server.model_to_sketch(None, [5, 10, -20]) == pytest.approx((5, 20, 0))
    assert server.model_to_sketch(None, [5, 13, -20]) == pytest.approx((5, 20, 3))
    assert server.sketch_to_model(None, 5, 20) == pytest.approx([5, 10, -20])
