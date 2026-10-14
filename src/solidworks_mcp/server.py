"""
SolidWorks MCP server
=====================

Connects an AI assistant to SolidWorks on this PC through the SolidWorks COM API (pywin32).

Principles:
  * Every tool takes MILLIMETRES and DEGREES; the server converts to the API's
    internal units (metres, radians).
  * Every COM call goes through ONE dedicated thread (STA) and commands run one at
    a time, in the order they arrive. A command that exceeds the timeout is reported
    as an error and the commands behind it fail fast instead of piling up.
  * If SolidWorks is not running the server starts it (configurable).
  * HTTP transports listen ONLY on 127.0.0.1 (never on the local network).
  * Running arbitrary code (execute_solidworks_macro) is OFF unless enabled in the
    configuration.

Settings come from a TOML file, see config.py and config.example.toml.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import winreg
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Literal

import pythoncom
import pywintypes
import win32com.client
import win32com.client.dynamic
from win32com.client import VARIANT
# NB: the right class is dynamic.CDispatch (win32com.client.CDispatch is a different
# subclass: objects created by dynamic.Dispatch are not instances of it).
from win32com.client.dynamic import CDispatch

from mcp.server.fastmcp import FastMCP, Image

from solidworks_mcp import __version__
from solidworks_mcp.config import Config, ConfigError, load_config, normalize_format

# --------------------------------------------------------------------------- #
# Configuration and logging (always on stderr: stdout belongs to the protocol
# when the stdio transport is used)
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - SW-MCP - %(levelname)s - %(message)s",
)
logger = logging.getLogger("solidworks_mcp")

HOST = "127.0.0.1"  # do NOT change to 0.0.0.0: it would expose the PC to the network
DEFAULT_PORT = 8000
TRANSPORTS = ("stdio", "sse", "streamable-http")

CONFIG: Config = Config()  # replaced by main() with the file on disk

MM = 0.001                 # 1 mm in metres
DEG = math.pi / 180.0      # 1 degree in radians

# SolidWorks API constants used by the server
SW_DEFAULT_TEMPLATE_PART = 8      # swUserPreferenceStringValue_e.swDefaultTemplatePart
SW_DOC_PART, SW_DOC_ASSEMBLY, SW_DOC_DRAWING = 1, 2, 3
SW_END_BLIND, SW_END_THROUGH_ALL, SW_END_MIDPLANE = 0, 1, 6
SW_SAVE_SILENT = 1                # swSaveAsOptions_Silent
SW_SAVE_COPY = 2                  # swSaveAsOptions_Copy (the open document is left untouched)
DOC_TYPE_NAMES = {SW_DOC_PART: "Part", SW_DOC_ASSEMBLY: "Assembly", SW_DOC_DRAWING: "Drawing"}

SERVER_INSTRUCTIONS = """\
Server that drives SolidWorks on the user's PC.
Rules:
- Every tool dimension is in millimetres, every angle in degrees.
- Typical flow for a part: new_part -> select_plane -> start_sketch ->
  sketch_rectangle / sketch_circle / sketch_line / sketch_polygon -> extrude
  (or cut_extrude for holes/pockets) -> get_screenshot or get_bounding_box to check.
- To work on an existing face: select_face_at_point (mm coordinates of a point on
  the face) then start_sketch.
- The user may be working in SolidWorks too: call get_active_document_info before
  modifying, to be sure the active document is the one you expect.
- On error use undo, then retry. get_feature_tree shows what is in the model.
- export_document writes the exchange formats configured by the user (STEP by default).
"""

mcp = FastMCP("SolidWorks", instructions=SERVER_INSTRUCTIONS, host=HOST, port=DEFAULT_PORT)

# --------------------------------------------------------------------------- #
# Dedicated COM thread and command queue
# --------------------------------------------------------------------------- #
def _com_thread_init() -> None:
    pythoncom.CoInitialize()  # STA, once for the whole life of the thread


_com_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sw-com", initializer=_com_thread_init)
_sw_cache: dict[str, Any] = {"app": None}
_queue: dict[str, Any] = {"loop": None, "lock": None}
_stuck: dict[str, Any] = {"name": None, "since": 0.0}  # the command that exceeded the timeout, if any


def _queue_lock() -> asyncio.Lock:
    """One command at a time, in arrival order (the lock belongs to the running event loop)."""
    loop = asyncio.get_running_loop()
    if _queue["loop"] is not loop:
        _queue.update(loop=loop, lock=asyncio.Lock())
    return _queue["lock"]


class SWError(Exception):
    """Error with a readable message to hand back to the AI."""


def _busy_message() -> str:
    seconds = int(time.monotonic() - _stuck["since"])
    return (f"SolidWorks is still busy with '{_stuck['name']}' (started {seconds} s ago), so this "
            "command was not sent. A dialog may be waiting for an answer in the SolidWorks window, or "
            "SolidWorks may still be starting up. Retry once it responds again.")


async def run_com(fn: Callable[..., Any], *args: Any) -> Any:
    """Runs fn in the COM thread, after the commands already queued.

    Raises SWError instead of waiting forever when SolidWorks stops answering.
    """
    name = fn.__qualname__.split(".")[0]
    if _stuck["name"]:
        raise SWError(_busy_message())
    async with _queue_lock():
        if _stuck["name"]:  # the command ahead of this one timed out while we were queued
            raise SWError(_busy_message())
        timeout = CONFIG.command_timeout_s
        if CONFIG.auto_launch and _sw_cache["app"] is None:
            timeout += CONFIG.startup_timeout_s  # this command may have to start SolidWorks first
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(_com_executor, fn, *args)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            _stuck.update(name=name, since=time.monotonic() - timeout)

            def _finished(f: asyncio.Future) -> None:
                _stuck["name"] = None
                if not f.cancelled() and f.exception() is not None:
                    logger.warning("Late failure of '%s': %s", name, f.exception())

            future.add_done_callback(_finished)
            logger.warning("Command '%s' exceeded %.0f s", name, timeout)
            raise SWError(
                f"'{name}' got no answer from SolidWorks within {timeout:.0f} s. It may "
                "still complete. Check the SolidWorks window for an open dialog; until SolidWorks "
                "answers, further commands are refused instead of being queued.") from None


# --------------------------------------------------------------------------- #
# COM helpers (to be used ONLY inside the COM thread)
# --------------------------------------------------------------------------- #
NOTHING = VARIANT(pythoncom.VT_DISPATCH, None)
"""Same as 'Nothing' in VBA: pass it for optional object parameters
(e.g. the Callout of SelectByID2). Passing None gives 'Type mismatch'."""


def call(obj: Any, name: str, *args: Any) -> Any:
    """Calls a COM method or reads a property without caring which one it is.

    With pywin32 late binding some argument-less methods (GetTitle, GetType, ...)
    already arrive as a value: both cases are handled here.
    """
    attr = getattr(obj, name)
    if isinstance(attr, CDispatch):  # already a COM object returned by a property
        return attr
    if callable(attr):
        return attr(*args)
    return attr


def _attach() -> Any | None:
    """The running SolidWorks instance, or None when there is none."""
    try:
        clsid = pywintypes.IID("SldWorks.Application")
        unk = pythoncom.GetActiveObject(clsid)
        return win32com.client.dynamic.Dispatch(unk.QueryInterface(pythoncom.IID_IDispatch))
    except pywintypes.com_error:
        return None


def find_solidworks_exe() -> str | None:
    """SLDWORKS.exe from the configuration, else from the COM registration."""
    if CONFIG.executable:
        return CONFIG.executable if os.path.isfile(CONFIG.executable) else None
    try:
        clsid = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CLSID")
        command = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32")
    except OSError:
        return None
    match = re.match(r'\s*"([^"]+)"|\s*(.+?\.exe)', command, re.IGNORECASE)
    exe = (match.group(1) or match.group(2)) if match else None
    return exe if exe and os.path.isfile(exe) else None


def _launch() -> Any:
    """Starts SolidWorks and waits until it accepts commands."""
    exe = find_solidworks_exe()
    if exe is None:
        raise SWError("SolidWorks is not running and SLDWORKS.exe was not found. Start SolidWorks, or set "
                      "[solidworks] executable in the configuration file.")
    logger.info("SolidWorks is not running: starting %s", exe)
    subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    deadline = time.monotonic() + CONFIG.startup_timeout_s
    while time.monotonic() < deadline:
        time.sleep(2)
        app = _attach()
        if app is None:
            continue
        try:
            if call(app, "StartupProcessCompleted"):
                return app
        except (pywintypes.com_error, AttributeError):
            return app  # property missing in old releases: being attached is enough
    raise SWError(f"SolidWorks did not finish starting within {CONFIG.startup_timeout_s:.0f} s.")


def get_sw() -> Any:
    """Returns the running SolidWorks, starting it when allowed by the configuration."""
    app = _sw_cache["app"]
    if app is not None:
        try:
            call(app, "RevisionNumber")  # check it is still alive
            return app
        except Exception:
            logger.warning("Connection to SolidWorks lost, reconnecting...")
            _sw_cache["app"] = None
    app = _attach()
    if app is None:
        if not CONFIG.auto_launch:
            raise SWError(
                "SolidWorks is not running (or cannot be reached). Start it and retry. If it is already "
                "open, make sure SolidWorks and this server run with the same privileges "
                "(neither one as administrator).")
        app = _launch()
    _sw_cache["app"] = app
    logger.info("Connected to SolidWorks %s", call(app, "RevisionNumber"))
    return app


def active_doc(required_type: int | None = None) -> Any:
    doc = get_sw().ActiveDoc
    if doc is None:
        raise SWError("No document is open in SolidWorks. Use new_part to create one.")
    if required_type is not None:
        doc_type = call(doc, "GetType")
        if doc_type != required_type:
            raise SWError(
                f"The active document is a '{DOC_TYPE_NAMES.get(doc_type, doc_type)}', "
                f"a '{DOC_TYPE_NAMES.get(required_type, required_type)}' is needed.")
    return doc


def iter_features(doc: Any):
    feat = call(doc, "FirstFeature")
    while feat is not None:
        yield feat
        feat = call(feat, "GetNextFeature")


def ref_planes(doc: Any) -> list[Any]:
    """The three base reference planes, in the order Front, Top, Right.

    Works with SolidWorks in any language (plane names are not used).
    """
    planes = [f for f in iter_features(doc) if call(f, "GetTypeName2") == "RefPlane"]
    if len(planes) < 3:
        raise SWError("The three reference planes were not found in the document.")
    return planes[:3]


def in_sketch(doc: Any) -> bool:
    return doc.SketchManager.ActiveSketch is not None


def require_sketch(doc: Any) -> Any:
    if not in_sketch(doc):
        raise SWError("No sketch is open. Select a plane/face and use start_sketch.")
    sm = doc.SketchManager
    sm.AddToDB = True  # no automatic snapping to nearby geometry: exact dimensions
    return sm


def last_sketch_feature(doc: Any) -> Any:
    last = None
    for f in iter_features(doc):
        if call(f, "GetTypeName2") == "ProfileFeature":
            last = f
    if last is None:
        raise SWError("No sketch found to build the feature from.")
    return last


def close_sketch_and_select_it(doc: Any) -> str:
    """Closes the open sketch, if any, and selects the last sketch."""
    sm = doc.SketchManager
    if in_sketch(doc):
        sm.AddToDB = False
        sm.InsertSketch(True)
    sketch = last_sketch_feature(doc)
    doc.ClearSelection2(True)
    if not sketch.Select2(False, 0):
        raise SWError("Could not select the sketch.")
    return call(sketch, "Name")


def rebuild(doc: Any) -> None:
    try:
        doc.ForceRebuild3(False)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Wrapper: every tool runs its logic in the COM thread and always returns a
# readable message (never a raw exception)
# --------------------------------------------------------------------------- #
async def com_tool(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return await run_com(fn, *args)
    except SWError as e:
        return f"ERROR: {e}"
    except pywintypes.com_error as e:
        logger.exception("COM error")
        if _is_disconnected(e):
            _sw_cache["app"] = None
        return f"COM ERROR from SolidWorks: {e}"
    except Exception as e:
        logger.exception("Unexpected error")
        return f"ERROR: {type(e).__name__}: {e}"


def _is_disconnected(e: Exception) -> bool:
    code = e.args[0] if getattr(e, "args", None) else None
    return code in (-2147417848, -2147023174, -2147023170)  # RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE, RPC_S_CALL_FAILED


# =========================================================================== #
#                                  TOOLS                                      #
# =========================================================================== #

# ------------------------------- Information ------------------------------- #
@mcp.tool()
async def get_active_document_info() -> str:
    """Name, path and type of the active SolidWorks document, and the SolidWorks version.
    Starts SolidWorks if it is not running."""
    def work():
        sw = get_sw()
        doc = sw.ActiveDoc
        rev = call(sw, "RevisionNumber")
        if doc is None:
            return f"SolidWorks {rev} connected. No document open."
        doc_type = call(doc, "GetType")
        path = call(doc, "GetPathName") or "(not saved yet)"
        sketch = " - sketch open" if in_sketch(doc) else ""
        return (f"SolidWorks {rev}\nActive document: {call(doc, 'GetTitle')}\n"
                f"Type: {DOC_TYPE_NAMES.get(doc_type, doc_type)}{sketch}\nPath: {path}")
    return await com_tool(work)


@mcp.tool()
async def get_mass_properties() -> str:
    """Mass, volume, surface area and centre of mass of the active document (part or assembly)."""
    def work():
        doc = active_doc()
        ext = doc.Extension
        try:
            mp = call(ext, "CreateMassProperty2")   # SolidWorks 2020+
        except Exception:
            mp = None
        if mp is None:
            mp = call(ext, "CreateMassProperty")    # earlier releases
        if mp is None:
            raise SWError("Could not compute the mass properties (does the model have a solid body?).")
        mass, vol, area = call(mp, "Mass"), call(mp, "Volume"), call(mp, "SurfaceArea")
        cg = call(mp, "CenterOfMass")
        cg_txt = f"\nCentre of mass: X={cg[0]/MM:.3f} Y={cg[1]/MM:.3f} Z={cg[2]/MM:.3f} mm" if cg else ""
        return (f"Mass: {mass:.4f} kg\nVolume: {vol/MM**3:.2f} mm^3 ({vol*1e6:.3f} cm^3)\n"
                f"Surface area: {area/MM**2:.2f} mm^2{cg_txt}")
    return await com_tool(work)


@mcp.tool()
async def get_feature_tree() -> str:
    """List of the features of the active document (design tree), with the type of each."""
    def work():
        doc = active_doc()
        rows = []
        for i, f in enumerate(iter_features(doc)):
            t = call(f, "GetTypeName2")
            if t in ("HistoryFolder", "SensorFolder", "DocsFolder", "DetailCabinet", "SurfaceBodyFolder",
                     "SolidBodyFolder", "EqnFolder", "MaterialFolder", "CommentsFolder",
                     "FavoriteFolder", "SelectionSetFolder", "EnvFolder", "InkMarkupFolder",
                     "OriginProfileFeature", "MateReferenceGroupFolder", "BlockFolder"):
                continue
            rows.append(f"{i:3d}. {call(f, 'Name')}  [{t}]")
        return "Feature tree:\n" + ("\n".join(rows) if rows else "(empty)")
    return await com_tool(work)


@mcp.tool()
async def get_bounding_box() -> str:
    """Bounding box of the active part: min/max on X, Y, Z and overall size, in mm."""
    def work():
        doc = active_doc(SW_DOC_PART)
        box = doc.GetPartBox(True)
        if not box:
            raise SWError("The part has no solid geometry yet.")
        x1, y1, z1, x2, y2, z2 = (v / MM for v in box)
        return (f"Min: X={x1:.3f} Y={y1:.3f} Z={z1:.3f} mm\nMax: X={x2:.3f} Y={y2:.3f} Z={z2:.3f} mm\n"
                f"Size: {x2-x1:.3f} x {y2-y1:.3f} x {z2-z1:.3f} mm")
    return await com_tool(work)


@mcp.tool()
async def get_screenshot(view: Literal["current", "isometric", "front", "top", "right"] = "isometric"):
    """PNG image of the active model, to check the result visually."""
    view_ids = {"isometric": 7, "front": 1, "top": 5, "right": 4}  # swStandardViews_e (language independent)

    def work():
        doc = active_doc()
        if view != "current":
            try:
                doc.ShowNamedView2("", view_ids[view])
            except Exception:
                pass
        call(doc, "ViewZoomtofit2")
        path = os.path.join(tempfile.gettempdir(), "sw_mcp_snapshot.png")
        if os.path.exists(path):
            os.remove(path)
        err = doc.SaveAs3(path, 0, SW_SAVE_SILENT | SW_SAVE_COPY)
        if not os.path.exists(path):
            raise SWError(f"SolidWorks did not save the image (code {err}).")
        with open(path, "rb") as fh:
            return fh.read()

    result = await com_tool(work)
    if isinstance(result, bytes):
        return Image(data=result, format="png")
    return result


# -------------------------------- Documents -------------------------------- #
@mcp.tool()
async def new_part() -> str:
    """Creates a new empty part from the default SolidWorks part template."""
    def work():
        sw = get_sw()
        template = sw.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_PART)
        if not template or not os.path.exists(template):
            raise SWError("Default part template not set or not found "
                          "(SolidWorks > Options > File Locations / Default Templates).")
        doc = sw.NewDocument(template, 0, 0, 0)
        if doc is None:
            raise SWError("SolidWorks did not create the document.")
        return f"New part created: {call(doc, 'GetTitle')}"
    return await com_tool(work)


@mcp.tool()
async def save_document(path: str = "") -> str:
    """Saves the active document. With 'path' (e.g. C:\\\\projects\\\\part.SLDPRT, .step, .stl, .pdf...)
    saves/exports to that file: the format is taken from the extension."""
    def work():
        doc = active_doc()
        if not path:
            if not call(doc, "GetPathName"):
                raise SWError("The document was never saved: give a full path.")
            ok = doc.Save3(SW_SAVE_SILENT, VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0),
                           VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0))
            if not ok:
                raise SWError("Save failed.")
            return "Document saved."
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            raise SWError(f"The folder {folder} does not exist.")
        err = doc.SaveAs3(path, 0, SW_SAVE_SILENT)
        if err != 0 or not os.path.exists(path):
            raise SWError(f"Save failed (SolidWorks error code {err}).")
        return f"Saved to {path}"
    return await com_tool(work)


@mcp.tool()
async def export_document(formats: list[str] | None = None, directory: str = "") -> str:
    """Exports the active document to exchange formats, next to the document or in 'directory'.

    formats: e.g. ["step", "stl"]; omit to use the formats set by the user in the
      configuration (STEP by default). Supported: step, stp, iges, igs, x_t, stl, 3mf, pdf, png.
    directory: absolute, or relative to the folder of the document; "{format}" is replaced
      by the format name. Omit to use the configured directory. Created if missing.
    Files are named after the document. The open document is left untouched."""
    def work():
        doc = active_doc()
        try:
            exts = [normalize_format(f) for f in (formats or CONFIG.export_formats)]
        except ConfigError as e:
            raise SWError(str(e)) from e
        doc_path = call(doc, "GetPathName")
        stem = os.path.splitext(os.path.basename(doc_path) if doc_path else call(doc, "GetTitle"))[0]
        template = directory or CONFIG.export_directory
        written = []
        for ext in dict.fromkeys(exts):
            folder = template.replace("{format}", ext)
            if not os.path.isabs(folder):
                if not doc_path:
                    raise SWError("The document was never saved: save it first, or give an absolute directory.")
                folder = os.path.join(os.path.dirname(doc_path), folder)
            folder = os.path.normpath(folder)
            os.makedirs(folder, exist_ok=True)
            target = os.path.join(folder, f"{stem}.{ext}")
            if os.path.exists(target):
                os.remove(target)  # otherwise a failed export would leave the old file looking fresh
            err = doc.SaveAs3(target, 0, SW_SAVE_SILENT | SW_SAVE_COPY)
            if err != 0 or not os.path.exists(target):
                raise SWError(f"Export to {target} failed (SolidWorks error code {err})."
                              + (f" Already written: {', '.join(written)}." if written else ""))
            written.append(target)
        return "Exported:\n" + "\n".join(written)
    return await com_tool(work)


# -------------------------------- Selection -------------------------------- #
@mcp.tool()
async def select_plane(plane: Literal["front", "top", "right"]) -> str:
    """Selects one of the base planes: front (XY), top (XZ), right (YZ).
    Works with SolidWorks in any language."""
    def work():
        doc = active_doc(SW_DOC_PART)
        p = ref_planes(doc)[{"front": 0, "top": 1, "right": 2}[plane]]
        doc.ClearSelection2(True)
        if not p.Select2(False, 0):
            raise SWError("Could not select the plane.")
        return f"Selected plane '{call(p, 'Name')}'."
    return await com_tool(work)


@mcp.tool()
async def select_face_at_point(x_mm: float, y_mm: float, z_mm: float) -> str:
    """Selects the face of the part passing through the point (x, y, z) in mm, model coordinates.
    Use get_bounding_box to find the coordinates of the outer faces."""
    def work():
        doc = active_doc(SW_DOC_PART)
        doc.ClearSelection2(True)
        ok = doc.Extension.SelectByID2("", "FACE", x_mm * MM, y_mm * MM, z_mm * MM, False, 0, NOTHING, 0)
        if not ok:
            raise SWError("No face found at that point. The point must lie exactly on the face.")
        return f"Face selected at point ({x_mm}, {y_mm}, {z_mm}) mm."
    return await com_tool(work)


# --------------------------------- Sketches -------------------------------- #
@mcp.tool()
async def start_sketch() -> str:
    """Opens a new sketch on the selected plane or face."""
    def work():
        doc = active_doc(SW_DOC_PART)
        if in_sketch(doc):
            return "A sketch is already open: keep drawing or use end_sketch."
        if doc.SelectionManager.GetSelectedObjectCount2(-1) < 1:
            raise SWError("First select a plane (select_plane) or a face (select_face_at_point).")
        doc.SketchManager.InsertSketch(True)
        if not in_sketch(doc):
            raise SWError("The sketch did not open (is the selection a plane or a planar face?).")
        return "Sketch open. Coordinates are in mm in the sketch system (origin = origin of the plane)."
    return await com_tool(work)


@mcp.tool()
async def end_sketch() -> str:
    """Closes the open sketch without creating a feature."""
    def work():
        doc = active_doc(SW_DOC_PART)
        if not in_sketch(doc):
            return "No sketch is open."
        doc.SketchManager.AddToDB = False
        doc.SketchManager.InsertSketch(True)
        return "Sketch closed."
    return await com_tool(work)


@mcp.tool()
async def sketch_rectangle(width_mm: float, height_mm: float, center_x_mm: float = 0.0,
                           center_y_mm: float = 0.0) -> str:
    """Draws a rectangle in the open sketch, given centre and size (mm)."""
    def work():
        if width_mm <= 0 or height_mm <= 0:
            raise SWError("Width and height must be positive.")
        sm = require_sketch(active_doc(SW_DOC_PART))
        cx, cy = center_x_mm * MM, center_y_mm * MM
        segs = sm.CreateCenterRectangle(cx, cy, 0, cx + width_mm * MM / 2, cy + height_mm * MM / 2, 0)
        if not segs:
            raise SWError("Rectangle not created.")
        return f"Rectangle {width_mm} x {height_mm} mm centred at ({center_x_mm}, {center_y_mm})."
    return await com_tool(work)


@mcp.tool()
async def sketch_circle(diameter_mm: float, center_x_mm: float = 0.0, center_y_mm: float = 0.0) -> str:
    """Draws a circle in the open sketch, given centre and DIAMETER (mm)."""
    def work():
        if diameter_mm <= 0:
            raise SWError("The diameter must be positive.")
        sm = require_sketch(active_doc(SW_DOC_PART))
        seg = sm.CreateCircleByRadius(center_x_mm * MM, center_y_mm * MM, 0, diameter_mm * MM / 2)
        if seg is None:
            raise SWError("Circle not created.")
        return f"Circle diameter {diameter_mm} mm centred at ({center_x_mm}, {center_y_mm})."
    return await com_tool(work)


@mcp.tool()
async def sketch_line(x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float,
                      construction: bool = False) -> str:
    """Draws a line in the open sketch between two points (mm).
    construction=True creates a construction line (e.g. the axis of a revolve)."""
    def work():
        sm = require_sketch(active_doc(SW_DOC_PART))
        if construction:
            seg = sm.CreateCenterLine(x1_mm * MM, y1_mm * MM, 0, x2_mm * MM, y2_mm * MM, 0)
        else:
            seg = sm.CreateLine(x1_mm * MM, y1_mm * MM, 0, x2_mm * MM, y2_mm * MM, 0)
        if seg is None:
            raise SWError("Line not created (coincident points?).")
        return f"Line from ({x1_mm}, {y1_mm}) to ({x2_mm}, {y2_mm})."
    return await com_tool(work)


@mcp.tool()
async def sketch_polygon(sides: int, circumscribed_diameter_mm: float, center_x_mm: float = 0.0,
                         center_y_mm: float = 0.0, rotation_deg: float = 0.0) -> str:
    """Draws a regular polygon (e.g. hexagon: sides=6) inscribed in a circle of the given diameter (mm)."""
    def work():
        if sides < 3:
            raise SWError("At least 3 sides are needed.")
        sm = require_sketch(active_doc(SW_DOC_PART))
        r = circumscribed_diameter_mm * MM / 2
        cx, cy = center_x_mm * MM, center_y_mm * MM
        a = rotation_deg * DEG
        segs = sm.CreatePolygon(cx, cy, 0, cx + r * math.cos(a), cy + r * math.sin(a), 0, sides, True)
        if not segs:
            raise SWError("Polygon not created.")
        return f"Polygon with {sides} sides, diameter {circumscribed_diameter_mm} mm."
    return await com_tool(work)


@mcp.tool()
async def sketch_arc(center_x_mm: float, center_y_mm: float, start_x_mm: float, start_y_mm: float,
                     end_x_mm: float, end_y_mm: float, counterclockwise: bool = True) -> str:
    """Draws an arc given centre, start point and end point (mm)."""
    def work():
        sm = require_sketch(active_doc(SW_DOC_PART))
        seg = sm.CreateArc(center_x_mm * MM, center_y_mm * MM, 0, start_x_mm * MM, start_y_mm * MM, 0,
                           end_x_mm * MM, end_y_mm * MM, 0, 1 if counterclockwise else -1)
        if seg is None:
            raise SWError("Arc not created (start and end must be at the same distance from the centre).")
        return "Arc created."
    return await com_tool(work)


# --------------------------------- Features -------------------------------- #
END_CONDITIONS = {"blind": SW_END_BLIND, "through_all": SW_END_THROUGH_ALL, "midplane": SW_END_MIDPLANE}


@mcp.tool()
async def extrude(depth_mm: float, end_condition: Literal["blind", "midplane"] = "blind",
                  reverse: bool = False) -> str:
    """Extrudes the last sketch as a solid (closing it automatically if open).
    blind = given depth on one side; midplane = total depth split on both sides."""
    def work():
        if depth_mm <= 0:
            raise SWError("The depth must be positive.")
        doc = active_doc(SW_DOC_PART)
        name = close_sketch_and_select_it(doc)
        d = depth_mm * MM
        t1 = END_CONDITIONS[end_condition]
        feat = doc.FeatureManager.FeatureExtrusion3(
            True, False, reverse, t1, 0, d, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            True, True, True, 0, 0.0, False)
        if feat is None:
            raise SWError(f"Extrusion of '{name}' failed: the sketch must contain closed profiles "
                          "that do not intersect.")
        doc.ClearSelection2(True)
        rebuild(doc)
        return f"Extrusion '{call(feat, 'Name')}' created from '{name}' ({depth_mm} mm, {end_condition})."
    return await com_tool(work)


@mcp.tool()
async def cut_extrude(depth_mm: float = 0.0, through_all: bool = False, reverse: bool = False,
                      flip_side: bool = False) -> str:
    """Extruded cut (holes, pockets) with the last sketch. through_all=True goes right through
    (depth_mm ignored). reverse flips the direction; flip_side removes the material OUTSIDE the profile."""
    def work():
        if not through_all and depth_mm <= 0:
            raise SWError("Give depth_mm > 0 or through_all=True.")
        doc = active_doc(SW_DOC_PART)
        name = close_sketch_and_select_it(doc)
        t1 = SW_END_THROUGH_ALL if through_all else SW_END_BLIND
        d = 0.01 if through_all else depth_mm * MM
        fm = doc.FeatureManager
        common = (True, flip_side, reverse, t1, 0, d, 0.0, False, False, False, False, 0.0, 0.0,
                  False, False, False, False, False, True, True, True, True, False, 0, 0.0, False)
        try:
            feat = fm.FeatureCut4(*common, False)   # SolidWorks 2017+
        except pywintypes.com_error:
            feat = fm.FeatureCut3(*common)          # earlier releases
        if feat is None:
            raise SWError(f"Cut with '{name}' failed (does the sketch intersect the solid?).")
        doc.ClearSelection2(True)
        rebuild(doc)
        what = "through all" if through_all else f"{depth_mm} mm"
        return f"Cut '{call(feat, 'Name')}' created from '{name}' ({what})."
    return await com_tool(work)


@mcp.tool()
async def revolve(angle_deg: float = 360.0, cut: bool = False) -> str:
    """Revolves the last sketch around the construction line it contains
    (create it with sketch_line construction=True). cut=True removes material instead of adding it."""
    def work():
        if not 0 < angle_deg <= 360:
            raise SWError("The angle must be between 0 and 360 degrees.")
        doc = active_doc(SW_DOC_PART)
        name = close_sketch_and_select_it(doc)
        a = angle_deg * DEG
        feat = doc.FeatureManager.FeatureRevolve2(
            True, True, False, cut, False, False, 0, 0, a, 0.0,
            False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True)
        if feat is None:
            raise SWError(f"Revolve of '{name}' failed: it needs a closed profile and ONE construction "
                          "line as the axis, not crossing the profile.")
        doc.ClearSelection2(True)
        rebuild(doc)
        return f"Revolve '{call(feat, 'Name')}' created ({angle_deg} degrees)."
    return await com_tool(work)


# ------------------------------- Undo / view ------------------------------- #
@mcp.tool()
async def undo(steps: int = 1) -> str:
    """Undoes the last operations in SolidWorks."""
    def work():
        doc = active_doc()
        if in_sketch(doc):
            doc.SketchManager.AddToDB = False
        doc.EditUndo2(max(1, steps))
        rebuild(doc)
        return f"Undone {max(1, steps)} operation(s)."
    return await com_tool(work)


@mcp.tool()
async def delete_feature(name: str) -> str:
    """Deletes a feature from the tree by name (see get_feature_tree)."""
    def work():
        doc = active_doc(SW_DOC_PART)
        target = next((f for f in iter_features(doc) if call(f, "Name") == name), None)
        if target is None:
            raise SWError(f"Feature '{name}' not found.")
        doc.ClearSelection2(True)
        target.Select2(False, 0)
        if not doc.Extension.DeleteSelection2(1 | 2):  # also deletes absorbed and child features
            raise SWError(f"Could not delete '{name}'.")
        rebuild(doc)
        return f"Feature '{name}' deleted."
    return await com_tool(work)


@mcp.tool()
async def zoom_to_fit() -> str:
    """Fits the view to show the whole model."""
    def work():
        call(active_doc(), "ViewZoomtofit2")
        return "View fitted."
    return await com_tool(work)


# --------------------------- Free macro (last resort) ---------------------- #
MACRO_DOC = """Runs Python code that drives the SolidWorks COM API directly.
USE ONLY when no dedicated tool is enough (e.g. fillets, chamfers, patterns, assemblies).

Available in the code:
  sw       -> SldWorks.Application
  doc      -> active document (may be None)
  MM, DEG  -> 0.001 (mm->m) and pi/180 (deg->rad): the API works in METRES and RADIANS
  NOTHING  -> pass it instead of None for object parameters (e.g. the Callout of SelectByID2)
  call(obj, 'Method', *args) -> calls a method or reads a property without ambiguity;
             prefer it to obj.Method(...) for every argument-less method or property
  ref_planes(doc) -> [front, top, right] independent of the language
  iter_features(doc), VARIANT, pythoncom, math
Set the variable 'result' to return a text; print() output is returned too.
Example: doc.Extension.SelectByID2('', 'EDGE', 10*MM, 0, 5*MM, False, 0, NOTHING, 0)"""


def _macro(python_code: str) -> str:
    sw = get_sw()
    env: dict[str, Any] = {
        "__builtins__": __builtins__,
        "sw": sw, "doc": sw.ActiveDoc,
        "MM": MM, "DEG": DEG, "NOTHING": NOTHING, "VARIANT": VARIANT,
        "pythoncom": pythoncom, "math": math,
        "call": call, "ref_planes": ref_planes, "iter_features": iter_features,
        "result": None,
    }
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(python_code, "<macro>", "exec"), env)  # a single dict: no scoping problems
    except Exception:
        tb = traceback.format_exc(limit=3)
        return f"ERROR in the macro:\n{tb}\nOutput before the error:\n{out.getvalue()}"
    finally:
        try:
            d = sw.ActiveDoc
            if d is not None and in_sketch(d):
                d.SketchManager.AddToDB = False
        except Exception:
            pass
    printed = out.getvalue().strip()
    res = env.get("result")
    parts = [p for p in (printed, None if res is None else str(res)) if p]
    return "\n".join(parts) or "Macro executed."


async def execute_solidworks_macro(python_code: str) -> str:
    logger.info("Running Python macro (%d characters)", len(python_code))

    def work():
        return _macro(python_code)
    return await com_tool(work)


def register_macro_tool() -> None:
    """Exposes execute_solidworks_macro; called only when the configuration enables it."""
    mcp.add_tool(execute_solidworks_macro, description=MACRO_DOC)


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> None:
    global CONFIG
    parser = argparse.ArgumentParser(prog="solidworks-mcp", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--transport", choices=TRANSPORTS,
                        default=os.environ.get("SOLIDWORKS_MCP_TRANSPORT", "stdio"),
                        help="stdio (default) for MCP clients that start the server themselves; "
                             "sse / streamable-http to serve on http://127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SOLIDWORKS_MCP_PORT", DEFAULT_PORT)),
                        help=f"port for the HTTP transports (default {DEFAULT_PORT})")
    parser.add_argument("--config", help="path to the TOML configuration file")
    parser.add_argument("--version", action="version", version=f"solidworks-mcp {__version__}")
    args = parser.parse_args(argv)

    try:
        CONFIG = load_config(args.config)
    except ConfigError as e:
        logger.error("%s", e)
        sys.exit(2)
    logger.info("Configuration: %s", CONFIG.source)

    if CONFIG.allow_macro:
        register_macro_tool()
        logger.warning("execute_solidworks_macro is ENABLED: the assistant can run arbitrary Python code.")

    mcp.settings.port = args.port
    if args.transport == "stdio":
        logger.info("Starting SolidWorks MCP server on stdio.")
    else:
        path = "/sse" if args.transport == "sse" else "/mcp"
        logger.info("Starting SolidWorks MCP server: http://%s:%d%s (this PC only)", HOST, args.port, path)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
