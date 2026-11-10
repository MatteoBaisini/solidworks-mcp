"""Activity log written by the Claude Code hooks and read by the control panel."""
import io
import json

from solidworks_mcp import status
from solidworks_mcp.status import Activity, LogReader, event_from_hook


def hook(event, **extra):
    return {"hook_event_name": event, "session_id": "s1", **extra}


def test_tool_events_are_logged_only_for_this_server():
    rec = event_from_hook(hook("PreToolUse", tool_name="mcp__solidworks__extrude",
                               tool_input={"depth_mm": 10}), now=5.0)
    assert rec == {"t": 5.0, "event": "PreToolUse", "session": "s1", "tool": "extrude", "input": {"depth_mm": 10}}
    assert event_from_hook(hook("PreToolUse", tool_name="Bash", tool_input={})) is None
    assert event_from_hook(hook("SomethingElse")) is None


def test_failed_tool_is_detected():
    ok = event_from_hook(hook("PostToolUse", tool_name="mcp__solidworks__fillet",
                              tool_response=[{"type": "text", "text": "Fillet1 created"}]))
    assert ok["ok"] is True
    bad = event_from_hook(hook("PostToolUse", tool_name="mcp__solidworks__fillet",
                               tool_response={"isError": True, "content": [{"type": "text", "text": "no edge"}]}))
    assert bad["ok"] is False and bad["error"] == "no edge"
    fail = event_from_hook(hook("PostToolUseFailure", tool_name="mcp__solidworks__fillet", error="boom"))
    assert fail["ok"] is False and fail["error"] == "boom"


def test_long_arguments_are_shortened():
    rec = event_from_hook(hook("PreToolUse", tool_name="mcp__solidworks__execute_solidworks_macro",
                               tool_input={"python_code": "x" * 5000}))
    assert len(rec["input"]["python_code"]) <= status.MAX_VALUE_CHARS + 3


def test_hook_main_appends_and_never_fails(tmp_path, monkeypatch):
    log = tmp_path / "activity.jsonl"
    monkeypatch.setattr(status, "log_path", lambda: log)
    assert status.hook_main(io.StringIO(json.dumps(hook("UserPromptSubmit", prompt="hi")))) == 0
    assert status.hook_main(io.StringIO("not json")) == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "UserPromptSubmit"


def test_reader_returns_only_new_complete_lines(tmp_path):
    log = tmp_path / "a.jsonl"
    reader = LogReader(log)
    assert reader.read_new() == []
    status.append_event({"event": "Stop", "t": 1}, log)
    assert [r["event"] for r in reader.read_new()] == ["Stop"]
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"event": "Sto')  # half-written line
    assert reader.read_new() == []
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('p", "t": 2}\n')
    assert [r["t"] for r in reader.read_new()] == [2]


def test_reader_survives_rotation(tmp_path, monkeypatch):
    log = tmp_path / "a.jsonl"
    monkeypatch.setattr(status, "MAX_LOG_BYTES", 10)
    reader = LogReader(log)
    status.append_event({"event": "Stop", "t": 1, "pad": "x" * 20}, log)
    reader.read_new()
    status.append_event({"event": "Stop", "t": 2}, log)  # rotates, starts a new file
    assert (tmp_path / "a.old").is_file()
    assert [r["t"] for r in reader.read_new()] == [2]


def test_activity_follows_a_turn():
    a = Activity()
    a.apply({"event": "SessionStart", "t": 1})
    assert a.state == "idle"
    a.apply({"event": "UserPromptSubmit", "t": 2})
    assert a.state == "thinking"
    a.apply({"event": "PreToolUse", "t": 3, "tool": "extrude", "input": {"depth_mm": 10}})
    assert a.state == "working" and a.current.tool == "extrude"
    a.apply({"event": "PostToolUse", "t": 4, "tool": "extrude", "ok": False, "error": "no sketch"})
    assert a.state == "thinking" and a.current is None
    assert a.steps[0].ok is False and a.steps[0].error == "no sketch" and a.errors == 1
    a.apply({"event": "Notification", "t": 5, "message": "Claude needs your permission to use X",
             "kind": "permission_prompt"})
    assert a.state == "asking"
    a.apply({"event": "Stop", "t": 6})
    assert a.state == "idle" and a.message == ""
    a.apply({"event": "SessionEnd", "t": 7})
    assert a.state == "offline"


def test_activity_keeps_a_bounded_history():
    a = Activity(max_steps=3)
    for i in range(5):
        a.apply({"event": "PreToolUse", "t": i, "tool": f"t{i}"})
    assert [s.tool for s in a.steps] == ["t2", "t3", "t4"]
