"""Offline checks: the server loads and queues commands correctly. SolidWorks is not needed."""
import asyncio
import math
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
    "new_assembly", "insert_component", "add_mate", "check_interference",
    "new_drawing", "insert_model_dimensions",
    "list_configurations", "activate_configuration", "add_configuration",
    "base_flange", "edge_flange", "export_flat_pattern",
    "open_document", "list_open_documents", "activate_document", "close_document",
    "list_faces", "list_edges",
    "sketch_point", "sketch_trim", "sketch_mirror", "sketch_linear_pattern", "sketch_circular_pattern",
    "delete_sketch_entity", "edit_sketch",
    "edit_feature", "rename_feature", "draft", "create_axis",
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


class FakeFaults:
    """IFaultEntity: Body2.Check3 returns one even for a healthy body, with Count = 0."""
    def __init__(self, count):
        self.Count = count


class FakeBody:
    def __init__(self, name="Body1", faults=0):
        self.Name = name
        self.Check3 = FakeFaults(faults)


def test_check_model_reports_healthy_model(monkeypatch):
    class FakeDoc:
        def GetBodies2(self, kind, visible):
            return [FakeBody()]
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
            return [FakeBody("Body1"), FakeBody("Body2", faults=2)]
    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    monkeypatch.setattr(server, "rebuild", lambda doc: None)
    monkeypatch.setattr(server, "iter_features", lambda doc: [FakeFeat()])
    result = asyncio.run(server.check_model())
    assert "ERRORS" in result and "Boss-Extrude1" in result
    assert "MULTIPLE BODIES" in result
    assert "GEOMETRY FAULTS" in result and "Body2 (2 fault(s))" in result and "Body1 (" not in result


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


def test_apply_read_only_mode():
    orig_tools = dict(server.mcp._tool_manager._tools)
    try:
        removed = server.apply_read_only_mode()
        assert len(removed) > 0
        assert "extrude" in removed
        assert "new_part" in removed
        assert "save_document" in removed

        current_tools = asyncio.run(server.mcp.list_tools())
        assert len(current_tools) == 16
        for t in current_tools:
            assert t.annotations.readOnlyHint is True
            assert t.annotations.destructiveHint is False
    finally:
        server.mcp._tool_manager._tools = orig_tools


def test_new_assembly(monkeypatch):
    class FakeDoc:
        def GetTitle(self): return "Assem1"

    class FakeSW:
        def GetUserPreferenceStringValue(self, pref): return "C:/templates/assembly.asmdot"
        def NewDocument(self, tmpl, *args): return FakeDoc()

    monkeypatch.setattr(server, "get_sw", lambda: FakeSW())
    monkeypatch.setattr(server.os.path, "isfile", lambda p: True)
    res = asyncio.run(server.new_assembly())
    assert "New assembly created: Assem1" in res


def test_insert_component(tmp_path, monkeypatch):
    part_file = tmp_path / "part1.SLDPRT"
    part_file.write_text("fake")

    class FakeTransform:
        ArrayData = None

    class FakeComp:
        Name2 = "part1-1"
        Transform2 = FakeTransform()

    class FakeDoc:
        def GetTitle(self): return "Assem1"
        def EditRebuild3(self): return True
        def AddComponent5(self, path, opt, cfg, silent, ds, x, y, z):
            assert (x, y, z) == (0.010, 0.020, 0.030)
            return FakeComp()

    opened = []

    class FakeSW:
        def GetOpenDocumentByName(self, path): return None   # not loaded yet: must be opened first
        def OpenDoc6(self, path, doc_type, options, cfg, errors, warnings):
            assert doc_type == server.SW_DOC_PART and options == server.SW_OPEN_SILENT
            opened.append(path)
            return FakeDoc()
        def ActivateDoc3(self, title, silent, option, errors): assert title == "Assem1"
        def CloseDoc(self, title): opened.pop()

    monkeypatch.setattr(server, "get_sw", lambda: FakeSW())
    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    res = asyncio.run(server.insert_component(str(part_file), 10.0, 20.0, 30.0))
    assert "Component 'part1-1' inserted with its origin at (10, 20, 30) mm" in res
    assert opened == []   # the part window opened for the insertion was closed again
    assert FakeComp.Transform2.ArrayData is not None   # the origin was moved to the requested point


def test_add_mate(monkeypatch):
    class FakeMateFeat:
        def Name(self): return "Coincident1"
        def GetErrorCode2(self, is_warn): return 0

    class FakeExtension:
        def SelectByID2(self, name, kind, x, y, z, append, mark, nothing, opt):
            return True

    class FakeDoc:
        def __init__(self):
            self.Extension = FakeExtension()
        def ClearSelection2(self, all): return True
        def EditRebuild3(self): return True
        def AddMate5(self, *args):
            args[-1].value = server.SW_MATE_NO_ERROR   # 1 = swAddMateError_NoError, not 0
            return FakeMateFeat()

    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    res = asyncio.run(server.add_mate("coincident", entity1_name="Face1@Part1-1", entity2_name="Face1@Part2-1"))
    assert "Mate 'Coincident1' (coincident) added" in res


def test_check_interference(monkeypatch):
    class FakeInterference:
        Volume = 0.000005  # 5000 mm³
        class FakeComp:
            def Name(self): return "Part1-1"
        Components = [FakeComp()]

    class FakeMgr:
        TreatCoincidenceAsInterference = False
        TreatSubAssembliesAsComponents = False
        IncludeMultibodyPartInterferences = True
        def GetInterferences(self): return [FakeInterference()]
        def GetInterferenceCount(self): return 1
        def Done(self): pass

    class FakeDoc:
        InterferenceDetectionManager = FakeMgr()
        def ToolsCheckInterference(self): pass

    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDoc())
    res = asyncio.run(server.check_interference())
    assert "Found 1 interference(s)" in res
    assert "5000.00 mm³" in res


def test_new_drawing(monkeypatch):
    class FakePartDoc:
        def GetType(self): return server.SW_DOC_PART
        def GetPathName(self): return "C:/projects/part1.SLDPRT"
        def GetModelViewNames(self): return ("*Anteriore", "*Isometrico", "*Trimetrico")

    class FakeDrawDoc:
        def GetTitle(self): return "Drawing1"
        def Create3rdAngleViews2(self, path): return True
        def CreateDrawViewFromModelView3(self, path, name, x, y, z):
            assert name == "*Isometrico"   # the localised name reported by the model
            return object()

    class FakeSW:
        ActiveDoc = FakePartDoc()
        def GetUserPreferenceStringValue(self, pref): return "C:/templates/drawing.drwdot"
        def NewDocument(self, tmpl, *args): return FakeDrawDoc()

    monkeypatch.setattr(server, "get_sw", lambda: FakeSW())
    monkeypatch.setattr(server.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(server, "rebuild", lambda doc: None)
    res = asyncio.run(server.new_drawing())
    assert "New drawing 'Drawing1' created from 'part1.SLDPRT' with front, top, right and isometric views." in res


def test_insert_model_dimensions(monkeypatch):
    class FakeDrawDoc:
        def InsertModelAnnotations3(self, opt, types, all_views, dup, hidden, sketch_pos):
            return ["dim1", "dim2", "dim3"]

    monkeypatch.setattr(server, "active_doc", lambda *args: FakeDrawDoc())
    monkeypatch.setattr(server, "rebuild", lambda doc: None)
    res = asyncio.run(server.insert_model_dimensions())
    assert "Inserted 3 model dimension(s)" in res


def test_configurations(monkeypatch):
    class FakeConfig:
        def __init__(self, name):
            self.Name = name

    class FakeConfigManager:
        def __init__(self, parent):
            self.parent = parent
        def AddConfiguration2(self, name, comment, alt, opt, parent, desc, use_desc):
            self.parent.configs.append(name)
            return FakeConfig(name)

    class FakeDoc:
        def __init__(self):
            self.configs = ["Default", "LightWeight"]
            self.active = "Default"
            self.ConfigurationManager = FakeConfigManager(self)
        def GetType(self): return server.SW_DOC_PART
        def GetTitle(self): return "Part1"
        def GetConfigurationNames(self): return list(self.configs)
        def GetActiveConfiguration(self): return FakeConfig(self.active)
        def ShowConfiguration2(self, name):
            if name in self.configs:
                self.active = name
                return True
            return False

    fake_doc = FakeDoc()
    monkeypatch.setattr(server, "active_doc", lambda *args: fake_doc)
    monkeypatch.setattr(server, "rebuild", lambda doc: None)

    # 1. list_configurations
    res = asyncio.run(server.list_configurations())
    assert "Configurations for 'Part1' (active: 'Default'):" in res
    assert "- Default [ACTIVE]" in res
    assert "- LightWeight" in res

    # 2. activate_configuration
    res = asyncio.run(server.activate_configuration("LightWeight"))
    assert "Activated configuration 'LightWeight'." in res
    assert fake_doc.active == "LightWeight"

    # Non-existent config
    with pytest.raises(server.SWError, match="not found"):
        asyncio.run(server.activate_configuration("NonExistent"))

    # 3. add_configuration
    res = asyncio.run(server.add_configuration("HeavyDuty", description="Heavy duty model"))
    assert "Configuration 'HeavyDuty' created and activated." in res
    assert "HeavyDuty" in fake_doc.configs
    assert fake_doc.active == "HeavyDuty"

    # Duplicate config
    with pytest.raises(server.SWError, match="already exists"):
        asyncio.run(server.add_configuration("HeavyDuty"))


def test_sheet_metal(monkeypatch, tmp_path):
    class FakeFeature:
        def __init__(self, name): self._name = name
        def Name(self): return self._name
        def GetErrorCode2(self, *args): return 0

    calls = {}

    class FakeFeatureManager:
        def InsertSheetMetalBaseFlange2(self, *args):
            calls["base"] = args
            assert fake_doc.SketchManager.ActiveSketch is not None, "the sketch must be open"
            return FakeFeature("BaseFlange1")
        def InsertSheetMetalEdgeFlange2(self, edges, sketches, *args):
            calls["edge"] = (edges, sketches) + args
            return FakeFeature("EdgeFlange1")

    class FakeVertex:
        def __init__(self, p): self._p = p
        def GetPoint(self): return self._p

    class FakeEdge:
        def GetStartVertex(self): return FakeVertex((0.0, 0.0, 0.0))
        def GetEndVertex(self): return FakeVertex((0.0, 0.0, 0.04))

    class FakeSketch:
        def __init__(self, name): self.Name = name
        def Select2(self, append, mark): return True

    class FakeSketchManager:
        def __init__(self): self.ActiveSketch = None; self.AddToDB = False; self.rect = None
        def CreateCornerRectangle(self, *args): self.rect = args; return [1, 2, 3, 4]
        def InsertSketch(self, update): self.ActiveSketch = None

    class FakeSelMgr:
        def GetSelectedObjectCount2(self, mark): return 1
        def GetSelectedObject6(self, idx, mark): return FakeEdge()

    class FakePartDoc:
        def __init__(self):
            self.FeatureManager = FakeFeatureManager()
            self.SelectionManager = FakeSelMgr()
            self.SketchManager = FakeSketchManager()
            self._path = str(tmp_path / "sheet_part.SLDPRT")
        def GetType(self): return server.SW_DOC_PART
        def GetPathName(self): return self._path
        def GetTitle(self): return "sheet_part"
        def ClearSelection2(self, all_obj): pass
        def FirstFeature(self): return None
        def EditSketch(self): self.SketchManager.ActiveSketch = self._edit
        def InsertSketchForEdgeFlange(self, edge, angle, rev):
            self._edit = FakeSketch("FlangeSketch")
            return self._edit
        def ExportFlatPatternView(self, filepath, options):
            with open(filepath, "w") as f:
                f.write("FAKE DXF CONTENT")
            return True

    fake_doc = FakePartDoc()
    fake_doc._edit = FakeSketch("Sketch1")   # the last sketch, reopened by base_flange
    monkeypatch.setattr(server, "active_doc", lambda *args: fake_doc)
    monkeypatch.setattr(server, "last_sketch_feature", lambda doc: fake_doc._edit)
    monkeypatch.setattr(server, "model_to_sketch", lambda doc, p: (p[2], p[1], 0.0))
    monkeypatch.setattr(server, "leave_sketch", lambda doc: None)
    monkeypatch.setattr(server, "rebuild", lambda doc: None)

    # 1. base_flange reopens the last sketch and passes a COM 'Nothing' for the bend allowance
    res = asyncio.run(server.base_flange(thickness_mm=2.0, depth_mm=50.0, bend_radius_mm=1.5))
    assert "Sheet metal base flange 'BaseFlange1' created from 'Sketch1'" in res
    assert calls["base"][:4] == (0.002, False, 0.0015, 0.05) and calls["base"][9] is server.NOTHING

    # 2. edge_flange draws the profile (edge length x flange length) and passes VARIANT arrays
    res = asyncio.run(server.edge_flange(length_mm=25.0, angle_deg=90.0))
    assert "Edge flange 'EdgeFlange1' created" in res
    assert fake_doc.SketchManager.rect == (0.0, 0.0, 0, 0.04, 0.025, 0)
    edges, sketches, options = calls["edge"][:3]
    assert isinstance(edges, server.VARIANT) and isinstance(sketches, server.VARIANT)
    assert options == 1 and calls["edge"][-1] is server.NOTHING

    # 3. export_flat_pattern
    out_dxf = str(tmp_path / "custom_flat.dxf")
    res = asyncio.run(server.export_flat_pattern(path=out_dxf))
    assert f"Exported flat pattern to {os.path.normpath(out_dxf)}" in res
    assert os.path.exists(out_dxf)


# ------------------------------- Files ------------------------------------- #
def test_next_version_path(tmp_path):
    (tmp_path / "bracket_v1_base.SLDPRT").write_text("x")
    (tmp_path / "bracket_v2_base.SLDPRT").write_text("x")
    assert server.next_version_path(str(tmp_path / "bracket_v1_base.SLDPRT")) == \
        str(tmp_path / "bracket_v3_base.SLDPRT")                      # v2 exists: never overwritten
    assert server.next_version_path(str(tmp_path / "plate_v9.SLDPRT")) == str(tmp_path / "plate_v10.SLDPRT")
    assert server.next_version_path(str(tmp_path / "part.SLDPRT")) == str(tmp_path / "part_v2.SLDPRT")
    assert server.next_version_path(str(tmp_path / "my_vise_v3_jaw.SLDPRT")) == str(tmp_path / "my_vise_v4_jaw.SLDPRT")


def test_resolve_open_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CONFIG, "projects_directory", str(tmp_path))
    part = tmp_path / "a" / "cad" / "a_v1.SLDPRT"
    part.parent.mkdir(parents=True)
    part.write_text("x")
    assert server.resolve_open_path("a/cad/a_v1.SLDPRT") == str(part)
    assert server.resolve_open_path(str(part)) == str(part)
    with pytest.raises(server.SWError, match="not found"):
        server.resolve_open_path("a/cad/missing.SLDPRT")
    with pytest.raises(server.SWError, match="Only SolidWorks files"):
        server.resolve_open_path("a/cad/a_v1.step")


class FakeOpenDoc:
    def __init__(self, title, path="", kind=1, modified=False, visible=True):
        self.title, self.path, self.kind, self.modified, self.Visible = title, path, kind, modified, visible
        self.saved = False
    def GetTitle(self): return self.title
    def GetPathName(self): return self.path
    def GetType(self): return self.kind
    def GetSaveFlag(self): return self.modified
    def IsOpenedReadOnly(self): return False
    def Save3(self, options, errors, warnings):
        self.saved = True
        return True


class FakeSession:
    """ISldWorks with a few open documents."""
    def __init__(self, docs):
        self.docs = list(docs)
        self.ActiveDoc = self.docs[0] if self.docs else None
        self.opened = []
    def GetDocuments(self): return list(self.docs)
    def GetOpenDocumentByName(self, name):
        return next((d for d in self.docs if d.path == name), None)
    def OpenDoc6(self, path, kind, options, cfg, errors, warnings):
        self.opened.append((path, kind, options))
        doc = FakeOpenDoc(os.path.basename(path), path, kind)
        self.docs.append(doc)
        return doc
    def ActivateDoc3(self, title, use_prefs, option, errors):
        assert option == server.SW_DONT_REBUILD_ON_ACTIVATION   # never a rebuild dialog
        doc = next(d for d in self.docs if d.title == title)
        if doc.Visible:
            self.ActiveDoc = doc
        return doc
    def CloseDoc(self, title):
        self.docs = [d for d in self.docs if d.title != title]
        self.ActiveDoc = self.docs[0] if self.docs else None


def test_open_document_opens_and_activates(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CONFIG, "projects_directory", str(tmp_path))
    part = tmp_path / "plate_v1.SLDPRT"
    part.write_text("x")
    sw = FakeSession([FakeOpenDoc("Part1")])
    monkeypatch.setattr(server, "get_sw", lambda: sw)
    res = asyncio.run(server.open_document("plate_v1.SLDPRT", read_only=True))
    assert sw.opened == [(str(part), server.SW_DOC_PART, server.SW_OPEN_SILENT | server.SW_OPEN_READ_ONLY)]
    assert sw.ActiveDoc.title == "plate_v1.SLDPRT" and "is now the active document" in res


def test_list_and_activate_open_documents(monkeypatch):
    part = FakeOpenDoc("plate_v1.SLDPRT", "C:/p/plate_v1.SLDPRT", modified=True)
    hidden = FakeOpenDoc("screw.SLDPRT", "C:/p/screw.SLDPRT", visible=False)
    asm = FakeOpenDoc("frame.SLDASM", "C:/p/frame.SLDASM", kind=2)
    sw = FakeSession([asm, part, hidden])
    monkeypatch.setattr(server, "get_sw", lambda: sw)
    res = asyncio.run(server.list_open_documents())
    assert res["active"] == "frame.SLDASM" and res["count"] == 3
    assert res["documents"][1] == {"title": "plate_v1.SLDPRT", "path": "C:/p/plate_v1.SLDPRT", "type": "Part",
                                   "modified": True, "visible": True, "read_only": False}
    assert "now the active document" in asyncio.run(server.activate_document("plate_v1"))   # no extension
    assert sw.ActiveDoc is part
    with pytest.raises(server.SWError, match="only loaded by an assembly"):
        asyncio.run(server.activate_document("screw"))
    with pytest.raises(server.SWError, match="No open document"):
        asyncio.run(server.activate_document("nothing"))


def test_close_document_protects_unsaved_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CONFIG, "projects_directory", str(tmp_path))
    part = FakeOpenDoc("plate.SLDPRT", str(tmp_path / "plate.SLDPRT"), modified=True)
    other = FakeOpenDoc("other.SLDPRT", str(tmp_path / "other.SLDPRT"), modified=True)
    sw = FakeSession([part, other])
    monkeypatch.setattr(server, "get_sw", lambda: sw)
    monkeypatch.setattr(server, "active_doc", lambda *a: sw.ActiveDoc)
    with pytest.raises(server.SWError, match="unsaved changes"):
        asyncio.run(server.close_document())
    assert part in sw.docs
    res = asyncio.run(server.close_document(save=True))
    assert part.saved and part not in sw.docs and "after saving" in res
    res = asyncio.run(server.close_document("other", discard_changes=True))
    assert "discarded" in res and not other.saved and sw.docs == []


def test_save_document_new_version(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CONFIG, "projects_directory", str(tmp_path))
    current = tmp_path / "bracket" / "cad" / "bracket_v1_base.SLDPRT"
    current.parent.mkdir(parents=True)
    current.write_text("v1")
    saved = []

    class Doc:
        def GetPathName(self): return str(current)
        def SaveAs3(self, target, version, options):
            saved.append(target)
            with open(target, "w") as fh:
                fh.write("v2")
            return 0

    monkeypatch.setattr(server, "active_doc", lambda *a: Doc())
    res = asyncio.run(server.save_document(new_version=True))
    assert saved == [str(current.parent / "bracket_v2_base.SLDPRT")]
    assert current.read_text() == "v1" and "new version" in res
    with pytest.raises(server.SWError, match="not both"):
        asyncio.run(server.save_document("x.SLDPRT", new_version=True))


# ------------------------------ Topology ----------------------------------- #
class FakeFeature:
    def __init__(self, name): self._name = name
    def Name(self): return self._name


class FakeSurface:
    def __init__(self, kind, params=None, normal=None):
        self.kind, self.params, self.normal = kind, params, normal
    def IsPlane(self): return self.kind == "plane"
    def IsCylinder(self): return self.kind == "cylinder"
    def IsCone(self): return False
    def IsSphere(self): return False
    def IsTorus(self): return False
    @property
    def CylinderParams(self): return self.params
    def EvaluateAtPoint(self, x, y, z): return tuple(self.normal) + (0.0,) * 8


class FakeFace:
    def __init__(self, surface, tris, area, normal=None, feature="Boss-Extrude1", reversed_sense=False):
        self.surface, self.tris, self.area, self.normal = surface, tris, area, normal
        self.feature, self.reversed_sense = feature, reversed_sense
    def GetSurface(self): return self.surface
    def GetTessTriangles(self, no_conversion): return self.tris
    def GetClosestPointOn(self, x, y, z): return (x, y, z, 0.0)
    def GetArea(self): return self.area
    @property
    def Normal(self): return self.normal
    def GetFeature(self): return FakeFeature(self.feature)
    def FaceInSurfaceSense(self): return self.reversed_sense


def top_face():
    # top of a 100 x 50 x 10 block (z = 10 mm), two display triangles; the larger one wins
    big = (0, 0, .01, .1, 0, .01, .1, .05, .01)
    small = (0, 0, .01, .01, 0, .01, 0, .01, .01)
    return FakeFace(FakeSurface("plane"), small + big, 0.005, normal=(0, 0, 2.0))


def hole_face():
    # hole of diameter 10 along Z through (50, 25): the surface normal points out of the
    # cylinder, the face (material side) is reversed, so the face is concave
    return FakeFace(FakeSurface("cylinder", params=(.05, .025, 0, 0, 0, 1, .005), normal=(1, 0, 0)),
                    (.055, .025, 0, .055, .025, .01, .055, .0251, .005), 0.000314, feature="Cut-Extrude1",
                    reversed_sense=True)


def test_face_info_plane_and_hole():
    info = server.face_info(top_face())
    assert info["type"] == "plane" and info["normal"] == [0.0, 0.0, 1.0]
    assert info["point_mm"] == pytest.approx([66.667, 16.667, 10.0], abs=1e-3)   # inside the face
    assert info["area_mm2"] == 5000.0 and info["feature"] == "Boss-Extrude1"
    hole = server.face_info(hole_face())
    assert hole["type"] == "cylinder" and hole["diameter_mm"] == 10.0 and hole["axis"] == [0.0, 0.0, 1.0]
    assert hole["axis_point_mm"] == [50.0, 25.0, 0.0] and hole["concave"] is True


def test_list_faces_filters_and_truncates(monkeypatch):
    class Body:
        def GetFaces(self): return [top_face(), hole_face(), top_face()]

    class Doc:
        def GetBodies2(self, kind, visible): return [Body()]

    monkeypatch.setattr(server, "active_doc", lambda *a: Doc())
    res = asyncio.run(server.list_faces(face_type="cylinder"))
    assert res["total"] == 1 and res["faces"][0]["concave"] is True
    res = asyncio.run(server.list_faces(limit=2))
    assert res["count"] == 2 and res["total"] == 3 and res["truncated"] is True
    assert [f["index"] for f in res["faces"]] == [1, 2]


class FakeCurve:
    def __init__(self, kind, circle=None):
        self.kind, self.circle = kind, circle
    def IsLine(self): return self.kind == "line"
    def IsCircle(self): return self.kind == "circle"
    def IsEllipse(self): return False
    def IsBcurve(self): return False
    @property
    def CircleParams(self): return self.circle
    def Evaluate2(self, t, n):
        if self.kind == "line":          # parameter = distance from (0, 0, 0) along X
            return (t, 0.0, 0.0, 1.0)
        cx, cy, cz, *_rest, r = self.circle
        return (cx + r * math.cos(t), cy + r * math.sin(t), cz, 1.0)
    def GetLength3(self, t0, t1):
        return t1 - t0 if self.kind == "line" else self.circle[-1] * (t1 - t0)


class FakeEdge:
    def __init__(self, curve, params):
        self.curve, self.params = curve, params
    def GetCurve(self): return self.curve
    def GetCurveParams2(self): return self.params
    def GetClosestPointOn(self, x, y, z): return (x, y, z, 0.0)
    def GetTwoAdjacentFaces2(self): return [hole_face(), top_face()]


def test_edge_info_line_and_circle():
    line = server.edge_info(FakeEdge(FakeCurve("line"), (0, 0, 0, .1, 0, 0, 0, .1, 0, 0, 0)))
    assert line["type"] == "line" and line["point_mm"] == [50.0, 0.0, 0.0] and line["length_mm"] == 100.0
    assert line["start_mm"] == [0.0, 0.0, 0.0] and line["end_mm"] == [100.0, 0.0, 0.0]
    assert line["features"] == ["Boss-Extrude1", "Cut-Extrude1"]
    circle = server.edge_info(FakeEdge(FakeCurve("circle", (.05, .025, .01, 0, 0, 1, .005)),
                                       (.055, .025, .01, .055, .025, .01, 0, 2 * math.pi, 0, 0, 0)))
    assert circle["type"] == "circle" and circle["diameter_mm"] == 10.0
    assert circle["center_mm"] == [50.0, 25.0, 10.0] and "start_mm" not in circle
    assert circle["point_mm"] == pytest.approx([45.0, 25.0, 10.0], abs=1e-3)   # on the rim, half way round
    assert circle["length_mm"] == pytest.approx(31.416, abs=1e-3)


def test_list_edges_type_filter(monkeypatch):
    edges = [FakeEdge(FakeCurve("line"), (0, 0, 0, .1, 0, 0, 0, .1, 0, 0, 0)),
             FakeEdge(FakeCurve("circle", (0, 0, 0, 0, 0, 1, .005)), (.005, 0, 0, 0, .005, 0, 0, math.pi / 2, 0, 0, 0))]

    class Body:
        def GetEdges(self): return edges

    class Doc:
        def GetBodies2(self, kind, visible): return [Body()]

    monkeypatch.setattr(server, "active_doc", lambda *a: Doc())
    res = asyncio.run(server.list_edges(edge_type="arc"))
    assert res["total"] == 1 and res["edges"][0]["type"] == "arc" and res["edges"][0]["diameter_mm"] == 10.0


# ------------------------------- Sketch ------------------------------------ #
class FakeSketchDoc:
    """Part with an open sketch whose segment count the tools check."""
    def __init__(self, segments=4):
        doc = self

        class Sketch:
            def GetSketchSegments(self): return [object()] * doc.segments

        class SM:
            ActiveSketch = Sketch()
            AddToDB = False
            def SketchTrim(self, option, x, y, z):
                doc.calls.append(("trim", option, x, y, z))
                doc.segments += doc.trim_adds
                return False   # SolidWorks returns False even when it trims
            def CreateLinearSketchStepAndRepeat(self, *args):
                doc.calls.append(("linear",) + args)
                doc.segments += 4
                return True
            def CreateCircularSketchStepAndRepeat(self, *args):
                doc.calls.append(("circular",) + args)
                doc.segments += 3
                return True
            def CreatePoint(self, x, y, z):
                doc.calls.append(("point", x, y, z))
                return object()

        self.segments, self.calls, self.trim_adds = segments, [], 1
        self.SketchManager = SM()
    def ClearSelection2(self, all_): pass
    def SketchMirror(self):
        self.calls.append(("mirror",))
        self.segments += 1
    def EditDelete(self):
        self.calls.append(("delete",))
        self.segments -= 2


@pytest.fixture
def sketch_doc(monkeypatch):
    doc = FakeSketchDoc()
    picks = []
    monkeypatch.setattr(server, "active_doc", lambda *a: doc)
    monkeypatch.setattr(server, "select_sketch_entity",
                        lambda d, x, y, point=False, append=True, mark=0: picks.append((x, y, mark, append)))
    doc.picks = picks
    return doc


def test_sketch_trim_and_point(sketch_doc):
    asyncio.run(server.sketch_trim(5, 7))
    assert sketch_doc.picks == [(5, 7, 0, False)]
    # the pick point goes in SKETCH coordinates, in metres
    assert sketch_doc.calls == [("trim", server.SW_TRIM_CLOSEST, pytest.approx(0.005), pytest.approx(0.007), 0.0)]
    asyncio.run(server.sketch_point(10, -5))
    assert sketch_doc.calls[-1] == ("point", 0.01, -0.005, 0)


def test_sketch_trim_reports_when_nothing_changes(sketch_doc):
    sketch_doc.trim_adds = 0
    with pytest.raises(server.SWError, match="Nothing was trimmed"):
        asyncio.run(server.sketch_trim(5, 7))


def test_sketch_mirror_uses_marks_1_and_2(sketch_doc):
    res = asyncio.run(server.sketch_mirror([[10, 5], [20, 5]], [0, 3]))
    assert [p[2] for p in sketch_doc.picks] == [1, 1, 2]
    assert "1 new segments" in res


def test_sketch_mirror_reports_a_missing_construction_line(sketch_doc):
    sketch_doc.SketchMirror = lambda: None   # nothing created
    with pytest.raises(server.SWError, match="construction line"):
        asyncio.run(server.sketch_mirror([[10, 5]], [0, 3]))


def test_sketch_linear_pattern(sketch_doc):
    asyncio.run(server.sketch_linear_pattern([[0, 5]], 3, 20.0))
    assert sketch_doc.calls[-1] == ("linear", 3, 1, 0.02, 0.0, 0.0, 90 * server.DEG, "",
                                    False, False, False, False, False)
    with pytest.raises(server.SWError, match="positive spacing"):
        asyncio.run(server.sketch_linear_pattern([[0, 5]], 1, 0.0, count_y=3, spacing_y_mm=0.0))


def test_sketch_circular_pattern_radius_and_angle(sketch_doc):
    # a hole at (40, 0) repeated 6 times around the origin: the centre is to its left (180 deg)
    asyncio.run(server.sketch_circular_pattern([[45, 0]], 6, reference_x_mm=40, reference_y_mm=0))
    _, radius, angle, count, step, rotate, *_ = sketch_doc.calls[-1]
    assert radius == pytest.approx(0.04) and angle == pytest.approx(math.pi)
    assert count == 6 and step == pytest.approx(60 * server.DEG) and rotate is True
    # partial arc: 3 instances over 90 degrees are 45 degrees apart
    asyncio.run(server.sketch_circular_pattern([[45, 0]], 3, angle_deg=90, reference_x_mm=40, reference_y_mm=0))
    assert sketch_doc.calls[-1][4] == pytest.approx(45 * server.DEG)
    with pytest.raises(server.SWError, match="pattern centre"):
        asyncio.run(server.sketch_circular_pattern([[1, 0]], 3, reference_x_mm=0, reference_y_mm=0))


def test_delete_sketch_entity(sketch_doc):
    assert "Deleted 2" in asyncio.run(server.delete_sketch_entity([[1, 1], [2, 2]]))
    sketch_doc.EditDelete = lambda: None
    with pytest.raises(server.SWError, match="Nothing was deleted"):
        asyncio.run(server.delete_sketch_entity([[1, 1]]))


def test_edit_sketch_rejects_other_features(monkeypatch):
    class Feat:
        def GetTypeName2(self): return "Extrusion"

    monkeypatch.setattr(server, "active_doc", lambda *a: object())
    monkeypatch.setattr(server, "leave_sketch", lambda doc: None)
    monkeypatch.setattr(server, "find_feature", lambda doc, name: Feat())
    with pytest.raises(server.SWError, match="not a sketch"):
        asyncio.run(server.edit_sketch("Boss-Extrude1"))


# ------------------------------ Features ----------------------------------- #
class FakeExtrudeData:
    def __init__(self, accept=True):
        self.calls, self.accept, self.released = [], accept, False
        self.ReverseDirection = False
        self.BothDirections = False
    def AccessSelections(self, doc, comp): return True
    def ReleaseSelectionAccess(self): self.released = True
    def SetDepth(self, forward, depth): self.calls.append(("depth", forward, depth))
    def SetEndCondition(self, forward, cond): self.calls.append(("end", forward, cond))


def extrusion(data, kind="Extrusion"):
    class Feat:
        def GetTypeName2(self): return kind
        def GetDefinition(self): return data
        def ModifyDefinition(self, d, doc, comp): return data.accept
        def Name(self): return "Boss-Extrude1"
        def GetErrorCode2(self, warn): return 0
    return Feat()


@pytest.fixture
def part(monkeypatch):
    class Doc:
        def ClearSelection2(self, all_): pass
    monkeypatch.setattr(server, "active_doc", lambda *a: Doc())
    monkeypatch.setattr(server, "leave_sketch", lambda doc: None)
    monkeypatch.setattr(server, "rebuild", lambda doc: None)


def test_edit_feature_changes_only_given_values(part, monkeypatch):
    data = FakeExtrudeData()
    monkeypatch.setattr(server, "find_feature", lambda doc, name: extrusion(data))
    res = asyncio.run(server.edit_feature("Boss-Extrude1", depth_mm=25, depth2_mm=5))
    assert data.calls == [("depth", True, 0.025), ("end", False, server.SW_END_BLIND), ("depth", False, 0.005)]
    assert data.BothDirections is True and data.ReverseDirection is False
    assert "depth_mm = 25" in res
    data = FakeExtrudeData()
    asyncio.run(server.edit_feature("Boss-Extrude1", end_condition="through_all", reverse=True))
    assert data.calls == [("end", True, server.SW_END_THROUGH_ALL)] and data.ReverseDirection is True


def test_edit_feature_refuses_and_releases(part, monkeypatch):
    data = FakeExtrudeData(accept=False)
    monkeypatch.setattr(server, "find_feature", lambda doc, name: extrusion(data))
    with pytest.raises(server.SWError, match="refused"):
        asyncio.run(server.edit_feature("Boss-Extrude1", depth_mm=5))
    assert data.released
    monkeypatch.setattr(server, "find_feature", lambda doc, name: extrusion(data, kind="Fillet"))
    with pytest.raises(server.SWError, match="set_dimension"):
        asyncio.run(server.edit_feature("Fillet1", depth_mm=5))
    with pytest.raises(server.SWError, match="at least one"):
        asyncio.run(server.edit_feature("Boss-Extrude1"))


def test_rename_feature(monkeypatch):
    class Feat:
        def __init__(self, name): self.Name = name

    feats = [Feat("Sketch1"), Feat("Boss-Extrude1")]
    monkeypatch.setattr(server, "active_doc", lambda *a: object())
    monkeypatch.setattr(server, "iter_features", lambda doc: feats)
    monkeypatch.setattr(server, "find_feature", lambda doc, name: next(f for f in feats if f.Name == name))
    assert "renamed" in asyncio.run(server.rename_feature("Boss-Extrude1", "Base plate"))
    assert feats[1].Name == "Base plate"
    with pytest.raises(server.SWError, match="already exists"):
        asyncio.run(server.rename_feature("Sketch1", "Base plate"))


class FakeFeatureManager:
    def __init__(self): self.calls = []
    def InsertRefPlane(self, *args):
        self.calls.append(("plane",) + args)
        return FakeFeatureOk("Plane1")
    def InsertMultiFaceDraft(self, *args):
        self.calls.append(("draft",) + args)
        return FakeFeatureOk("Draft1")
    def InsertCosmeticThread3(self, *args):
        self.calls.append(("thread",) + args)
        return FakeFeatureOk("Cosmetic Thread1")


class FakeFeatureOk:
    def __init__(self, name):
        self._name = name
        self.selected = []
    def Name(self): return self._name
    def GetErrorCode2(self, warn): return 0
    def Select2(self, append, mark):
        self.selected.append(mark)
        return True


@pytest.fixture
def featured(monkeypatch):
    class Doc:
        def __init__(self): self.FeatureManager = FakeFeatureManager()
        def ClearSelection2(self, all_): pass
    doc = Doc()
    picks = []
    monkeypatch.setattr(server, "active_doc", lambda *a: doc)
    monkeypatch.setattr(server, "leave_sketch", lambda d: None)
    monkeypatch.setattr(server, "rebuild", lambda d: None)
    monkeypatch.setattr(server, "select_at", lambda d, kind, p, append=True, mark=0: picks.append((kind, p, mark)))
    doc.picks = picks
    return doc


def test_create_plane_through_three_vertices(featured):
    pts = [[0, 0, 10], [100, 0, 10], [0, 50, 0]]
    assert "through three vertices" in asyncio.run(server.create_plane(through_points=pts))
    assert featured.picks == [("VERTEX", pts[0], 0), ("VERTEX", pts[1], 1), ("VERTEX", pts[2], 2)]
    c = server.SW_REF_PLANE_COINCIDENT
    assert featured.FeatureManager.calls[-1] == ("plane", c, 0.0, c, 0.0, c, 0.0)


def test_create_plane_at_an_angle(featured, monkeypatch):
    front, axis = FakeFeatureOk("Front Plane"), FakeFeatureOk("Axis Y (MCP)")
    monkeypatch.setattr(server, "plane_feature", lambda d, name: front)
    monkeypatch.setattr(server, "model_axis", lambda d, a: (axis, False))
    asyncio.run(server.create_plane(angle_deg=30, axis="y", flip=True))
    assert front.selected == [0] and axis.selected == [1]
    args = featured.FeatureManager.calls[-1]
    assert args[1] == server.SW_REF_PLANE_ANGLE | server.SW_REF_PLANE_FLIP and args[2] == pytest.approx(30 * server.DEG)
    assert args[3] == server.SW_REF_PLANE_COINCIDENT
    with pytest.raises(server.SWError, match="needs axis"):
        asyncio.run(server.create_plane(angle_deg=30))
    with pytest.raises(server.SWError, match="offset_mm > 0"):
        asyncio.run(server.create_plane())


def test_create_axis_needs_exactly_one_reference(featured):
    with pytest.raises(server.SWError, match="exactly one"):
        asyncio.run(server.create_axis())
    with pytest.raises(server.SWError, match="exactly one"):
        asyncio.run(server.create_axis(face_point=[0, 0, 0], edge_point=[1, 0, 0]))


def test_draft_selects_neutral_face_with_mark_1_and_faces_with_mark_2(featured):
    asyncio.run(server.draft(3, [[0, 25, 5], [100, 25, 5]], neutral_face_point=[50, 25, 0]))
    assert featured.picks == [("FACE", [50, 25, 0], 1), ("FACE", [0, 25, 5], 2), ("FACE", [100, 25, 5], 2)]
    assert featured.FeatureManager.calls[-1] == ("draft", pytest.approx(3 * server.DEG), False, False,
                                                 server.SW_DRAFT_NO_PROPAGATION, False, False)
    with pytest.raises(server.SWError, match="either"):
        asyncio.run(server.draft(3, [[0, 25, 5]]))


def test_thread_callout():
    assert server.thread_callout("m6") == (6.0, "M6x1.0")
    assert server.thread_callout("M8") == (8.0, "M8x1.25")
    with pytest.raises(server.SWError):
        server.thread_callout("M7")


def test_hole_adds_a_cosmetic_thread_on_each_rim(featured, monkeypatch):
    monkeypatch.setattr(server, "_face_normal", lambda d: (0.0, 0.0, 1.0))
    monkeypatch.setattr(server, "_cut_circles", lambda *a, **k: "Cut-Extrude1")
    res = asyncio.run(server.hole([[20, 10, 10], [80, 10, 10]], size="M6", fit="tapped", depth_mm=12,
                                  thread=True, thread_depth_mm=10))
    rims = [p for kind, p, mark in featured.picks if kind == "EDGE"]
    assert len(rims) == 2
    for rim, centre in zip(rims, ([20, 10, 10], [80, 10, 10])):
        assert math.dist(rim, centre) == pytest.approx(2.5)          # on the 5 mm tap drill rim
        assert rim[2] == pytest.approx(10)                            # on the face
    thread = featured.FeatureManager.calls[-1]
    assert thread == ("thread", server.SW_COSMETIC_STANDARD_NONE, "", "", pytest.approx(0.006),
                      server.SW_COSMETIC_BLIND, pytest.approx(0.010), "M6x1.0")
    assert "thread M6x1.0 10 mm long" in res
    with pytest.raises(server.SWError, match="tapped"):
        asyncio.run(server.hole([[20, 10, 10]], size="M6", depth_mm=12, thread=True))


# --- geometric pick of sketch entities -------------------------------------------------------
class FakePt:
    def __init__(self, x, y): self.X, self.Y = x * server.MM, y * server.MM


class FakeLine:
    def __init__(self, a, b):
        self.a, self.b, self.selected = FakePt(*a), FakePt(*b), None
    def GetType(self): return server.SW_SKETCH_LINE
    def GetStartPoint2(self): return self.a
    def GetEndPoint2(self): return self.b
    def Select4(self, append, data):
        self.selected = (append, data.Mark)
        return True


class FakeArc(FakeLine):
    def __init__(self, c, r, s, e, direction=1):
        super().__init__(s, e)
        self.c, self.r, self.dir = FakePt(*c), r * server.MM, direction
    def GetType(self): return server.SW_SKETCH_ARC
    def GetCenterPoint2(self): return self.c
    def GetRadius(self): return self.r
    def GetRotationDir(self): return self.dir


class PickDoc:
    def __init__(self, segments):
        segs = segments

        class Data:
            Mark = 0

        class Sel:
            def CreateSelectData(self): return Data()

        class Sketch:
            def GetSketchSegments(self): return segs

        class SM:
            ActiveSketch = Sketch()

        self.SketchManager, self.SelectionManager, self.by_id = SM(), Sel(), []

        class Ext:
            def SelectByID2(ext, *args):
                self.by_id.append(args)
                return True
        self.Extension = Ext()


@pytest.fixture
def no_qi(monkeypatch):
    monkeypatch.setattr(server, "_as", lambda obj, iid: obj)


def test_segment_distance_lines_and_arcs(no_qi):
    line = FakeLine((0, 0), (10, 0))
    assert server._segment_distance(line, 5, 3) == pytest.approx(3)
    assert server._segment_distance(line, 13, 4) == pytest.approx(5)          # beyond the end point
    circle = FakeArc((0, 0), 5, (5, 0), (5, 0))
    assert server._segment_distance(circle, 0, 7) == pytest.approx(2)
    quarter = FakeArc((0, 0), 10, (10, 0), (0, 10), direction=1)             # counter-clockwise
    assert server._segment_distance(quarter, 7.0710678, 7.0710678) == pytest.approx(0, abs=1e-6)
    assert server._segment_distance(quarter, 0, -10) == pytest.approx(math.dist((0, -10), (10, 0)))
    three_quarters = FakeArc((0, 0), 10, (10, 0), (0, 10), direction=-1)     # clockwise: the long way
    assert server._segment_distance(three_quarters, 0, -10) == pytest.approx(0, abs=1e-6)
    assert server._segment_distance(three_quarters, 7.0710678, 7.0710678) > 1


def test_select_sketch_entity_picks_the_nearest_segment(no_qi):
    # a rectangle edge 10 mm away must not be picked instead of the line under the point
    edge, line = FakeLine((50, -30), (50, 30)), FakeLine((40, -40), (40, 40))
    doc = PickDoc([edge, line])
    server.select_sketch_entity(doc, 40, 0.2, append=False, mark=2)
    assert line.selected == (False, 2) and edge.selected is None and doc.by_id == []
    with pytest.raises(server.SWError, match="nearest is 5.00 mm away"):
        server.select_sketch_entity(doc, 45, 0)


def test_launch_accepts_a_visible_solidworks_that_never_reports_startup_done(monkeypatch):
    clock = [0.0]

    class App:
        StartupProcessCompleted = False
        Visible = True
        RevisionNumber = "34"

    started = []
    monkeypatch.setattr(server, "find_solidworks_exe", lambda: "C:/sw/SLDWORKS.exe")
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(server.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server, "_attach", lambda: App())
    monkeypatch.setattr(server.CONFIG, "startup_timeout_s", 180.0)
    monkeypatch.setitem(server._launch_state, "since", float("-inf"))
    assert isinstance(server._launch(), App)
    assert len(started) == 1
    assert server.STARTUP_SETTLE_S <= clock[0] < server.STARTUP_SETTLE_S + 2 * server.STARTUP_POLL_S
    # a second launch while the first one may still be starting does not start another copy
    clock[0] += 5
    server._launch()
    assert len(started) == 1


def test_solidworks_is_started_outside_the_server_process_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(server, "_user_environment", lambda: {"windir": "C:/Windows"})
    monkeypatch.setattr(server, "_in_job", lambda: True)
    server._start_detached("C:/sw/SLDWORKS.exe")
    assert calls[-1][0] == ["explorer.exe", "C:/sw/SLDWORKS.exe"]     # the desktop shell starts it
    monkeypatch.setattr(server, "_in_job", lambda: False)
    server._start_detached("C:/sw/SLDWORKS.exe")
    assert calls[-1][0] == ["C:/sw/SLDWORKS.exe"]
    assert all(kw["env"] == {"windir": "C:/Windows"} for _, kw in calls)   # full user environment


def test_sketch_trim_undoes_a_trim_of_the_wrong_piece(monkeypatch, no_qi):
    class Seg(FakeLine):
        def __init__(self, a, b): super().__init__(a, b)
        def GetLength(self): return math.dist((self.a.X, self.a.Y), (self.b.X, self.b.Y))

    line, edge = Seg((40, -40), (40, 40)), Seg((50, -30), (50, 30))
    doc = PickDoc([line, edge])
    undone = []
    doc.ClearSelection2 = lambda all_: None
    doc.EditUndo2 = lambda n: undone.append(n)

    class SM:
        ActiveSketch = doc.SketchManager.ActiveSketch
        AddToDB = False
        def SketchTrim(self, *args):   # SolidWorks shortens the far edge instead of the line
            edge.b = FakePt(50, 0)
            return False

    doc.SketchManager = SM()
    monkeypatch.setattr(server, "active_doc", lambda *a: doc)
    monkeypatch.setattr(server, "require_sketch", lambda d: d.SketchManager)
    with pytest.raises(server.SWError, match="was undone"):
        asyncio.run(server.sketch_trim(40, 35))
    assert undone == [1]
