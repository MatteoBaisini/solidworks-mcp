---
name: m8ven-audit
description: Run before every commit or push, after adding or changing any MCP tool, or when the user asks about the M8ven trust score. Runs a local static audit that mirrors the M8ven checks (literal tool annotations, description vs code, destructive labels, repo hygiene) and fixes what it finds.
---

# M8ven pre-flight audit

M8ven (https://m8ven.ai) grades this server by reading its source statically. This skill finds,
before pushing, the issues that cost points, and says how to fix each one.

## Steps

1. From the repository root run:

   ```
   python .agents/skills/m8ven-audit/scripts/m8ven_audit.py --quiet
   ```

   Use `--json` for the full list, `--strict` to make warnings fail too, `--help` for options.
   The script uses only the standard library and never imports the server: it runs without
   pywin32 or SolidWorks.

2. Fix every `[FAIL]` using the table below, then every `[WARN]` that applies. Change the code,
   never the audit script, to make a finding go away.

3. Run the audit again until it reports `0 FAIL`, then run `pytest`.

4. Tell the user in two or three lines: tools audited, how many are fully annotated, what you
   fixed, warnings left on purpose.

## How to fix each finding

| Check | Fix |
| --- | --- |
| `annotations` | Add `annotations={"readOnlyHint": …, "destructiveHint": …, "idempotentHint": …, "openWorldHint": …}` as a literal dict inside `@mcp.tool(...)` (or `mcp.add_tool(...)`). Pick the profile from the table in `AGENTS.md`. Never use a constant or helper: M8ven cannot see it. |
| `all-four-hints` | Every one of the four keys present, value exactly `True` or `False`. |
| `hint-consistency` | A tool cannot be read-only and destructive at once: re-check what the handler does. |
| `claim-vs-code` | The tool says read-only but writes, deletes or executes. Remove the side effect, or set `readOnlyHint: False` and describe it. |
| `destructive-labelled` | The handler deletes something (`os.remove`, `Path.unlink`, COM `Delete*`). Set `destructiveHint: True` and say in the docstring what gets replaced or removed. |
| `description-vs-behaviour` | A read-style tool (`get_`, `list_`, `measure` …) deletes or executes. Replace a fixed temp path plus `os.remove` with `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`; otherwise rename or redescribe the tool. |
| `overwrite-check` | The handler writes files. If it can replace an existing file, set `destructiveHint: True` and document it; if it only writes to a fresh temp folder, use `tempfile` so the audit can tell. |
| `exec` | The handler starts processes or runs code: say so in the docstring; `openWorldHint: True` if the effect is not bounded. |
| `description` | Add a docstring of at least one full sentence. If `description=` is a variable, keep it but add a docstring too: M8ven shows the docstring. |
| `tool-coverage` | Name every tool in the tests (`EXPECTED_TOOLS` in `tests/test_server.py`). |
| `tests-cross-platform` | Make `solidworks_mcp.server` importable without pywin32 (task M6 in `docs/IMPROVEMENT_PLAN.md`) so the tests run on Linux and CI. |
| `ci` | Add `.github/workflows/tests.yml` (task M6). |
| `license` / `readme` / `tests` | Restore the missing file. |
| `hardcoded-secret` / `network-exposure` / `supply-chain` | Remove the secret, bind to `127.0.0.1`, remove the build hook. These are serious: stop and tell the user. |

## Limits

The audit is a heuristic. A clean run removes the known causes of lost points but does not
guarantee a grade: M8ven also caps new projects by adoption (stars, contributors, registry listings).
