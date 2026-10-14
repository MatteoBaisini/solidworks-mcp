"""Offline checks: the server loads and queues commands correctly. SolidWorks is not needed."""
import asyncio
import threading
import time

import pytest

server = pytest.importorskip("solidworks_mcp.server", reason="needs Windows with pywin32")

EXPECTED_TOOLS = {
    "get_active_document_info", "get_mass_properties", "get_feature_tree", "get_bounding_box",
    "get_screenshot", "new_part", "save_document", "export_document", "select_plane",
    "select_face_at_point", "start_sketch", "end_sketch", "sketch_rectangle", "sketch_circle",
    "sketch_line", "sketch_polygon", "sketch_arc", "extrude", "cut_extrude", "revolve", "undo",
    "delete_feature", "zoom_to_fit",
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
