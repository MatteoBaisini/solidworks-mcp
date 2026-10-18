#!/usr/bin/env python3
"""Local pre-flight audit that approximates the M8ven checks for this MCP server.

Run from the repository root:

    python .agents/skills/m8ven-audit/scripts/m8ven_audit.py
    python .agents/skills/m8ven-audit/scripts/m8ven_audit.py --json
    python .agents/skills/m8ven-audit/scripts/m8ven_audit.py --strict   # warnings fail too

It reads the source with `ast` and never imports the server, so it runs on any OS
without pywin32 or SolidWorks. Exit code: 0 = no FAIL, 1 = at least one FAIL.

What it checks (mirrors https://m8ven.ai/verified/verify/methodology):
  per tool   - annotations present and written as a LITERAL in the decorator
               (M8ven reads source statically: shared constants are not seen)
             - all four hints are explicit booleans
             - readOnlyHint=True handlers contain no delete / write / exec calls
             - handlers that delete data declare destructiveHint=True
             - read-style tools (get_, list_, ...) do not delete files
             - a description is present and statically readable
  repo       - LICENSE, README, tests, tool coverage in tests, CI workflow,
               build hooks, hard-coded secrets, 0.0.0.0 binding, network clients

It is a heuristic: a clean run does not guarantee a score, it removes the known
causes of lost points.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
SNAKE = {"read_only_hint": "readOnlyHint", "destructive_hint": "destructiveHint",
         "idempotent_hint": "idempotentHint", "open_world_hint": "openWorldHint"}
READ_PREFIXES = ("get_", "list_", "read_", "show_", "find_", "search_", "describe_",
                 "inspect_", "check_", "measure", "query_")

# (module, function) pairs and bare method names, classified by effect
DELETE_PAIRS = {("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("os", "removedirs"),
                ("shutil", "rmtree")}
DELETE_METHODS = {"unlink", "rmdir", "rmtree", "EditDelete"}          # Path.unlink, COM EditDelete
DELETE_PREFIX = "Delete"                                               # COM DeleteSelection2, DeleteFeature...
MKDIR_PAIRS = {("os", "makedirs"), ("os", "mkdir")}                    # creating folders: minor
WRITE_PAIRS = {("os", "replace"), ("os", "rename"), ("shutil", "move"), ("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile")}
WRITE_METHODS = {"write_text", "write_bytes", "Save3", "Save2", "Save"}
WRITE_PREFIX = "SaveAs"                                                # COM SaveAs3, SaveAs4...
EXEC_PAIRS = {("os", "system"), ("os", "popen"), ("os", "startfile"), ("os", "execv"),
              ("os", "execvp"), ("subprocess", "run"), ("subprocess", "Popen"),
              ("subprocess", "call"), ("subprocess", "check_call"), ("subprocess", "check_output")}
EXEC_NAMES = {"exec", "eval"}
TEMP_CALLS = {"TemporaryDirectory", "mkdtemp", "NamedTemporaryFile", "TemporaryFile"}
NETWORK_MODULES = {"requests", "httpx", "urllib.request", "aiohttp", "socket", "http.client"}
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|"
                       r"xox[bap]-[A-Za-z0-9-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


@dataclass
class Finding:
    level: str          # FAIL | WARN | INFO
    check: str
    target: str
    detail: str


@dataclass
class Tool:
    name: str
    func: str
    file: str
    line: int
    node: ast.AST | None
    annotations: dict | None = None
    annotations_kind: str = "missing"   # missing | literal | non-literal
    description: str | None = None
    description_kind: str = "missing"   # missing | literal | non-literal
    effects: dict = field(default_factory=lambda: {"delete": [], "write": [], "exec": [], "temp": [], "mkdir": []})


# --------------------------------------------------------------------------- #
def dotted(node: ast.AST) -> tuple[str | None, str | None]:
    """(base, attr) of a call target: os.remove -> ('os', 'remove'); exec -> (None, 'exec')."""
    if isinstance(node, ast.Name):
        return None, node.id
    if isinstance(node, ast.Attribute):
        base = node.value
        while isinstance(base, ast.Attribute):
            base = base.value
        base_name = base.id if isinstance(base, ast.Name) else None
        if isinstance(node.value, ast.Name):
            return node.value.id, node.attr
        return base_name, node.attr
    return None, None


def open_mode(call: ast.Call) -> str:
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return mode if isinstance(mode, str) else "r"


def scan_effects(fn: ast.AST) -> dict:
    effects = {"delete": [], "write": [], "exec": [], "temp": [], "mkdir": []}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        base, attr = dotted(node.func)
        if attr is None:
            continue
        where = f"line {node.lineno}: {(base + '.') if base else ''}{attr}()"
        if (base, attr) in DELETE_PAIRS or attr in DELETE_METHODS or (
                base is not None and attr.startswith(DELETE_PREFIX)):
            effects["delete"].append(where)
        elif (base, attr) in MKDIR_PAIRS or (base is not None and attr == "mkdir"):
            effects["mkdir"].append(where)
        elif (base, attr) in WRITE_PAIRS or attr in WRITE_METHODS or attr.startswith(WRITE_PREFIX):
            effects["write"].append(where)
        elif attr == "open" and base is None and any(c in open_mode(node) for c in "wax+"):
            effects["write"].append(where + f" mode={open_mode(node)!r}")
        elif (base, attr) in EXEC_PAIRS or (base is None and attr in EXEC_NAMES):
            effects["exec"].append(where)
        if attr in TEMP_CALLS:
            effects["temp"].append(where)
    return effects


def literal_annotations(value: ast.AST) -> tuple[str, dict | None]:
    pairs: list[tuple[str, ast.AST]] = []
    if isinstance(value, ast.Dict):
        for k, v in zip(value.keys, value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return "non-literal", None
            pairs.append((k.value, v))
    elif isinstance(value, ast.Call) and dotted(value.func)[1] == "ToolAnnotations":
        if value.args or any(kw.arg is None for kw in value.keywords):
            return "non-literal", None
        pairs = [(kw.arg, kw.value) for kw in value.keywords]
    else:
        return "non-literal", None
    out = {}
    for key, v in pairs:
        key = SNAKE.get(key, key)
        out[key] = v.value if isinstance(v, ast.Constant) else "<expr>"
    return "literal", out


def decorator_tool_call(dec: ast.AST) -> ast.Call | None | bool:
    """ast.Call for @x.tool(...), True for bare @x.tool, None otherwise."""
    if isinstance(dec, ast.Call) and dotted(dec.func)[1] == "tool":
        return dec
    if isinstance(dec, ast.Attribute) and dec.attr == "tool":
        return True
    return None


def kw(call: ast.Call, name: str) -> ast.AST | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def collect_tools(files: list[Path], root: Path) -> list[Tool]:
    tools: list[Tool] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        rel = str(path.relative_to(root))
        for fn in funcs.values():
            for dec in fn.decorator_list:
                call = decorator_tool_call(dec)
                if call is None:
                    continue
                call = call if isinstance(call, ast.Call) else None
                name_node = kw(call, "name") if call else None
                name = name_node.value if isinstance(name_node, ast.Constant) else fn.name
                t = Tool(name, fn.name, rel, fn.lineno, fn)
                fill_meta(t, call, fn)
                tools.append(t)
        for node in ast.walk(tree):     # mcp.add_tool(fn, ...)
            if isinstance(node, ast.Call) and dotted(node.func)[1] == "add_tool" and node.args:
                target = node.args[0]
                fname = target.id if isinstance(target, ast.Name) else "<expr>"
                name_node = kw(node, "name")
                name = name_node.value if isinstance(name_node, ast.Constant) else fname
                fn = funcs.get(fname)
                t = Tool(name, fname, rel, node.lineno, fn)
                fill_meta(t, node, fn)
                tools.append(t)
    return tools


def fill_meta(t: Tool, call: ast.Call | None, fn: ast.AST | None) -> None:
    ann = kw(call, "annotations") if call else None
    if ann is not None:
        t.annotations_kind, t.annotations = literal_annotations(ann)
    desc = kw(call, "description") if call else None
    if desc is not None:
        if isinstance(desc, ast.Constant) and isinstance(desc.value, str):
            t.description, t.description_kind = desc.value, "literal"
        else:
            t.description_kind = "non-literal"
    if t.description_kind in ("missing", "non-literal") and fn is not None:
        doc = ast.get_docstring(fn)      # a static reader falls back to the docstring
        if doc:
            t.description, t.description_kind = doc, "literal"
    if fn is not None:
        t.effects = scan_effects(fn)


# --------------------------------------------------------------------------- #
def audit_tool(t: Tool) -> list[Finding]:
    f: list[Finding] = []
    tgt = f"{t.name} ({t.file}:{t.line})"
    if t.annotations_kind == "missing":
        f.append(Finding("FAIL", "annotations", tgt, "no annotations= in the decorator"))
    elif t.annotations_kind == "non-literal":
        f.append(Finding("FAIL", "annotations", tgt,
                         "annotations are not a literal dict/ToolAnnotations(...) in the decorator: "
                         "M8ven reads source statically and will report them as missing"))
    ann = t.annotations or {}
    if t.annotations_kind == "literal":
        missing = [h for h in HINTS if not isinstance(ann.get(h), bool)]
        if missing:
            f.append(Finding("FAIL", "all-four-hints", tgt, "missing or non-boolean: " + ", ".join(missing)))
        if ann.get("readOnlyHint") is True and ann.get("destructiveHint") is True:
            f.append(Finding("FAIL", "hint-consistency", tgt, "readOnlyHint and destructiveHint both true"))

    eff = t.effects
    read_intent = t.name.startswith(READ_PREFIXES) or ann.get("readOnlyHint") is True
    if ann.get("readOnlyHint") is True:
        bad = eff["delete"] + eff["write"] + eff["exec"]
        if bad:
            f.append(Finding("FAIL", "claim-vs-code", tgt,
                             "readOnlyHint=true but the handler mutates: " + "; ".join(bad)))
        if eff["mkdir"]:
            f.append(Finding("WARN", "claim-vs-code", tgt,
                             "readOnlyHint=true but the handler creates folders: " + "; ".join(eff["mkdir"])))
    if eff["delete"] and ann.get("destructiveHint") is not True:
        f.append(Finding("FAIL", "destructive-labelled", tgt,
                         "deletes without destructiveHint=true: " + "; ".join(eff["delete"])))
    if read_intent and (eff["delete"] or eff["exec"]):
        f.append(Finding("FAIL", "description-vs-behaviour", tgt,
                         "read-style tool deletes/executes: " + "; ".join(eff["delete"] + eff["exec"])
                         + " -> remove the side effect (e.g. tempfile.TemporaryDirectory) or rename/redescribe"))
    if eff["write"] and not eff["temp"] and ann.get("destructiveHint") is not True and not eff["delete"]:
        f.append(Finding("WARN", "overwrite-check", tgt,
                         "writes files (" + "; ".join(eff["write"]) + "): if it can replace an existing "
                         "file, set destructiveHint=true and say so in the description"))
    if eff["exec"] and ann.get("openWorldHint") is not True and not read_intent:
        f.append(Finding("WARN", "exec", tgt, "executes code/processes: say so in the description; "
                         "consider openWorldHint=true"))

    if t.description_kind == "missing":
        f.append(Finding("FAIL", "description", tgt, "no docstring or description"))
    elif t.description_kind == "non-literal":
        f.append(Finding("WARN", "description", tgt, "description is a variable: M8ven may show the tool "
                         "with no description; give the function a docstring too"))
    elif t.description and len(t.description.strip()) < 20:
        f.append(Finding("WARN", "description", tgt, "description shorter than 20 characters"))
    return f


def audit_repo(root: Path, src: Path, tests: Path, tools: list[Tool]) -> list[Finding]:
    f: list[Finding] = []
    if not any((root / n).exists() for n in ("LICENSE", "LICENSE.md", "LICENSE.txt")):
        f.append(Finding("FAIL", "license", "repo", "no LICENSE file"))
    if not (root / "README.md").exists():
        f.append(Finding("FAIL", "readme", "repo", "no README.md"))
    test_files = sorted(tests.rglob("test_*.py")) if tests.exists() else []
    if not test_files:
        f.append(Finding("FAIL", "tests", "repo", f"no test_*.py under {tests}"))
    else:
        text = "\n".join(p.read_text(encoding="utf-8") for p in test_files)
        uncovered = [t.name for t in tools if not re.search(rf"\b{re.escape(t.name)}\b", text)]
        pct = 100 * (len(tools) - len(uncovered)) // max(len(tools), 1)
        level = "INFO" if not uncovered else "WARN"
        f.append(Finding(level, "tool-coverage", "tests",
                         f"{len(tools) - len(uncovered)}/{len(tools)} tools named in tests ({pct}%)"
                         + (": missing " + ", ".join(uncovered[:15]) if uncovered else "")))
        if "importorskip" in text and ("pywin32" in text or "win32" in text):
            f.append(Finding("WARN", "tests-cross-platform", "tests",
                             "server tests are skipped without pywin32: an analyser on Linux never runs "
                             "them. Make the server importable without pywin32 (lazy import)."))
    if not list((root / ".github" / "workflows").glob("*.y*ml")):
        f.append(Finding("WARN", "ci", "repo", "no .github/workflows: tests are not run publicly"))
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and re.search(r"\[tool\.hatch\.build\.hooks|cmdclass", pyproject.read_text(encoding="utf-8")):
        f.append(Finding("WARN", "supply-chain", "pyproject.toml", "build hooks run code at install time"))
    if (root / "setup.py").exists():
        f.append(Finding("WARN", "supply-chain", "setup.py", "setup.py can run code at install time"))

    for path in sorted(src.rglob("*.py")):
        rel = str(path.relative_to(root))
        body = path.read_text(encoding="utf-8")
        for m in SECRET_RE.finditer(body):
            f.append(Finding("FAIL", "hardcoded-secret", rel, f"looks like a credential: {m.group(0)[:12]}..."))
        if re.search(r"""['"]0\.0\.0\.0['"]""", body):
            f.append(Finding("FAIL", "network-exposure", rel, "binds 0.0.0.0: keep HTTP transports on 127.0.0.1"))
        tree = ast.parse(body)
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                if m in NETWORK_MODULES or m.split(".")[0] in {"requests", "httpx", "aiohttp"}:
                    f.append(Finding("INFO", "outbound-network", rel,
                                     f"imports {m}: M8ven lists every host the code contacts"))
    return f


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="repository root (default: current directory)")
    ap.add_argument("--src", default="src", help="source folder relative to root (default: src)")
    ap.add_argument("--tests", default="tests", help="tests folder relative to root (default: tests)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    ap.add_argument("--quiet", action="store_true", help="hide INFO lines")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    src, tests = root / args.src, root / args.tests
    if not src.exists():
        print(f"source folder not found: {src}", file=sys.stderr)
        return 2
    files = sorted(src.rglob("*.py"))
    tools = collect_tools(files, root)
    findings: list[Finding] = []
    for t in tools:
        findings += audit_tool(t)
    findings += audit_repo(root, src, tests, tools)

    counts = {lvl: sum(1 for x in findings if x.level == lvl) for lvl in ("FAIL", "WARN", "INFO")}
    annotated = sum(1 for t in tools if t.annotations_kind == "literal"
                    and all(isinstance((t.annotations or {}).get(h), bool) for h in HINTS))
    if args.json:
        print(json.dumps({"tools": len(tools), "fully_annotated": annotated, "counts": counts,
                          "findings": [x.__dict__ for x in findings]}, indent=2))
    else:
        print(f"M8ven pre-flight audit: {len(tools)} tools, {annotated} with all four literal hints")
        order = {"FAIL": 0, "WARN": 1, "INFO": 2}
        by_check: dict[tuple[str, str], list[Finding]] = {}
        for x in sorted(findings, key=lambda x: (order[x.level], x.check)):
            if args.quiet and x.level == "INFO":
                continue
            by_check.setdefault((x.level, x.check), []).append(x)
        for (lvl, check), items in by_check.items():
            print(f"\n[{lvl}] {check} ({len(items)})")
            shown = items if len(items) <= 12 else items[:10]
            for x in shown:
                print(f"  - {x.target}: {x.detail}")
            if len(items) > len(shown):
                print(f"  ... and {len(items) - len(shown)} more (use --json for the full list)")
        print(f"\nSummary: {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['INFO']} INFO")
    if counts["FAIL"] or (args.strict and counts["WARN"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
