"""Activity log shared by Claude Code and the control panel.

Claude Code runs ``python -m solidworks_mcp.status`` as a hook on its session events
(prompt sent, tool started / finished, waiting for a permission, turn finished). The hook
appends one JSON line per event to a per-user log file; the control panel (panel.py) reads
the file to show what the assistant is doing. The MCP server itself is not involved.

This module imports only the standard library, because Claude Code waits for the hook
before every tool call.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL_PREFIX = "mcp__solidworks__"
MAX_LOG_BYTES = 512 * 1024  # the log is rotated past this size (one .old copy is kept)
MAX_VALUE_CHARS = 200       # long tool arguments are shortened in the log

# Hook events written to the Claude Code settings, with their matcher (None = every event).
HOOK_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": TOOL_PREFIX + ".*",
    "PostToolUse": TOOL_PREFIX + ".*",
    "PostToolUseFailure": TOOL_PREFIX + ".*",
    "Notification": None,
    "Stop": None,
    "SessionEnd": None,
}
HOOK_MARKER = "solidworks_mcp.status"  # identifies the hooks this package owns


def status_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "solidworks-mcp"


def log_path() -> Path:
    return status_dir() / "activity.jsonl"


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "..."
    if isinstance(value, dict):
        return {k: _short(v) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_short(v) for v in value[:20]]
    return value


def _response_error(response: Any) -> str | None:
    """Error text of an MCP tool response, if the tool failed."""
    if isinstance(response, dict):
        if response.get("isError") or response.get("is_error"):
            return _first_text(response.get("content")) or "error"
        return None
    if isinstance(response, list):  # list of content blocks
        text = _first_text(response)
        if text and text.startswith(("Error", "ERROR")):
            return text
    if isinstance(response, str) and response.startswith(("Error", "ERROR")):
        return response
    return None


def _first_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]
    return None


def event_from_hook(data: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """Turns the JSON Claude Code passes to a hook into one log record (None = not logged)."""
    name = data.get("hook_event_name")
    if name not in HOOK_EVENTS:
        return None
    record: dict[str, Any] = {"t": round(now if now is not None else time.time(), 3), "event": name,
                              "session": str(data.get("session_id", ""))[:64]}
    tool = data.get("tool_name")
    if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        if not isinstance(tool, str) or not tool.startswith(TOOL_PREFIX):
            return None
        record["tool"] = tool[len(TOOL_PREFIX):]
        if name == "PreToolUse":
            record["input"] = _short(data.get("tool_input") or {})
        elif name == "PostToolUse":
            error = _response_error(data.get("tool_response"))
            record["ok"] = error is None
            if error:
                record["error"] = _short(error)
        else:
            record["ok"] = False
            record["error"] = _short(str(data.get("error") or "error"))
    elif name == "Notification":
        record["message"] = _short(str(data.get("message", "")))
        record["kind"] = str(data.get("notification_type", ""))
    return record


def append_event(record: dict[str, Any], path: Path | None = None) -> None:
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_suffix(".old"))
    except OSError:
        pass  # no log yet, or the panel holds it open: rotate next time
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def hook_main(stdin: Any = None) -> int:
    """Entry point of the Claude Code hook. Never fails: a broken log must not block Claude."""
    try:
        data = json.load(stdin or sys.stdin)
        record = event_from_hook(data) if isinstance(data, dict) else None
        if record:
            append_event(record)
    except Exception:
        pass
    return 0


# --------------------------------------------------------------------------- #
# Reading the log (control panel side)
# --------------------------------------------------------------------------- #
class LogReader:
    """Reads the records appended since the previous call; survives rotation."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or log_path()
        self.offset = 0

    def read_new(self) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # rotated
            self.offset = 0
        records = []
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        for line in chunk[:end].splitlines():
            try:
                rec = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
        return records


@dataclass
class Step:
    t: float
    tool: str
    input: dict[str, Any]
    ok: bool | None = None  # None = still running
    error: str = ""
    finished: float | None = None


@dataclass
class Activity:
    """What the assistant is doing, rebuilt from the log records."""

    state: str = "offline"  # offline | idle | thinking | working | asking
    since: float = 0.0
    session: str = ""
    message: str = ""
    current: Step | None = None
    steps: list[Step] = field(default_factory=list)
    errors: int = 0
    max_steps: int = 200

    def apply(self, rec: dict[str, Any]) -> None:
        event, t = rec.get("event"), float(rec.get("t", 0.0))
        if rec.get("session"):
            self.session = rec["session"]
        if event == "SessionStart":
            self.errors = 0
            self._set("idle", t)
        elif event == "SessionEnd":
            self._set("offline", t)
            self.current = None
        elif event == "UserPromptSubmit":
            self._set("thinking", t)
        elif event == "PreToolUse":
            step = Step(t=t, tool=str(rec.get("tool", "")), input=rec.get("input") or {})
            self.steps.append(step)
            del self.steps[:-self.max_steps]
            self.current = step
            self._set("working", t)
        elif event in ("PostToolUse", "PostToolUseFailure"):
            step = self.current if self.current and self.current.tool == rec.get("tool") else None
            if step is None:
                step = next((s for s in reversed(self.steps) if s.tool == rec.get("tool") and s.ok is None), None)
            if step is not None:
                step.ok = bool(rec.get("ok", False))
                step.error = str(rec.get("error", ""))
                step.finished = t
                if not step.ok:
                    self.errors += 1
            self.current = None
            self._set("thinking", t)
        elif event == "Notification":
            self.message = str(rec.get("message", ""))
            if rec.get("kind") in ("permission_prompt", "elicitation_dialog") or "permission" in self.message.lower():
                self._set("asking", t)
            elif rec.get("kind") == "idle_prompt":
                self._set("idle", t)
        elif event == "Stop":
            self.current = None
            self._set("idle", t)

    def _set(self, state: str, t: float) -> None:
        if state != self.state:
            self.state, self.since = state, t
        if state != "asking":
            self.message = ""


if __name__ == "__main__":
    sys.exit(hook_main())
