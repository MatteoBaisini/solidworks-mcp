# SolidWorks-MCP

## Italiano

Questa è la cartella di lavoro di SolidWorks-MCP. Apri l'icona **SolidWorks-MCP** sul desktop
o nel menu Start: si apre Claude Code qui dentro, collegato a SolidWorks.

- **Primo avvio:** Claude Code chiede di accedere con il tuo account Claude (piano Pro, Max,
  Team o Enterprise) e di confermare che ti fidi di questa cartella. Rispondi sì.
- **Come si usa:** scrivi cosa vuoi progettare, per esempio
  *"una piastra 120×80×10 mm in alluminio con 4 fori M6 a 10 mm dai bordi"*.
  Il modello si costruisce nella finestra di SolidWorks mentre guardi.
- **Dove finiscono i file:** in `projects\<nome progetto>\` (`cad` per i pezzi, `step` per gli export).
- **Impostazioni:** `solidworks-mcp.toml` (formati di export, cartelle). `CLAUDE.md` contiene le
  istruzioni di progettazione per l'assistente: puoi modificarle.
- **Problemi:** menu Start → SolidWorks-MCP → **Ripara**. Il registro è in
  `%LOCALAPPDATA%\Programs\SolidWorks-MCP\logs\setup.log`.

SolidWorks e SolidWorks-MCP devono girare con gli stessi privilegi: non avviare nessuno dei due
come amministratore.

## English

This is the SolidWorks-MCP work folder. Open the **SolidWorks-MCP** icon on the desktop or in
the Start menu: Claude Code opens here, connected to SolidWorks.

- **First run:** Claude Code asks you to log in with your Claude account (Pro, Max, Team or
  Enterprise plan) and to confirm you trust this folder. Answer yes.
- **How to use it:** describe what to design, e.g.
  *"a 120×80×10 mm aluminium plate with four M6 holes 10 mm from the edges"*.
- **Where files go:** `projects\<project name>\` (`cad` for parts, `step` for exports).
- **Settings:** `solidworks-mcp.toml`. `CLAUDE.md` holds the design instructions for the
  assistant and can be edited.
- **Problems:** Start menu → SolidWorks-MCP → **Repair**. Log:
  `%LOCALAPPDATA%\Programs\SolidWorks-MCP\logs\setup.log`.

Run SolidWorks and SolidWorks-MCP with the same privileges: never as administrator.
