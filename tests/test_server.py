"""Offline checks: the server loads and queues commands correctly. SolidWorks is not needed."""
import asyncio
import os
import threading
import time

import pytest

from solidworks_mcp import server

EXPECTED_TOOLS = {
    "get_active_document_info", "get_mass_properties", "get_feature_tree", "get_bounding_box",
    "get_screenshot", "check_model", "new_part", "save_document", "export_document", "select_plane",
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


HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def test_every_tool_declares_all_four_hints():
    server.register_macro_tool()
    try:
        for tool in asyncio.run(server.mcp.list_tools()):
            assert tool.annotations is not None, tool.name
            for hint in HINTS:
                assert isinstance(getattr(tool.annotations, hint), bool), (tool.name, hint)
            if tool.annotations.readOnlyHint:
                assert not tool.annotations.destructiveHint, tool.name
    finally:
        server.mcp._tool_manager._tools.pop("execute_solidworks_macro", None)


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
        first, queued = await asyncio.gather(
            server.com_tool(hang), server.com_tool(quick), return_exceptions=True
        )
        started = time.monotonic()
        try:
            refused = await server.com_tool(quick)
        except server.SWError as e:
            refused = e
        refused_in = time.monotonic() - started
        release.set()
        for _ in range(50):          # the queue reopens once SolidWorks answers
            await asyncio.sleep(0.05)
            if not server._stuck["name"]:
                break
        return first, queued, refused, refused_in, await server.com_tool(quick)

    first, queued, refused, refused_in, after = asyncio.run(go())
    assert isinstance(first, server.SWError) and "no answer" in str(first)
    assert isinstance(queued, server.SWError) and "still busy" in str(queued)
    assert isinstance(refused, server.SWError) and "still busy" in str(refused)
    assert refused_in < 0.1
    assert after == "ok"


def test_com_tool_raises_sw_error_on_failure():
    def fail():
        raise server.SWError("command failed")

    with pytest.raises(server.SWError, match="command failed"):
        asyncio.run(server.com_tool(fail))


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
    with pytest.raises(server.SWError, match="outside the configured projects directory"):
        server.resolve_save_path(str(existing / "part.step"))


def test_relative_save_path_needs_a_projects_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CONFIG, "projects_directory", "")
    with pytest.raises(server.SWError):
        server.resolve_save_path("bracket/cad/part.SLDPRT")
    existing = tmp_path / "existing"
    existing.mkdir()
    assert server.resolve_save_path(str(existing / "part.step")) == str(existing / "part.step")


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


def test_check_model_reports_healthy_model(monkeypatch):
    class FakeDoc:
        def GetBodies2(self, kind, visible):
            return [object()]
    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    monkeypatch.setattr(server, "rebuild", lambda doc: None)
    monkeypatch.setattr(server, "iter_features", lambda doc: [])
    result = asyncio.run(server.check_model())
    assert "healthy" in result.lower()


def test_check_model_detects_errors_and_multiple_bodies(monkeypatch):
    class FakeFeat:
        def Name(self):
            return "Boss-Extrude1"
        def GetErrorCode2(self, is_warning):
            return 2
    class FakeDoc:
        def GetBodies2(self, kind, visible):
            return [object(), object()]
    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    monkeypatch.setattr(server, "rebuild", lambda doc: None)
    monkeypatch.setattr(server, "iter_features", lambda doc: [FakeFeat()])
    result = asyncio.run(server.check_model())
    assert "ERRORS" in result and "Boss-Extrude1" in result
    assert "MULTIPLE BODIES" in result


def test_structured_output_tools_declare_output_schemas():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("get_bounding_box", "get_mass_properties", "get_dimensions", "measure"):
        assert tools[name].outputSchema is not None, f"{name} missing outputSchema"
        assert tools[name].outputSchema.get("type") == "object", f"{name} schema not object"


def test_get_bounding_box_returns_structured_dict(monkeypatch):
    class FakeBody:
        def GetExtremePoint(self, x, y, z):
            return (None, 0.1 if x > 0 else 0.0, 0.05 if y > 0 else 0.0, 0.02 if z > 0 else 0.0)

    class FakeDoc:
        def GetBodies2(self, kind, visible):
            return [FakeBody()]

    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    res = asyncio.run(server.get_bounding_box())
    assert res["min_mm"] == [0.0, 0.0, 0.0]
    assert res["max_mm"] == [100.0, 50.0, 20.0]
    assert res["size_mm"] == [100.0, 50.0, 20.0]


def test_get_mass_properties_returns_structured_dict(monkeypatch):
    class FakeMP:
        def Mass(self): return 1.5
        def Volume(self): return 0.0005  # m^3
        def SurfaceArea(self): return 0.04  # m^2
        def CenterOfMass(self): return (0.01, 0.02, 0.03)

    class FakeDoc:
        class Extension:
            @staticmethod
            def CreateMassProperty2(): return FakeMP()

    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    res = asyncio.run(server.get_mass_properties())
    assert res["mass_kg"] == 1.5
    assert res["volume_cm3"] == 500.0
    assert res["center_of_mass_mm"] == [10.0, 20.0, 30.0]


def test_get_dimensions_returns_structured_dict(monkeypatch):
    class FakeDim:
        def FullName(self): return "D1@Sketch1@Part1.Part"
        def SystemValue(self): return 0.05  # 50 mm
        def GetType(self): return 0

    class FakeDisp:
        def GetDimension2(self, idx): return FakeDim()

    class FakeFeat:
        def GetFirstDisplayDimension(self): return FakeDisp()
        def GetNextDisplayDimension(self, disp): return None

    class FakeDoc:
        pass

    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    monkeypatch.setattr(server, "find_feature", lambda doc, name: FakeFeat())
    res = asyncio.run(server.get_dimensions("Sketch1"))
    assert res["target"] == "Sketch1"
    assert res["count"] == 1
    assert res["dimensions"][0]["name"] == "D1@Sketch1"
    assert res["dimensions"][0]["value"] == 50.0
    assert res["dimensions"][0]["unit"] == "mm"


def test_input_validation_schema():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    circle_props = tools["sketch_circle"].inputSchema["properties"]
    assert circle_props["diameter_mm"].get("exclusiveMinimum") == 0
    assert "description" in circle_props["diameter_mm"]

    pattern_props = tools["linear_pattern"].inputSchema["properties"]
    assert pattern_props["count"].get("minimum") == 2
    assert pattern_props["spacing_mm"].get("exclusiveMinimum") == 0


def test_resolve_save_path_confinement(tmp_path, monkeypatch):
    proj_dir = str(tmp_path / "projects")
    os.makedirs(proj_dir, exist_ok=True)
    monkeypatch.setattr(server.CONFIG, "projects_directory", proj_dir)

    # Valid relative path
    resolved = server.resolve_save_path("part1.SLDPRT")
    assert resolved == os.path.normpath(os.path.join(proj_dir, "part1.SLDPRT"))

    # Valid subfolder
    resolved_sub = server.resolve_save_path("sub/folder/part2.SLDPRT")
    assert resolved_sub == os.path.normpath(os.path.join(proj_dir, "sub", "folder", "part2.SLDPRT"))
    assert os.path.isdir(os.path.join(proj_dir, "sub", "folder"))

    # Path traversal attempt outside projects_directory
    with pytest.raises(server.SWError, match="outside the configured projects directory"):
        server.resolve_save_path("../outside.SLDPRT")

    # Absolute path outside projects_directory
    outside_abs = str(tmp_path / "outside.SLDPRT")
    with pytest.raises(server.SWError, match="outside the configured projects directory"):
        server.resolve_save_path(outside_abs)


def test_save_document_overwrite_protection(tmp_path, monkeypatch):
    proj_dir = str(tmp_path / "projects")
    os.makedirs(proj_dir, exist_ok=True)
    monkeypatch.setattr(server.CONFIG, "projects_directory", proj_dir)

    existing_file = os.path.join(proj_dir, "existing.SLDPRT")
    with open(existing_file, "w") as f:
        f.write("content")

    class FakeDoc:
        def SaveAs3(self, target, opt, flags):
            return 0

    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())

    # Saving with overwrite=False on existing file must raise SWError
    with pytest.raises(server.SWError, match="already exists. Set overwrite=True"):
        asyncio.run(server.save_document("existing.SLDPRT", overwrite=False))

    # Saving with overwrite=True succeeds
    res = asyncio.run(server.save_document("existing.SLDPRT", overwrite=True))
    assert "Saved to" in res


def test_mcp_prompts_and_resources(monkeypatch):
    prompts = {p.name: p for p in asyncio.run(server.mcp.list_prompts())}
    assert "design_part" in prompts
    prompt_res = asyncio.run(server.mcp.get_prompt("design_part", arguments={"description": "mounting bracket"}))
    assert len(prompt_res.messages) == 1
    assert "mounting bracket" in prompt_res.messages[0].content.text
    assert "extrude" in prompt_res.messages[0].content.text

    resources = {str(r.uri): r for r in asyncio.run(server.mcp.list_resources())}
    assert "solidworks://active/info" in resources
    assert "solidworks://active/feature-tree" in resources
    assert "solidworks://materials" in resources

    # Test reading solidworks://materials
    monkeypatch.setattr(server, "material_library", lambda: [("SolidWorks Materials", "Steel", "AISI 304")])
    mat_content = asyncio.run(server.mcp.read_resource("solidworks://materials"))
    assert any("AISI 304" in str(c) for c in mat_content)

