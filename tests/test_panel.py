"""Control panel logic that does not need a window, SolidWorks or Claude Code."""
import asyncio
import json

from solidworks_mcp import panel, server
from solidworks_mcp.status import HOOK_EVENTS, HOOK_MARKER


def test_ensure_hooks_creates_settings(tmp_path):
    path, changed = panel.ensure_hooks(tmp_path, command='"C:/py/python.exe" -m solidworks_mcp.status')
    assert changed and path == tmp_path / ".claude" / "settings.local.json"
    hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == set(HOOK_EVENTS)
    assert hooks["PreToolUse"][0]["matcher"] == "mcp__solidworks__.*"
    assert "matcher" not in hooks["Stop"][0]
    assert hooks["Stop"][0]["hooks"][0]["command"].endswith("-m solidworks_mcp.status")


def test_ensure_hooks_keeps_user_settings_and_is_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    user_hook = {"hooks": [{"type": "command", "command": "echo mine"}]}
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                    "hooks": {"Stop": [user_hook]}}), encoding="utf-8")
    panel.ensure_hooks(tmp_path, command="old -m solidworks_mcp.status")
    _, changed = panel.ensure_hooks(tmp_path, command="new -m solidworks_mcp.status")
    assert changed
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    stop = data["hooks"]["Stop"]
    assert stop[0] == user_hook
    ours = [g for g in stop if HOOK_MARKER in g["hooks"][0]["command"]]
    assert len(ours) == 1 and ours[0]["hooks"][0]["command"].startswith("new")
    assert panel.ensure_hooks(tmp_path, command="new -m solidworks_mcp.status")[1] is False


def test_ensure_hooks_saves_an_invalid_file(tmp_path):
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text("{broken", encoding="utf-8")
    panel.ensure_hooks(tmp_path, command="x -m solidworks_mcp.status")
    assert (tmp_path / ".claude" / "settings.local.json.invalid").read_text(encoding="utf-8") == "{broken"
    assert "hooks" in json.loads(settings.read_text(encoding="utf-8"))


def test_hook_command_uses_the_console_python(tmp_path):
    cmd = panel.hook_command(r"C:\tools\venv\Scripts\pythonw.exe")
    assert cmd == '"C:/tools/venv/Scripts/python.exe" -m solidworks_mcp.status'


def test_describe_step_in_both_languages():
    assert panel.describe_step("extrude", {"depth_mm": 10.0}, "en") == "Extrusion 10 mm"
    assert panel.describe_step("extrude", {"depth_mm": 10.0}, "it") == "Estrusione 10 mm"
    assert panel.describe_step("sketch_rectangle", {"width_mm": 100, "height_mm": 50.5}) == "Rectangle 100 x 50.5 mm"
    assert panel.describe_step("extrude", {}, "en") == "Extrusion"          # missing argument
    assert panel.describe_step("some_new_tool", {}, "en") == "Some new tool"


def test_every_tool_has_a_plain_label():
    server.register_macro_tool()
    try:
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    finally:
        server.mcp._tool_manager._tools.pop("execute_solidworks_macro", None)
    assert names <= set(panel.STEP_LABELS), sorted(names - set(panel.STEP_LABELS))


def test_templates_build_complete_prompts():
    for t in panel.TEMPLATES:
        for lang in ("it", "en"):
            values = {key: default for key, _it, _en, default in t.fields}
            text = panel.build_prompt(t, values, "bracket", "", "", lang)
            assert "{" not in text and "  " not in text
            assert "bracket" in text
    plate = panel.TEMPLATES[0]
    text = panel.build_prompt(plate, {"l": "120", "w": "80", "t": "10", "screw": "M6", "e": "10"},
                              "", "Aluminium 6061", "anodised", "en")
    assert text.startswith("Project: new_part. A 120 x 80 x 10 mm plate")
    assert "Material: Aluminium 6061." in text and "Other details: anodised." in text


def test_template_labels_are_complete():
    for t in panel.TEMPLATES:
        assert all(it and en for it, en in [t.names, t.prompt]) or t.key == "free"
        for key, it, en, default in t.fields:
            assert it and en and f"{{{key}}}" in t.prompt[0] and f"{{{key}}}" in t.prompt[1]


def test_texts_have_the_same_keys():
    assert set(panel.TEXTS["it"]) == set(panel.TEXTS["en"])
    assert all(panel.TEXTS[lang]["guide"] for lang in panel.TEXTS)


def test_latest_cad_file(tmp_path):
    import os
    a = tmp_path / "p" / "cad" / "a_v1.SLDPRT"
    b = tmp_path / "q" / "b_v1.sldasm"
    for f in (a, b):
        f.parent.mkdir(parents=True)
        f.write_text("x")
    (tmp_path / "q" / "~$b_v1.sldasm").write_text("lock")
    (tmp_path / "notes.txt").write_text("x")
    os.utime(a, (1, 1))
    assert panel.latest_cad_file(tmp_path) == b
    assert panel.latest_cad_file(tmp_path / "missing") is None


def test_projects_dir_reads_the_workspace_config(tmp_path):
    (tmp_path / "solidworks-mcp.toml").write_text('[projects]\ndirectory = "parts"\n', encoding="utf-8")
    assert panel.projects_dir(tmp_path) == (tmp_path / "parts").resolve()
    assert panel.projects_dir(tmp_path / "none") == tmp_path / "none" / "projects"
