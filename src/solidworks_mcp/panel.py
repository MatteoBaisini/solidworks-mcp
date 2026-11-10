"""Control panel: a small window that starts Claude Code in the work folder and shows,
in plain words, what SolidWorks and the assistant are doing.

It never talks to the MCP server. Its information comes from:
  * the activity log written by the Claude Code hooks (status.py), installed by the panel
    in <work folder>/.claude/settings.local.json;
  * SolidWorks itself, polled read-only through COM (version, active document);
  * the process list (is Claude Code open?).

Run it with ``solidworks-mcp panel`` or ``solidworks-mcp-panel`` (no console window).
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from solidworks_mcp import __version__
from solidworks_mcp.status import HOOK_EVENTS, HOOK_MARKER, Activity, LogReader, log_path

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
POLL_MS = 1000             # activity log refresh
PROBE_EVERY_S = 3.0        # SolidWorks refresh
PROBE_STUCK_S = 8.0        # a SolidWorks probe slower than this means "not answering"
SLOW_STEP_S = 45.0         # a tool call longer than this gets a warning
CAD_SUFFIXES = (".sldprt", ".sldasm", ".slddrw")

# --------------------------------------------------------------------------- #
# Texts (Italian when Windows is in Italian, English otherwise)
# --------------------------------------------------------------------------- #
TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "SolidWorks-MCP",
        "start_claude": "Start Claude",
        "claude_open": "Claude is open",
        "projects": "Projects folder",
        "last_part": "Open last part",
        "card_sw": "SolidWorks",
        "card_claude": "Claude",
        "card_server": "Server",
        "sw_no_com": "pywin32 missing: cannot read SolidWorks",
        "sw_closed": "Not running (Claude starts it when needed)",
        "sw_busy": "Not answering: busy, or a dialog is open in SolidWorks",
        "sw_open": "Running ({rev})",
        "sw_doc": "Active document: {doc}",
        "sw_no_doc": "No document open",
        "cl_offline": "Not open",
        "cl_idle": "Waiting for your instructions",
        "cl_thinking": "Thinking...",
        "cl_working": "Designing: {step}",
        "cl_asking": "Asking for your permission: look at the Claude window",
        "cl_slow": "No answer from SolidWorks for {s} s: check for an open dialog in SolidWorks",
        "srv_line": "Version {v} - {mode}",
        "srv_normal": "full access",
        "srv_ro": "read-only",
        "srv_macro": "macros ENABLED",
        "tab_activity": "Activity",
        "tab_new": "New part",
        "tab_quick": "Quick requests",
        "tab_guide": "Guide",
        "tab_diag": "Diagnostics",
        "col_time": "Time",
        "col_step": "Step",
        "col_result": "Result",
        "col_dur": "Duration",
        "res_ok": "OK",
        "res_err": "Error",
        "res_run": "running",
        "no_activity": "Nothing yet. Start Claude and describe the part you need.",
        "errors_n": "Failed steps in this session: {n} (Claude usually corrects them by itself)",
        "now_offline": "Claude is not open: press Start Claude",
        "template": "Kind of part",
        "project": "Project name",
        "material": "Material",
        "notes": "Other details (optional)",
        "request": "Request for Claude (you can edit it)",
        "send_new": "Start Claude with this request",
        "copy": "Copy request",
        "copied": "Copied. Click the Claude window, press Ctrl+V, then Enter.",
        "claude_running_copy": "Claude is already open: the request was copied. Paste it with Ctrl+V.",
        "no_claude": "Claude Code was not found. Run 'Repair SolidWorks-MCP' from the Start menu.",
        "no_parts": "No parts saved yet in the projects folder.",
        "run_doctor": "Run checks",
        "open_log": "Open activity log",
        "open_folder": "Open work folder",
        "doctor_wait": "Checking...",
        "hooks_on": "Status reporting is set up in {path}",
        "quick_hint": "Each button copies a request: paste it in the Claude window (Ctrl+V) and press Enter.",
        "guide": "",  # filled below
    },
    "it": {
        "title": "SolidWorks-MCP",
        "start_claude": "Avvia Claude",
        "claude_open": "Claude è aperto",
        "projects": "Cartella progetti",
        "last_part": "Apri ultimo pezzo",
        "card_sw": "SolidWorks",
        "card_claude": "Claude",
        "card_server": "Server",
        "sw_no_com": "pywin32 mancante: impossibile leggere SolidWorks",
        "sw_closed": "Non aperto (Claude lo avvia quando serve)",
        "sw_busy": "Non risponde: è occupato o c'è una finestra di dialogo aperta in SolidWorks",
        "sw_open": "Aperto ({rev})",
        "sw_doc": "Documento attivo: {doc}",
        "sw_no_doc": "Nessun documento aperto",
        "cl_offline": "Non aperto",
        "cl_idle": "In attesa delle tue istruzioni",
        "cl_thinking": "Sta ragionando...",
        "cl_working": "Sta progettando: {step}",
        "cl_asking": "Ti chiede un'autorizzazione: guarda la finestra di Claude",
        "cl_slow": "SolidWorks non risponde da {s} s: controlla se c'è una finestra di dialogo aperta",
        "srv_line": "Versione {v} - {mode}",
        "srv_normal": "accesso completo",
        "srv_ro": "sola lettura",
        "srv_macro": "macro ATTIVE",
        "tab_activity": "Attività",
        "tab_new": "Nuovo pezzo",
        "tab_quick": "Richieste rapide",
        "tab_guide": "Guida",
        "tab_diag": "Diagnostica",
        "col_time": "Ora",
        "col_step": "Passo",
        "col_result": "Esito",
        "col_dur": "Durata",
        "res_ok": "OK",
        "res_err": "Errore",
        "res_run": "in corso",
        "no_activity": "Ancora niente. Avvia Claude e descrivi il pezzo che ti serve.",
        "errors_n": "Passi non riusciti in questa sessione: {n} (di solito Claude li corregge da solo)",
        "now_offline": "Claude non è aperto: premi Avvia Claude",
        "template": "Tipo di pezzo",
        "project": "Nome del progetto",
        "material": "Materiale",
        "notes": "Altri dettagli (facoltativo)",
        "request": "Richiesta per Claude (puoi modificarla)",
        "send_new": "Avvia Claude con questa richiesta",
        "copy": "Copia richiesta",
        "copied": "Copiata. Clicca sulla finestra di Claude, premi Ctrl+V e poi Invio.",
        "claude_running_copy": "Claude è già aperto: la richiesta è stata copiata. Incollala con Ctrl+V.",
        "no_claude": "Claude Code non trovato. Esegui 'Ripara SolidWorks-MCP' dal menu Start.",
        "no_parts": "Nessun pezzo salvato nella cartella progetti.",
        "run_doctor": "Esegui controlli",
        "open_log": "Apri registro attività",
        "open_folder": "Apri cartella di lavoro",
        "doctor_wait": "Controllo in corso...",
        "hooks_on": "Comunicazione dello stato configurata in {path}",
        "quick_hint": "Ogni pulsante copia una richiesta: incollala nella finestra di Claude (Ctrl+V) e premi Invio.",
        "guide": "",
    },
}

TEXTS["en"]["guide"] = """\
HOW TO WORK WITH CLAUDE

1. Start Claude with the button at the top. A window opens: that is where you write.
   The first time it asks you to log in with your Claude account.
2. Describe the part as you would to a colleague: overall size in millimetres, what
   it is for, holes and their screws (M4, M6...), material.
   Example: "A 120 x 80 x 10 mm steel plate with four M6 holes 10 mm from the corners."
3. Claude asks once for anything important that is missing, then models the part in
   SolidWorks. You can follow it here, in the Activity tab, and in SolidWorks.
4. Check the result and ask for changes in plain words:
   "make it 150 mm long", "round the outer edges with a 3 mm radius".
5. Ask it to save: every change becomes a new version, earlier ones are kept.

GOOD TO KNOW
- One part at a time. Finish and save it before starting the next one.
- Do not click in SolidWorks while Claude is designing: wait for "Waiting for your
  instructions".
- If SolidWorks shows a dialog, close it yourself: Claude cannot see it.
- Press Esc in the Claude window to stop it. Type /clear to start a new conversation.
- When Claude asks for permission, read what it wants to do and answer 1 (yes) or 2/3.
"""

TEXTS["it"]["guide"] = """\
COME LAVORARE CON CLAUDE

1. Avvia Claude con il pulsante in alto. Si apre una finestra: è lì che scrivi.
   La prima volta ti chiede di accedere con il tuo account Claude.
2. Descrivi il pezzo come faresti con un collega: ingombro in millimetri, a cosa serve,
   fori e relative viti (M4, M6...), materiale.
   Esempio: "Una piastra in acciaio 120 x 80 x 10 mm con quattro fori M6 a 10 mm dagli angoli."
3. Claude chiede una sola volta ciò che manca di importante, poi modella il pezzo in
   SolidWorks. Puoi seguirlo qui, nella scheda Attività, e in SolidWorks.
4. Controlla il risultato e chiedi le modifiche a parole:
   "allungala a 150 mm", "raccorda gli spigoli esterni con raggio 3 mm".
5. Chiedigli di salvare: ogni modifica diventa una nuova versione, le precedenti restano.

DA SAPERE
- Un pezzo alla volta. Finiscilo e salvalo prima di iniziare il successivo.
- Non cliccare in SolidWorks mentre Claude sta progettando: aspetta "In attesa delle tue
  istruzioni".
- Se SolidWorks mostra una finestra di dialogo, chiudila tu: Claude non la vede.
- Premi Esc nella finestra di Claude per fermarlo. Scrivi /clear per una nuova conversazione.
- Quando Claude chiede un'autorizzazione, leggi cosa vuole fare e rispondi 1 (sì) o 2/3.
"""

# Tool name -> (Italian label, English label, detail template). The detail uses the tool
# arguments; when one is missing only the label is shown.
STEP_LABELS: dict[str, tuple[str, str, str]] = {
    "get_active_document_info": ("Controlla il documento attivo", "Checks the active document", ""),
    "new_part": ("Nuovo pezzo", "New part", ""),
    "new_assembly": ("Nuovo assieme", "New assembly", ""),
    "new_drawing": ("Nuova tavola", "New drawing", ""),
    "open_document": ("Apre un file", "Opens a file", "{path}"),
    "close_document": ("Chiude un documento", "Closes a document", "{name}"),
    "list_open_documents": ("Elenca i documenti aperti", "Lists open documents", ""),
    "activate_document": ("Passa al documento", "Switches to document", "{name}"),
    "save_document": ("Salva", "Saves", "{path}"),
    "export_document": ("Esporta", "Exports", "{formats}"),
    "select_plane": ("Seleziona il piano", "Selects plane", "{plane}"),
    "select_face_at_point": ("Seleziona una faccia", "Selects a face", ""),
    "select_edge_at_point": ("Seleziona uno spigolo", "Selects an edge", ""),
    "list_faces": ("Analizza le facce", "Reads the faces", "{feature}"),
    "list_edges": ("Analizza gli spigoli", "Reads the edges", "{feature}"),
    "start_sketch": ("Apre uno schizzo", "Opens a sketch", ""),
    "edit_sketch": ("Modifica lo schizzo", "Edits sketch", "{name}"),
    "end_sketch": ("Chiude lo schizzo", "Closes the sketch", ""),
    "sketch_line": ("Linea", "Line", ""),
    "sketch_rectangle": ("Rettangolo", "Rectangle", "{width_mm} x {height_mm} mm"),
    "sketch_circle": ("Cerchio", "Circle", "D{diameter_mm} mm"),
    "sketch_arc": ("Arco", "Arc", ""),
    "sketch_polygon": ("Poligono", "Polygon", "{sides}"),
    "sketch_slot": ("Asola", "Slot", "{width_mm} mm"),
    "sketch_ellipse": ("Ellisse", "Ellipse", ""),
    "sketch_spline": ("Spline", "Spline", ""),
    "sketch_point": ("Punto", "Point", ""),
    "sketch_fillet": ("Raccordo nello schizzo", "Sketch fillet", "R{radius_mm}"),
    "sketch_offset": ("Offset nello schizzo", "Sketch offset", "{distance_mm} mm"),
    "sketch_mirror": ("Specchia nello schizzo", "Sketch mirror", ""),
    "sketch_trim": ("Taglia nello schizzo", "Sketch trim", ""),
    "sketch_linear_pattern": ("Ripetizione lineare nello schizzo", "Sketch linear pattern", ""),
    "sketch_circular_pattern": ("Ripetizione circolare nello schizzo", "Sketch circular pattern", ""),
    "delete_sketch_entity": ("Cancella un elemento dello schizzo", "Deletes a sketch entity", ""),
    "convert_entities": ("Converte entità", "Converts entities", ""),
    "convert_point": ("Converte coordinate", "Converts coordinates", ""),
    "fully_define_sketch": ("Definisce completamente lo schizzo", "Fully defines the sketch", ""),
    "add_sketch_dimension": ("Quota", "Dimension", "{value}"),
    "add_sketch_relation": ("Relazione", "Relation", "{relation}"),
    "extrude": ("Estrusione", "Extrusion", "{depth_mm} mm"),
    "cut_extrude": ("Taglio estruso", "Extruded cut", "{depth_mm} mm"),
    "revolve": ("Rivoluzione", "Revolve", "{angle_deg} gradi"),
    "rib": ("Nervatura", "Rib", "{thickness_mm} mm"),
    "sweep": ("Sweep", "Sweep", ""),
    "loft": ("Loft", "Loft", ""),
    "create_plane": ("Piano di riferimento", "Reference plane", ""),
    "create_axis": ("Asse di riferimento", "Reference axis", ""),
    "fillet": ("Raccordo", "Fillet", "R{radius_mm}"),
    "chamfer": ("Smusso", "Chamfer", "{distance_mm} mm"),
    "shell": ("Svuotamento", "Shell", "{thickness_mm} mm"),
    "draft": ("Sformo", "Draft", "{angle_deg} gradi"),
    "hole": ("Fori", "Holes", "{size}"),
    "linear_pattern": ("Ripetizione lineare", "Linear pattern", "{count}"),
    "circular_pattern": ("Ripetizione circolare", "Circular pattern", "{count}"),
    "mirror": ("Specchia", "Mirror", ""),
    "edit_feature": ("Modifica la feature", "Edits feature", "{name}"),
    "rename_feature": ("Rinomina", "Renames", "{name} -> {new_name}"),
    "set_material": ("Materiale", "Material", "{name}"),
    "list_materials": ("Cerca materiali", "Looks up materials", ""),
    "set_dimension": ("Cambia quota", "Changes dimension", "{name} = {value}"),
    "get_dimensions": ("Legge le quote", "Reads dimensions", ""),
    "suppress_feature": ("Sopprime / riattiva", "Suppresses / unsuppresses", "{name}"),
    "delete_feature": ("Cancella la feature", "Deletes feature", "{name}"),
    "undo": ("Annulla", "Undo", ""),
    "check_model": ("Verifica il modello", "Checks the model", ""),
    "get_bounding_box": ("Misura l'ingombro", "Measures the overall size", ""),
    "get_mass_properties": ("Calcola massa e volume", "Computes mass and volume", ""),
    "get_feature_tree": ("Legge l'albero delle feature", "Reads the feature tree", ""),
    "get_screenshot": ("Guarda il modello", "Looks at the model", ""),
    "measure": ("Misura", "Measures", ""),
    "zoom_to_fit": ("Adatta la vista", "Zooms to fit", ""),
    "get_equations": ("Legge le equazioni", "Reads equations", ""),
    "add_equation": ("Aggiunge un'equazione", "Adds an equation", "{equation}"),
    "delete_equation": ("Cancella un'equazione", "Deletes an equation", ""),
    "get_custom_properties": ("Legge le proprietà", "Reads properties", ""),
    "set_custom_property": ("Imposta la proprietà", "Sets property", "{name}"),
    "list_configurations": ("Elenca le configurazioni", "Lists configurations", ""),
    "activate_configuration": ("Attiva la configurazione", "Activates configuration", "{name}"),
    "add_configuration": ("Nuova configurazione", "New configuration", "{name}"),
    "insert_component": ("Inserisce un componente", "Inserts a component", "{path}"),
    "add_mate": ("Accoppiamento", "Mate", "{mate_type}"),
    "check_interference": ("Controlla le interferenze", "Checks interference", ""),
    "insert_model_dimensions": ("Inserisce le quote in tavola", "Inserts model dimensions", ""),
    "base_flange": ("Lamiera: flangia base", "Sheet metal: base flange", "{thickness_mm} mm"),
    "edge_flange": ("Lamiera: flangia su spigolo", "Sheet metal: edge flange", "{length_mm} mm"),
    "export_flat_pattern": ("Esporta lo sviluppo", "Exports the flat pattern", ""),
    "execute_solidworks_macro": ("Esegue una macro", "Runs a macro", ""),
}


def lang() -> str:
    forced = os.environ.get("SOLIDWORKS_MCP_LANG", "").lower()
    if forced in TEXTS:
        return forced
    try:
        return "it" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x10 else "en"
    except Exception:
        return "en"


def _num(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return ", ".join(str(_num(v)) for v in value)
    return value


def describe_step(tool: str, args: dict[str, Any], language: str = "en") -> str:
    """Plain-language label of a tool call, e.g. 'Extrusion 10 mm'."""
    it, en, detail = STEP_LABELS.get(tool, ("", "", ""))
    label = (it if language == "it" else en) or tool.replace("_", " ").capitalize()
    if detail:
        try:
            text = detail.format(**{k: _num(v) for k, v in (args or {}).items()})
        except (KeyError, IndexError, ValueError):
            text = ""
        if text.strip():
            label += " " + text
    return label


# --------------------------------------------------------------------------- #
# Part templates for people who do not know how to ask
# --------------------------------------------------------------------------- #
@dataclass
class Template:
    key: str
    names: tuple[str, str]               # (it, en)
    fields: list[tuple[str, str, str, str]]  # (key, it label, en label, default)
    prompt: tuple[str, str]              # (it, en) with {field} placeholders


TEMPLATES = [
    Template("plate", ("Piastra con fori", "Plate with holes"),
             [("l", "Lunghezza (mm)", "Length (mm)", "120"), ("w", "Larghezza (mm)", "Width (mm)", "80"),
              ("t", "Spessore (mm)", "Thickness (mm)", "10"), ("screw", "Vite (es. M6)", "Screw (e.g. M6)", "M6"),
              ("e", "Distanza fori dai bordi (mm)", "Hole distance from edges (mm)", "10")],
             ("Una piastra {l} x {w} x {t} mm con quattro fori passanti per viti {screw}, a {e} mm dai bordi, "
              "e spigoli verticali raccordati con raggio 2 mm.",
              "A {l} x {w} x {t} mm plate with four through holes for {screw} screws, {e} mm from the edges, "
              "and the vertical edges rounded with a 2 mm radius.")),
    Template("bracket", ("Staffa a L", "L bracket"),
             [("a", "Lato orizzontale (mm)", "Horizontal leg (mm)", "60"), ("b", "Lato verticale (mm)", "Vertical leg (mm)", "50"),
              ("w", "Larghezza (mm)", "Width (mm)", "40"), ("t", "Spessore (mm)", "Thickness (mm)", "5"),
              ("screw", "Vite (es. M5)", "Screw (e.g. M5)", "M5")],
             ("Una staffa a L larga {w} mm e spessa {t} mm, con un lato orizzontale di {a} mm e uno verticale di {b} mm "
              "(misure esterne), raccordo interno raggio {t} mm e due fori per viti {screw} su ciascun lato.",
              "An L bracket {w} mm wide and {t} mm thick, with a {a} mm horizontal leg and a {b} mm vertical leg "
              "(outside dimensions), a {t} mm inner fillet and two holes for {screw} screws on each leg.")),
    Template("flange", ("Flangia", "Flange"),
             [("od", "Diametro esterno (mm)", "Outer diameter (mm)", "120"), ("id", "Foro centrale (mm)", "Centre bore (mm)", "50"),
              ("t", "Spessore (mm)", "Thickness (mm)", "12"), ("n", "Numero fori", "Number of holes", "6"),
              ("pcd", "Diametro circonferenza fori (mm)", "Bolt circle diameter (mm)", "90"),
              ("screw", "Vite (es. M8)", "Screw (e.g. M8)", "M8")],
             ("Una flangia circolare diametro {od} mm, spessore {t} mm, foro centrale diametro {id} mm e {n} fori per viti "
              "{screw} equidistanti su una circonferenza di diametro {pcd} mm. Smusso 1 mm sugli spigoli del foro centrale.",
              "A round flange, {od} mm diameter, {t} mm thick, with a {id} mm centre bore and {n} holes for {screw} screws "
              "evenly spaced on a {pcd} mm bolt circle. 1 mm chamfer on the centre bore edges.")),
    Template("spacer", ("Distanziale / boccola", "Spacer / bushing"),
             [("od", "Diametro esterno (mm)", "Outer diameter (mm)", "20"), ("id", "Diametro interno (mm)", "Inner diameter (mm)", "8.5"),
              ("h", "Altezza (mm)", "Height (mm)", "15")],
             ("Un distanziale cilindrico diametro esterno {od} mm, foro passante diametro {id} mm, altezza {h} mm, "
              "con smussi di 0,5 mm su tutti gli spigoli circolari.",
              "A cylindrical spacer, {od} mm outer diameter, {id} mm through bore, {h} mm high, "
              "with 0.5 mm chamfers on every circular edge.")),
    Template("box", ("Scatola / contenitore", "Box / enclosure"),
             [("l", "Lunghezza esterna (mm)", "Outside length (mm)", "150"), ("w", "Larghezza esterna (mm)", "Outside width (mm)", "100"),
              ("h", "Altezza esterna (mm)", "Outside height (mm)", "60"), ("t", "Spessore pareti (mm)", "Wall thickness (mm)", "2.5")],
             ("Una scatola aperta in alto, misure esterne {l} x {w} x {h} mm, pareti e fondo spessi {t} mm, "
              "spigoli verticali esterni raccordati con raggio 5 mm.",
              "A box open at the top, outside size {l} x {w} x {h} mm, walls and bottom {t} mm thick, "
              "outside vertical edges rounded with a 5 mm radius.")),
    Template("free", ("Descrivo io il pezzo", "I describe the part"),
             [],
             ("", "")),
]

MATERIALS = {
    "it": ["", "Acciaio S235 (strutturale)", "Acciaio inox AISI 304", "Alluminio 6061", "Ottone", "PLA (stampa 3D)",
           "ABS", "Nylon PA6"],
    "en": ["", "Structural steel S235", "Stainless steel AISI 304", "Aluminium 6061", "Brass", "PLA (3D printing)",
           "ABS", "Nylon PA6"],
}

PROMPT_FRAME = {
    "it": ("Progetto: {project}. {body}{material}{notes} Salvalo nella cartella del progetto e poi verificalo.",
           " Materiale: {m}.", " Altri dettagli: {n}"),
    "en": ("Project: {project}. {body}{material}{notes} Save it in the project folder, then check it.",
           " Material: {m}.", " Other details: {n}"),
}

QUICK_REQUESTS = {
    "it": [("Verifica il pezzo", "Verifica il pezzo attivo: errori, ingombro, massa e uno screenshot. Dimmi se c'è qualcosa che non va."),
           ("Mostrami il pezzo", "Fammi uno screenshot isometrico del pezzo e descrivimi cosa vedi."),
           ("Spiegami cosa hai fatto", "Spiegami in parole semplici come hai costruito il pezzo, feature per feature."),
           ("Salva una nuova versione", "Salva il pezzo come nuova versione nella cartella del progetto e aggiorna il README."),
           ("Esporta STEP", "Esporta il pezzo attivo in STEP."),
           ("Esporta per la stampa 3D", "Esporta il pezzo attivo in STL per la stampa 3D."),
           ("Annulla l'ultimo passo", "Annulla l'ultima modifica che hai fatto al pezzo e dimmi com'è ora."),
           ("Cambia una misura", "Elenca le quote principali del pezzo, così ti dico quale cambiare.")],
    "en": [("Check the part", "Check the active part: errors, overall size, mass and a screenshot. Tell me if anything is wrong."),
           ("Show me the part", "Take an isometric screenshot of the part and describe what you see."),
           ("Explain what you did", "Explain in simple words how you built the part, feature by feature."),
           ("Save a new version", "Save the part as a new version in the project folder and update the README."),
           ("Export STEP", "Export the active part to STEP."),
           ("Export for 3D printing", "Export the active part to STL for 3D printing."),
           ("Undo the last step", "Undo the last change you made to the part and tell me how it looks now."),
           ("Change a dimension", "List the main dimensions of the part, so I can tell you which one to change.")],
}


def build_prompt(template: Template, values: dict[str, str], project: str, material: str, notes: str,
                 language: str = "en") -> str:
    """Request text for Claude from a filled-in template."""
    i = 0 if language == "it" else 1
    body = template.prompt[i].format(**{k: (v.strip() or "?") for k, v in values.items()}) if template.prompt[i] else ""
    frame, mat, note = PROMPT_FRAME["it" if language == "it" else "en"]
    default_project = "nuovo_pezzo" if language == "it" else "new_part"
    text = frame.format(project=project.strip() or default_project,
                        body=body,
                        material=mat.format(m=material.strip()) if material.strip() else "",
                        notes=note.format(n=notes.strip().rstrip(".") + ".") if notes.strip() else "")
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Work folder, hooks and processes
# --------------------------------------------------------------------------- #
def default_workspace() -> Path:
    cwd = Path.cwd()
    if (cwd / ".mcp.json").is_file():
        return cwd
    docs = Path(os.path.expanduser("~")) / "Documents"
    return docs / "SolidWorks-MCP"


def hook_command(python: str | None = None) -> str:
    """Command Claude Code runs for every hook event (forward slashes work in cmd and bash)."""
    exe = PureWindowsPath(python or sys.executable)   # Windows paths, also when tested elsewhere
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    return f'"{exe.as_posix()}" -m {HOOK_MARKER}'


def ensure_hooks(workspace: Path, command: str | None = None) -> tuple[Path, bool]:
    """Adds the status hooks to <workspace>/.claude/settings.local.json.

    Other settings and the user's own hooks are kept. Returns (path, changed)."""
    command = command or hook_command()
    path = workspace / ".claude" / "settings.local.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            shutil.copyfile(path, path.with_suffix(".json.invalid"))
    before = json.dumps(data, sort_keys=True)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    for event, matcher in HOOK_EVENTS.items():
        groups = [g for g in hooks.get(event, []) if isinstance(g, dict) and not any(
            HOOK_MARKER in str(h.get("command", "")) for h in g.get("hooks", []) if isinstance(h, dict))]
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks[event] = groups + [group]
    data["hooks"] = hooks
    changed = json.dumps(data, sort_keys=True) != before
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path, changed


def find_claude() -> str | None:
    local = Path(os.path.expanduser("~")) / ".local" / "bin" / "claude.exe"
    if local.is_file():
        return str(local)
    return shutil.which("claude")


def claude_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=5, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude.exe" in out.lower()


def start_claude(workspace: Path, prompt: str = "") -> bool:
    claude = find_claude()
    if not claude:
        return False
    args = [claude] + ([prompt] if prompt else [])
    subprocess.Popen(args, cwd=str(workspace), creationflags=CREATE_NEW_CONSOLE)
    return True


def projects_dir(workspace: Path) -> Path:
    toml = workspace / "solidworks-mcp.toml"
    if toml.is_file():
        try:
            from solidworks_mcp.config import load_config
            folder = load_config(toml).projects_directory
            if folder:
                return Path(folder)
        except Exception:
            pass
    return workspace / "projects"


def latest_cad_file(folder: Path) -> Path | None:
    best: tuple[float, Path] | None = None
    if folder.is_dir():
        for p in folder.rglob("*"):
            if p.suffix.lower() in CAD_SUFFIXES and not p.name.startswith("~$"):
                m = p.stat().st_mtime
                if best is None or m > best[0]:
                    best = (m, p)
    return best[1] if best else None


def server_summary(workspace: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"version": __version__, "read_only": False, "macro": False}
    toml = workspace / "solidworks-mcp.toml"
    if toml.is_file():
        try:
            from solidworks_mcp.config import load_config
            cfg = load_config(toml)
            info.update(read_only=cfg.read_only, macro=cfg.allow_macro)
        except Exception as e:
            info["error"] = str(e)
    return info


# --------------------------------------------------------------------------- #
# SolidWorks probe (own thread, read-only)
# --------------------------------------------------------------------------- #
DOC_TYPES = {1: ("Parte", "Part"), 2: ("Assieme", "Assembly"), 3: ("Tavola", "Drawing")}


def _get(obj: Any, name: str) -> Any:
    value = getattr(obj, name)
    return value() if callable(value) else value


class SolidWorksProbe:
    """Polls SolidWorks in a background thread. Never starts it and never changes anything."""

    def __init__(self) -> None:
        self.result: dict[str, Any] = {"state": "unknown"}
        self.started = 0.0
        self.busy = False
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="sw-probe", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def stuck(self) -> bool:
        return self.busy and time.monotonic() - self.started > PROBE_STUCK_S

    def _loop(self) -> None:
        try:
            import pythoncom
            import win32com.client
        except ImportError:
            self.result = {"state": "no_com"}
            return
        pythoncom.CoInitialize()
        while not self._stop.is_set():
            self.busy, self.started = True, time.monotonic()
            self.result = self._probe(win32com.client)
            self.busy = False
            self._stop.wait(PROBE_EVERY_S)

    @staticmethod
    def _probe(client: Any) -> dict[str, Any]:
        try:
            sw = client.GetActiveObject("SldWorks.Application")
        except Exception:
            return {"state": "closed"}
        try:
            info: dict[str, Any] = {"state": "open", "rev": str(_get(sw, "RevisionNumber"))}
            doc = _get(sw, "ActiveDoc")
            if doc is not None:
                info["doc"] = str(_get(doc, "GetTitle"))
                info["doc_type"] = int(_get(doc, "GetType"))
            return info
        except Exception:
            return {"state": "open", "rev": "?"}
        finally:
            del sw  # never keep SolidWorks alive after the user closes it


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
class Panel:
    def __init__(self, workspace: Path) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.ws = workspace
        self.lang = lang()
        self.T = TEXTS[self.lang]
        self.activity = Activity()
        self.reader = LogReader()
        self.probe = SolidWorksProbe()
        self.claude_alive = False
        self._claude_checked = 0.0
        self._shown_steps = 0

        # Replay the log: the history of recent sessions stays visible.
        for rec in self.reader.read_new():
            self.activity.apply(rec)

        self.root = tk.Tk()
        self.root.title(f"{self.T['title']} {__version__}")
        self.root.geometry("860x640")
        self.root.minsize(720, 520)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self._build()
        try:
            path, _ = ensure_hooks(self.ws)
            self.hooks_info = self.T["hooks_on"].format(path=path)
        except OSError as e:
            self.hooks_info = str(e)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._tick()

    # ----------------------------------------------------------------- layout
    def _build(self) -> None:
        tk, ttk, T = self.tk, self.ttk, self.T
        root = self.root
        style = ttk.Style()
        style.configure("Now.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 6))

        top = ttk.Frame(root, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.btn_claude = ttk.Button(top, text=T["start_claude"], style="Big.TButton", command=self.on_start_claude)
        self.btn_claude.pack(side="left")
        ttk.Button(top, text=T["projects"], command=self.on_projects).pack(side="left", padx=(10, 0))
        ttk.Button(top, text=T["last_part"], command=self.on_last_part).pack(side="left", padx=(6, 0))

        self.now = ttk.Label(root, text="", style="Now.TLabel", padding=(14, 6))
        self.now.pack(fill="x")
        self.now_sub = ttk.Label(root, text="", foreground="#a33", padding=(14, 0))
        self.now_sub.pack(fill="x")

        cards = ttk.Frame(root, padding=(12, 4))
        cards.pack(fill="x")
        self.card_vals = {}
        for i, key in enumerate(("card_sw", "card_claude", "card_server")):
            box = ttk.LabelFrame(cards, text=T[key], style="Card.TLabelframe", padding=8)
            box.grid(row=0, column=i, sticky="nsew", padx=4)
            cards.columnconfigure(i, weight=1, uniform="c")
            dot = tk.Canvas(box, width=14, height=14, highlightthickness=0)
            dot.grid(row=0, column=0, sticky="n", pady=3)
            lab = ttk.Label(box, text="", wraplength=230, justify="left")
            lab.grid(row=0, column=1, sticky="w", padx=(6, 0))
            self.card_vals[key] = (dot, lab)

        self.msg = ttk.Label(root, text="", foreground="#2a6", padding=(14, 2))
        self.msg.pack(fill="x")

        nb = ttk.Notebook(root, padding=(8, 4, 8, 8))
        nb.pack(fill="both", expand=True)
        nb.add(self._tab_activity(nb), text=T["tab_activity"])
        nb.add(self._tab_new(nb), text=T["tab_new"])
        nb.add(self._tab_quick(nb), text=T["tab_quick"])
        nb.add(self._tab_text(nb, T["guide"]), text=T["tab_guide"])
        nb.add(self._tab_diag(nb), text=T["tab_diag"])

    def _tab_activity(self, parent: Any) -> Any:
        ttk, T = self.ttk, self.T
        frame = ttk.Frame(parent, padding=6)
        cols = ("time", "step", "result", "dur")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=12)
        for c, title, w, stretch in (("time", T["col_time"], 70, False), ("step", T["col_step"], 420, True),
                                     ("result", T["col_result"], 90, False), ("dur", T["col_dur"], 70, False)):
            tree.heading(c, text=title)
            tree.column(c, width=w, stretch=stretch, anchor="w")
        tree.tag_configure("err", foreground="#b00")
        tree.tag_configure("run", foreground="#06c")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.detail = ttk.Label(frame, text=T["no_activity"], wraplength=780, justify="left", padding=(0, 6))
        self.detail.grid(row=1, column=0, columnspan=2, sticky="we")
        tree.bind("<<TreeviewSelect>>", self._on_select_step)
        self.tree = tree
        return frame

    def _tab_new(self, parent: Any) -> Any:
        tk, ttk, T = self.tk, self.ttk, self.T
        i = 0 if self.lang == "it" else 1
        frame = ttk.Frame(parent, padding=10)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=T["template"]).grid(row=0, column=0, sticky="w")
        self.tpl_var = tk.StringVar(value=TEMPLATES[0].names[i])
        combo = ttk.Combobox(frame, textvariable=self.tpl_var, state="readonly",
                             values=[t.names[i] for t in TEMPLATES], width=34)
        combo.grid(row=0, column=1, sticky="w", pady=2)
        combo.bind("<<ComboboxSelected>>", lambda e: self._render_fields())
        ttk.Label(frame, text=T["project"]).grid(row=1, column=0, sticky="w")
        self.project_var = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.project_var, width=36).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(frame, text=T["material"]).grid(row=2, column=0, sticky="w")
        self.material_var = tk.StringVar(value="")
        ttk.Combobox(frame, textvariable=self.material_var, values=MATERIALS[self.lang], width=34).grid(
            row=2, column=1, sticky="w", pady=2)
        self.fields_frame = ttk.Frame(frame)
        self.fields_frame.grid(row=3, column=0, columnspan=2, sticky="we", pady=(6, 0))
        ttk.Label(frame, text=T["notes"]).grid(row=4, column=0, sticky="nw", pady=(6, 0))
        self.notes_var = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.notes_var).grid(row=4, column=1, sticky="we", pady=(6, 0))
        ttk.Label(frame, text=T["request"]).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.prompt_text = tk.Text(frame, height=6, wrap="word", font=("Segoe UI", 10))
        self.prompt_text.grid(row=6, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(6, weight=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text=T["send_new"], command=self.on_send_prompt).pack(side="left")
        ttk.Button(buttons, text=T["copy"], command=lambda: self._copy(self._prompt())).pack(side="left", padx=6)
        for var in (self.project_var, self.material_var, self.notes_var):
            var.trace_add("write", lambda *a: self._update_prompt())
        self.field_vars: dict[str, Any] = {}
        self._render_fields()
        return frame

    def _template(self) -> Template:
        i = 0 if self.lang == "it" else 1
        return next((t for t in TEMPLATES if t.names[i] == self.tpl_var.get()), TEMPLATES[0])

    def _render_fields(self) -> None:
        tk, ttk = self.tk, self.ttk
        for w in self.fields_frame.winfo_children():
            w.destroy()
        self.field_vars = {}
        for row, (key, it, en, default) in enumerate(self._template().fields):
            ttk.Label(self.fields_frame, text=it if self.lang == "it" else en, width=32).grid(row=row, column=0, sticky="w")
            var = tk.StringVar(value=default)
            var.trace_add("write", lambda *a: self._update_prompt())
            ttk.Entry(self.fields_frame, textvariable=var, width=12).grid(row=row, column=1, sticky="w", pady=1)
            self.field_vars[key] = var
        self._update_prompt()

    def _update_prompt(self) -> None:
        text = build_prompt(self._template(), {k: v.get() for k, v in self.field_vars.items()},
                            self.project_var.get(), self.material_var.get(), self.notes_var.get(), self.lang)
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", text)

    def _prompt(self) -> str:
        return " ".join(self.prompt_text.get("1.0", "end").split())

    def _tab_quick(self, parent: Any) -> Any:
        ttk = self.ttk
        frame = ttk.Frame(parent, padding=10)
        ttk.Label(frame, text=self.T["quick_hint"], wraplength=780).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for n, (label, text) in enumerate(QUICK_REQUESTS[self.lang]):
            ttk.Button(frame, text=label, width=30, command=lambda t=text: self._copy(t)).grid(
                row=1 + n // 2, column=n % 2, sticky="w", padx=4, pady=3)
        return frame

    def _tab_text(self, parent: Any, text: str) -> Any:
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(parent, padding=6)
        box = tk.Text(frame, wrap="word", font=("Segoe UI", 10), relief="flat", padx=10, pady=8)
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)
        return frame

    def _tab_diag(self, parent: Any) -> Any:
        tk, ttk, T = self.tk, self.ttk, self.T
        frame = ttk.Frame(parent, padding=8)
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        ttk.Button(bar, text=T["run_doctor"], command=self.on_doctor).pack(side="left")
        ttk.Button(bar, text=T["open_folder"], command=lambda: self._open(self.ws)).pack(side="left", padx=6)
        ttk.Button(bar, text=T["open_log"], command=lambda: self._open(log_path().parent)).pack(side="left")
        self.diag = tk.Text(frame, wrap="word", font=("Consolas", 9), height=14)
        self.diag.pack(fill="both", expand=True, pady=(8, 0))
        return frame

    # ---------------------------------------------------------------- actions
    def on_start_claude(self) -> None:
        if self.claude_alive and self.activity.state != "offline":
            self._flash(self.T["claude_open"])
            return
        self._launch("")

    def on_send_prompt(self) -> None:
        prompt = self._prompt()
        if self.claude_alive and self.activity.state != "offline":
            self._copy(prompt, self.T["claude_running_copy"])
        else:
            self._launch(prompt)

    def _launch(self, prompt: str) -> None:
        try:
            ensure_hooks(self.ws)
            ok = start_claude(self.ws, prompt)
        except OSError as e:
            self._flash(str(e), error=True)
            return
        if not ok:
            self._flash(self.T["no_claude"], error=True)
        self._claude_checked = 0.0

    def on_projects(self) -> None:
        folder = projects_dir(self.ws)
        folder.mkdir(parents=True, exist_ok=True)
        self._open(folder)

    def on_last_part(self) -> None:
        part = latest_cad_file(projects_dir(self.ws))
        if part is None:
            self._flash(self.T["no_parts"], error=True)
        else:
            self._open(part)

    def on_doctor(self) -> None:
        self.diag.delete("1.0", "end")
        self.diag.insert("end", self.T["doctor_wait"] + "\n")
        info = self.hooks_info

        def run() -> None:
            buf = io.StringIO()
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except ImportError:
                pass
            try:
                from solidworks_mcp.cli import run_doctor
                with contextlib.redirect_stdout(buf):
                    run_doctor()
            except Exception as e:  # report, never crash the window
                buf.write(f"\n{type(e).__name__}: {e}\n")
            text = buf.getvalue() + f"\n{info}\nLog: {log_path()}\n"
            self.root.after(0, lambda: (self.diag.delete("1.0", "end"), self.diag.insert("end", text)))

        threading.Thread(target=run, daemon=True).start()

    def _copy(self, text: str, message: str | None = None) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._flash(message or self.T["copied"])

    def _flash(self, text: str, error: bool = False) -> None:
        self.msg.configure(text=text, foreground="#b00" if error else "#2a6")
        self.root.after(8000, lambda: self.msg.configure(text="") if self.msg.cget("text") == text else None)

    def _open(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as e:
            self._flash(str(e), error=True)

    def _on_select_step(self, _event: Any) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.activity.steps):
            step = self.activity.steps[idx]
            args = ", ".join(f"{k}={v}" for k, v in step.input.items())
            text = f"{step.tool}({args})"
            if step.error:
                text += f"\n{self.T['res_err']}: {step.error}"
            self.detail.configure(text=text)

    def _close(self) -> None:
        self.probe.stop()
        self.root.destroy()

    # ----------------------------------------------------------------- update
    def _tick(self) -> None:
        for rec in self.reader.read_new():
            self.activity.apply(rec)
        now = time.monotonic()
        if now - self._claude_checked > 3.0:
            self._claude_checked = now
            alive = claude_running()
            if not alive and self.activity.state != "offline":
                self.activity.apply({"event": "SessionEnd", "t": time.time()})
            self.claude_alive = alive
        self._render()
        self.root.after(POLL_MS, self._tick)

    def _dot(self, key: str, color: str) -> None:
        dot, _ = self.card_vals[key]
        dot.delete("all")
        dot.create_oval(2, 2, 12, 12, fill=color, outline=color)

    def _render(self) -> None:
        T, a = self.T, self.activity
        green, amber, red, grey, blue = "#2a2", "#e90", "#c22", "#999", "#27c"

        # SolidWorks
        r = self.probe.result
        if self.probe.stuck():
            sw_text, sw_col = T["sw_busy"], amber
        elif r["state"] == "no_com":
            sw_text, sw_col = T["sw_no_com"], red
        elif r["state"] == "open":
            sw_text = T["sw_open"].format(rev=r.get("rev", "?")) + "\n"
            if r.get("doc"):
                kind = DOC_TYPES.get(r.get("doc_type", 0), ("", ""))[0 if self.lang == "it" else 1]
                sw_text += T["sw_doc"].format(doc=f"{r['doc']} ({kind})" if kind else r["doc"])
            else:
                sw_text += T["sw_no_doc"]
            sw_col = green
        elif r["state"] == "closed":
            sw_text, sw_col = T["sw_closed"], grey
        else:
            sw_text, sw_col = "...", grey
        self._dot("card_sw", sw_col)
        self.card_vals["card_sw"][1].configure(text=sw_text)

        # Claude
        sub = ""
        if not self.claude_alive or a.state == "offline":
            # Claude Code open in another folder does not report here: it counts as closed.
            state_text, col = T["cl_offline"], grey
        elif a.state == "working" and a.current:
            state_text = T["cl_working"].format(step=describe_step(a.current.tool, a.current.input, self.lang))
            col = blue
            elapsed = time.time() - a.current.t
            if elapsed > SLOW_STEP_S:
                sub, col = T["cl_slow"].format(s=int(elapsed)), amber
        elif a.state == "thinking":
            state_text, col = T["cl_thinking"], blue
        elif a.state == "asking":
            state_text, col = T["cl_asking"], amber
        else:
            state_text, col = T["cl_idle"], green
        self._dot("card_claude", col)
        self.card_vals["card_claude"][1].configure(text=state_text)
        self.now.configure(text=T["now_offline"] if col == grey else state_text)
        if not sub and a.errors:
            sub = T["errors_n"].format(n=a.errors)
        self.now_sub.configure(text=sub)
        self.btn_claude.configure(text=T["claude_open"] if self.claude_alive and a.state != "offline"
                                  else T["start_claude"])

        # Server
        s = server_summary(self.ws) if not hasattr(self, "_srv") else self._srv
        self._srv = s
        mode = T["srv_ro"] if s["read_only"] else T["srv_normal"]
        if s["macro"]:
            mode += ", " + T["srv_macro"]
        self._dot("card_server", red if s.get("error") else (amber if s["macro"] else green))
        self.card_vals["card_server"][1].configure(text=s.get("error") or T["srv_line"].format(v=s["version"], mode=mode))

        self._render_steps()

    def _render_steps(self) -> None:
        T, steps = self.T, self.activity.steps
        if len(steps) < self._shown_steps:  # trimmed: redraw everything
            self.tree.delete(*self.tree.get_children())
            self._shown_steps = 0
        for idx, step in enumerate(steps):
            iid = str(idx)
            if step.ok is None:
                result, tag, dur = T["res_run"], "run", f"{int(time.time() - step.t)} s"
            else:
                result, tag = (T["res_ok"], "") if step.ok else (T["res_err"], "err")
                dur = f"{(step.finished or step.t) - step.t:.1f} s"
            values = (time.strftime("%H:%M:%S", time.localtime(step.t)),
                      describe_step(step.tool, step.input, self.lang), result, dur)
            if idx >= self._shown_steps:
                self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
                self.tree.see(iid)
            elif step.ok is None or idx >= self._shown_steps - 3:
                self.tree.item(iid, values=values, tags=(tag,))
        self._shown_steps = len(steps)
        if steps and self.detail.cget("text") == T["no_activity"]:
            self.detail.configure(text="")

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="solidworks-mcp-panel", description="SolidWorks-MCP control panel")
    parser.add_argument("--workspace", help="work folder Claude Code opens (default: Documents\\SolidWorks-MCP)")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else default_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    Panel(workspace).run()


if __name__ == "__main__":
    main()
