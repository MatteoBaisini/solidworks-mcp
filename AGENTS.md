# Istruzioni per l'agente — solidworks-mcp

Server MCP in Python che fa modellare parti in SolidWorks a un assistente AI tramite l'API COM.
Questo file è sempre attivo: contiene le regole da rispettare a ogni modifica. Il lavoro da fare,
in ordine di priorità, è in `docs/IMPROVEMENT_PLAN.md`: leggilo quando l'utente chiede di
migliorare il server o il punteggio M8ven, e spunta i task completati.

## Progetto in breve

- Stack: Python 3.11+, SDK MCP ufficiale (`mcp`, FastMCP), `pywin32` per COM. Solo Windows a runtime.
- Codice: `src/solidworks_mcp/server.py` (tool e thread COM), `config.py` (TOML), `__main__.py`.
- Tutte le chiamate COM passano da `com_tool(work)`, che le esegue in sequenza su un unico thread STA.
  Non chiamare COM da altri thread e non bloccare l'event loop.
- Unità esposte all'assistente: millimetri e gradi. L'API SolidWorks lavora in metri e radianti
  (`MM`, `DEG`): converti sempre ai confini.
- Errori: solleva `SWError` con un messaggio che dice cosa fare dopo ("save it first", "select a face").
- Codice, docstring e descrizioni dei tool in inglese.
- Test: `.venv\Scripts\pytest` (non serve SolidWorks). Prova reale: `examples\smoke_test.py`.

## Regole M8ven (non negoziabili)

M8ven assegna il trust score leggendo il sorgente in modo statico. Queste regole evitano di perdere punti.

1. **Ogni tool dichiara le annotazioni come dizionario letterale nel decoratore**, con tutti e quattro
   gli hint booleani. Mai costanti, helper o decoratori personalizzati: M8ven non li risolve e
   segnala i tool come non annotati (è successo a un altro server SolidWorks che usava costanti).

   ```python
   @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": False})
   async def get_bounding_box() -> str:
       """Bounding box of the active part: min/max on X, Y, Z and overall size, in mm."""
   ```

   Per `mcp.add_tool(...)` passa lo stesso dizionario letterale in `annotations=` e dai alla
   funzione anche una docstring.

2. **Scegli i valori in base a cosa fa davvero il codice**, con questi profili:

   | Profilo | readOnly | destructive | idempotent | openWorld | Quando |
   | --- | --- | --- | --- | --- | --- |
   | READ | true | false | true | false | Legge e basta |
   | VIEW | false | false | true | false | Cambia vista o selezione, o scrive solo file temporanei |
   | EDIT | false | false | false | false | Aggiunge geometria, sketch, feature |
   | OVERWRITE | false | true | true | false | Sostituisce valori o file esistenti |
   | DESTROY | false | true | false | false | Cancella o annulla lavoro |
   | MACRO | false | true | false | true | Esegue codice arbitrario |

3. **Un tool `readOnlyHint: true` non scrive, non cancella e non esegue nulla** nel suo corpo
   (incluse le funzioni annidate come `work()`). Niente `os.remove`, `SaveAs3`, `open(..., "w")`,
   `subprocess`.
4. **Nessuna cancellazione esplicita nei tool di lettura o di vista.** Per file temporanei usa
   `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`, non un percorso fisso più `os.remove`.
5. **Ogni tool che cancella o sovrascrive dati ha `destructiveHint: true`** e lo dice nella descrizione
   ("Existing files with the same name are replaced.").
6. **La descrizione dichiara ogni effetto collaterale**: cambia la vista, sostituisce la selezione,
   sovrascrive file, avvia SolidWorks, esegue codice. Una descrizione che promette una lettura mentre
   il codice modifica è il rilievo che pesa di più.
7. **Non introdurre**: chiamate di rete, lettura di segreti, binding diverso da `127.0.0.1`, hook di
   build in `pyproject.toml`, codice offuscato, nuovi modi di eseguire codice arbitrario.
   `execute_solidworks_macro` resta disattivato di default.
8. **Ogni nuovo tool** va aggiunto a `EXPECTED_TOOLS` in `tests/test_server.py`, alla tabella Tools del
   README e deve passare il test sulle annotazioni.

## Prima di ogni commit

1. Esegui la skill `m8ven-audit` (oppure
   `python .agents/skills/m8ven-audit/scripts/m8ven_audit.py --quiet`): deve dare **0 FAIL**.
2. Esegui `pytest`: tutti i test verdi.
3. Messaggio di commit in inglese, all'imperativo, che dica cosa cambia per l'utente.
