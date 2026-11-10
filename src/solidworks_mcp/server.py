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


class FaceInfo(TypedDict, total=False):
    index: int
    type: str                    # plane, cylinder, cone, sphere, torus, other
    point_mm: list[float]        # lies ON the face, inside its boundary: use it to pick the face
    area_mm2: float
    normal: list[float]          # planar faces: outward unit normal
    diameter_mm: float           # cylinders, cones (at the reference), spheres
    axis: list[float]            # cylinders and cones: axis direction
    axis_point_mm: list[float]   # cylinders and cones: a point on the axis; spheres: the centre
    concave: bool                # cylinders: True for a hole, False for a boss / shaft
    feature: str                 # feature that created the face


class FaceList(TypedDict):
    count: int        # faces returned
    total: int        # faces matching the filters
    truncated: bool
    faces: list[FaceInfo]


class EdgeInfo(TypedDict, total=False):
    index: int
    type: str                    # line, circle, arc, ellipse, spline, other
    point_mm: list[float]        # mid-point, ON the edge: use it to pick the edge
    length_mm: float
    start_mm: list[float]
    end_mm: list[float]
    center_mm: list[float]       # circles and arcs
    diameter_mm: float           # circles and arcs
    axis: list[float]            # circles and arcs: normal of the circle plane
    features: list[str]          # features of the two faces that meet at the edge


class EdgeList(TypedDict):
    count: int
    total: int
    truncated: bool
    edges: list[EdgeInfo]


class OpenDocument(TypedDict):
    title: str
    path: str
    type: str
    modified: bool
    visible: bool
    read_only: bool


class OpenDocuments(TypedDict):
    active: str | None
    count: int
    documents: list[OpenDocument]


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
SW_DEFAULT_TEMPLATE_PART = 8          # swUserPreferenceStringValue_e.swDefaultTemplatePart
SW_DEFAULT_TEMPLATE_ASSEMBLY = 9      # swUserPreferenceStringValue_e.swDefaultTemplateAssembly
SW_DEFAULT_TEMPLATE_DRAWING = 10      # swUserPreferenceStringValue_e.swDefaultTemplateDrawing
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
SW_REF_PLANE_COINCIDENT, SW_REF_PLANE_ANGLE = 4, 16   # swRefPlaneReferenceConstraints_e
SW_OPEN_SILENT = 1                # swOpenDocOptions_Silent
SW_OPEN_READ_ONLY = 2             # swOpenDocOptions_ReadOnly
SW_DONT_REBUILD_ON_ACTIVATION = 1  # swRebuildOnActivation_e.swDontRebuildActiveDoc (no dialog)
SW_TRIM_CLOSEST = 0               # swSketchTrimChoice_e.swSketchTrimClosest
SW_SKETCH_LINE, SW_SKETCH_ARC = 0, 1  # swSketchSegments_e
SW_COSMETIC_STANDARD_NONE = -2    # swCosmeticStandardType_e.swStandardType_StandardNone
SW_COSMETIC_BLIND, SW_COSMETIC_THROUGH = 0, 2   # swCosmeticEndConditions_e
SW_DRAFT_NO_PROPAGATION = 0       # swDraftFacePropagationType_e.swFacePropNone
SW_MATE_NO_ERROR = 1              # swAddMateError_NoError
DOC_EXTENSIONS = {".sldprt": SW_DOC_PART, ".sldasm": SW_DOC_ASSEMBLY, ".slddrw": SW_DOC_DRAWING}
OPEN_ERRORS = {   # swFileLoadError_e: what to tell the user
    2: "file not found", 1024: "not a SolidWorks file of that type", 8192: "saved by a newer SolidWorks",
    65536: "another open document has the same name", 2097152: "the file needs repairing",
    8388608: "SolidWorks is busy",
}
DOC_TYPE_NAMES = {SW_DOC_PART: "Part", SW_DOC_ASSEMBLY: "Assembly", SW_DOC_DRAWING: "Drawing"}

MATE_TYPES = {
    "coincident": 0,
    "concentric": 1,
    "perpendicular": 2,
    "parallel": 3,
    "tangent": 4,
    "distance": 5,
    "angle": 6,
}
MATE_ALIGN = {
    "aligned": 0,
    "anti_aligned": 1,
    "closest": 2,
}

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
  of a point lying exactly on the entity. Do not guess them: list_faces and list_edges
  (optionally for one feature, or one type such as cylinder / circle) give a point_mm on
  each face and edge, with normals, diameters and axes.
- Existing work: open_document opens a saved part (relative paths are in the projects
  folder), list_open_documents / activate_document / close_document manage the windows.
  edit_sketch reopens a sketch (finish with end_sketch), edit_feature changes an
  extrusion or cut, set_dimension any other dimension, rename_feature gives clear names.
  save_document(new_version=True) saves the next version (_v2_, _v3_...) of the file.
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


STARTUP_POLL_S = 2.0
STARTUP_SETTLE_S = 20.0   # visible for this long = ready, when StartupProcessCompleted stays False
_launch_state: dict[str, float] = {"since": float("-inf")}   # when this server last started SLDWORKS.exe


CREATE_NEW_PROCESS_GROUP = 0x00000200


def _user_environment() -> dict[str, str] | None:
    """The user's full default environment, as Windows builds it for a new logon session.

    MCP clients often start their server with only a dozen variables (the Python MCP client keeps
    APPDATA, PATH, TEMP...): SolidWorks inheriting that misses windir, ProgramFiles, ProgramData...
    and fails to load the .NET Framework at startup. None = inherit this process's environment."""
    try:
        import win32api
        import win32profile
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                               win32security.TOKEN_QUERY | win32security.TOKEN_DUPLICATE)
        return {str(k): str(v) for k, v in win32profile.CreateEnvironmentBlock(token, False).items()}
    except Exception:
        return None


def _in_job() -> bool:
    """True when this process belongs to a Windows job (MCP clients often put their server in one)."""
    try:
        import win32api
        import win32job
        return bool(win32job.IsProcessInJob(win32api.GetCurrentProcess(), None))
    except Exception:
        return False


def _start_detached(exe: str) -> None:
    """Starts SLDWORKS.exe so that it does not belong to this server's process tree.

    MCP clients may put their server in a Windows job that kills every process in it on exit (the
    Python MCP client does): SolidWorks, with the user's unsaved work, must not go with it. Inside
    a job the desktop shell (explorer.exe) starts it, as a double-click would: that also gives it
    the user's full environment. Breaking away from the job is not reliable with nested jobs.
    (COM activation would detach it too, but runs SolidWorks in automation mode, where some of its
    .NET components fail to load and show an error at startup.)"""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                 env=_user_environment())
    if _in_job():
        subprocess.Popen(["explorer.exe", exe], **quiet)
        logger.info("SolidWorks started through the desktop shell")
    else:
        subprocess.Popen([exe], creationflags=CREATE_NEW_PROCESS_GROUP, **quiet)


def _ready(app: Any, visible_since: list[float]) -> bool:
    """True once SolidWorks accepts commands."""
    try:
        call(app, "RevisionNumber")   # fails while the object is not fully registered yet
    except (pywintypes.com_error, AttributeError):
        return False
    try:
        if call(app, "StartupProcessCompleted"):
            return True
    except (pywintypes.com_error, AttributeError):
        return True  # property missing in old releases: answering is enough
    # Some releases (seen on SolidWorks 2026) never set
    # StartupProcessCompleted although they accept commands: a window shown for a while is enough.
    try:
        visible = bool(call(app, "Visible"))
    except (pywintypes.com_error, AttributeError):
        visible = False
    if not visible:
        return False
    if not visible_since:
        visible_since.append(time.monotonic())
    return time.monotonic() - visible_since[0] >= STARTUP_SETTLE_S


def _launch() -> Any:
    """Starts SolidWorks and waits until it accepts commands."""
    exe = find_solidworks_exe()
    if exe is None:
        raise SWError("SolidWorks is not running and SLDWORKS.exe was not found. Start SolidWorks, or set "
                      "[solidworks] executable in the configuration file.")
    if time.monotonic() - _launch_state["since"] > CONFIG.startup_timeout_s:  # not twice while it starts
        logger.info("SolidWorks is not running: starting %s", exe)
        _start_detached(exe)
        _launch_state["since"] = time.monotonic()
    app = None
    deadline = time.monotonic() + CONFIG.startup_timeout_s
    visible_since: list[float] = []
    while time.monotonic() < deadline:
        if app is not None and _ready(app, visible_since):
            return app
        time.sleep(STARTUP_POLL_S)
        app = app or _attach()
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


def active_doc(required_type: int | tuple[int, ...] | None = None) -> Any:
    doc = get_sw().ActiveDoc
    if doc is None:
        raise SWError("No document is open in SolidWorks. Use new_part to create one.")
    if required_type is not None:
        doc_type = call(doc, "GetType")
        valid = (required_type,) if isinstance(required_type, int) else required_type
        if doc_type not in valid:
            expected = " or ".join(f"'{DOC_TYPE_NAMES.get(t, t)}'" for t in valid)
            raise SWError(
                f"The active document is a '{DOC_TYPE_NAMES.get(doc_type, doc_type)}', "
                f"a {expected} is needed.")
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
    folders are created. When a projects folder is set, paths are confined to it and
    escaping via '..' or absolute paths outside the projects folder is forbidden."""
    root = CONFIG.projects_directory
    if root:
        norm_root = os.path.normcase(os.path.abspath(root))
        if not os.path.isabs(path):
            full_path = os.path.normpath(os.path.abspath(os.path.join(root, path)))
        else:
            full_path = os.path.normpath(os.path.abspath(path))
        norm_full = os.path.normcase(full_path)
        if not (norm_full == norm_root or norm_full.startswith(norm_root + os.sep)):
            raise SWError(f"Access denied: '{path}' is outside the configured projects directory ({root}).")
        folder = os.path.dirname(full_path)
        os.makedirs(folder, exist_ok=True)
        return full_path
    else:
        if not os.path.isabs(path):
            raise SWError("Give a full path: no projects folder is set in the configuration.")
        full_path = os.path.normpath(os.path.abspath(path))
        folder = os.path.dirname(full_path)
        if not os.path.isdir(folder):
            raise SWError(f"The folder {folder} does not exist.")
        return full_path


def check_inside_projects(target: str, what: str = "document") -> None:
    """Refuses a file outside the projects folder, when one is configured."""
    root = CONFIG.projects_directory
    if not root:
        return
    norm_root = os.path.normcase(os.path.abspath(root))
    norm_target = os.path.normcase(os.path.abspath(target))
    if not (norm_target == norm_root or norm_target.startswith(norm_root + os.sep)):
        raise SWError(f"Access denied: {what} '{target}' is outside the configured projects directory.")


def resolve_open_path(path: str) -> str:
    """Full path of an existing SolidWorks file: relative paths are relative to the projects folder.
    Reading is allowed anywhere; only saving is confined to the projects folder."""
    if not path.strip():
        raise SWError("Give the path of the file to open.")
    if os.path.isabs(path):
        full = os.path.normpath(path)
    elif CONFIG.projects_directory:
        full = os.path.normpath(os.path.join(CONFIG.projects_directory, path))
    else:
        raise SWError("Give a full path: no projects folder is set in the configuration.")
    if os.path.splitext(full)[1].lower() not in DOC_EXTENSIONS:
        raise SWError("Only SolidWorks files can be opened (.SLDPRT, .SLDASM, .SLDDRW).")
    if not os.path.isfile(full):
        raise SWError(f"File not found: {full}")
    return full


VERSION_STEM = re.compile(r"^(?P<head>.*_v)(?P<n>\d+)(?P<tail>_.*)?$", re.IGNORECASE)


def next_version_path(path: str) -> str:
    """The next free version of a file: bracket_v1_base.SLDPRT -> bracket_v2_base.SLDPRT
    (part.SLDPRT -> part_v2.SLDPRT). Never returns an existing file."""
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    m = VERSION_STEM.match(stem)
    head, n, tail = (m["head"], int(m["n"]), m["tail"] or "") if m else (stem + "_v", 1, "")
    while True:
        n += 1
        candidate = os.path.join(folder, f"{head}{n}{tail}{ext}")
        if not os.path.exists(candidate):
            return candidate


def open_documents(sw: Any) -> list[Any]:
    return list(call(sw, "GetDocuments") or ())


def find_open_doc(sw: Any, name: str) -> Any:
    """An open document by title, file name (with or without extension) or full path."""
    key = name.strip().lower()
    docs = open_documents(sw)
    for doc in docs:
        title = str(call(doc, "GetTitle") or "")
        path = str(call(doc, "GetPathName") or "")
        base = os.path.basename(path)
        names = {title.lower(), os.path.splitext(title)[0].lower(), path.lower(), base.lower(),
                 os.path.splitext(base)[0].lower()}
        if key and key in names:
            return doc
    titles = ", ".join(str(call(d, "GetTitle")) for d in docs) or "none"
    raise SWError(f"No open document called '{name}'. Open documents: {titles}.")


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


def _searchable_bodies(doc: Any) -> list[tuple[Any, Callable[[list[float]], list[float]]]]:
    """Solid bodies to search, each with a function mapping a model point into the body's own space.

    In a part the bodies live in model space. In an assembly each component's body is in the
    component's space, so the query point is moved there with the inverse of its transform
    (row-vector convention: assembly = local * R * scale + T).
    """
    if call(doc, "GetType") != SW_DOC_ASSEMBLY:
        return [(body, lambda p: p) for body in doc.GetBodies2(0, True) or ()]   # swSolidBody, visible only
    found = []
    for comp in call(doc, "GetComponents", False) or ():   # every component, sub-assemblies included
        try:
            body = call(comp, "GetBody")
            if body is None:
                continue
            a = list(call(call(comp, "Transform2"), "ArrayData"))
        except Exception:
            continue
        rot, t, s = [a[0:3], a[3:6], a[6:9]], a[9:12], a[12] or 1.0

        def to_local(p: list[float], rot=rot, t=t, s=s) -> list[float]:
            v = [(p[j] - t[j]) / s for j in range(3)]
            return [sum(v[j] * rot[i][j] for j in range(3)) for i in range(3)]   # times R transposed
        found.append((body, to_local))
    return found


def pick(doc: Any, kind: str, point_mm: list[float]) -> Any:
    """The face / edge / vertex of the solid bodies nearest to a point (mm, model coordinates).

    Geometric search instead of SelectByID2, which misses entities hidden in the current view.
    In an assembly the bodies of every component are searched (assembly coordinates).
    """
    p = [v * MM for v in xyz(point_mm)]
    best, best_d = None, math.inf
    for body, to_local in _searchable_bodies(doc):
        q0 = to_local(p)
        for ent in call(body, BODY_LISTS[kind]) or ():
            q = call(ent, "GetPoint") if kind == "VERTEX" else ent.GetClosestPointOn(*q0)
            d = math.dist(q0, q[:3])
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
    if kind in BODY_LISTS and call(doc, "GetType") in (SW_DOC_PART, SW_DOC_ASSEMBLY):
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


SKETCH_PICK_TOLERANCE_MM = 0.5


def _sketch_xy(p: Any) -> tuple[float, float]:
    """A sketch point's coordinates in mm (sketch system)."""
    return float(call(p, "X")) / MM, float(call(p, "Y")) / MM


def _segment_distance(seg: Any, x: float, y: float) -> float | None:
    """Distance (mm, sketch plane) from (x, y) to a line or arc; None for other segment types."""
    kind = call(seg, "GetType")
    if kind == SW_SKETCH_LINE:
        line = _as(seg, IID_SKETCH_LINE)
        (ax, ay), (bx, by) = _sketch_xy(call(line, "GetStartPoint2")), _sketch_xy(call(line, "GetEndPoint2"))
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - ax - t * dx, y - ay - t * dy)
    if kind == SW_SKETCH_ARC:
        arc = _as(seg, IID_SKETCH_ARC)
        cx, cy = _sketch_xy(call(arc, "GetCenterPoint2"))
        r = float(call(arc, "GetRadius")) / MM
        s, e = _sketch_xy(call(arc, "GetStartPoint2")), _sketch_xy(call(arc, "GetEndPoint2"))
        on_circle = abs(math.hypot(x - cx, y - cy) - r)
        if math.dist(s, e) < 1e-6:          # full circle
            return on_circle
        if call(arc, "GetRotationDir") < 0:  # clockwise from start to end = counter-clockwise from end
            s, e = e, s
        a_s, a_e, a_p = (math.atan2(py - cy, px - cx) for px, py in (s, e, (x, y)))
        if (a_p - a_s) % math.tau <= (a_e - a_s) % math.tau + 1e-9:
            return on_circle
        return min(math.dist((x, y), s), math.dist((x, y), e))
    return None


def _pick_sketch(doc: Any, x_mm: float, y_mm: float, point: bool) -> tuple[Any, float, bool]:
    """Nearest sketch segment (or point) to (x, y) mm: (entity, distance mm, other_types_present)."""
    sketch = doc.SketchManager.ActiveSketch
    best, best_d, unknown = None, math.inf, False
    if point:
        for p in call(sketch, "GetSketchPoints2") or ():
            d = math.dist((x_mm, y_mm), _sketch_xy(p))
            if d < best_d:
                best, best_d = p, d
        return best, best_d, False
    for seg in call(sketch, "GetSketchSegments") or ():
        d = _segment_distance(seg, x_mm, y_mm)
        if d is None:
            unknown = True
        elif d < best_d:
            best, best_d = seg, d
    return best, best_d, unknown


def select_sketch_entity(doc: Any, x_mm: float, y_mm: float, point: bool = False,
                         append: bool = True, mark: int = 0) -> None:
    """Selects the sketch segment (or sketch point) through (x, y) in sketch coordinates.

    Geometric search over the sketch's lines, arcs and points: SelectByID2 picks through the
    screen and can grab a neighbouring entity (or a whole rectangle) depending on the zoom.
    Ellipses and splines, which are not measured here, fall back to SelectByID2."""
    kind = "SKETCHPOINT" if point else "SKETCHSEGMENT"
    try:
        entity, dist, unknown = _pick_sketch(doc, x_mm, y_mm, point)
    except Exception:  # geometry not readable: let SolidWorks pick
        entity, dist, unknown = None, math.inf, True
    if entity is not None and dist <= SKETCH_PICK_TOLERANCE_MM:
        data = call(doc.SelectionManager, "CreateSelectData")
        data.Mark = mark
        if not entity.Select4(append, data):
            raise SWError("SolidWorks refused the selection.")
        return
    if unknown or entity is None:
        try:
            select_at(doc, kind, sketch_to_model(doc, x_mm, y_mm), append, mark)
            return
        except SWError:
            pass
    near = f" (the nearest is {dist:.2f} mm away)" if entity is not None else ""
    raise SWError(f"No {ENTITY_NAMES[kind]} found at ({x_mm:g}, {y_mm:g}) in the sketch{near}.")


def sketch_signature(doc: Any) -> tuple:
    """Count and total length of the open sketch's segments, to tell whether an edit changed it."""
    segs = call(doc.SketchManager.ActiveSketch, "GetSketchSegments") or ()
    total = 0.0
    for s in segs:
        try:
            total += float(call(s, "GetLength"))
        except Exception:
            pass
    return len(segs), round(total / MM, 4)


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
        # a drawing sheet always reports an active sketch: only meaningful for parts and assemblies
        sketch = " - sketch open" if doc_type != SW_DOC_DRAWING and in_sketch(doc) else ""
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
                faults = call(b, "Check3")   # an IFaultEntity, returned even for healthy bodies
                count = call(faults, "Count") if faults is not None else 0
                if count:
                    faulty_bodies.append(f"{b_name} ({count} fault(s))")
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
async def save_document(path: str = "", overwrite: bool = False, new_version: bool = False) -> str:
    """Saves the active document.
    With 'path' (e.g. C:\\\\projects\\\\part.SLDPRT, .step, .stl, .pdf...)
    saves/exports to that file: the format is taken from the extension. When the user has set a
    projects folder, relative paths are confined to it and missing folders inside it are created.
    If the target file already exists, overwrite must be True or the save is rejected.
    new_version=True (no path) saves the document as the next version next to its current file
    (bracket_v1_base.SLDPRT -> bracket_v2_base.SLDPRT), never overwriting: the open document
    then becomes the new file and the previous version is kept unchanged.
    Without path or new_version, the document's own file is overwritten."""
    def work():
        doc = active_doc()
        if new_version:
            if path:
                raise SWError("Give either path or new_version=True, not both.")
            current = call(doc, "GetPathName")
            if not current:
                raise SWError("The document was never saved: save it once with a path (e.g. "
                              "'<project>/cad/<project>_v1_<description>.SLDPRT').")
            target = resolve_save_path(next_version_path(current))
            err = doc.SaveAs3(target, 0, SW_SAVE_SILENT)
            if err != 0 or not os.path.exists(target):
                raise SWError(f"Save failed (SolidWorks error code {err}).")
            return f"Saved as new version {target}; {os.path.basename(current)} is kept unchanged."
        if not path:
            target = call(doc, "GetPathName")
            if not target:
                raise SWError("The document was never saved: give a full path.")
            check_inside_projects(target)
            ok = doc.Save3(SW_SAVE_SILENT, VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0),
                           VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0))
            if not ok:
                raise SWError("Save failed.")
            return "Document saved."
        target = resolve_save_path(path)
        if os.path.exists(target) and not overwrite:
            raise SWError(f"File '{target}' already exists. Set overwrite=True to overwrite it.")
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
      configuration (STEP by default). Supported: step, stp, iges, igs, x_t, stl, 3mf, pdf, png,
      dxf, dwg (pdf, dxf and dwg for drawings).
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
            if CONFIG.projects_directory:
                norm_root = os.path.normcase(os.path.abspath(CONFIG.projects_directory))
                norm_folder = os.path.normcase(os.path.abspath(folder))
                if not (norm_folder == norm_root or norm_folder.startswith(norm_root + os.sep)):
                    raise SWError(f"Access denied: export directory '{folder}' is outside the configured projects directory.")
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


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def open_document(path: str, read_only: bool = False) -> str:
    """Opens a part, assembly or drawing (.SLDPRT / .SLDASM / .SLDDRW) and makes it the active
    document, to continue work on it. A relative path is relative to the projects folder.
    If the file is already open it is just brought to the front. read_only=True opens it so that
    it cannot be saved over. The file itself is not modified. Starts SolidWorks if needed."""
    def work():
        full = resolve_open_path(path)
        sw = get_sw()
        already = call(sw, "GetOpenDocumentByName", full) is not None
        errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        options = SW_OPEN_SILENT | (SW_OPEN_READ_ONLY if read_only else 0)
        doc = sw.OpenDoc6(full, DOC_EXTENSIONS[os.path.splitext(full)[1].lower()], options, "", errors, warnings)
        if doc is None:
            code = int(getattr(errors, "value", 0) or 0)
            reasons = [text for bit, text in OPEN_ERRORS.items() if code & bit]
            raise SWError(f"Could not open {full}: {', '.join(reasons) or f'SolidWorks error {code}'}.")
        title = call(doc, "GetTitle")
        sw.ActivateDoc3(title, False, SW_DONT_REBUILD_ON_ACTIVATION, VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0))
        kind = DOC_TYPE_NAMES.get(call(doc, "GetType"), "document")
        state = " (read-only)" if call(doc, "IsOpenedReadOnly") else ""
        note = "It was already open: switched to it. " if already else ""
        return (f"{note}{kind} '{title}' is now the active document{state}. "
                "Use get_feature_tree to see how it is built.")
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def list_open_documents() -> OpenDocuments:
    """Documents open in SolidWorks: title, path, type, whether they have unsaved changes, whether
    they have a window (parts loaded only by an assembly have none) and which one is active."""
    def work():
        sw = get_sw()
        active = sw.ActiveDoc
        docs: list[OpenDocument] = []
        for doc in open_documents(sw):
            docs.append({
                "title": str(call(doc, "GetTitle")),
                "path": str(call(doc, "GetPathName") or ""),
                "type": DOC_TYPE_NAMES.get(call(doc, "GetType"), "Other"),
                "modified": bool(call(doc, "GetSaveFlag")),
                "visible": bool(call(doc, "Visible")),
                "read_only": bool(call(doc, "IsOpenedReadOnly")),
            })
        return {"active": str(call(active, "GetTitle")) if active is not None else None,
                "count": len(docs), "documents": docs}
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def activate_document(name: str) -> str:
    """Makes an open document the active one (brings its window to the front), by title, file
    name or full path as shown by list_open_documents. Every other tool works on the active document."""
    def work():
        sw = get_sw()
        doc = find_open_doc(sw, name)
        title = call(doc, "GetTitle")
        sw.ActivateDoc3(title, False, SW_DONT_REBUILD_ON_ACTIVATION, VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0))
        active = sw.ActiveDoc
        if active is None or call(active, "GetTitle") != title:
            raise SWError(f"'{title}' is only loaded by an assembly and has no window: open it with "
                          "open_document and its path.")
        return f"'{title}' is now the active document."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": False, "openWorldHint": False})
async def close_document(name: str = "", save: bool = False, discard_changes: bool = False) -> str:
    """Closes a document (by title, file name or path; empty = the active one).
    With unsaved changes it refuses, unless save=True (saves over its own file first, which must be
    inside the projects folder) or discard_changes=True (the unsaved changes are LOST)."""
    def work():
        sw = get_sw()
        doc = find_open_doc(sw, name) if name.strip() else active_doc()
        title, target = call(doc, "GetTitle"), call(doc, "GetPathName")
        how = ""
        if call(doc, "GetSaveFlag"):
            if save:
                if not target:
                    raise SWError(f"'{title}' was never saved: save it with save_document and a path first.")
                check_inside_projects(target)
                ok = doc.Save3(SW_SAVE_SILENT, VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0),
                               VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0))
                if not ok:
                    raise SWError(f"Could not save '{title}': it was not closed.")
                how = " after saving it"
            elif discard_changes:
                how = ", unsaved changes discarded"
            else:
                raise SWError(f"'{title}' has unsaved changes: pass save=True to save them first, or "
                              "discard_changes=True to lose them.")
        sw.CloseDoc(title)
        active = sw.ActiveDoc
        now = f"Active document: {call(active, 'GetTitle')}." if active is not None else "No document is active."
        return f"Closed '{title}'{how}. {now}"
    return await com_tool(work)


# -------------------------------- Assemblies ------------------------------- #
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def new_assembly(template_path: str = "") -> str:
    """Creates a new empty assembly in SolidWorks using the default or given assembly template."""
    def work():
        sw = get_sw()
        template = template_path or sw.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_ASSEMBLY)
        if not template or not os.path.isfile(template):
            try:
                template = sw.GetDocumentTemplate(SW_DOC_ASSEMBLY, "", 0, 0, 0)
            except Exception:
                pass
        if not template or not os.path.isfile(template):
            raise SWError("Default assembly template not set or not found "
                          "(SolidWorks > Options > File Locations / Default Templates).")
        doc = sw.NewDocument(template, 0, 0, 0)
        if doc is None:
            raise SWError("SolidWorks did not create the assembly.")
        return f"New assembly created: {call(doc, 'GetTitle')}"
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def insert_component(
    path: str,
    x_mm: float = 0.0,
    y_mm: float = 0.0,
    z_mm: float = 0.0,
) -> str:
    """Inserts a part or sub-assembly (a .SLDPRT / .SLDASM file, relative to the projects folder
    when one is set) into the active assembly, with the component's origin at (x, y, z) mm and
    no rotation. The file is opened in the background if it is not already open, and its window
    is closed again afterwards."""
    def work():
        sw = get_sw()
        doc = active_doc(SW_DOC_ASSEMBLY)
        if not os.path.isabs(path) and CONFIG.projects_directory:
            resolved = os.path.normpath(os.path.join(CONFIG.projects_directory, path))
        else:
            resolved = os.path.abspath(path)
        if not os.path.isfile(resolved):
            raise SWError(f"Component file not found: '{resolved}'.")
        # AddComponent only works on documents already loaded in SolidWorks
        opened = None
        if call(sw, "GetOpenDocumentByName", resolved) is None:
            doc_type = SW_DOC_ASSEMBLY if resolved.lower().endswith(".sldasm") else SW_DOC_PART
            errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            opened = sw.OpenDoc6(resolved, doc_type, SW_OPEN_SILENT, "", errors, warnings)
            if opened is None:
                raise SWError(f"Could not open '{resolved}' (SolidWorks error {getattr(errors, 'value', '?')}).")
            sw.ActivateDoc3(call(doc, "GetTitle"), False, 0, VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0))
        try:
            comp = doc.AddComponent5(resolved, 0, "", False, "", x_mm * MM, y_mm * MM, z_mm * MM)
            if comp is None:
                raise SWError(f"SolidWorks failed to insert component from '{resolved}'.")
            # AddComponent places the component by its centre: move its origin to the requested point
            transform = call(comp, "Transform2")
            transform.ArrayData = doubles([1, 0, 0, 0, 1, 0, 0, 0, 1, x_mm * MM, y_mm * MM, z_mm * MM, 1, 0, 0, 0])
            comp.Transform2 = transform
            call(doc, "EditRebuild3")
        finally:
            if opened is not None:
                sw.CloseDoc(call(opened, "GetTitle"))
        comp_name = call(comp, "Name2")
        return f"Component '{comp_name}' inserted with its origin at ({x_mm:g}, {y_mm:g}, {z_mm:g}) mm."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def add_mate(
    mate_type: Literal["coincident", "concentric", "parallel", "perpendicular", "tangent", "distance", "angle"],
    entity1_point: list[float] | None = None,
    entity2_point: list[float] | None = None,
    entity1_name: str = "",
    entity2_name: str = "",
    distance_mm: Annotated[float, Field(ge=0, description="Distance in mm for distance mate")] = 0.0,
    angle_deg: Annotated[float, Field(ge=0, le=360, description="Angle in degrees for angle mate")] = 0.0,
    alignment: Literal["aligned", "anti_aligned", "closest"] = "closest",
    flip: bool = False,
) -> str:
    """Adds a geometric mate constraint between two components in the active assembly.

    Specify entities either by 3D points [x, y, z] mm in assembly coordinates lying on the faces/edges,
    or by entity names (e.g. 'Plane1@Part1-1@Assembly1').
    Supported mate types: coincident, concentric, parallel, perpendicular, tangent, distance, angle.
    The components move to satisfy the mate. Replaces the current selection."""
    def work():
        doc = active_doc(SW_DOC_ASSEMBLY)
        doc.ClearSelection2(True)

        if entity1_point:
            select_at(doc, "FACE", entity1_point, append=False, mark=1)
        elif entity1_name:
            if not doc.Extension.SelectByID2(entity1_name, "", 0, 0, 0, False, 1, NOTHING, 0):
                raise SWError(f"Could not select entity '{entity1_name}'.")
        else:
            raise SWError("Provide entity1_point or entity1_name.")

        if entity2_point:
            select_at(doc, "FACE", entity2_point, append=True, mark=1)
        elif entity2_name:
            if not doc.Extension.SelectByID2(entity2_name, "", 0, 0, 0, True, 1, NOTHING, 0):
                doc.ClearSelection2(True)
                raise SWError(f"Could not select entity '{entity2_name}'.")
        else:
            doc.ClearSelection2(True)
            raise SWError("Provide entity2_point or entity2_name.")

        mate_type_val = MATE_TYPES[mate_type]
        align_val = MATE_ALIGN[alignment]
        dist_val = distance_mm * MM if mate_type == "distance" else 0.0
        ang_val = angle_deg * DEG if mate_type == "angle" else 0.0

        mate_error = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        try:
            mate_feat = doc.AddMate5(
                mate_type_val, align_val, flip,
                dist_val, 0.0, 0.0, 0.0, 0.0,
                ang_val, 0.0, 0.0,
                False, False, 0, mate_error
            )
        except (pywintypes.com_error, AttributeError):
            try:
                mate_feat = doc.AddMate3(
                    mate_type_val, align_val, flip,
                    dist_val, 0.0, 0.0, 0.0, 0.0,
                    ang_val, 0.0, 0.0,
                    False, mate_error
                )
            except Exception as e:
                doc.ClearSelection2(True)
                raise SWError(f"Failed to add mate: {e}") from e

        err_code = getattr(mate_error, "value", SW_MATE_NO_ERROR)
        doc.ClearSelection2(True)
        if mate_feat is None or err_code != SW_MATE_NO_ERROR:
            raise SWError(f"Mate '{mate_type}' failed (swAddMateError code {err_code}). Check that the "
                          "selected entities are compatible with this mate type.")
        call(doc, "EditRebuild3")
        return f"Mate '{call(mate_feat, 'Name')}' ({mate_type}) added."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def check_interference() -> str:
    """Checks the active assembly for volumetric interferences (collisions) between components."""
    def work():
        doc = active_doc(SW_DOC_ASSEMBLY)
        mgr = getattr(doc, "InterferenceDetectionManager", None)
        if mgr is None:
            raise SWError("Interference detection manager is not available for this assembly.")
        try:
            mgr.TreatCoincidenceAsInterference = False
            mgr.TreatSubAssembliesAsComponents = False
            mgr.IncludeMultibodyPartInterferences = True
            count = call(mgr, "GetInterferenceCount") or 0
            interferences = call(mgr, "GetInterferences") or ()
            if count == 0 or not interferences:
                return "No interferences detected."
            rows = []
            for i, item in enumerate(interferences):
                vol_m3 = getattr(item, "Volume", 0.0) or 0.0
                vol_mm3 = vol_m3 / (MM ** 3)
                comps = getattr(item, "Components", None)
                names = []
                if comps:
                    try:
                        for c in comps:
                            c_name = getattr(c, "Name2", None) or call(c, "Name")
                            if c_name:
                                names.append(str(c_name))
                    except Exception:
                        pass
                comp_str = f" between [{', '.join(names)}]" if names else ""
                rows.append(f"{i + 1}. Interference{comp_str}: volume {vol_mm3:.2f} mm³")
            return f"Found {count} interference(s):\n" + "\n".join(rows)
        finally:
            try:
                mgr.Done()
            except Exception:
                pass
    return await com_tool(work)


# -------------------------------- Drawings --------------------------------- #
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def new_drawing(template_path: str = "") -> str:
    """Creates a new 2D drawing from the currently active part or assembly document.
    Generates 3 standard 3rd-angle projection views (Front, Top, Right) and an Isometric view."""
    def work():
        sw = get_sw()
        active = sw.ActiveDoc
        if active is None:
            raise SWError("Open or create a part or assembly first before creating a drawing.")
        model_type = call(active, "GetType")
        if model_type not in (SW_DOC_PART, SW_DOC_ASSEMBLY):
            raise SWError("The active document must be a Part or Assembly.")
        model_path = call(active, "GetPathName")
        if not model_path:
            raise SWError("Save the active part or assembly first (save_document) before creating a drawing.")
        template = template_path or sw.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_DRAWING)
        if not template or not os.path.isfile(template):
            try:
                template = sw.GetDocumentTemplate(SW_DOC_DRAWING, "", 0, 0, 0)
            except Exception:
                pass
        if not template or not os.path.isfile(template):
            raise SWError("Default drawing template not set or not found "
                          "(SolidWorks > Options > File Locations / Default Templates).")
        draw_doc = sw.NewDocument(template, 0, 0, 0)
        if draw_doc is None:
            raise SWError("SolidWorks did not create the drawing.")
        try:
            standard = bool(draw_doc.Create3rdAngleViews2(model_path))
        except pywintypes.com_error:
            standard = False
        # the standard view names are localised ("*Isometric", "*Isometrico", ...): ask the model
        names = call(active, "GetModelViewNames") or ()
        iso_name = next((n for n in names if "iso" in n.lower()), "*Isometric")
        try:
            # Isometric view placed at upper right (approx 0.22m, 0.22m)
            iso = draw_doc.CreateDrawViewFromModelView3(model_path, iso_name, 0.22, 0.22, 0.0) is not None
        except pywintypes.com_error:
            iso = False
        rebuild(draw_doc)
        if not standard and not iso:
            raise SWError("The drawing was created but no view could be placed: check that the model is "
                          "saved and rebuilds (check_model), then retry or delete the empty drawing.")
        views = (["front, top, right"] if standard else []) + (["isometric"] if iso else [])
        missing = "" if standard and iso else " (the other views could not be placed)"
        title = call(draw_doc, "GetTitle")
        return (f"New drawing '{title}' created from '{os.path.basename(model_path)}' with "
                f"{' and '.join(views)} views{missing}.")
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def insert_model_dimensions() -> str:
    """Inserts model dimensions marked for drawing across all views in the active 2D drawing."""
    def work():
        doc = active_doc(SW_DOC_DRAWING)
        try:
            annotations = doc.InsertModelAnnotations3(0, 1, True, False, False, False)
        except (pywintypes.com_error, AttributeError) as e:
            raise SWError(f"Could not insert model dimensions: {e}") from e
        count = len(annotations) if annotations else 0
        rebuild(doc)
        return f"Inserted {count} model dimension(s) into the drawing."
    return await com_tool(work)


# ----------------------------- Configurations ----------------------------- #
@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def list_configurations() -> str:
    """Lists all configurations in the active part or assembly document, indicating the active one."""
    def work():
        doc = active_doc((SW_DOC_PART, SW_DOC_ASSEMBLY))
        names = call(doc, "GetConfigurationNames")
        if not names:
            return "No configurations found."
        active_cfg = call(doc, "GetActiveConfiguration")
        active_name = call(active_cfg, "Name") if active_cfg is not None else ""
        lines = [f"Configurations for '{call(doc, 'GetTitle')}' (active: '{active_name}'):"]
        for name in names:
            marker = " [ACTIVE]" if name == active_name else ""
            lines.append(f"- {name}{marker}")
        return "\n".join(lines)
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def activate_configuration(name: str) -> str:
    """Activates an existing configuration by name in the active part or assembly."""
    def work():
        clean_name = name.strip() if name else ""
        if not clean_name:
            raise SWError("Configuration name cannot be empty.")
        doc = active_doc((SW_DOC_PART, SW_DOC_ASSEMBLY))
        names = call(doc, "GetConfigurationNames")
        if not names or clean_name not in names:
            avail = ", ".join(f"'{n}'" for n in (names or []))
            raise SWError(f"Configuration '{clean_name}' not found. Available configurations: {avail or 'none'}.")
        active = call(doc, "GetActiveConfiguration")
        if active is not None and call(active, "Name") == clean_name:
            return f"Configuration '{clean_name}' is already active."
        doc.ShowConfiguration2(clean_name)   # returns False when nothing changed: verify instead
        active = call(doc, "GetActiveConfiguration")
        if active is None or call(active, "Name") != clean_name:
            raise SWError(f"Failed to activate configuration '{clean_name}'.")
        rebuild(doc)
        return f"Activated configuration '{clean_name}'."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def add_configuration(name: str, description: str = "", comment: str = "") -> str:
    """Adds a new configuration to the active part or assembly document and activates it."""
    def work():
        clean_name = name.strip() if name else ""
        if not clean_name:
            raise SWError("Configuration name cannot be empty.")
        doc = active_doc((SW_DOC_PART, SW_DOC_ASSEMBLY))
        names = call(doc, "GetConfigurationNames")
        if names and clean_name in names:
            raise SWError(f"Configuration '{clean_name}' already exists.")
        mgr = doc.ConfigurationManager
        if mgr is None:
            raise SWError("ConfigurationManager not available.")
        cfg = mgr.AddConfiguration2(clean_name, comment, "", 0, "", description, False)
        if cfg is None:
            raise SWError(f"Failed to add configuration '{clean_name}'.")
        doc.ShowConfiguration2(clean_name)
        rebuild(doc)
        return f"Configuration '{clean_name}' created and activated."
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


# ------------------------- Topology (faces and edges) ---------------------- #
def _mm(values: Any, digits: int = 3) -> list[float]:
    return [round(float(v) / MM, digits) + 0.0 for v in values]


def _unit(values: Any) -> list[float]:
    v = [float(x) for x in values]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / n, 4) + 0.0 for x in v]


def _feature_name(face: Any) -> str:
    try:
        feat = call(face, "GetFeature")
        return str(call(feat, "Name")) if feat is not None else ""
    except Exception:
        return ""


def _face_kind(surf: Any) -> str:
    for kind in ("Plane", "Cylinder", "Cone", "Sphere", "Torus"):
        try:
            if call(surf, f"Is{kind}"):
                return kind.lower()
        except Exception:
            pass
    return "other"


def _point_inside_face(face: Any) -> list[float]:
    """A point (m) on the face and inside its boundary, away from its edges.

    The centroid of the largest display triangle lies inside the face; projecting it back onto
    the face removes the chord error on curved faces. The centre of the face box is the fallback.
    """
    guess = None
    try:
        tris = list(face.GetTessTriangles(True) or ())
        best = -1.0
        for i in range(0, len(tris) - 8, 9):
            a, b, c = tris[i:i + 3], tris[i + 3:i + 6], tris[i + 6:i + 9]
            u = [b[k] - a[k] for k in range(3)]
            v = [c[k] - a[k] for k in range(3)]
            cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            area = math.sqrt(sum(x * x for x in cross))
            if area > best:
                best, guess = area, [(a[k] + b[k] + c[k]) / 3 for k in range(3)]
    except Exception:
        guess = None
    if guess is None:
        box = list(call(face, "GetBox"))
        guess = [(box[k] + box[k + 3]) / 2 for k in range(3)]
    return list(face.GetClosestPointOn(*guess))[:3]


def face_info(face: Any) -> FaceInfo:
    """Type, a pick point and the main geometry of a face (mm)."""
    surf = call(face, "GetSurface")
    kind = _face_kind(surf)
    point = _point_inside_face(face)
    info: FaceInfo = {"type": kind, "point_mm": _mm(point),
                      "area_mm2": round(float(call(face, "GetArea")) / MM ** 2, 2)}
    if kind == "plane":
        info["normal"] = _unit(call(face, "Normal"))
    elif kind in ("cylinder", "cone"):
        p = list(call(surf, "CylinderParams" if kind == "cylinder" else "ConeParams"))
        origin, axis = p[0:3], _unit(p[3:6])
        info.update(axis=axis, axis_point_mm=_mm(origin), diameter_mm=round(2 * float(p[6]) / MM, 3))
        if kind == "cylinder":
            try:   # face normal = surface normal, reversed when FaceInSurfaceSense is True
                n = list(surf.EvaluateAtPoint(*point))[:3]
                if call(face, "FaceInSurfaceSense"):
                    n = [-x for x in n]
                radial = [point[k] - origin[k] for k in range(3)]
                along = sum(radial[k] * axis[k] for k in range(3))
                radial = [radial[k] - along * axis[k] for k in range(3)]
                info["concave"] = sum(n[k] * radial[k] for k in range(3)) < 0
            except Exception:
                pass
    elif kind == "sphere":
        p = list(call(surf, "SphereParams"))
        info.update(axis_point_mm=_mm(p[0:3]), diameter_mm=round(2 * float(p[3]) / MM, 3))
    feature = _feature_name(face)
    if feature:
        info["feature"] = feature
    return info


def _curve_kind(curve: Any) -> str:
    for kind, test in (("line", "IsLine"), ("circle", "IsCircle"), ("ellipse", "IsEllipse"), ("spline", "IsBcurve")):
        try:
            if call(curve, test):
                return kind
        except Exception:
            pass
    return "other"


def _edge_kind(edge: Any) -> str:
    """Edge type as edge_info reports it, without the rest of the geometry."""
    kind = _curve_kind(call(edge, "GetCurve"))   # GetCurve must come before GetCurveParams2
    if kind == "circle":
        p = list(call(edge, "GetCurveParams2"))
        return "circle" if math.dist(p[0:3], p[3:6]) < 1e-9 else "arc"
    return kind


def edge_info(edge: Any) -> EdgeInfo:
    """Type, mid-point (to pick it), length and the main geometry of an edge (mm)."""
    curve = call(edge, "GetCurve")
    kind = _curve_kind(curve)
    params = list(call(edge, "GetCurveParams2"))   # start xyz, end xyz, start t, end t, ...
    start, end, t0, t1 = params[0:3], params[3:6], float(params[6]), float(params[7])
    try:
        mid = list(curve.Evaluate2((t0 + t1) / 2, 0))[:3]
    except Exception:
        mid = [(start[k] + end[k]) / 2 for k in range(3)]
    point = list(edge.GetClosestPointOn(*mid))[:3]
    info: EdgeInfo = {"type": kind, "point_mm": _mm(point)}
    try:
        info["length_mm"] = round(abs(float(curve.GetLength3(min(t0, t1), max(t0, t1)))) / MM, 3)
    except Exception:
        pass
    closed = math.dist(start, end) < 1e-9
    if kind == "circle":
        c = list(call(curve, "CircleParams"))   # centre xyz, axis xyz, radius
        info.update(center_mm=_mm(c[0:3]), axis=_unit(c[3:6]), diameter_mm=round(2 * float(c[6]) / MM, 3))
        if not closed:
            info["type"] = "arc"
    if not closed:
        info.update(start_mm=_mm(start), end_mm=_mm(end))
    try:
        names = [_feature_name(f) for f in call(edge, "GetTwoAdjacentFaces2") or () if f is not None]
        info["features"] = sorted({n for n in names if n})
    except Exception:
        pass
    return info


def _part_faces(doc: Any, feature: str) -> list[Any]:
    if feature:
        return list(call(find_feature(doc, feature), "GetFaces") or ())
    faces = []
    for body in doc.GetBodies2(0, True) or ():   # visible solid bodies
        faces.extend(call(body, "GetFaces") or ())
    return faces


def _part_edges(doc: Any, feature: str) -> list[Any]:
    if not feature:
        edges = []
        for body in doc.GetBodies2(0, True) or ():
            edges.extend(call(body, "GetEdges") or ())
        return edges
    edges, seen = [], set()
    for face in _part_faces(doc, feature):
        for edge in call(face, "GetEdges") or ():
            try:   # shared edges come back once per face: keep one
                call(edge, "GetCurve")   # GetCurveParams2 needs the curve to be generated first
                key = tuple(round(float(v), 7) for v in list(call(edge, "GetCurveParams2"))[:6])
            except Exception:
                key = (id(edge),)
            if key not in seen:
                seen.add(key)
                edges.append(edge)
    return edges


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def list_faces(feature: str = "",
                     face_type: Literal["any", "plane", "cylinder", "cone", "sphere", "torus", "other"] = "any",
                     limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of faces returned")] = 60) -> FaceList:
    """Faces of the active part (or only those created by 'feature'), each with its type, a point
    lying ON it (point_mm, model coordinates: pass it to select_face_at_point, hole, shell,
    fillet face_points, create_plane face_point...), area, outward normal of planar faces, and
    diameter / axis of cylinders (concave=True for holes). Use it instead of guessing coordinates."""
    def work():
        doc = active_doc(SW_DOC_PART)
        found: list[FaceInfo] = []
        total = 0
        for face in _part_faces(doc, feature):
            if face_type != "any" and _face_kind(call(face, "GetSurface")) != face_type:
                continue
            total += 1
            if len(found) < limit:
                info = face_info(face)
                info["index"] = total
                found.append(info)
        if not total and not feature and not doc.GetBodies2(0, True):
            raise SWError("The part has no solid geometry yet.")
        return {"count": len(found), "total": total, "truncated": total > len(found), "faces": found}
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def list_edges(feature: str = "",
                     edge_type: Literal["any", "line", "circle", "arc", "ellipse", "spline", "other"] = "any",
                     limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of edges returned")] = 80) -> EdgeList:
    """Edges of the active part (or only those of the faces created by 'feature'), each with its
    type, its mid-point (point_mm, lying ON the edge: pass it to fillet / chamfer edge_points,
    select_edge_at_point, circular_pattern axis_edge_point...), length, end points, and centre /
    diameter of circles and arcs. Use it instead of guessing coordinates."""
    def work():
        doc = active_doc(SW_DOC_PART)
        found: list[EdgeInfo] = []
        total = 0
        for edge in _part_edges(doc, feature):
            if edge_type != "any" and _edge_kind(edge) != edge_type:
                continue
            total += 1
            if len(found) < limit:
                info = edge_info(edge)
                info["index"] = total
                found.append(info)
        if not total and not feature and not doc.GetBodies2(0, True):
            raise SWError("The part has no solid geometry yet.")
        return {"count": len(found), "total": total, "truncated": total > len(found), "edges": found}
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
                         circumscribed_diameter_mm: Annotated[float, Field(gt=0, description="Diameter in mm of the circle through the vertices")],
                         center_x_mm: float = 0.0, center_y_mm: float = 0.0, rotation_deg: float = 0.0) -> str:
    """Draws a regular polygon (e.g. hexagon: sides=6) inscribed in a circle of the given diameter (mm):
    the vertices lie on the circle (for a hexagon, the diameter is the distance across corners)."""
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


IID_SKETCH_LINE = pywintypes.IID("{83A33DC1-27C5-11CE-BFD4-00400513BB57}")   # ISketchLine
IID_SKETCH_ARC = pywintypes.IID("{83A33DC3-27C5-11CE-BFD4-00400513BB57}")    # ISketchArc


def _as(obj: Any, iid: Any) -> Any:
    """The same COM object seen through another of its interfaces (late binding)."""
    return win32com.client.dynamic.Dispatch(obj._oleobj_.QueryInterface(iid, pythoncom.IID_IDispatch))


def segment_count(doc: Any) -> int:
    """Number of sketch segments in the open sketch."""
    return len(call(doc.SketchManager.ActiveSketch, "GetSketchSegments") or ())


def select_sketch_entities(doc: Any, points: list[list[float]], mark: int = 0) -> None:
    """Replaces the selection with the sketch segments through the points [[x, y], ...]."""
    if not points or any(len(p) != 2 for p in points):
        raise SWError("Give one [x, y] point (sketch coordinates) on each sketch entity.")
    doc.ClearSelection2(True)
    for x, y in points:
        select_sketch_entity(doc, x, y, mark=mark)


def selected_segments_center(doc: Any, fallback: list[list[float]]) -> tuple[float, float]:
    """Centre (mm, sketch coordinates) of the box around the selected sketch segments.

    Lines count with their end points, arcs and circles with their full circle; other
    entities with the point used to pick them."""
    xs, ys = [], []
    sel = doc.SelectionManager
    for i in range(1, sel.GetSelectedObjectCount2(-1) + 1):
        seg = sel.GetSelectedObject6(i, -1)
        try:
            kind = call(seg, "GetType")
            if kind == SW_SKETCH_LINE:
                line = _as(seg, IID_SKETCH_LINE)
                for p in (call(line, "GetStartPoint2"), call(line, "GetEndPoint2")):
                    xs.append(float(call(p, "X")) / MM)
                    ys.append(float(call(p, "Y")) / MM)
                continue
            if kind == SW_SKETCH_ARC:
                arc = _as(seg, IID_SKETCH_ARC)
                c, r = call(arc, "GetCenterPoint2"), float(call(arc, "GetRadius")) / MM
                cx, cy = float(call(c, "X")) / MM, float(call(c, "Y")) / MM
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
                continue
        except Exception:
            pass
        if i <= len(fallback):
            xs.append(float(fallback[i - 1][0]))
            ys.append(float(fallback[i - 1][1]))
    if not xs:
        raise SWError("Could not read the selected sketch entities: give reference_x_mm / reference_y_mm.")
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_point(x_mm: float, y_mm: float) -> str:
    """Adds a sketch point at (x, y) mm in the open sketch (e.g. a reference for dimensions or a
    hole position)."""
    def work():
        sm = require_sketch(active_doc(SW_DOC_PART))
        if sm.CreatePoint(x_mm * MM, y_mm * MM, 0) is None:
            raise SWError("Point not created.")
        return f"Point at ({x_mm}, {y_mm})."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_trim(x_mm: float, y_mm: float) -> str:
    """Trims the sketch entity through (x, y) in the open sketch back to its nearest intersections
    with other entities: the piece you point at is removed (like Trim to closest). An entity that
    crosses nothing is deleted entirely. Replaces the selection."""
    def work():
        doc = active_doc(SW_DOC_PART)
        sm = require_sketch(doc)
        doc.ClearSelection2(True)
        select_sketch_entity(doc, x_mm, y_mm, append=False)
        try:
            seg = _pick_sketch(doc, x_mm, y_mm, False)[0]
            seg_len = float(call(seg, "GetLength")) / MM if seg is not None else None
        except Exception:
            seg_len = None
        before = sketch_signature(doc)
        # The pick point is in SKETCH coordinates (metres). SketchTrim returns False even when it
        # trims, so the result is judged from the geometry.
        sm.SketchTrim(SW_TRIM_CLOSEST, x_mm * MM, y_mm * MM, 0.0)
        doc.ClearSelection2(True)
        after = sketch_signature(doc)
        if after == before:
            raise SWError(f"Nothing was trimmed at ({x_mm:g}, {y_mm:g}): point at the piece to remove.")
        # Only a piece of the entity under the point may go, and the point must be free afterwards.
        wrong = seg_len is not None and not 0 < before[1] - after[1] <= seg_len + 1e-3
        try:
            wrong = wrong or _pick_sketch(doc, x_mm, y_mm, False)[1] < 1e-3
        except Exception:
            pass
        if wrong:
            doc.EditUndo2(1)
            raise SWError(f"SolidWorks trimmed something other than the piece at ({x_mm:g}, {y_mm:g}), so the "
                          "trim was undone. Point at the middle of the piece to remove, or delete the entity "
                          "with delete_sketch_entity and redraw it.")
        return f"Entity trimmed at ({x_mm}, {y_mm})."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_mirror(entity_points: list[list[float]], mirror_line_point: list[float]) -> str:
    """Mirrors sketch entities of the open sketch about a construction line (sketch_line with
    construction=True). entity_points: [[x, y], ...] one point on each entity to mirror;
    mirror_line_point: [x, y] a point on the construction line. Replaces the selection."""
    def work():
        if len(mirror_line_point) != 2:
            raise SWError("mirror_line_point must be [x, y].")
        doc = active_doc(SW_DOC_PART)
        require_sketch(doc)
        before = segment_count(doc)
        select_sketch_entities(doc, entity_points, mark=1)
        select_sketch_entity(doc, mirror_line_point[0], mirror_line_point[1], mark=2)
        doc.SketchMirror()
        doc.ClearSelection2(True)
        added = segment_count(doc) - before
        if added <= 0:
            raise SWError("Nothing was mirrored: the mirror line must be a construction line "
                          "(sketch_line construction=True).")
        return f"Mirrored {len(entity_points)} entities ({added} new segments)."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_linear_pattern(entity_points: list[list[float]],
                                count_x: Annotated[int, Field(ge=1, description="Instances along the first direction, original included")],
                                spacing_x_mm: Annotated[float, Field(ge=0, description="Spacing along the first direction in mm")],
                                angle_x_deg: float = 0.0,
                                count_y: Annotated[int, Field(ge=1, description="Instances along the second direction, original included")] = 1,
                                spacing_y_mm: Annotated[float, Field(ge=0, description="Spacing along the second direction in mm")] = 0.0,
                                angle_y_deg: float = 90.0) -> str:
    """Repeats sketch entities of the open sketch in a row or a grid (e.g. a row of holes before
    one cut). entity_points: [[x, y], ...] one point on each entity. count_x copies spacing_x_mm
    apart along the direction at angle_x_deg from sketch X; count_y / spacing_y_mm / angle_y_deg
    add a second direction. Replaces the selection."""
    def work():
        if count_x * count_y < 2:
            raise SWError("The pattern needs at least 2 instances in total.")
        if (count_x > 1 and spacing_x_mm <= 0) or (count_y > 1 and spacing_y_mm <= 0):
            raise SWError("Each direction with more than one instance needs a positive spacing.")
        doc = active_doc(SW_DOC_PART)
        sm = require_sketch(doc)
        before = segment_count(doc)
        select_sketch_entities(doc, entity_points)
        ok = sm.CreateLinearSketchStepAndRepeat(count_x, count_y, spacing_x_mm * MM, spacing_y_mm * MM,
                                                angle_x_deg * DEG, angle_y_deg * DEG, "",
                                                False, False, False, False, False)
        doc.ClearSelection2(True)
        if not ok or segment_count(doc) <= before:
            raise SWError("Linear sketch pattern not created.")
        return f"Linear sketch pattern: {count_x} x {count_y} instances."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def sketch_circular_pattern(entity_points: list[list[float]],
                                  count: Annotated[int, Field(ge=2, description="Instances, original included (>= 2)")],
                                  center_x_mm: float = 0.0, center_y_mm: float = 0.0,
                                  angle_deg: Annotated[float, Field(gt=0, le=360, description="Total angle in degrees")] = 360.0,
                                  reference_x_mm: float | None = None, reference_y_mm: float | None = None,
                                  rotate: bool = True) -> str:
    """Repeats sketch entities of the open sketch around a centre (e.g. bolt holes on a flange
    before one cut). entity_points: [[x, y], ...] one point on each entity; the copies are spread
    evenly over angle_deg around (center_x_mm, center_y_mm). The entities' centre is computed
    (for a circle: its centre); give reference_x_mm / reference_y_mm to set it explicitly.
    rotate=False keeps the copies' orientation. Replaces the selection."""
    def work():
        doc = active_doc(SW_DOC_PART)
        sm = require_sketch(doc)
        before = segment_count(doc)
        select_sketch_entities(doc, entity_points)
        if reference_x_mm is not None and reference_y_mm is not None:
            rx, ry = reference_x_mm, reference_y_mm
        else:
            rx, ry = selected_segments_center(doc, entity_points)
        radius = math.hypot(center_x_mm - rx, center_y_mm - ry)
        if radius < 1e-6:
            doc.ClearSelection2(True)
            raise SWError("The entities sit on the pattern centre: move the centre or the entities.")
        direction = math.atan2(center_y_mm - ry, center_x_mm - rx)   # from the entities to the centre
        step = angle_deg / count if angle_deg >= 360 else angle_deg / (count - 1)
        ok = sm.CreateCircularSketchStepAndRepeat(radius * MM, direction % (2 * math.pi), count, step * DEG,
                                                  rotate, "", False, False, False)
        doc.ClearSelection2(True)
        if not ok or segment_count(doc) <= before:
            raise SWError("Circular sketch pattern not created.")
        return (f"Circular sketch pattern: {count} instances over {angle_deg} degrees around "
                f"({center_x_mm}, {center_y_mm}), radius {round(radius, 3)} mm.")
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": False, "openWorldHint": False})
async def delete_sketch_entity(points: list[list[float]]) -> str:
    """Deletes sketch entities of the open sketch: one [x, y] point on each, in sketch
    coordinates. Their relations and dimensions are deleted with them."""
    def work():
        doc = active_doc(SW_DOC_PART)
        require_sketch(doc)
        before = segment_count(doc)
        select_sketch_entities(doc, points)
        doc.EditDelete()
        doc.ClearSelection2(True)
        removed = before - segment_count(doc)
        if removed <= 0:
            raise SWError("Nothing was deleted.")
        return f"Deleted {removed} sketch entities."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def edit_sketch(name: str) -> str:
    """Reopens an existing sketch (name from get_feature_tree, e.g. "Sketch1") to change it with
    the sketch tools; a sketch already open is closed first. Close it with end_sketch: the
    features built on it then update."""
    def work():
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        feat = find_feature(doc, name)
        if call(feat, "GetTypeName2") != "ProfileFeature":
            raise SWError(f"'{name}' is not a sketch (see get_feature_tree).")
        doc.ClearSelection2(True)
        if not feat.Select2(False, 0):
            raise SWError(f"Could not select '{name}'.")
        doc.EditSketch()
        doc.ClearSelection2(True)
        if not in_sketch(doc):
            raise SWError(f"'{name}' did not open for editing.")
        return f"Sketch '{name}' open for editing. " + sketch_axes(doc)
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
async def create_plane(offset_mm: Annotated[float, Field(ge=0, description="Offset distance in mm (offset mode)")] = 0.0,
                       reference: str = "front", face_point: list[float] | None = None,
                       flip: bool = False,
                       angle_deg: Annotated[float, Field(ge=0, lt=360, description="Angle in degrees (angle mode)")] = 0.0,
                       axis: str = "", axis_edge_point: list[float] | None = None,
                       through_points: list[list[float]] | None = None) -> str:
    """Creates a reference plane and returns its name, to use with select_plane. Three modes:

    offset (default): parallel to 'reference' ("front", "top", "right" or a plane name) or to the
      planar face through face_point ([x, y, z] mm), offset_mm away; flip=True for the other side.
    angle: angle_deg > 0 turns the plane by that angle from 'reference' (or the face) about an
      axis lying in it or parallel to it: axis = "x", "y", "z" (model axes) or the name of a
      reference axis, or axis_edge_point = a point on a straight edge. flip reverses the angle.
    through_points: three vertices [[x, y, z], ...] of the part (corners), in mm.
    Using a model axis the first time adds a hidden reference axis to the tree."""
    def work():
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        fm = doc.FeatureManager
        if through_points:
            if len(through_points) != 3:
                raise SWError("through_points needs exactly three vertices.")
            for mark, p in enumerate(through_points):
                select_at(doc, "VERTEX", p, mark=mark)
            feat = fm.InsertRefPlane(SW_REF_PLANE_COINCIDENT, 0.0, SW_REF_PLANE_COINCIDENT, 0.0,
                                     SW_REF_PLANE_COINCIDENT, 0.0)
            name = finish_feature(doc, feat, "Plane not created (are the three points corners of the "
                                             "part, and not in a line?).")
            return f"Plane '{name}' created through three vertices."
        if angle_deg > 0:
            if not axis and not axis_edge_point:
                raise SWError("The angle mode needs axis (x, y, z or an axis name) or axis_edge_point.")
            axis_feat = None
            if axis and not axis_edge_point:   # before any selection: it may create the axis
                axis_feat = model_axis(doc, axis)[0] if axis.lower() in AXIS_PLANES else find_feature(doc, axis)
            doc.ClearSelection2(True)
            if face_point:
                select_at(doc, "FACE", face_point, mark=0)
            elif not plane_feature(doc, reference).Select2(True, 0):
                raise SWError(f"Could not select '{reference}'.")
            if axis_edge_point:
                select_at(doc, "EDGE", axis_edge_point, mark=1)
            elif not axis_feat.Select2(True, 1):
                raise SWError(f"Could not select the axis '{axis}'.")
            flags = SW_REF_PLANE_ANGLE | (SW_REF_PLANE_FLIP if flip else 0)
            feat = fm.InsertRefPlane(flags, angle_deg * DEG, SW_REF_PLANE_COINCIDENT, 0.0, 0, 0.0)
            name = finish_feature(doc, feat, "Plane not created: the axis or edge must be straight and lie in "
                                             "(or be parallel to) the reference plane.")
            return f"Plane '{name}' created at {angle_deg} degrees from {'the face' if face_point else reference}."
        if offset_mm <= 0:
            raise SWError("Give offset_mm > 0 (flip for the other side), angle_deg with an axis, or through_points.")
        if face_point:
            select_at(doc, "FACE", face_point, append=False)
        elif not plane_feature(doc, reference).Select2(False, 0):
            raise SWError(f"Could not select '{reference}'.")
        flags = SW_REF_PLANE_DISTANCE | (SW_REF_PLANE_FLIP if flip else 0)
        feat = fm.InsertRefPlane(flags, offset_mm * MM, 0, 0.0, 0, 0.0)
        name = finish_feature(doc, feat, "Plane not created (is the face planar?).")
        return f"Plane '{name}' created {offset_mm} mm from {'the face' if face_point else reference}."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def create_axis(face_point: list[float] | None = None, edge_point: list[float] | None = None,
                      planes: list[str] | None = None, name: str = "") -> str:
    """Creates a reference axis, e.g. for a circular pattern, a revolve or an angled plane:
    face_point = a point on a cylindrical or conical face (its axis, e.g. the axis of a hole);
    edge_point = a point on a straight edge; or planes = two plane names / "front", "top",
    "right" (their intersection). name renames it. Returns its name."""
    def work():
        given = sum(x is not None and x != [] for x in (face_point, edge_point, planes))
        if given != 1:
            raise SWError("Give exactly one of face_point, edge_point or planes.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        if face_point:
            select_at(doc, "FACE", face_point)
        elif edge_point:
            select_at(doc, "EDGE", edge_point)
        else:
            if len(planes) != 2:
                raise SWError("planes needs exactly two planes.")
            for plane in planes:
                if not plane_feature(doc, plane).Select2(True, 0):
                    raise SWError(f"Could not select '{plane}'.")
        before = last_feature_name(doc)
        doc.InsertAxis2(True)
        feat = new_feature(doc, before, "Axis not created: the face must be cylindrical or conical, the edge "
                                        "straight, the two planes not parallel.")
        if name:
            feat.Name = name
        return f"Axis '{finish_feature(doc, feat, '')}' created."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def draft(angle_deg: Annotated[float, Field(gt=0, lt=90, description="Draft angle in degrees")],
                face_points: list[list[float]], neutral_plane: str = "",
                neutral_face_point: list[float] | None = None, flip: bool = False) -> str:
    """Tilts faces by angle_deg (draft for moulding or casting), pivoting about a neutral plane:
    neutral_plane = "front", "top", "right" or a plane name, or neutral_face_point = a point on a
    planar face of the part (e.g. the parting face). face_points: a point on each face to tilt
    ([x, y, z] mm, model coordinates; see list_faces). flip reverses the pull direction."""
    def work():
        if not face_points:
            raise SWError("Give at least one face to draft in face_points.")
        if bool(neutral_plane) == bool(neutral_face_point):
            raise SWError("Give either neutral_plane or neutral_face_point.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        doc.ClearSelection2(True)
        if neutral_face_point:
            select_at(doc, "FACE", neutral_face_point, mark=1)
        elif not plane_feature(doc, neutral_plane).Select2(True, 1):
            raise SWError(f"Could not select '{neutral_plane}'.")
        for p in face_points:
            select_at(doc, "FACE", p, mark=2)
        feat = doc.FeatureManager.InsertMultiFaceDraft(angle_deg * DEG, flip, False, SW_DRAFT_NO_PROPAGATION,
                                                       False, False)
        name = finish_feature(doc, feat, "Draft failed: the faces must meet the neutral plane or face, and "
                                         "not be parallel to it.")
        return f"Draft '{name}' created ({angle_deg} degrees on {len(face_points)} faces)."
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
    direction2/count2/spacing2_mm add a second direction for a grid.
    The first use of an axis adds a hidden reference axis "Axis X (MCP)" (Y, Z) to the tree."""
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
    edge through axis_edge_point ([x, y, z] in mm, e.g. on the rim of a central hole).
    The first use of a model axis adds a hidden reference axis "Axis Z (MCP)" (X, Y) to the tree."""
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
METRIC_PITCH = {"M2": 0.4, "M2.5": 0.45, "M3": 0.5, "M4": 0.7, "M5": 0.8, "M6": 1.0, "M8": 1.25,   # ISO 261 coarse
                "M10": 1.5, "M12": 1.75, "M14": 2.0, "M16": 2.0, "M20": 2.5, "M24": 3.0}


def thread_callout(size: str) -> tuple[float, str]:
    """Major diameter (mm) and callout of a coarse metric thread: 'm6' -> (6.0, 'M6x1.0')."""
    key = size.upper().replace(" ", "")
    if key not in METRIC_PITCH:
        raise SWError(f"Threads need a metric size: {', '.join(METRIC_PITCH)}.")
    return float(key[1:]), f"{key}x{METRIC_PITCH[key]:g}" if METRIC_PITCH[key] % 1 else f"{key}x{METRIC_PITCH[key]:.1f}"


def _perpendicular(n: tuple[float, float, float]) -> list[float]:
    """A unit vector perpendicular to n."""
    helper = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    v = [n[1] * helper[2] - n[2] * helper[1], n[2] * helper[0] - n[0] * helper[2], n[0] * helper[1] - n[1] * helper[0]]
    length = math.sqrt(sum(x * x for x in v))
    return [x / length for x in v]


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
               countersink_angle_deg: Annotated[float, Field(gt=0, lt=180, description="Countersink angle in degrees (0 < angle < 180)")] = 90.0,
               thread: bool = False,
               thread_depth_mm: Annotated[float, Field(ge=0, description="Thread length in mm (0 = the whole hole)")] = 0.0) -> str:
    """Drills round holes into a planar face, perpendicular to it.

    centers: the hole centres [[x, y, z], ...] in mm, model coordinates, all on the SAME face
      (the face is found through the first one).
    size: metric screw size ("M3" ... "M24") with fit: close / normal / loose clearance hole
      (ISO 273) or tapped = tap drill diameter; or give diameter_mm.
    depth_mm or through_all=True.
    style: counterbore / countersink for socket head screws; with a metric size their
      dimensions are standard (ISO 4762 / ISO 10642, 90 degrees), otherwise give them.
    thread=True (with a metric size, fit="tapped", style="simple") adds a cosmetic thread to each
      hole (e.g. M6x1, shown in drawings and the thread callout), over thread_depth_mm or the
      whole hole."""
    def work():
        if not centers:
            raise SWError("Give at least one centre.")
        if not through_all and depth_mm <= 0:
            raise SWError("Give depth_mm > 0 or through_all=True.")
        d, cb_d, cb_h, cs_d = hole_sizes(size, fit, diameter_mm, style, counterbore_diameter_mm,
                                         counterbore_depth_mm, countersink_diameter_mm)
        if style == "counterbore" and not through_all and cb_h >= depth_mm:
            raise SWError("The hole must be deeper than the counterbore.")
        if thread:
            if fit != "tapped" or style != "simple" or not size:
                raise SWError('thread=True needs a metric size with fit="tapped" and style="simple".')
            major, callout = thread_callout(size)
            if not through_all and thread_depth_mm > depth_mm:
                raise SWError("The thread cannot be longer than the hole.")
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
        if thread:
            u = _perpendicular(n)
            length = thread_depth_mm or (0.0 if through_all else depth_mm)
            end = SW_COSMETIC_BLIND if length else SW_COSMETIC_THROUGH
            for c in centers:   # a point on the rim of each hole, on the face
                rim = [v + d / 2 * w for v, w in zip(xyz(c, "centre"), u)]
                doc.ClearSelection2(True)
                select_at(doc, "EDGE", rim, append=False)
                feat = doc.FeatureManager.InsertCosmeticThread3(SW_COSMETIC_STANDARD_NONE, "", "", major * MM,
                                                                end, length * MM, callout)
                names.append(finish_feature(doc, feat, f"Cosmetic thread on the hole at {c} failed."))
            done += f", thread {callout}" + (f" {length:g} mm long" if length else "")
        return (f"{len(centers)} hole(s) of {d} mm ({size + ' ' + fit if size else 'custom'}), {what}{done}. "
                f"Features: {', '.join(names)}.")
    return await com_tool(work)


# ------------------------------ Sheet Metal ------------------------------- #
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def base_flange(thickness_mm: Annotated[float, Field(gt=0, description="Sheet metal thickness in mm")] = 1.5,
                      depth_mm: Annotated[float, Field(gt=0, description="Extrusion depth in mm for open sketch profiles")] = 100.0,
                      bend_radius_mm: Annotated[float, Field(gt=0, description="Internal bend radius in mm")] = 1.0,
                      reverse_thickness: bool = False) -> str:
    """Creates a sheet metal base flange from the last sketch (the one open, or the last closed one):
    an open chain of lines gives a bent sheet extruded depth_mm, a closed profile gives a flat tab.
    Adds the Sheet-Metal and Flat-Pattern features to the tree. The sheet grows on one side of the
    sketch lines (the other with reverse_thickness=True)."""
    def work():
        if thickness_mm <= 0 or depth_mm <= 0 or bend_radius_mm <= 0:
            raise SWError("Thickness, depth and bend radius must all be positive.")
        doc = active_doc(SW_DOC_PART)
        # the API wants the sketch open (as when the command is started from the UI)
        if not in_sketch(doc):
            sketch = last_sketch_feature(doc)
            doc.ClearSelection2(True)
            sketch.Select2(False, 0)
            doc.EditSketch()
            if not in_sketch(doc):
                raise SWError("Could not reopen the last sketch for the base flange.")
        sketch_name = call(doc.SketchManager.ActiveSketch, "Name")
        doc.SketchManager.AddToDB = False
        # arguments as in the SolidWorks API example: blind extrusion, K-factor 0.5, rectangular
        # relief; PCBA must be a COM 'Nothing', not None (type mismatch)
        feat = doc.FeatureManager.InsertSheetMetalBaseFlange2(
            thickness_mm * MM, reverse_thickness, bend_radius_mm * MM, depth_mm * MM, 0.0, False,
            SW_END_BLIND, SW_END_BLIND, 1, NOTHING, False, 2, 0.0001, 0.0001, 0.5, True, False, True, True)
        if feat is None:
            leave_sketch(doc)
        name = finish_feature(doc, feat, f"Base flange from '{sketch_name}' failed: the sketch must be "
                                         "an open chain of lines and arcs, or one closed profile.")
        return (f"Sheet metal base flange '{name}' created from '{sketch_name}' "
                f"(thickness {thickness_mm} mm, bend radius {bend_radius_mm} mm).")
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def edge_flange(length_mm: Annotated[float, Field(gt=0, description="Flange length in mm")],
                      angle_deg: Annotated[float, Field(gt=0, lt=180, description="Flange bend angle in degrees (default 90)")] = 90.0,
                      edge_point_mm: list[float] | None = None,
                      radius_mm: Annotated[float, Field(ge=0, description="Bend radius in mm; 0 uses part default")] = 0.0) -> str:
    """Bends a flange of length_mm along a straight edge of a sheet metal part (made with
    base_flange): edge_point_mm is a point [x, y, z] in mm on that edge, or the edge selected
    with select_edge_at_point is used. The flange is as wide as the edge; radius_mm = 0 uses the
    bend radius of the part. Replaces the current selection."""
    def work():
        if length_mm <= 0 or not 0 < angle_deg < 180:
            raise SWError("Flange length must be positive and angle between 0 and 180 degrees.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        if edge_point_mm:
            doc.ClearSelection2(True)
            select_at(doc, "EDGE", edge_point_mm, append=False)
        sel_mgr = doc.SelectionManager
        if sel_mgr.GetSelectedObjectCount2(-1) == 0:
            raise SWError("No edge selected. Select an edge first (select_edge_at_point) or pass edge_point_mm.")
        edge = sel_mgr.GetSelectedObject6(1, -1)
        if edge is None:
            raise SWError("Could not retrieve selected edge.")
        # 1. an empty sketch on the flange plane (sketch X along the edge, Y away from the sheet)
        sketch = doc.InsertSketchForEdgeFlange(edge, angle_deg * DEG, False)
        if sketch is None:
            raise SWError("SolidWorks refused that edge for a flange: it must be a straight edge of the "
                          "sheet metal body.")
        # 2. draw the flange profile in it: a rectangle with the edge as one side
        start = [v / MM for v in call(call(edge, "GetStartVertex"), "GetPoint")]
        end = [v / MM for v in call(call(edge, "GetEndVertex"), "GetPoint")]
        doc.ClearSelection2(True)
        sketch.Select2(False, 0)
        doc.EditSketch()
        if not in_sketch(doc):
            raise SWError("Could not open the flange sketch.")
        (x1, y1, _), (x2, _, _) = model_to_sketch(doc, start), model_to_sketch(doc, end)
        sm = doc.SketchManager
        sm.AddToDB = True
        sm.CreateCornerRectangle(x1 * MM, y1 * MM, 0, x2 * MM, (y1 + length_mm) * MM, 0)
        sm.AddToDB = False
        sm.InsertSketch(True)
        # 3. the flange itself (arrays of COM objects must be VARIANT SAFEARRAYs)
        edges = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [edge])
        sketches = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [sketch])
        options = 1 if radius_mm <= 0 else 0   # swInsertEdgeFlangeUseDefaultRadius
        feat = doc.FeatureManager.InsertSheetMetalEdgeFlange2(
            edges, sketches, options, angle_deg * DEG, radius_mm * MM, 0, 0.0, 0, 0.5, 0.0, 0.0, 0, NOTHING)
        name = finish_feature(doc, feat, "Edge flange failed: the edge must belong to the sheet metal body "
                                         "and the flange must not collide with the part (try another angle).")
        return f"Edge flange '{name}' created (length {length_mm} mm, angle {angle_deg} deg)."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def export_flat_pattern(path: str = "", remove_bends: bool = False) -> str:
    """Exports the flat pattern of the active sheet metal part to a DXF file.
    Existing files with the same name are replaced.
    path: absolute path or relative filename for the DXF export. Omit to export next to the part."""
    def work():
        doc = active_doc(SW_DOC_PART)
        doc_path = call(doc, "GetPathName")
        stem = os.path.splitext(os.path.basename(doc_path) if doc_path else call(doc, "GetTitle"))[0]
        target = path.strip() if path else ""
        if not target:
            if not doc_path:
                if CONFIG.projects_directory:
                    target = os.path.join(CONFIG.projects_directory, f"{stem}_flat.dxf")
                else:
                    raise SWError("Save the part first (save_document) or specify an absolute path.")
            else:
                target = os.path.join(os.path.dirname(doc_path), f"{stem}_flat.dxf")
        else:
            if not os.path.isabs(target) and CONFIG.projects_directory:
                target = resolve_save_path(target if target.lower().endswith(".dxf") else f"{target}.dxf")
            elif not os.path.isabs(target):
                if not doc_path:
                    raise SWError("The document was never saved: save it first, or provide an absolute path.")
                target = os.path.join(os.path.dirname(doc_path), target if target.lower().endswith(".dxf") else f"{target}.dxf")
            elif not target.lower().endswith(".dxf"):
                target = f"{target}.dxf"
        target = os.path.normpath(target)
        if CONFIG.projects_directory:
            norm_root = os.path.normcase(os.path.abspath(CONFIG.projects_directory))
            norm_target = os.path.normcase(os.path.abspath(target))
            if not (norm_target == norm_root or norm_target.startswith(norm_root + os.sep)):
                raise SWError(f"Access denied: target path '{target}' is outside the configured projects directory.")
        folder = os.path.dirname(target)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError as e:
                raise SWError(f"Could not overwrite existing file '{target}': {e}") from e
        options = 1 if remove_bends else 0
        ok = False
        try:
            ok = bool(doc.ExportFlatPatternView(target, options))
        except Exception:
            pass
        if not ok or not os.path.exists(target):
            try:
                model_name = doc_path or stem
                doc.ExportToDWG2(target, model_name, 1, True, [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], False, False, 1, None)
                ok = os.path.exists(target)
            except Exception:
                pass
        if not ok or not os.path.exists(target):
            raise SWError("Flat pattern export to DXF failed (ensure the part has a sheet metal feature and valid flat pattern).")
        return f"Exported flat pattern to {target}"
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
async def undo(steps: Annotated[int, Field(ge=1, description="Number of steps to undo (>= 1)")] = 1) -> str:
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


EXTRUSION_TYPES = {"Extrusion", "Cut", "BossThin", "CutThin", "Boss"}
EDITABLE_ENDS = {"blind": SW_END_BLIND, "through_all": SW_END_THROUGH_ALL, "midplane": SW_END_MIDPLANE}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": False})
async def edit_feature(name: str, depth_mm: float | None = None,
                       end_condition: Literal["blind", "through_all", "midplane"] | None = None,
                       reverse: bool | None = None, depth2_mm: float | None = None) -> str:
    """Changes an existing extrusion or extruded cut (name from get_feature_tree) and rebuilds:
    depth_mm, end_condition (blind / through_all / midplane), reverse (direction), depth2_mm
    (second direction, 0 removes it). Only the values given are replaced. For other features
    change their dimensions with get_dimensions / set_dimension."""
    def work():
        if all(v is None for v in (depth_mm, end_condition, reverse, depth2_mm)):
            raise SWError("Give at least one value to change.")
        if (depth_mm is not None and depth_mm <= 0) or (depth2_mm is not None and depth2_mm < 0):
            raise SWError("depth_mm must be positive and depth2_mm not negative.")
        doc = active_doc(SW_DOC_PART)
        leave_sketch(doc)
        feat = find_feature(doc, name)
        if call(feat, "GetTypeName2") not in EXTRUSION_TYPES:
            raise SWError(f"'{name}' is not an extrusion or a cut: change its dimensions with set_dimension.")
        data = call(feat, "GetDefinition")
        if not data.AccessSelections(doc, NOTHING):
            raise SWError(f"SolidWorks did not allow editing '{name}'.")
        try:
            if end_condition is not None:
                data.SetEndCondition(True, EDITABLE_ENDS[end_condition])
            if depth_mm is not None:
                data.SetDepth(True, depth_mm * MM)
            if reverse is not None:
                data.ReverseDirection = reverse
            if depth2_mm is not None:
                data.BothDirections = depth2_mm > 0
                if depth2_mm > 0:
                    data.SetEndCondition(False, SW_END_BLIND)
                    data.SetDepth(False, depth2_mm * MM)
            ok = feat.ModifyDefinition(data, doc, NOTHING)
        except Exception:
            data.ReleaseSelectionAccess()
            raise
        if not ok:
            data.ReleaseSelectionAccess()
            raise SWError(f"SolidWorks refused the change to '{name}' (midplane cannot have a second direction).")
        finish_feature(doc, feat, "Undo the change or choose other values.")
        changes = ", ".join(f"{k} = {v}" for k, v in (("depth_mm", depth_mm), ("end_condition", end_condition),
                                                      ("reverse", reverse), ("depth2_mm", depth2_mm)) if v is not None)
        return f"'{name}' changed ({changes}); part rebuilt."
    return await com_tool(work)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def rename_feature(name: str, new_name: str) -> str:
    """Renames a feature or sketch of the active part (e.g. "Boss-Extrude1" -> "Base plate"), so
    the tree is readable and dimensions get meaningful names (D1@Base plate)."""
    def work():
        if not new_name.strip() or "@" in new_name:
            raise SWError("The new name must not be empty or contain '@'.")
        doc = active_doc(SW_DOC_PART)
        feat = find_feature(doc, name)
        if any(call(f, "Name") == new_name for f in iter_features(doc)):
            raise SWError(f"A feature called '{new_name}' already exists.")
        feat.Name = new_name
        if call(feat, "Name") != new_name:
            raise SWError(f"SolidWorks did not accept the name '{new_name}'.")
        return f"'{name}' renamed to '{new_name}'."
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


# ---------------------------- Prompts & Resources ------------------------- #
@mcp.prompt(name="design_part", description="Guidance and recommended workflow for modeling a 3D part in SolidWorks")
def design_part(description: str = "") -> str:
    """Returns step-by-step guidance for designing a 3D mechanical part in SolidWorks."""
    prompt_text = (
        "Follow this rigorous workflow to design a mechanical part in SolidWorks:\n\n"
        "1. Preparation:\n"
        "   - Call `get_active_document_info` to inspect the current state.\n"
        "   - If no part document is open, call `new_part` to create a fresh model.\n\n"
        "2. Base Feature:\n"
        "   - Select a reference plane (`select_plane` with 'front', 'top', or 'right').\n"
        "   - Open a sketch using `start_sketch`.\n"
        "   - Draw profiles using 2D sketch tools (e.g. `sketch_rectangle`, `sketch_circle`).\n"
        "   - Call `end_sketch` or directly invoke `extrude` (or `revolve`) to build the base solid.\n\n"
        "3. Secondary Features & Details:\n"
        "   - Select model faces via `select_face_at_point` or create planes via `create_plane`.\n"
        "   - Add cuts (`cut_extrude`), standard holes (`hole` with ISO metric sizes like 'M4', 'M6'), ribs (`rib`), fillets (`fillet`), chamfers (`chamfer`), or patterns (`linear_pattern`, `circular_pattern`).\n\n"
        "4. Validation & Quality Check:\n"
        "   - Verify rebuild health, multi-body issues, and geometry faults with `check_model`.\n"
        "   - Check final dimensions and volume with `get_bounding_box` and `get_mass_properties`.\n"
        "   - Optionally assign material with `set_material` (e.g. 6061 alloy, steel) and capture a visual check with `get_screenshot`.\n\n"
        "5. Finalizing & Saving:\n"
        "   - Save the part using `save_document(path=...)`.\n"
        "   - If needed, export manufacturing formats with `export_document(formats=['step', 'stl'])`."
    )
    if description:
        prompt_text += f"\n\nGoal for this part:\n{description}"
    return prompt_text


@mcp.resource("solidworks://active/info", description="Active SolidWorks document information and version")
async def resource_active_info() -> str:
    return await get_active_document_info()


@mcp.resource("solidworks://active/feature-tree", description="Active SolidWorks document feature tree")
async def resource_feature_tree() -> str:
    return await get_feature_tree()


@mcp.resource("solidworks://materials", description="Available SolidWorks material databases and categories")
async def resource_materials() -> str:
    return await list_materials()


def apply_read_only_mode() -> list[str]:
    """Retains only tools with readOnlyHint=True. Returns the names of removed tools."""
    non_read = [
        name for name, t in mcp._tool_manager._tools.items()
        if not (getattr(t.annotations, "readOnlyHint", False) is True)
    ]
    for name in non_read:
        mcp._tool_manager._tools.pop(name, None)
    logger.info("Read-only mode active: retained %d READ tools, removed %d write tools.",
                len(mcp._tool_manager._tools), len(non_read))
    return non_read


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> None:
    global CONFIG
    parser = argparse.ArgumentParser(prog="solidworks-mcp", description=__doc__.strip().splitlines()[0])
    subparsers = parser.add_subparsers(dest="command")

    # doctor subcommand
    subparsers.add_parser("doctor", help="Check SolidWorks COM connectivity, permissions and default templates")

    # install subcommand
    install_parser = subparsers.add_parser("install", help="Configure MCP clients (Claude Desktop, Cursor, Windsurf)")
    install_parser.add_argument("--client", choices=["claude", "cursor", "windsurf", "all"], default="claude",
                                help="Target MCP client (default: claude)")

    # panel subcommand
    panel_parser = subparsers.add_parser("panel", help="Open the control panel (starts Claude Code, shows status)")
    panel_parser.add_argument("--workspace", help="work folder Claude Code opens")

    parser.add_argument("--transport", choices=TRANSPORTS,
                        default=os.environ.get("SOLIDWORKS_MCP_TRANSPORT", "stdio"),
                        help="stdio (default) for MCP clients that start the server themselves; "
                             "sse / streamable-http to serve on http://127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SOLIDWORKS_MCP_PORT", DEFAULT_PORT)),
                        help=f"port for the HTTP transports (default {DEFAULT_PORT})")
    parser.add_argument("--config", help="path to the TOML configuration file")
    parser.add_argument("--version", action="version", version=f"solidworks-mcp {__version__}")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        from solidworks_mcp.cli import run_doctor
        sys.exit(run_doctor())
    elif args.command == "install":
        from solidworks_mcp.cli import run_install
        sys.exit(run_install(client=args.client))
    elif args.command == "panel":
        from solidworks_mcp.panel import main as panel_main
        panel_main(["--workspace", args.workspace] if args.workspace else [])
        return

    try:
        CONFIG = load_config(args.config)
    except ConfigError as e:
        logger.error("%s", e)
        sys.exit(2)
    logger.info("Configuration: %s", CONFIG.source)

    if CONFIG.read_only:
        apply_read_only_mode()
        logger.info("Server is running in READ-ONLY mode.")
    elif CONFIG.allow_macro:
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
