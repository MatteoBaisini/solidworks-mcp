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
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, Callable, Literal, TypedDict
from pydantic import Field


class BoundingBox(TypedDict):
    min_mm: list[float]
    max_mm: list[float]
    size_mm: list[float]


class MassProperties(TypedDict):
    mass_kg: float
    volume_mm3: float
    volume_cm3: float
    surface_area_mm2: float
    center_of_mass_mm: list[float] | None


class DimensionItem(TypedDict):
    name: str
    value: float
    unit: str


class DimensionResult(TypedDict):
    target: str
    count: int
    dimensions: list[DimensionItem]


class MeasureResult(TypedDict, total=False):
    distance_mm: float
    center_distance_mm: float
    normal_distance_mm: float
    angle_deg: float
    length_mm: float
    diameter_mm: float
    radius_mm: float
    area_mm2: float
    perimeter_mm: float
    delta_mm: list[float]
    is_parallel: bool
    is_perpendicular: bool


try:
    import winreg
except ImportError:
    winreg = None

try:
    import pythoncom
    import pywintypes
    import win32com.client
    import win32com.client.dynamic
    from win32com.client import VARIANT
    # NB: the right class is dynamic.CDispatch (win32com.client.CDispatch is a different
    # subclass: objects created by dynamic.Dispatch are not instances of it).
    from win32com.client.dynamic import CDispatch
    COM_AVAILABLE = True
except ImportError:
    class _DummyComError(Exception):
        """Placeholder for pywintypes.com_error when pywin32 is not installed."""
        pass

    class _PywintypesDummy:
        com_error = _DummyComError

        @staticmethod
        def IID(val: str) -> str:
            return val

    class _PythoncomDummy:
        VT_DISPATCH = 9
        VT_ARRAY = 0x2000
        VT_R8 = 5
        VT_BYREF = 0x4000
        VT_BOOL = 11
        VT_I4 = 3
        VT_BSTR = 8
        IID_IDispatch = None

        @staticmethod
        def CoInitialize() -> None:
            pass

        @staticmethod
        def GetActiveObject(clsid: Any) -> Any:
            raise _DummyComError("COM not available")

    class _VariantDummy:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class CDispatch:
        pass

    pythoncom = _PythoncomDummy()
    pywintypes = _PywintypesDummy()
    win32com = None
    VARIANT = _VariantDummy
    COM_AVAILABLE = False

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
SW_MATERIAL_DATABASES = 28        # swUserPreferenceStringValue_e.swFileLocationsMaterialDatabases
SW_INPUT_DIM_ON_CREATE = 10       # swUserPreferenceToggle_e.swInputDimValOnCreate
SW_DOC_PART, SW_DOC_ASSEMBLY, SW_DOC_DRAWING = 1, 2, 3
SW_END_BLIND, SW_END_THROUGH_ALL, SW_END_UP_TO_SURFACE, SW_END_MIDPLANE = 0, 1, 4, 6
SW_SAVE_SILENT = 1                # swSaveAsOptions_Silent
SW_SAVE_COPY = 2                  # swSaveAsOptions_Copy (the open document is left untouched)
SW_FM_FILLET = 1                  # swFeatureNameID_e.swFmFillet
SW_CHAMFER_ANGLE_DISTANCE, SW_CHAMFER_DISTANCE_DISTANCE = 1, 2
SW_CHAMFER_TANGENT_PROPAGATION = 4
SW_THIS_CONFIGURATION = 1         # swInConfigurationOpts_e
SW_SET_VALUE_THIS_CONFIGURATION = 1                   # swSetValueInConfiguration_e
SW_DIM_ANGULAR, SW_DIM_INTEGER = 1, 2   # swDimensionParamType_e
SW_CUSTOM_INFO_TEXT = 30
SW_CUSTOM_PROPERTY_REPLACE = 2
SW_REF_PLANE_DISTANCE, SW_REF_PLANE_FLIP = 8, 256
DOC_TYPE_NAMES = {SW_DOC_PART: "Part", SW_DOC_ASSEMBLY: "Assembly", SW_DOC_DRAWING: "Drawing"}

SERVER_INSTRUCTIONS = """\
Server that drives SolidWorks on the user's PC.
Rules:
- Every tool dimension is in millimetres, every angle in degrees.
- Typical flow for a part: new_part -> select_plane -> start_sketch ->
  sketch_rectangle / sketch_circle / sketch_line / ... -> extrude (or cut_extrude for
  pockets) -> hole, fillet, chamfer, patterns -> set_material -> get_screenshot or
  get_bounding_box to check.
- To work on an existing face: select_face_at_point (mm coordinates of a point on
  the face) then start_sketch. start_sketch reports how the sketch axes map to the
  model; convert_point converts between model and sketch coordinates.
- Sketch tools take SKETCH coordinates (x, y); tools that pick faces, edges or
  vertices (fillet, chamfer, shell, hole, measure, ...) take MODEL coordinates [x, y, z]
  of a point lying exactly on the entity.
- Features are referred to by the names shown by get_feature_tree; every tool that
  creates one returns its name.
- The user may be working in SolidWorks too: call get_active_document_info before
  modifying, to be sure the active document is the one you expect.
- On error use undo, then retry. get_feature_tree shows what is in the model.
- export_document writes the exchange formats configured by the user (STEP by default).
"""

PROJECTS_INSTRUCTIONS = """\
- The user keeps every part in the projects folder: {root}
  Save each part as <project>/cad/<project>_v<N>_<description>.SLDPRT (lowercase, underscores),
  e.g. save_document("bracket/cad/bracket_v1_base.SLDPRT"): a relative path is relative to the
  projects folder and missing folders are created. A new version of a part gets the next number
  instead of overwriting the previous file. Keep a short README.md in the project folder with
  the main dimensions and the list of versions.
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
    if not COM_AVAILABLE:
        return None
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
    if winreg is None:
        return None
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
    if not COM_AVAILABLE:
        raise SWError(
            "SolidWorks COM API is not available on this platform (requires Windows with pywin32)."
        )
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


def resolve_save_path(path: str) -> str:
    """Full path for a save: relative paths go under the projects folder, where missing
    folders are created; anywhere else the folder must already exist."""
    root = CONFIG.projects_directory
    if not os.path.isabs(path):
        if not root:
            raise SWError("Give a full path: no projects folder is set in the configuration.")
        path = os.path.join(root, path)
    path = os.path.normpath(path)
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        inside = bool(root) and os.path.normcase(folder + os.sep).startswith(
            os.path.normcase(os.path.normpath(root) + os.sep))
        if not inside:
            raise SWError(f"The folder {folder} does not exist.")
        os.makedirs(folder)
    return path


def rebuild(doc: Any) -> None:
    try:
        doc.ForceRebuild3(False)
    except Exception:
        pass


def doubles(values: Any) -> VARIANT:
    """A SAFEARRAY of doubles, as the API wants for points and vectors."""
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def xyz(point: list[float], what: str = "point") -> tuple[float, float, float]:
    if len(point) != 3:
        raise SWError(f"Each {what} must be [x, y, z] in mm, got {point}.")
    return float(point[0]), float(point[1]), float(point[2])


ENTITY_NAMES = {"FACE": "face", "EDGE": "edge", "VERTEX": "vertex",
                "SKETCHSEGMENT": "sketch entity", "SKETCHPOINT": "sketch point"}


IID_ENTITY = pywintypes.IID("{83A33D65-27C5-11CE-BFD4-00400513BB57}")  # IEntity, which has Select4
PICK_TOLERANCE_MM = 0.05
BODY_LISTS = {"FACE": "GetFaces", "EDGE": "GetEdges", "VERTEX": "GetVertices"}


def pick(doc: Any, kind: str, point_mm: list[float]) -> Any:
    """The face / edge / vertex of the solid bodies nearest to a point (mm, model coordinates).

    Geometric search instead of SelectByID2, which misses entities hidden in the current view.
    """
    p = [v * MM for v in xyz(point_mm)]
    best, best_d = None, math.inf
    for body in doc.GetBodies2(0, True) or ():   # swSolidBody, visible only
        for ent in call(body, BODY_LISTS[kind]) or ():
            q = call(ent, "GetPoint") if kind == "VERTEX" else ent.GetClosestPointOn(*p)
            d = math.dist(p, q[:3])
            if d < best_d:
                best, best_d = ent, d
    if best is None or best_d > PICK_TOLERANCE_MM * MM:
        near = f" (the nearest is {best_d / MM:.3f} mm away)" if best is not None else ""
        raise SWError(f"No {ENTITY_NAMES[kind]} at ({point_mm[0]:g}, {point_mm[1]:g}, {point_mm[2]:g}) mm"
                      f"{near}: the point must lie exactly on it.")
    return best


def select_entity(doc: Any, entity: Any, append: bool, mark: int) -> None:
    data = call(doc.SelectionManager, "CreateSelectData")
    data.Mark = mark
    ent = win32com.client.dynamic.Dispatch(entity._oleobj_.QueryInterface(IID_ENTITY, pythoncom.IID_IDispatch))
    if not ent.Select4(append, data):
        raise SWError("SolidWorks refused the selection.")


def select_at(doc: Any, kind: str, point_mm: list[float], append: bool = True, mark: int = 0) -> None:
    """Selects the face / edge / vertex / sketch entity through a point (mm, model coordinates)."""
    if kind in BODY_LISTS:
        select_entity(doc, pick(doc, kind, point_mm), append, mark)
        return
    x, y, z = xyz(point_mm)
    if not doc.Extension.SelectByID2("", kind, x * MM, y * MM, z * MM, append, mark, NOTHING, 0):
        raise SWError(f"No {ENTITY_NAMES.get(kind, kind.lower())} found at ({x:g}, {y:g}, {z:g}) mm: "
                      "the point must lie exactly on it.")


def find_feature(doc: Any, name: str) -> Any:
    for f in iter_features(doc):
        if call(f, "Name") == name:
            return f
    raise SWError(f"Feature '{name}' not found (see get_feature_tree).")


def select_features(doc: Any, names: list[str], mark: int) -> None:
    try:
        for name in names:
            if not find_feature(doc, name).Select2(True, mark):
                raise SWError(f"Could not select '{name}'.")
    except SWError:
        doc.ClearSelection2(True)
        raise


def last_feature_name(doc: Any) -> str | None:
    name = None
    for f in iter_features(doc):
        name = call(f, "Name")
    return name


def new_feature(doc: Any, before: str | None, failure: str) -> Any:
    """The feature added after 'before', for API calls that do not return it."""
    last = None
    for f in iter_features(doc):
        last = f
    if last is None or call(last, "Name") == before:
        raise SWError(failure)
    return last


def leave_sketch(doc: Any) -> None:
    """Closes the open sketch, if any: features on faces and edges need it closed."""
    if in_sketch(doc):
        doc.SketchManager.AddToDB = False
        doc.SketchManager.InsertSketch(True)


def finish_feature(doc: Any, feat: Any, failure: str) -> str:
    """Name of a new feature, with a note if SolidWorks flags it; raises if it was not created
    or does not rebuild."""
    if feat is None:
        doc.ClearSelection2(True)
        raise SWError(failure)
    doc.ClearSelection2(True)
    rebuild(doc)
    name = call(feat, "Name")
    is_warning = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
    code = feat.GetErrorCode2(is_warning)   # swFeatureError_e
    if isinstance(code, tuple):
        code, is_warn = code[0], bool(code[1])
    else:
        is_warn = bool(getattr(is_warning, "value", False))
    if code and not is_warn:
        raise SWError(f"'{name}' was added but does not rebuild (SolidWorks error {code}). "
                      f"{failure} Remove it with undo or delete_feature.")
    if code:
        return f"{name} [warning {code}: it may have produced no geometry, check with get_screenshot]"
    return name


def plane_feature(doc: Any, plane: str) -> Any:
    """A base plane by role (front/top/right, any language) or any plane by its name."""
    roles = {"front": 0, "top": 1, "right": 2}
    if plane.lower() in roles:
        return ref_planes(doc)[roles[plane.lower()]]
    feat = find_feature(doc, plane)
    if call(feat, "GetTypeName2") != "RefPlane":
        raise SWError(f"'{plane}' is not a plane.")
    return feat


AXIS_PLANES = {"x": ("front", "top"), "y": ("front", "right"), "z": ("top", "right")}


def model_axis(doc: Any, axis: str) -> tuple[Any, bool]:
    """Reference axis along model X, Y or Z through the origin, created on first use.

    Returns the axis and whether it points towards the negative side of the model axis.
    """
    axis = axis.lower()
    if axis not in AXIS_PLANES:
        raise SWError("The axis must be x, y or z.")
    name = f"Axis {axis.upper()} (MCP)"
    feat = next((f for f in iter_features(doc) if call(f, "Name") == name), None)
    if feat is None:
        doc.ClearSelection2(True)
        for plane in AXIS_PLANES[axis]:
            plane_feature(doc, plane).Select2(True, 0)
        if not doc.InsertAxis2(True):
            raise SWError(f"Could not create the {axis.upper()} axis.")
        feat = new_feature(doc, None, f"Could not create the {axis.upper()} axis.")
        feat.Name = name
        doc.ClearSelection2(True)
        feat.Select2(False, 0)
        call(doc, "BlankRefGeom")  # hidden: it only serves as a direction
        doc.ClearSelection2(True)
    params = call(call(feat, "GetSpecificFeature2"), "GetRefAxisParams")
    index = "xyz".index(axis)
    return feat, params[3 + index] - params[index] < 0


# ------------------------- Sketch coordinate system ------------------------ #
def _sketch_transform(doc: Any) -> tuple[list[list[float]], list[float], float]:
    """Rotation rows, translation and scale of the open sketch's model-to-sketch transform.

    The API uses row vectors: sketch = model * R * scale + T. (Computed here because
    MathUtility.CreatePoint cannot be called through late binding.)
    """
    a = list(call(call(doc.SketchManager.ActiveSketch, "ModelToSketchTransform"), "ArrayData"))
    return [a[0:3], a[3:6], a[6:9]], a[9:12], a[12] or 1.0


def model_to_sketch(doc: Any, point_mm: list[float]) -> tuple[float, float, float]:
    """A model point (mm) in the coordinates of the open sketch: (x, y, distance from its plane)."""
    p = xyz(point_mm)
    r, t, s = _sketch_transform(doc)
    return tuple(sum(p[i] * r[i][j] for i in range(3)) * s + t[j] / MM for j in range(3))


def sketch_to_model(doc: Any, x_mm: float, y_mm: float) -> list[float]:
    """A point (mm) of the open sketch in model coordinates."""
    r, t, s = _sketch_transform(doc)
    v = [((x_mm, y_mm, 0.0)[j] - t[j] / MM) / s for j in range(3)]
    return [sum(v[j] * r[i][j] for j in range(3)) for i in range(3)]


def fmt_vec(values: Any) -> str:
    return "(" + ", ".join(f"{(round(v, 4) + 0.0):g}" for v in values) + ")"


def sketch_axes(doc: Any) -> str:
    o = sketch_to_model(doc, 0, 0)
    ux = [a - b for a, b in zip(sketch_to_model(doc, 1, 0), o)]
    uy = [a - b for a, b in zip(sketch_to_model(doc, 0, 1), o)]
    return (f"Sketch origin at model {fmt_vec(o)} mm; sketch X runs along model {fmt_vec(ux)}, "
            f"sketch Y along model {fmt_vec(uy)}.")


def select_sketch_entity(doc: Any, x_mm: float, y_mm: float, point: bool = False,
                         append: bool = True, mark: int = 0) -> None:
    """Selects the sketch segment (or sketch point) through (x, y) in sketch coordinates."""
    kind = "SKETCHPOINT" if point else "SKETCHSEGMENT"
    try:
        select_at(doc, kind, sketch_to_model(doc, x_mm, y_mm), append, mark)
    except SWError:
        raise SWError(f"No {ENTITY_NAMES[kind]} found at ({x_mm:g}, {y_mm:g}) in the sketch.") from None


# --------------------------------------------------------------------------- #
# Wrapper: every tool runs its logic in the COM thread.
# Exceptions are raised so that FastMCP sets isError: true on tool failures.
# --------------------------------------------------------------------------- #
async def com_tool(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return await run_com(fn, *args)
    except SWError:
        raise
    except pywintypes.com_error as e:
        logger.exception("COM error")
        if _is_disconnected(e):
            _sw_cache["app"] = None
        raise SWError(f"COM error from SolidWorks: {e}") from e
    except Exception as e:
        logger.exception("Unexpected error")
        raise SWError(f"{type(e).__name__}: {e}") from e


def _is_disconnected(e: Exception) -> bool:
    code = e.args[0] if getattr(e, "args", None) else None
    return code in (-2147417848, -2147023174, -2147023170)  # RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE, RPC_S_CALL_FAILED


# =========================================================================== #
#                                  TOOLS                                      #
# =========================================================================== #

# ------------------------------- Information ------------------------------- #
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def get_mass_properties() -> MassProperties:
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
        cg_mm = [round(float(cg[0] / MM), 3), round(float(cg[1] / MM), 3), round(float(cg[2] / MM), 3)] if cg else None
        return {
            "mass_kg": round(float(mass), 4),
            "volume_mm3": round(float(vol / MM ** 3), 2),
            "volume_cm3": round(float(vol * 1e6), 3),
            "surface_area_mm2": round(float(area / MM ** 2), 2),
            "center_of_mass_mm": cg_mm,
        }
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def get_bounding_box() -> BoundingBox:
    """Bounding box of the solid bodies of the active part: min/max on X, Y, Z and overall size, in mm."""
    def work():
        doc = active_doc(SW_DOC_PART)
        bodies = doc.GetBodies2(0, True) or ()   # solid bodies only: GetPartBox also counts sketches
        if not bodies:
            raise SWError("The part has no solid geometry yet.")
        lo, hi = [math.inf] * 3, [-math.inf] * 3
        for body in bodies:
            for axis in range(3):
                for sign in (-1, 1):
                    direction = [0.0, 0.0, 0.0]
                    direction[axis] = sign
                    _, *point = body.GetExtremePoint(*direction)   # exact, unlike GetBodyBox
                    lo[axis], hi[axis] = min(lo[axis], point[axis]), max(hi[axis], point[axis])
        x1, y1, z1, x2, y2, z2 = (v / MM for v in lo + hi)
        return {
            "min_mm": [round(float(x1), 3), round(float(y1), 3), round(float(z1), 3)],
            "max_mm": [round(float(x2), 3), round(float(y2), 3), round(float(z2), 3)],
            "size_mm": [round(float(x2 - x1), 3), round(float(y2 - y1), 3), round(float(z2 - z1), 3)],
        }
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def check_model() -> str:
    """Checks the active part for rebuild errors, multiple bodies, and geometry faults."""
    def work():
        doc = active_doc(SW_DOC_PART)
        rebuild(doc)
        features_with_errors = []
        features_with_warnings = []
        for feat in iter_features(doc):
            name = call(feat, "Name")
            try:
                is_warning = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
                code = feat.GetErrorCode2(is_warning)
                if isinstance(code, tuple):
                    code, is_warn = code[0], bool(code[1])
                else:
                    is_warn = bool(getattr(is_warning, "value", False))
                if code:
                    if is_warn:
                        features_with_warnings.append(f"'{name}' (code {code})")
                    else:
                        features_with_errors.append(f"'{name}' (code {code})")
            except Exception:
                pass

        all_bodies = doc.GetBodies2(0, False) or ()
        faulty_bodies = []
        for b in all_bodies:
            b_name = getattr(b, "Name", "Body")
            try:
                fault = call(b, "Check3")
                if fault is not None:
                    faulty_bodies.append(b_name)
            except Exception:
                pass

        lines = []
        if features_with_errors:
            lines.append(f"ERRORS ({len(features_with_errors)}): {', '.join(features_with_errors)}")
        if len(all_bodies) > 1:
            lines.append(f"MULTIPLE BODIES ({len(all_bodies)}): the model contains disjoint solid bodies (merge failed).")
        elif len(all_bodies) == 0:
            lines.append("NO BODIES: model has no solid geometry.")
        if faulty_bodies:
            lines.append(f"GEOMETRY FAULTS ({len(faulty_bodies)}): bodies with invalid geometry: {', '.join(faulty_bodies)}.")
        if features_with_warnings:
            lines.append(f"WARNINGS ({len(features_with_warnings)}): {', '.join(features_with_warnings)}")

        if not lines:
            return f"Model is healthy: {len(all_bodies)} solid body, 0 rebuild errors, 0 geometry faults."
        return "\n".join(lines)
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def get_screenshot(view: Literal["current", "isometric", "front", "top", "right"] = "isometric"):
    """PNG image of the active model, to check the result visually.
    Changes the view orientation (unless view='current') and zooms to fit; the model is not modified."""
    view_ids = {"isometric": 7, "front": 1, "top": 5, "right": 4}  # swStandardViews_e (language independent)

    def work():
        doc = active_doc()
        if view != "current":
            try:
                doc.ShowNamedView2("", view_ids[view])
            except Exception:
                pass
        call(doc, "ViewZoomtofit2")
        with tempfile.TemporaryDirectory(prefix="sw_mcp_", ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "snapshot.png")
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
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def save_document(path: str = "") -> str:
    """Saves the active document. Overwrites the target file if it exists.
    With 'path' (e.g. C:\\\\projects\\\\part.SLDPRT, .step, .stl, .pdf...)
    saves/exports to that file: the format is taken from the extension. When the user has set a
    projects folder, a relative path is relative to it and missing folders inside it are created."""
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
        target = resolve_save_path(path)
        err = doc.SaveAs3(target, 0, SW_SAVE_SILENT)
        if err != 0 or not os.path.exists(target):
            raise SWError(f"Save failed (SolidWorks error code {err}).")
        return f"Saved to {target}"
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def export_document(formats: list[str] | None = None, directory: str = "") -> str:
    """Exports the active document to exchange formats, next to the document or in 'directory'.
    Existing files with the same name are replaced.

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
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def select_plane(plane: str) -> str:
    """Selects a plane: one of the base planes "front" (XY), "top" (XZ), "right" (YZ), which
    works with SolidWorks in any language, or a plane by name (e.g. one made by create_plane)."""
    def work():
        doc = active_doc(SW_DOC_PART)
        p = plane_feature(doc, plane)
        doc.ClearSelection2(True)
        if not p.Select2(False, 0):
            raise SWError("Could not select the plane.")
        return f"Selected plane '{call(p, 'Name')}'."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def select_face_at_point(x_mm: float, y_mm: float, z_mm: float) -> str:
    """Selects the face of the part passing through the point (x, y, z) in mm, model coordinates.
    Use get_bounding_box to find the coordinates of the outer faces."""
    def work():
        doc = active_doc(SW_DOC_PART)
        doc.ClearSelection2(True)
        select_at(doc, "FACE", [x_mm, y_mm, z_mm], append=False)
        return f"Face selected at point ({x_mm}, {y_mm}, {z_mm}) mm."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def select_edge_at_point(x_mm: float, y_mm: float, z_mm: float, append: bool = False) -> str:
    """Selects the edge of the part passing through the point (x, y, z) in mm, model coordinates.
    append=True adds it to the current selection. Tools such as fillet and chamfer take the
    points directly, so this is mostly useful to check that a point really lies on an edge."""
    def work():
        doc = active_doc(SW_DOC_PART)
        if not append:
            doc.ClearSelection2(True)
        select_at(doc, "EDGE", [x_mm, y_mm, z_mm], append)
        return f"Edge selected at point ({x_mm}, {y_mm}, {z_mm}) mm."
    return await com_tool(work)


# --------------------------------- Sketches -------------------------------- #
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
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
        return "Sketch open. Coordinates are in mm in the sketch system. " + sketch_axes(doc)
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def convert_point(x_mm: float, y_mm: float, z_mm: float = 0.0,
                        to: Literal["sketch", "model"] = "sketch") -> str:
    """Converts a point between model coordinates and the coordinates of the open sketch.
    to="sketch": (x, y, z) is a model point, returns its sketch (x, y) and its distance from
    the sketch plane. to="model": (x, y) is a sketch point (z ignored), returns the model point."""
    def work():
        doc = active_doc(SW_DOC_PART)
        if not in_sketch(doc):
            raise SWError("No sketch is open.")
        if to == "model":
            return f"Sketch ({x_mm:g}, {y_mm:g}) = model {fmt_vec(sketch_to_model(doc, x_mm, y_mm))} mm."
        sx, sy, sz = model_to_sketch(doc, [x_mm, y_mm, z_mm])
        return (f"Model ({x_mm:g}, {y_mm:g}, {z_mm:g}) = sketch ({round(sx, 4) + 0.0:g}, "
                f"{round(sy, 4) + 0.0:g}), {round(sz, 4) + 0.0:g} mm from the sketch plane.")
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_rectangle(width_mm: Annotated[float, Field(gt=0, description="Rectangle width in mm")],
                           height_mm: Annotated[float, Field(gt=0, description="Rectangle height in mm")],
                           center_x_mm: float = 0.0, center_y_mm: float = 0.0) -> str:
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_circle(diameter_mm: Annotated[float, Field(gt=0, description="Circle diameter in mm")],
                        center_x_mm: float = 0.0, center_y_mm: float = 0.0) -> str:
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_polygon(sides: Annotated[int, Field(ge=3, description="Number of sides (>= 3)")],
                         circumscribed_diameter_mm: Annotated[float, Field(gt=0, description="Inscribed circle diameter in mm")],
                         center_x_mm: float = 0.0, center_y_mm: float = 0.0, rotation_deg: float = 0.0) -> str:
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_slot(x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float,
                      width_mm: Annotated[float, Field(gt=0, description="Slot width/diameter in mm")]) -> str:
    """Draws a straight slot (two half circles joined by lines) in the open sketch, given the
    centres of its two ends and its width (mm)."""
    def work():
        if width_mm <= 0:
            raise SWError("The width must be positive.")
        if (x1_mm, y1_mm) == (x2_mm, y2_mm):
            raise SWError("The two centres must be different.")
        sm = require_sketch(active_doc(SW_DOC_PART))
        slot = sm.CreateSketchSlot(0, 0, width_mm * MM, x1_mm * MM, y1_mm * MM, 0, x2_mm * MM, y2_mm * MM, 0,
                                   0, 0, 0, 1, False)
        if slot is None:
            raise SWError("Slot not created.")
        return f"Slot from ({x1_mm}, {y1_mm}) to ({x2_mm}, {y2_mm}), width {width_mm} mm."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_ellipse(major_diameter_mm: Annotated[float, Field(gt=0, description="Major diameter in mm")],
                         minor_diameter_mm: Annotated[float, Field(gt=0, description="Minor diameter in mm")],
                         center_x_mm: float = 0.0, center_y_mm: float = 0.0, rotation_deg: float = 0.0) -> str:
    """Draws an ellipse in the open sketch, given its two diameters (mm). rotation_deg turns the
    major axis counterclockwise from the sketch X axis."""
    def work():
        if minor_diameter_mm <= 0 or major_diameter_mm < minor_diameter_mm:
            raise SWError("Diameters must be positive, the major one at least as large as the minor one.")
        sm = require_sketch(active_doc(SW_DOC_PART))
        cx, cy, a = center_x_mm * MM, center_y_mm * MM, rotation_deg * DEG
        ra, rb = major_diameter_mm * MM / 2, minor_diameter_mm * MM / 2
        seg = sm.CreateEllipse(cx, cy, 0, cx + ra * math.cos(a), cy + ra * math.sin(a), 0,
                               cx - rb * math.sin(a), cy + rb * math.cos(a), 0)
        if seg is None:
            raise SWError("Ellipse not created.")
        return f"Ellipse {major_diameter_mm} x {minor_diameter_mm} mm centred at ({center_x_mm}, {center_y_mm})."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_spline(points: list[list[float]]) -> str:
    """Draws a smooth spline through the points [[x, y], ...] of the open sketch (mm, at least 2).
    Repeat the first point at the end to close it."""
    def work():
        if len(points) < 2 or any(len(p) != 2 for p in points):
            raise SWError("Give at least two points, each as [x, y].")
        sm = require_sketch(active_doc(SW_DOC_PART))
        seg = sm.CreateSpline2(doubles(v for x, y in points for v in (x * MM, y * MM, 0.0)), True)
        if seg is None:
            raise SWError("Spline not created.")
        return f"Spline through {len(points)} points."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_fillet(radius_mm: Annotated[float, Field(gt=0, description="Fillet radius in mm")],
                        corner_x_mm: float, corner_y_mm: float) -> str:
    """Rounds the corner of the open sketch at (x, y) (the point where two entities meet) with the
    given radius (mm)."""
    def work():
        if radius_mm <= 0:
            raise SWError("The radius must be positive.")
        doc = active_doc(SW_DOC_PART)
        sm = require_sketch(doc)
        doc.ClearSelection2(True)
        select_sketch_entity(doc, corner_x_mm, corner_y_mm, point=True)
        seg = sm.CreateFillet(radius_mm * MM, 1)  # swConstrainedCornerKeepGeometry
        doc.ClearSelection2(True)
        if seg is None:
            raise SWError("Fillet not created (radius too large for the entities at that corner?).")
        return f"Corner at ({corner_x_mm}, {corner_y_mm}) rounded with radius {radius_mm} mm."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_offset(distance_mm: Annotated[float, Field(gt=0, description="Offset distance in mm")],
                        x_mm: float, y_mm: float, both_sides: bool = False, reverse: bool = False) -> str:
    """Offsets the chain of connected sketch entities passing through (x, y) by distance_mm.
    reverse=True offsets to the other side; both_sides=True offsets to both sides."""
    def work():
        if distance_mm <= 0:
            raise SWError("The distance must be positive.")
        doc = active_doc(SW_DOC_PART)
        sm = require_sketch(doc)
        doc.ClearSelection2(True)
        select_sketch_entity(doc, x_mm, y_mm)
        d = -distance_mm * MM if reverse else distance_mm * MM
        ok = sm.SketchOffset2(d, both_sides, True, 0, 0, False)
        doc.ClearSelection2(True)
        if not ok:
            raise SWError("Offset failed (distance too large for the geometry?).")
        return f"Offset of {distance_mm} mm created."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def convert_entities(face_points: list[list[float]] | None = None,
                           edge_points: list[list[float]] | None = None) -> str:
    """Projects edges of the model into the open sketch (Convert Entities): all the edges of each
    face with a point in face_points, and each edge with a point in edge_points ([x, y, z] in mm,
    model coordinates). The new sketch entities follow the model when it changes."""
    def work():
        if not face_points and not edge_points:
            raise SWError("Give at least one face or edge point.")
        doc = active_doc(SW_DOC_PART)
        sm = require_sketch(doc)
        doc.ClearSelection2(True)
        for p in face_points or []:
            select_at(doc, "FACE", p)
        for p in edge_points or []:
            select_at(doc, "EDGE", p)
        ok = sm.SketchUseEdge3(False, False)
        doc.ClearSelection2(True)
        if not ok:
            raise SWError("Convert Entities failed.")
        return "Edges converted into the sketch."
    return await com_tool(work)


AUTODIM_STATUS = {
    6: "the sketch is empty", 7: "the sketch is over-defined", 8: "there is nothing to define",
    17: "the algorithm failed", 18: "no solution was found",
}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def fully_define_sketch() -> str:
    """Adds the relations and dimensions (measured from the sketch origin) that the open sketch needs
    to be fully defined, so it keeps its shape and can be edited later through its dimensions
    (get_dimensions / set_dimension)."""
    def work():
        doc = active_doc(SW_DOC_PART)
        sm = require_sketch(doc)
        doc.ClearSelection2(True)
        status = sm.FullyDefineSketch(True, True, 1023, True, 1, NOTHING, 1, NOTHING, -1, -1)
        if status != 0:
            raise SWError(f"Could not fully define the sketch: {AUTODIM_STATUS.get(status, f'code {status}')}.")
        return "Sketch fully defined."
    return await com_tool(work)


@contextlib.contextmanager
def no_dimension_prompt():
    """Stops SolidWorks from opening the Modify dialog when a dimension is added."""
    sw = get_sw()
    before = sw.GetUserPreferenceToggle(SW_INPUT_DIM_ON_CREATE)
    sw.SetUserPreferenceToggle(SW_INPUT_DIM_ON_CREATE, False)
    try:
        yield
    finally:
        sw.SetUserPreferenceToggle(SW_INPUT_DIM_ON_CREATE, before)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def add_sketch_dimension(x_mm: float, y_mm: float, value: float, x2_mm: float | None = None,
                               y2_mm: float | None = None, text_x_mm: float | None = None,
                               text_y_mm: float | None = None) -> str:
    """Adds a driving dimension to the open sketch and sets it to 'value' (mm, or degrees for angles).

    (x, y): a point on the entity (sketch coordinates). One entity: length of a line, diameter
    of a circle, radius of an arc. With (x2, y2) on a second entity: distance between them, or
    the angle between two lines. (text_x, text_y) places the text (default: next to the entity)."""
    def work():
        doc = active_doc(SW_DOC_PART)
        require_sketch(doc)
        doc.ClearSelection2(True)
        select_sketch_entity(doc, x_mm, y_mm)
        if x2_mm is not None and y2_mm is not None:
            select_sketch_entity(doc, x2_mm, y2_mm)
        tx = text_x_mm if text_x_mm is not None else x_mm + 5
        ty = text_y_mm if text_y_mm is not None else y_mm + 5
        px, py, pz = (v * MM for v in sketch_to_model(doc, tx, ty))
        with no_dimension_prompt():
            disp = doc.AddDimension2(px, py, pz)
        doc.ClearSelection2(True)
        if disp is None:
            raise SWError("Dimension not added (is that already fully dimensioned?).")
        dim = disp.GetDimension2(0)
        scale, unit = dimension_unit(dim)
        dim.SetSystemValue3(value * scale, SW_SET_VALUE_THIS_CONFIGURATION, None)
        name = "@".join(call(dim, "FullName").split("@")[:2])
        return f"Dimension '{name}' = {value:g}{unit}."
    return await com_tool(work)


SKETCH_RELATIONS = {
    "horizontal": "sgHORIZONTAL2D", "vertical": "sgVERTICAL2D", "coincident": "sgCOINCIDENT",
    "equal": "sgSAMELENGTH", "parallel": "sgPARALLEL", "perpendicular": "sgPERPENDICULAR",
    "tangent": "sgTANGENT", "concentric": "sgCONCENTRIC", "collinear": "sgCOLINEAR",
    "midpoint": "sgATMIDDLE", "fixed": "sgFIXED",
}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def add_sketch_relation(relation: Literal["horizontal", "vertical", "coincident", "equal", "parallel",
                                                "perpendicular", "tangent", "concentric", "collinear",
                                                "midpoint", "fixed"],
                              points: list[list[float]], vertex: list[bool] | None = None) -> str:
    """Adds a geometric relation between sketch entities of the open sketch.

    points: [[x, y], ...] one point (sketch coordinates) on each entity involved.
    vertex: optional, one flag per point; true picks the sketch POINT there (an end point or
    centre) instead of the line/arc, as needed by coincident and midpoint (point first)."""
    def work():
        if not points or any(len(p) != 2 for p in points):
            raise SWError("Give one [x, y] point per entity.")
        flags = vertex or [False] * len(points)
        if len(flags) != len(points):
            raise SWError("'vertex' must have one flag per point.")
        doc = active_doc(SW_DOC_PART)
        require_sketch(doc)
        doc.ClearSelection2(True)
        for (x, y), is_point in zip(points, flags):
            select_sketch_entity(doc, x, y, point=is_point)
        doc.SketchAddConstraints(SKETCH_RELATIONS[relation])
        doc.ClearSelection2(True)
        return f"Relation '{relation}' added between {len(points)} entities."
    return await com_tool(work)


# --------------------------------- Features -------------------------------- #
END_CONDITIONS = {"blind": SW_END_BLIND, "through_all": SW_END_THROUGH_ALL, "midplane": SW_END_MIDPLANE,
                  "up_to_surface": SW_END_UP_TO_SURFACE}


def extrusion_ends(depth_mm: float, end_condition: str, depth2_mm: float,
                   up_to_point: list[float] | None, doc: Any) -> tuple[bool, int, int, float, float]:
    """Validates the end conditions of an extrusion and selects the up-to face.

    Returns (single_direction, T1, T2, D1, D2) with depths in metres.
    """
    if end_condition in ("blind", "midplane") and depth_mm <= 0:
        raise SWError("The depth must be positive.")
    if depth2_mm < 0:
        raise SWError("depth2_mm cannot be negative.")
    if depth2_mm and end_condition == "midplane":
        raise SWError("A second direction cannot be combined with midplane.")
    if end_condition == "up_to_surface":
        if not up_to_point:
            raise SWError("up_to_surface needs up_to_point: a point [x, y, z] on the face to reach.")
        select_at(doc, "FACE", up_to_point, append=True, mark=1)
    d1 = depth_mm * MM if end_condition in ("blind", "midplane") else 0.01
    return (not depth2_mm, END_CONDITIONS[end_condition], SW_END_BLIND, d1, depth2_mm * MM)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def extrude(depth_mm: Annotated[float, Field(ge=0, description="Extrusion depth in mm (blind or midplane)")] = 0.0,
                  end_condition: Literal["blind", "midplane", "through_all", "up_to_surface"] = "blind",
                  reverse: bool = False,
                  depth2_mm: Annotated[float, Field(ge=0, description="Second direction depth in mm")] = 0.0,
                  draft_angle_deg: Annotated[float, Field(ge=0, lt=90, description="Draft angle in degrees (0 to <90)")] = 0.0,
                  draft_outward: bool = False, up_to_point: list[float] | None = None,
                  thin_wall_mm: Annotated[float, Field(ge=0, description="Thin wall thickness in mm (0 for solid)")] = 0.0,
                  merge: bool = True) -> str:
    """Extrudes the last sketch as a solid (closing it automatically if open).

    end_condition: blind = depth_mm on one side; midplane = depth_mm split on both sides;
      through_all = through the existing solid; up_to_surface = up to the face through
      up_to_point ([x, y, z] in mm, model coordinates).
    depth2_mm > 0 also extrudes that far in the opposite direction (not with midplane).
    draft_angle_deg tapers the sides (inwards, or outwards with draft_outward=True).
    thin_wall_mm > 0 makes a thin wall of that thickness along the profile instead of filling
      it; open profiles (e.g. a chain of lines) are then allowed.
    merge=False keeps the result as a separate body."""
    def work():
        if thin_wall_mm < 0 or not 0 <= draft_angle_deg < 90:
            raise SWError("thin_wall_mm cannot be negative; the draft angle must be between 0 and 90 degrees.")
        doc = active_doc(SW_DOC_PART)
        name = close_sketch_and_select_it(doc)
        sd, t1, t2, d1, d2 = extrusion_ends(depth_mm, end_condition, depth2_mm, up_to_point, doc)
        draft = draft_angle_deg > 0
        dang = draft_angle_deg * DEG
        ends = (sd, False, reverse, t1, t2, d1, d2, draft, draft and not sd, draft_outward, draft_outward,
                dang, dang, False, False, False, False, merge)
        fm = doc.FeatureManager
        if thin_wall_mm:
            feat = fm.FeatureExtrusionThin2(*ends, thin_wall_mm * MM, 0.0, 0.0, 0, 0, False, 0.0,
                                            True, True, 0, 0.0, False)
        else:
            feat = fm.FeatureExtrusion3(*ends, True, True, 0, 0.0, False)
        feat_name = finish_feature(doc, feat, f"Extrusion of '{name}' failed: the sketch must contain closed "
                                              "profiles that do not intersect (or use thin_wall_mm), and "
                                              "through_all / up_to_surface need a solid to reach.")
        what = f"{depth_mm} mm, {end_condition}" if end_condition in ("blind", "midplane") else end_condition
        extra = (f", {depth2_mm} mm the other way" if depth2_mm else "") + \
                (f", draft {draft_angle_deg} deg" if draft else "") + \
                (f", thin wall {thin_wall_mm} mm" if thin_wall_mm else "")
        return f"Extrusion '{feat_name}' created from '{name}' ({what}{extra})."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def cut_extrude(depth_mm: Annotated[float, Field(ge=0, description="Cut depth in mm (blind)")] = 0.0,
                      through_all: bool = False, reverse: bool = False,
                      flip_side: bool = False,
                      depth2_mm: Annotated[float, Field(ge=0, description="Second direction cut depth in mm")] = 0.0,
                      draft_angle_deg: Annotated[float, Field(ge=0, lt=90, description="Draft angle in degrees (0 to <90)")] = 0.0,
                      draft_outward: bool = False, up_to_point: list[float] | None = None) -> str:
    """Extruded cut (holes, pockets) with the last sketch. through_all=True goes right through
    (depth_mm ignored). reverse flips the direction; flip_side removes the material OUTSIDE the profile.
    up_to_point ([x, y, z] in mm on a face) cuts up to that face instead.
    depth2_mm > 0 also cuts that far in the opposite direction.
    draft_angle_deg tapers the walls (inwards, or outwards with draft_outward=True)."""
    def work():
        if not 0 <= draft_angle_deg < 90:
            raise SWError("The draft angle must be between 0 and 90 degrees.")
        end = "up_to_surface" if up_to_point else "through_all" if through_all else "blind"
        if end == "blind" and depth_mm <= 0:
            raise SWError("Give depth_mm > 0, through_all=True or up_to_point.")
        doc = active_doc(SW_DOC_PART)
        name = close_sketch_and_select_it(doc)
        sd, t1, t2, d1, d2 = extrusion_ends(depth_mm, end, depth2_mm, up_to_point, doc)
        draft = draft_angle_deg > 0
        dang = draft_angle_deg * DEG
        fm = doc.FeatureManager
        common = (sd, flip_side, reverse, t1, t2, d1, d2, draft, draft and not sd, draft_outward, draft_outward,
                  dang, dang, False, False, False, False, False, True, True, True, True, False, 0, 0.0, False)
        try:
            feat = fm.FeatureCut4(*common, False)   # SolidWorks 2017+
        except pywintypes.com_error:
            feat = fm.FeatureCut3(*common)          # earlier releases
        feat_name = finish_feature(doc, feat, f"Cut with '{name}' failed (does the sketch intersect the solid?).")
        what = {"through_all": "through all", "up_to_surface": "up to the face"}.get(end, f"{depth_mm} mm")
        extra = (f", {depth2_mm} mm the other way" if depth2_mm else "") + \
                (f", draft {draft_angle_deg} deg" if draft else "")
        return f"Cut '{feat_name}' created from '{name}' ({what}{extra})."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def revolve(angle_deg: Annotated[float, Field(gt=0, le=360, description="Revolve angle in degrees (0 < angle <= 360)")] = 360.0,
                  cut: bool = False) -> str:
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
        feat_name = finish_feature(
            doc, feat,
            f"Revolve of '{name}' failed: it needs a closed profile and ONE construction "
            "line as the axis, not crossing the profile.")
        return f"Revolve '{feat_name}' created ({angle_deg} degrees)."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def rib(thickness_mm: Annotated[float, Field(gt=0, description="Rib thickness in mm")],
              direction: Literal["parallel", "normal"] = "parallel",
              one_side: bool = False, flip_material: bool = False,
              draft_angle_deg: Annotated[float, Field(ge=0, lt=90, description="Draft angle in degrees (0 to <90)")] = 0.0) -> str:
    """Rib from the last sketch: usually an open line between walls of the part, drawn on a plane
    through the middle of the rib. thickness_mm is the total thickness, centred on the sketch
    unless one_side=True. direction: parallel = material grows in the sketch plane, normal =
    perpendicular to it. flip_material flips the side towards which material is added if the
    rib does not reach the part."""
    def work():
        if thickness_mm <= 0 or not 0 <= draft_angle_deg < 90:
            raise SWError("The thickness must be positive and the draft angle between 0 and 90 degrees.")
        doc = active_doc(SW_DOC_PART)
        name = close_sketch_and_select_it(doc)
        before = last_feature_name(doc)
        doc.FeatureManager.InsertRib(not one_side, False, thickness_mm * MM, 0, flip_material,
                                     draft_angle_deg > 0, False, draft_angle_deg * DEG,
                                     direction == "normal", False)
        feat = new_feature(doc, before, f"Rib from '{name}' failed: the sketch must reach the walls of "
                                        "the part (try flip_material or the other direction).")
        return f"Rib '{finish_feature(doc, feat, '')}' created from '{name}' ({thickness_mm} mm)."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sweep(path_sketch: str, profile_sketch: str = "",
                circle_diameter_mm: Annotated[float, Field(ge=0, description="Diameter in mm for circular sweep profile (0 if using profile_sketch)")] = 0.0,
                cut: bool = False) -> str:
    """Sweeps a profile along a path (pipes, springs, handles, grooves).

    path_sketch: name of the sketch with the path (open or closed curve).
    profile_sketch: name of the closed profile sketch, drawn on a plane crossing the start of the
      path; or leave it empty and give circle_diameter_mm for a round section.
    cut=True removes material instead of adding it. Close the sketches first (end_sketch)."""
    def work():
        if not profile_sketch and circle_diameter_mm <= 0:
            raise SWError("Give profile_sketch or circle_diameter_mm.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        circular = not profile_sketch
        if not circular:
            select_features(doc, [profile_sketch], 1)
        select_features(doc, [path_sketch], 4)
        fm, dia = doc.FeatureManager, circle_diameter_mm * MM
        if cut:
            feat = fm.InsertCutSwept5(False, True, 0, False, False, 0, 0, False, 0.0, 0.0, 0, 0, True, True, 0.0,
                                      True, True, True, False, circular, dia, 0)
        else:
            feat = fm.InsertProtrusionSwept4(False, True, 0, False, False, 0, 0, False, 0.0, 0.0, 0, 0, True, True,
                                             True, 0.0, True, circular, dia, 0)
        what = "Swept cut" if cut else "Sweep"
        feat_name = finish_feature(doc, feat, f"{what} failed: the profile must be closed and cross the start "
                                              "of the path, and the path must not make the section "
                                              "intersect itself.")
        return f"{what} '{feat_name}' created along '{path_sketch}'."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def loft(profile_sketches: list[str], guide_sketches: list[str] | None = None, cut: bool = False) -> str:
    """Loft through two or more closed profiles drawn on different planes (see create_plane),
    in order. guide_sketches: optional guide curves touching every profile. cut=True removes
    material instead of adding it. Close the sketches first (end_sketch)."""
    def work():
        if len(profile_sketches) < 2:
            raise SWError("A loft needs at least two profiles.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        select_features(doc, profile_sketches, 1)
        select_features(doc, guide_sketches or [], 2)
        fm = doc.FeatureManager
        if cut:
            feat = fm.InsertCutBlend(False, False, False, 1.0, 0, 0, False, 0.0, 0.0, 0, True, True)
        else:
            feat = fm.InsertProtrusionBlend2(False, False, False, 1.0, 0, 0, 1.0, 1.0, False, False, False,
                                             0.0, 0.0, 0, True, True, True, 0)
        what = "Lofted cut" if cut else "Loft"
        feat_name = finish_feature(doc, feat, f"{what} failed: profiles must be closed, on different planes, "
                                              "and not intersect each other.")
        return f"{what} '{feat_name}' created through {', '.join(profile_sketches)}."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def create_plane(offset_mm: Annotated[float, Field(gt=0, description="Offset distance in mm")],
                       reference: str = "front", face_point: list[float] | None = None,
                       flip: bool = False) -> str:
    """Creates a reference plane parallel to another one, offset_mm away (e.g. for loft profiles).
    reference: "front", "top", "right" or the name of a plane; or give face_point ([x, y, z] mm on a
    planar face) to offset from that face. flip=True puts it on the other side. Returns its name,
    to use with select_plane."""
    def work():
        if offset_mm <= 0:
            raise SWError("The offset must be positive (use flip for the other side).")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        if face_point:
            select_at(doc, "FACE", face_point, append=False)
        elif not plane_feature(doc, reference).Select2(False, 0):
            raise SWError(f"Could not select '{reference}'.")
        flags = SW_REF_PLANE_DISTANCE | (SW_REF_PLANE_FLIP if flip else 0)
        feat = doc.FeatureManager.InsertRefPlane(flags, offset_mm * MM, 0, 0.0, 0, 0.0)
        name = finish_feature(doc, feat, "Plane not created (is the face planar?).")
        return f"Plane '{name}' created {offset_mm} mm from {'the face' if face_point else reference}."
    return await com_tool(work)


# ------------------------- Fillets, patterns, holes ------------------------ #
def select_edges_and_faces(doc: Any, edge_points: list[list[float]] | None,
                           face_points: list[list[float]] | None, mark: int) -> None:
    if not edge_points and not face_points:
        raise SWError("Give at least one point in edge_points or face_points.")
    leave_sketch(doc)
    doc.ClearSelection2(True)
    for p in edge_points or []:
        select_at(doc, "EDGE", p, mark=mark)
    for p in face_points or []:
        select_at(doc, "FACE", p, mark=mark)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def fillet(radius_mm: Annotated[float, Field(gt=0, description="Fillet radius in mm")],
                 edge_points: list[list[float]] | None = None,
                 face_points: list[list[float]] | None = None) -> str:
    """Rounds edges with a constant radius. edge_points: a point on each edge ([x, y, z] in mm,
    model coordinates); face_points: a point on each face whose edges are all rounded. Edges
    tangent to the chosen ones are rounded too."""
    def work():
        if radius_mm <= 0:
            raise SWError("The radius must be positive.")
        doc = active_doc(SW_DOC_PART)
        select_edges_and_faces(doc, edge_points, face_points, mark=1)
        fm = doc.FeatureManager
        data = fm.CreateDefinition(SW_FM_FILLET)
        data.Initialize(0)  # swConstRadiusFillet
        data.DefaultRadius = radius_mm * MM
        feat = fm.CreateFeature(data)
        name = finish_feature(doc, feat, "Fillet failed (radius too large for the geometry?).")
        return f"Fillet '{name}' created (radius {radius_mm} mm)."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def chamfer(distance_mm: Annotated[float, Field(gt=0, description="Chamfer distance in mm")],
                  edge_points: list[list[float]] | None = None,
                  face_points: list[list[float]] | None = None,
                  angle_deg: Annotated[float, Field(gt=0, lt=90, description="Chamfer angle in degrees (0 < angle < 90)")] = 45.0,
                  distance2_mm: Annotated[float, Field(ge=0, description="Second chamfer distance in mm")] = 0.0,
                  flip: bool = False) -> str:
    """Bevels edges. edge_points / face_points as for fillet.
    Default: distance_mm at angle_deg (45 = equal sides). With distance2_mm > 0 the two sides are
    distance_mm and distance2_mm (angle ignored); flip swaps the sides of an unequal chamfer."""
    def work():
        if distance_mm <= 0 or distance2_mm < 0 or not 0 < angle_deg < 90:
            raise SWError("Distances must be positive and the angle between 0 and 90 degrees.")
        doc = active_doc(SW_DOC_PART)
        select_edges_and_faces(doc, edge_points, face_points, mark=0)
        options = SW_CHAMFER_TANGENT_PROPAGATION | (1 if flip else 0)
        kind = SW_CHAMFER_DISTANCE_DISTANCE if distance2_mm else SW_CHAMFER_ANGLE_DISTANCE
        feat = doc.FeatureManager.InsertFeatureChamfer(options, kind, distance_mm * MM, angle_deg * DEG,
                                                       distance2_mm * MM, 0.0, 0.0, 0.0)
        name = finish_feature(doc, feat, "Chamfer failed (distance too large for the geometry?).")
        size = f"{distance_mm} x {distance2_mm} mm" if distance2_mm else f"{distance_mm} mm at {angle_deg} deg"
        return f"Chamfer '{name}' created ({size})."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def shell(thickness_mm: Annotated[float, Field(gt=0, description="Wall thickness in mm")],
                open_face_points: list[list[float]] | None = None,
                outward: bool = False) -> str:
    """Hollows the part leaving walls thickness_mm thick. open_face_points: a point on each face to
    remove ([x, y, z] in mm), e.g. the top of a box; none gives a closed hollow part.
    outward=True adds the walls outside the current surfaces."""
    def work():
        if thickness_mm <= 0:
            raise SWError("The thickness must be positive.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        for p in open_face_points or []:
            select_at(doc, "FACE", p)
        before = last_feature_name(doc)
        doc.InsertFeatureShell(thickness_mm * MM, outward)
        feat = new_feature(doc, before, "Shell failed (walls too thick, or a fillet radius smaller than "
                                        "the thickness?).")
        return f"Shell '{finish_feature(doc, feat, '')}' created ({thickness_mm} mm walls)."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def linear_pattern(features: list[str], direction: Literal["x", "y", "z"],
                         count: Annotated[int, Field(ge=2, description="Number of copies along direction 1 (including original, >= 2)")],
                         spacing_mm: Annotated[float, Field(gt=0, description="Spacing between copies in mm")],
                         reverse: bool = False, direction2: Literal["", "x", "y", "z"] = "",
                         count2: Annotated[int, Field(ge=1, description="Number of copies along direction 2 (>= 1)")] = 1,
                         spacing2_mm: Annotated[float, Field(ge=0, description="Spacing between copies in direction 2 in mm")] = 0.0,
                         reverse2: bool = False) -> str:
    """Repeats features (names from get_feature_tree) in a row along a model axis: count copies in
    total (original included), spacing_mm apart, towards + (or - with reverse).
    direction2/count2/spacing2_mm add a second direction for a grid."""
    def work():
        if count < 2 or spacing_mm <= 0:
            raise SWError("count must be at least 2 and spacing_mm positive.")
        if direction2 and (count2 < 2 or spacing2_mm <= 0 or direction2 == direction):
            raise SWError("The second direction needs another axis, count2 >= 2 and spacing2_mm > 0.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        axis1, negative1 = model_axis(doc, direction)
        axis2, negative2 = model_axis(doc, direction2) if direction2 else (None, False)
        doc.ClearSelection2(True)
        axis1.Select2(True, 1)
        if axis2 is not None:
            axis2.Select2(True, 2)
        select_features(doc, features, 4)
        n2, s2 = (count2, spacing2_mm * MM) if direction2 else (1, 0.0)
        feat = doc.FeatureManager.FeatureLinearPattern5(
            count, spacing_mm * MM, n2, s2, reverse != negative1, reverse2 != negative2, "", "",
            False, False, False, False, False, False, False, False, False, False, 0.0, 0.0, False, False)
        name = finish_feature(doc, feat, "Linear pattern failed (do the copies fall outside the part?).")
        grid = f" x {count2} along {direction2.upper()}" if direction2 else ""
        return f"Linear pattern '{name}' created: {count} along {direction.upper()}{grid}."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def circular_pattern(features: list[str],
                           count: Annotated[int, Field(ge=2, description="Number of copies (including original, >= 2)")],
                           axis: Literal["x", "y", "z", "edge"] = "z",
                           axis_edge_point: list[float] | None = None,
                           angle_deg: Annotated[float, Field(gt=0, le=360, description="Total angle in degrees (0 < angle <= 360)")] = 360.0,
                           reverse: bool = False) -> str:
    """Repeats features (names from get_feature_tree) around an axis: count copies in total
    (original included), spread evenly over angle_deg.
    axis: a model axis through the origin, or "edge" to turn around the circular or straight
    edge through axis_edge_point ([x, y, z] in mm, e.g. on the rim of a central hole)."""
    def work():
        if count < 2 or not 0 < angle_deg <= 360:
            raise SWError("count must be at least 2 and angle_deg between 0 and 360.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        if axis == "edge":
            if not axis_edge_point:
                raise SWError("axis='edge' needs axis_edge_point.")
            doc.ClearSelection2(True)
            select_at(doc, "EDGE", axis_edge_point, mark=1)
        else:
            feat_axis, _ = model_axis(doc, axis)
            doc.ClearSelection2(True)
            feat_axis.Select2(True, 1)
        select_features(doc, features, 4)
        feat = doc.FeatureManager.FeatureCircularPattern5(count, angle_deg * DEG, reverse, "", False, True, False,
                                                          False, False, False, 1, 0.0, "", False)
        name = finish_feature(doc, feat, "Circular pattern failed (do the copies fall outside the part?).")
        return f"Circular pattern '{name}' created: {count} copies over {angle_deg} degrees."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def mirror(features: list[str], plane: str = "right", face_point: list[float] | None = None) -> str:
    """Mirrors features (names from get_feature_tree) about a plane: "front", "top", "right", a plane
    name, or the planar face through face_point ([x, y, z] in mm)."""
    def work():
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        if face_point:
            select_at(doc, "FACE", face_point, mark=2)
        elif not plane_feature(doc, plane).Select2(True, 2):
            raise SWError(f"Could not select '{plane}'.")
        select_features(doc, features, 1)
        feat = doc.FeatureManager.InsertMirrorFeature2(False, False, False, False, 0)
        name = finish_feature(doc, feat, "Mirror failed.")
        return f"Mirror '{name}' created."
    return await com_tool(work)


# ISO metric screws: clearance holes (ISO 273 close / normal / loose), tap drill, and the
# counterbore and 90-degree countersink for socket head screws (ISO 4762 / ISO 10642).
METRIC_HOLES = {
    #        close normal loose  tap   cbore_d cbore_h csink_d
    "M2":   (2.2, 2.4, 2.6, 1.6, 4.3, 2.1, 4.4),
    "M2.5": (2.7, 2.9, 3.1, 2.05, 5.0, 2.6, 5.5),
    "M3":   (3.2, 3.4, 3.6, 2.5, 6.5, 3.3, 6.6),
    "M4":   (4.3, 4.5, 4.8, 3.3, 8.0, 4.4, 9.0),
    "M5":   (5.3, 5.5, 5.8, 4.2, 10.0, 5.4, 11.0),
    "M6":   (6.4, 6.6, 7.0, 5.0, 11.0, 6.4, 13.0),
    "M8":   (8.4, 9.0, 10.0, 6.8, 15.0, 8.6, 17.2),
    "M10":  (10.5, 11.0, 12.0, 8.5, 18.0, 10.6, 21.5),
    "M12":  (13.0, 13.5, 14.5, 10.2, 20.0, 12.6, 25.5),
    "M14":  (15.0, 15.5, 16.5, 12.0, 24.0, 14.6, 28.5),
    "M16":  (17.0, 17.5, 18.5, 14.0, 26.0, 16.6, 31.5),
    "M20":  (21.0, 22.0, 24.0, 17.5, 33.0, 20.6, 38.0),
    "M24":  (25.0, 26.0, 28.0, 21.0, 40.0, 24.8, 45.0),
}
HOLE_FITS = {"close": 0, "normal": 1, "loose": 2, "tapped": 3}


def hole_sizes(size: str, fit: str, diameter_mm: float, style: str, cbore_d: float, cbore_h: float,
               csink_d: float) -> tuple[float, float, float, float]:
    """Hole, counterbore and countersink diameters (and counterbore depth) in mm."""
    row = None
    if size:
        row = METRIC_HOLES.get(size.upper().replace(" ", ""))
        if row is None:
            raise SWError(f"Unknown size '{size}'. Known: {', '.join(METRIC_HOLES)}; or give diameter_mm.")
    d = diameter_mm or (row[HOLE_FITS[fit]] if row else 0.0)
    if d <= 0:
        raise SWError("Give diameter_mm or size (e.g. 'M6').")
    if style == "counterbore":
        cbore_d, cbore_h = cbore_d or (row[4] if row else 0.0), cbore_h or (row[5] if row else 0.0)
        if cbore_d <= d or cbore_h <= 0:
            raise SWError("The counterbore needs a diameter larger than the hole and a depth "
                          "(counterbore_diameter_mm, counterbore_depth_mm), or a metric size.")
    if style == "countersink":
        csink_d = csink_d or (row[6] if row else 0.0)
        if csink_d <= d:
            raise SWError("The countersink needs a diameter larger than the hole (countersink_diameter_mm), "
                          "or a metric size.")
    return d, cbore_d, cbore_h, csink_d


def _face_normal(doc: Any) -> tuple[float, float, float]:
    face = doc.SelectionManager.GetSelectedObject6(1, -1)
    n = call(face, "Normal")
    if not n or not any(n):
        raise SWError("Holes need a planar face.")
    length = math.sqrt(sum(v * v for v in n))
    return tuple(v / length for v in n)


def _cut_circles(doc: Any, face_point: list[float], centers: list[list[float]], diameter_mm: float,
                 depth_mm: float, through_all: bool, draft_deg: float = 0.0) -> str:
    """Sketches circles on the face through face_point and cuts them into the part."""
    doc.ClearSelection2(True)
    select_at(doc, "FACE", face_point, append=False)
    doc.SketchManager.InsertSketch(True)
    if not in_sketch(doc):
        raise SWError("Could not sketch on the face.")
    sm = require_sketch(doc)
    level = model_to_sketch(doc, centers[0])[2]
    for c in centers:
        sx, sy, sz = model_to_sketch(doc, c)
        if abs(sz - level) > 1e-3:
            raise SWError(f"The centre {c} does not lie on the same face as {centers[0]}.")
        sm.CreateCircleByRadius(sx * MM, sy * MM, 0, diameter_mm * MM / 2)
    close_sketch_and_select_it(doc)
    t1 = SW_END_THROUGH_ALL if through_all else SW_END_BLIND
    d = 0.01 if through_all else depth_mm * MM
    draft = draft_deg > 0
    common = (True, False, False, t1, 0, d, 0.0, draft, False, False, False, draft_deg * DEG, 0.0,
              False, False, False, False, False, True, True, True, True, False, 0, 0.0, False)
    try:
        feat = doc.FeatureManager.FeatureCut4(*common, False)
    except pywintypes.com_error:
        feat = doc.FeatureManager.FeatureCut3(*common)
    return finish_feature(doc, feat, "The hole cut failed (does it go into the material?).")


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def hole(centers: list[list[float]], size: str = "",
               diameter_mm: Annotated[float, Field(ge=0, description="Hole diameter in mm (0 if using metric size)")] = 0.0,
               fit: Literal["close", "normal", "loose", "tapped"] = "normal",
               depth_mm: Annotated[float, Field(ge=0, description="Hole depth in mm (0 if through_all)")] = 0.0,
               through_all: bool = False,
               style: Literal["simple", "counterbore", "countersink"] = "simple",
               counterbore_diameter_mm: Annotated[float, Field(ge=0, description="Counterbore diameter in mm")] = 0.0,
               counterbore_depth_mm: Annotated[float, Field(ge=0, description="Counterbore depth in mm")] = 0.0,
               countersink_diameter_mm: Annotated[float, Field(ge=0, description="Countersink diameter in mm")] = 0.0,
               countersink_angle_deg: Annotated[float, Field(gt=0, lt=180, description="Countersink angle in degrees (0 < angle < 180)")] = 90.0) -> str:
    """Drills round holes into a planar face, perpendicular to it.

    centers: the hole centres [[x, y, z], ...] in mm, model coordinates, all on the SAME face
      (the face is found through the first one).
    size: metric screw size ("M3" ... "M24") with fit: close / normal / loose clearance hole
      (ISO 273) or tapped = tap drill diameter (no thread is modelled); or give diameter_mm.
    depth_mm or through_all=True.
    style: counterbore / countersink for socket head screws; with a metric size their
      dimensions are standard (ISO 4762 / ISO 10642, 90 degrees), otherwise give them."""
    def work():
        if not centers:
            raise SWError("Give at least one centre.")
        if not through_all and depth_mm <= 0:
            raise SWError("Give depth_mm > 0 or through_all=True.")
        d, cb_d, cb_h, cs_d = hole_sizes(size, fit, diameter_mm, style, counterbore_diameter_mm,
                                         counterbore_depth_mm, countersink_diameter_mm)
        if style == "counterbore" and not through_all and cb_h >= depth_mm:
            raise SWError("The hole must be deeper than the counterbore.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        select_at(doc, "FACE", centers[0], append=False)
        n = _face_normal(doc)
        names, start, done = [], 0.0, ""
        if style == "counterbore":
            names.append(_cut_circles(doc, centers[0], centers, cb_d, cb_h, False))
            start, done = cb_h, f", counterbore {cb_d} x {cb_h} mm"
        elif style == "countersink":
            if not 0 < countersink_angle_deg < 180:
                raise SWError("The countersink angle must be between 0 and 180 degrees.")
            half = countersink_angle_deg / 2
            start = (cs_d - d) / 2 / math.tan(half * DEG)
            names.append(_cut_circles(doc, centers[0], centers, cs_d, start, False, draft_deg=half))
            done = f", countersink {cs_d} mm at {countersink_angle_deg} deg"
        # the hole starts from the bottom of the counterbore / countersink, if any
        bottom = [c + (-start) * v for c, v in zip(xyz(centers[0], "centre"), n)]
        names.append(_cut_circles(doc, bottom, centers, d, depth_mm - start, through_all))
        what = "through all" if through_all else f"{depth_mm} mm deep"
        return (f"{len(centers)} hole(s) of {d} mm ({size + ' ' + fit if size else 'custom'}), {what}{done}. "
                f"Features: {', '.join(names)}.")
    return await com_tool(work)


# --------------------------------- Material -------------------------------- #
def material_library() -> list[tuple[str, str, str]]:
    """(database, category, material) for every material in the configured databases."""
    import glob
    import xml.etree.ElementTree as ET

    sw = get_sw()
    folders = [f.strip() for f in (sw.GetUserPreferenceStringValue(SW_MATERIAL_DATABASES) or "").split(";")]
    # empty preference = the databases installed with SolidWorks, in its language
    install = call(sw, "GetExecutablePath") or ""  # the installation folder, despite the name
    if not os.path.isdir(install):
        install = os.path.dirname(install)
    folders.append(os.path.join(install, "lang", call(sw, "GetCurrentLanguage") or "english", "sldmaterials"))
    found = []
    for folder in dict.fromkeys(filter(None, folders)):
        for path in sorted(glob.glob(os.path.join(folder, "*.sldmat"))):
            database = os.path.splitext(os.path.basename(path))[0]
            try:
                root = ET.parse(path).getroot()
            except (ET.ParseError, OSError):
                continue
            for category in root.iter():
                if category.tag.split("}")[-1] != "classification":
                    continue
                for mat in category:
                    if mat.tag.split("}")[-1] == "material" and mat.get("name"):
                        found.append((database, category.get("name", ""), mat.get("name")))
    return found


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def list_materials(search: str = "") -> str:
    """Materials available in the SolidWorks material databases, filtered by 'search' (part of the
    name or category, e.g. "6061", "steel", "abs"). Use the exact name with set_material."""
    def work():
        mats = material_library()
        if not mats:
            raise SWError("No material database found (SolidWorks > Options > File Locations > Material Databases).")
        s = search.lower()
        hits = [m for m in mats if s in m[1].lower() or s in m[2].lower()]
        if not hits:
            return f"No material matches '{search}'."
        rows = [f"{name}  [{category}, {database}]" for database, category, name in hits[:80]]
        more = f"\n... and {len(hits) - 80} more: narrow the search." if len(hits) > 80 else ""
        return "\n".join(rows) + more
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def set_material(name: str) -> str:
    """Assigns a material to the active part (exact name, see list_materials), so that
    get_mass_properties gives the real mass."""
    def work():
        doc = active_doc(SW_DOC_PART)
        match = next((m for m in material_library() if m[2].lower() == name.lower()), None)
        if match is None:
            raise SWError(f"Material '{name}' not found: use list_materials to find the exact name.")
        database, _, exact = match
        doc.SetMaterialPropertyName2("", database, exact)
        rebuild(doc)
        return f"Material '{exact}' ({database}) assigned."
    return await com_tool(work)


# ------------------------------- Parameters -------------------------------- #
def dimension_unit(dim: Any) -> tuple[float, str]:
    """Scale from API units and the unit label of a dimension."""
    kind = call(dim, "GetType")
    if kind == SW_DIM_ANGULAR:
        return DEG, " deg"
    if kind == SW_DIM_INTEGER:
        return 1.0, ""
    return MM, " mm"


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def get_dimensions(feature: str = "") -> DimensionResult:
    """Dimensions of a feature or sketch (or of the whole part when 'feature' is empty), with their
    full names (e.g. D1@Boss-Extrude1) and values in mm or degrees, for set_dimension."""
    def work():
        doc = active_doc(SW_DOC_PART)
        feats = [find_feature(doc, feature)] if feature else list(iter_features(doc))
        dims: list[DimensionItem] = []
        for f in feats:
            disp = call(f, "GetFirstDisplayDimension")
            while disp is not None:
                dim = disp.GetDimension2(0)
                scale, unit = dimension_unit(dim)
                name = "@".join(call(dim, "FullName").split("@")[:2])  # drop the "@Part1.Part" suffix
                val = float(call(dim, "SystemValue") / scale)
                dims.append({"name": name, "value": round(val, 4), "unit": unit.strip()})
                disp = f.GetNextDisplayDimension(disp)
        return {
            "target": feature if feature else "part",
            "count": len(dims),
            "dimensions": dims,
        }
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def set_dimension(name: str, value: float) -> str:
    """Changes a dimension (name from get_dimensions, e.g. "D1@Boss-Extrude1") to 'value'
    (mm, degrees for angles, a plain number for counts such as pattern instances) and rebuilds."""
    def work():
        doc = active_doc(SW_DOC_PART)
        dim = doc.Parameter(name)
        if dim is None:
            raise SWError(f"Dimension '{name}' not found (see get_dimensions).")
        scale, unit = dimension_unit(dim)
        err = dim.SetSystemValue3(value * scale, SW_SET_VALUE_THIS_CONFIGURATION, None)
        if err != 0:
            raise SWError(f"Could not set '{name}' (code {err}; is it driven by an equation?).")
        if not call(doc, "EditRebuild3"):
            raise SWError(f"'{name}' set to {value}, but the part does not rebuild: undo or choose another value.")
        return f"'{name}' = {value:g}{unit}; part rebuilt."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def suppress_feature(name: str, suppressed: bool = True) -> str:
    """Suppresses (temporarily removes from the model, keeping it in the tree) or, with
    suppressed=False, restores a feature."""
    def work():
        doc = active_doc(SW_DOC_PART)
        feat = find_feature(doc, name)
        if not feat.SetSuppression2(0 if suppressed else 1, SW_THIS_CONFIGURATION, None):
            raise SWError(f"Could not {'suppress' if suppressed else 'unsuppress'} '{name}'.")
        rebuild(doc)
        return f"'{name}' {'suppressed' if suppressed else 'restored'}."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def get_equations() -> str:
    """Global variables and equations of the active document, with their index."""
    def work():
        mgr = call(active_doc(), "GetEquationMgr")
        count = call(mgr, "GetCount")
        if not count:
            return "No equations."
        return "Equations:\n" + "\n".join(f"{i}. {mgr.Equation(i)}" for i in range(count))
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def add_equation(equation: str) -> str:
    """Adds a global variable or an equation and rebuilds. Names go in double quotes, e.g.
    '"width" = 80' (global variable), '"D1@Boss-Extrude1" = "width" / 2' (drives a dimension).
    Numbers without units take the document units."""
    def work():
        doc = active_doc()
        mgr = call(doc, "GetEquationMgr")
        index = mgr.Add2(-1, equation, True)  # Add3 refuses every equation through late binding
        if index < 0:
            raise SWError("Equation not added: check the syntax and the names (a global variable cannot "
                          "reuse an existing or reserved name, e.g. the sheet metal thickness).")
        rebuild(doc)
        return f"Equation {index} added: {mgr.Equation(index)}"
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": False, "openWorldHint": False})
async def delete_equation(index: int) -> str:
    """Deletes the equation with that index (see get_equations)."""
    def work():
        doc = active_doc()
        mgr = call(doc, "GetEquationMgr")
        if not 0 <= index < call(mgr, "GetCount"):
            raise SWError(f"No equation with index {index}.")
        text = mgr.Equation(index)
        if mgr.Delete(index) != 1:
            raise SWError(f"Could not delete equation {index}.")
        rebuild(doc)
        return f"Equation deleted: {text}"
    return await com_tool(work)


def property_manager(doc: Any) -> Any:
    return doc.Extension.CustomPropertyManager("")


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def get_custom_properties() -> str:
    """Custom properties of the active document (description, part number, ...), as used in
    BOMs and title blocks."""
    def work():
        mgr = property_manager(active_doc())
        names = call(mgr, "GetNames") or ()
        if not names:
            return "No custom properties."
        rows = []
        for n in names:
            value, resolved = (VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "") for _ in range(2))
            flags = [VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False) for _ in range(2)]
            mgr.Get6(n, False, value, resolved, *flags)
            value, resolved = value.value, resolved.value
            rows.append(f"{n} = {value}" + (f"  (-> {resolved})" if resolved != value else ""))
        return "Custom properties:\n" + "\n".join(rows)
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def set_custom_property(name: str, value: str) -> str:
    """Sets a custom property of the active document (created if missing). Replaces the current value.
    An empty value deletes it."""
    def work():
        mgr = property_manager(active_doc())
        if not value:
            mgr.Delete(name)
            return f"Property '{name}' deleted."
        if mgr.Add3(name, SW_CUSTOM_INFO_TEXT, value, SW_CUSTOM_PROPERTY_REPLACE) not in (0, 1):
            raise SWError(f"Could not set '{name}'.")
        return f"Property '{name}' = {value}"
    return await com_tool(work)


# --------------------------------- Measure --------------------------------- #
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def measure(point1: list[float], type1: Literal["face", "edge", "vertex"] = "face",
                  point2: list[float] | None = None, type2: Literal["face", "edge", "vertex"] = "face") -> MeasureResult:
    """Measures one entity (area, length, diameter...) or the distance and angle between two,
    picked by a point lying on each ([x, y, z] in mm, model coordinates). Replaces the current selection."""
    def work():
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        select_at(doc, type1.upper(), point1)
        if point2:
            select_at(doc, type2.upper(), point2)
        m = call(doc.Extension, "CreateMeasure")
        m.ArcOption = 0  # centre to centre
        ok = call(m, "Calculate", None)
        doc.ClearSelection2(True)
        if not ok:
            raise SWError("Could not measure the selected entities.")
        res: MeasureResult = {}
        for prop, key, scale in (
                ("Distance", "distance_mm", MM),
                ("CenterDistance", "center_distance_mm", MM),
                ("NormalDistance", "normal_distance_mm", MM),
                ("Angle", "angle_deg", DEG),
                ("Length", "length_mm", MM),
                ("Diameter", "diameter_mm", MM),
                ("Radius", "radius_mm", MM),
                ("Area", "area_mm2", MM ** 2),
                ("Perimeter", "perimeter_mm", MM)):
            v = call(m, prop)
            if v is not None and v != -1:
                res[key] = round(float(v / scale), 4)
        if point2:
            dx, dy, dz = (call(m, p) for p in ("DeltaX", "DeltaY", "DeltaZ"))
            if call(m, "Distance") not in (None, -1):
                res["delta_mm"] = [round(float(dx / MM), 4), round(float(dy / MM), 4), round(float(dz / MM), 4)]
            if call(m, "IsParallel"):
                res["is_parallel"] = True
            if call(m, "IsPerpendicular"):
                res["is_perpendicular"] = True
        return res
    return await com_tool(work)


# ------------------------------- Undo / view ------------------------------- #
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": False, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": False, "openWorldHint": False})
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def zoom_to_fit() -> str:
    """Fits the view to show the whole model."""
    def work():
        call(active_doc(), "ViewZoomtofit2")
        return "View fitted."
    return await com_tool(work)


# --------------------------- Free macro (last resort) ---------------------- #
MACRO_DOC = """Runs Python code against the SolidWorks COM API with the user's privileges.
USE ONLY when no dedicated tool is enough (e.g. assemblies, drawings, sheet metal).

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
        raise SWError(f"ERROR in the macro:\n{tb}\nOutput before the error:\n{out.getvalue()}")
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
    """Runs Python code against the SolidWorks COM API with the user's privileges.
    Last resort when no dedicated tool fits. Disabled unless enabled in the configuration."""
    logger.info("Running Python macro (%d characters)", len(python_code))

    def work():
        return _macro(python_code)
    return await com_tool(work)


def register_macro_tool() -> None:
    """Exposes execute_solidworks_macro; called only when the configuration enables it."""
    mcp.add_tool(
        execute_solidworks_macro,
        description=MACRO_DOC,
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": False, "openWorldHint": True},
    )


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

    if CONFIG.projects_directory:
        os.makedirs(CONFIG.projects_directory, exist_ok=True)
        # FastMCP takes the instructions only at construction, before the configuration is read
        mcp._mcp_server.instructions = SERVER_INSTRUCTIONS + PROJECTS_INSTRUCTIONS.format(
            root=CONFIG.projects_directory)
        logger.info("Projects folder: %s", CONFIG.projects_directory)

    mcp.settings.port = args.port
    if args.transport == "stdio":
        logger.info("Starting SolidWorks MCP server on stdio.")
    else:
        path = "/sse" if args.transport == "sse" else "/mcp"
        logger.info("Starting SolidWorks MCP server: http://%s:%d%s (this PC only)", HOST, args.port, path)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
