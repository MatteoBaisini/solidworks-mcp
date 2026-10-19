# Piano di miglioramento — solidworks-mcp

Stato al 18 ottobre 2026, codice al commit `e202f3c` (V2.0, 57 tool con la macro).
Obiettivo: portare il trust score M8ven da **C 62 "Caution"** al livello dei migliori server
SolidWorks (B 89), e rendere il prodotto più completo e affidabile dei concorrenti.

## Come usare questo piano (per l'agente)

- Lavora per ID, nell'ordine: prima tutta la Fase A (punteggio), poi la Fase B (prodotto).
- Un task alla volta: modifica, `m8ven-audit` a 0 FAIL, `pytest` verde, commit, spunta la casella qui.
- Rispetta sempre le regole di `AGENTS.md`. Se un task le contraddice, vince `AGENTS.md`: fermati e chiedi.
- Non toccare questo file oltre alle caselle, salvo richiesta dell'utente.

---

## 1. Dove sei rispetto agli altri server SolidWorks su M8ven

Su M8ven ci sono 21 server SolidWorks; 6 hanno una scheda con voto. Lo schema dei punteggi è netto:

| Server | Tool | Voto | Cosa lo determina |
| --- | --- | --- | --- |
| blussyya/solidworks-mcp | 39 | **B 89** Emerging | Descrizioni oneste, **tutti e 4 gli hint letterali su ogni tool**. Pur senza LICENSE né test. Listato su Glama |
| lildebil0/solidworks-mcp | 57 | C 74 Emerging | Nessun rilievo di onestà; nessuna annotazione; 79% dei tool nei test |
| Slacker-LLC/solidworks-mcp | 193 | C 74 Emerging | 21 stelle; nessuna annotazione; solo 49% degli input con schema |
| hjbaard/SolidWorks-MCP | 85 | C 74 Emerging | 10 stelle; nessuna annotazione |
| **MatteoBaisini/solidworks-mcp (tu)** | 57 | **C 62 Caution** | `get_screenshot` dichiara lettura ma cancella un file; nessuna annotazione |
| 0xFFD/solidworks-mcp | 101 | C 62 Caution | Stesso problema di screenshot; annotazioni dichiarate **tramite costanti**, che M8ven non vede |

Cosa se ne ricava (deduzione dai dati, M8ven non pubblica i pesi):

- **62 → 74**: togliere l'incoerenza descrizione/codice (task M1). Tutti i server senza rilievi di
  onestà ma senza annotazioni stanno a 74.
- **74 → ~89**: annotazioni letterali complete su ogni tool (task M2). È l'unica differenza netta tra
  blussyya (89) e gli altri.
- **Annotazioni come costanti non contano**: 0xFFD le ha nel codice ma M8ven riporta "nessuna".
  Per questo `AGENTS.md` impone il dizionario letterale in ogni decoratore.
- Oltre B servono adozione e storico (stelle, contributori, registri): task M9.

---

## 2. Fase A — Punteggio M8ven

### M1. `get_screenshot` onesto · impatto alto

- [x] Fatto

Oggi scrive in un percorso temporaneo fisso e lo cancella con `os.remove` (riga ~707): M8ven lo
segnala come tool di lettura che cancella. Sostituisci con una cartella temporanea che si pulisce da sola:

```python
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def get_screenshot(view: Literal["current", "isometric", "front", "top", "right"] = "isometric"):
    """PNG image of the active model, to check the result visually.
    Changes the view orientation (unless view='current') and zooms to fit; the model is not modified."""
    view_ids = {"isometric": 7, "front": 1, "top": 5, "right": 4}

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
    ...
```

Profilo VIEW (non READ): il tool cambia la vista e scrive un file temporaneo, quindi dichiararlo
read-only sarebbe di nuovo un'incoerenza.
**Accettazione:** nessun `os.remove` nel tool; l'audit non riporta `description-vs-behaviour`.

### M2. Annotazioni letterali su tutti i 57 tool · impatto alto

- [x] Fatto

Aggiungi a ogni `@mcp.tool()` il dizionario `annotations={...}` con i quattro hint (formato e profili
in `AGENTS.md`). Classificazione da applicare:

| Profilo | Tool |
| --- | --- |
| READ (10) | `get_active_document_info`, `get_mass_properties`, `get_feature_tree`, `get_bounding_box`, `convert_point`, `measure`, `list_materials`, `get_dimensions`, `get_equations`, `get_custom_properties` |
| VIEW (5) | `get_screenshot`, `select_plane`, `select_face_at_point`, `select_edge_at_point`, `zoom_to_fit` |
| EDIT (32) | `new_part`, `start_sketch`, `end_sketch`, `sketch_line`, `sketch_rectangle`, `sketch_circle`, `sketch_arc`, `sketch_polygon`, `sketch_slot`, `sketch_ellipse`, `sketch_spline`, `sketch_fillet`, `sketch_offset`, `convert_entities`, `fully_define_sketch`, `add_sketch_dimension`, `add_sketch_relation`, `extrude`, `cut_extrude`, `revolve`, `rib`, `sweep`, `loft`, `create_plane`, `fillet`, `chamfer`, `shell`, `hole`, `linear_pattern`, `circular_pattern`, `mirror`, `add_equation` |
| OVERWRITE (6) | `save_document`, `export_document`, `set_material`, `set_dimension`, `suppress_feature`, `set_custom_property` |
| DESTROY (3) | `undo`, `delete_feature`, `delete_equation` |
| MACRO (1) | `execute_solidworks_macro` (vedi M4) |

Note:
- `measure` svuota la selezione corrente: resta READ (non tocca il modello) ma aggiungi alla docstring
  "Replaces the current selection."
- `get_active_document_info` avvia SolidWorks se è chiuso: resta READ, la docstring lo dice già.
- Se un tool aggiunto in futuro non rientra nei profili, scegli i valori in base al codice e documentali.

**Accettazione:** l'audit riporta "57 tools, 57 with all four literal hints" e 0 FAIL.
Verificato su una copia del codice: con questa classificazione, M1 e M4 l'audit dà 0 FAIL.

### M3. Descrizioni che dichiarano la sovrascrittura · impatto medio

- [x] Fatto

- `export_document`: aggiungi "Existing files with the same name are replaced." (cancella il file prima di riscriverlo, riga ~789: comportamento corretto, va solo dichiarato).
- `save_document`: aggiungi "Overwrites the target file if it exists."
- `set_custom_property`: già dice che un valore vuoto cancella; aggiungi "Replaces the current value."

### M4. Macro visibile e dichiarata · impatto medio

- [x] Fatto

M8ven mostra `execute_solidworks_macro` **senza descrizione**, perché la descrizione arriva da una
variabile (`MACRO_DOC`). In `register_macro_tool()`:

```python
async def execute_solidworks_macro(python_code: str) -> str:
    """Runs Python code against the SolidWorks COM API with the user's privileges.
    Last resort when no dedicated tool fits. Disabled unless enabled in the configuration."""
    ...

def register_macro_tool() -> None:
    mcp.add_tool(execute_solidworks_macro, description=MACRO_DOC,
                 annotations={"readOnlyHint": False, "destructiveHint": True,
                              "idempotentHint": False, "openWorldHint": True})
```

Aggiungi in testa a `MACRO_DOC` una riga che dica che il codice gira con i privilegi dell'utente.

### M5. Test che impedisce regressioni · impatto medio

- [x] Fatto

In `tests/test_server.py`:

```python
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

def test_every_tool_declares_all_four_hints():
    server.register_macro_tool()
    try:
        for tool in asyncio.run(server.mcp.list_tools()):
            assert tool.annotations is not None, tool.name
            for hint in HINTS:
                assert isinstance(getattr(tool.annotations, hint), bool), (tool.name, hint)
            if tool.annotations.readOnlyHint:
                assert not tool.annotations.destructiveHint, tool.name
    finally:
        server.mcp._tool_manager._tools.pop("execute_solidworks_macro", None)
```

Se esiste già un modo per de-registrare la macro dopo il test attuale, usa quello.

### M6. Test eseguibili ovunque + CI · impatto medio

- [x] Server importabile senza pywin32
- [x] Workflow GitHub Actions

Oggi `test_server.py` viene saltato fuori da Windows (`importorskip`), quindi un analizzatore su Linux
non vede mai passare i test dei tool. In `server.py` importa `pythoncom`, `pywintypes`, `win32com`,
`winreg` dentro un blocco `try/except ImportError` che lascia dei segnaposto, e fai fallire con un
messaggio chiaro solo al primo uso reale di COM (`get_sw()` / `run_com()`), non all'import.
Poi togli `importorskip` dai test che non toccano COM (registrazione tool, annotazioni, config).

`.github/workflows/tests.yml`:

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [windows-latest, ubuntu-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e .[dev]
      - run: pytest -q
      - run: python .agents/skills/m8ven-audit/scripts/m8ven_audit.py --quiet
```

Aggiungi il badge dei test al README.

### M7. README · impatto basso

- [x] Fatto

- Badge M8ven con l'URL canonico `https://m8ven.ai/mcp/matteobaisini/solidworks-mcp` (quello attuale usa il vecchio slug `...-12f1ym`, che reindirizza). Togli `?variant=verified` per mostrare il voto.
- Correggi il titolo "M8ven ceritification" → "certification".
- Nella sezione Tools: una riga che spiega che ogni tool dichiara read-only/destructive, così i client possono approvare in automatico le letture.

### M8. Far rileggere il codice a M8ven · dopo M1–M7

- [ ] Fatto

La scheda è ferma al commit `d749dc8` (15 ottobre) nonostante la GitHub App. Dopo il push:
controlla che la scheda mostri il nuovo commit; se non si aggiorna entro un giorno, usa
"Confirm or correct these findings" sulla scheda o scrivi a support@m8ven.ai.

### M9. Reputazione (unica via verso B stabile e A) · continuo

- [x] Release `v2.1.0` con tag Git e `CHANGELOG.md`
- [ ] Pubblicazione su PyPI, così l'installazione diventa `uvx solidworks-mcp`
- [ ] Listing su Glama e sul registro MCP ufficiale (`server.json`); il server con B è listato su Glama
- [ ] GIF o video di 20 secondi nel README: dal prompt al pezzo finito
- [ ] Issue "good first issue" per attirare contributori; topic GitHub già presenti

---

## 3. Fase B — Prodotto

### Cosa fanno meglio i concorrenti

| Server | Punti di forza da copiare |
| --- | --- |
| 0xFFD | Assiemi con accoppiamenti, tavole 2D, lamiera, saldature, GD&T; tool `design_guide` e prompt MCP `design`; modalità sola lettura; file confinati alla cartella di lavoro; log di audit; CLI `doctor` e `install` per i client; verifica rileggendo lo STEP esportato |
| Slacker-LLC | 193 tool tra parti, assiemi, tavole; file `COVERAGE.md` con la copertura API; output confinato in una cartella; tool demo spenti di default |
| hjbaard | Ciclo "costruisci → misura → verifica": stato di rebuild e volumi confrontati con calcoli a mano nei test di integrazione; errori `{ok: false, error}` espliciti |
| lildebil0 | Tavole (viste, sezioni, dettagli, quote, note, cartiglio, distinta), configurazioni, generazione batch |
| blussyya | Tool di sessione `connect` / `status` / `disconnect`; installer `setup.bat` che rileva Python e SolidWorks; input validati con modelli Pydantic |

Il tuo vantaggio da difendere: selezione geometrica per punto in coordinate modello, fori ISO
(273, 4762, 10642), cartella progetti con versioni automatiche, configurazione TOML chiara, lingua
di SolidWorks indifferente, timeout invece di blocchi. Nessun concorrente espone **risorse MCP**:
è un'occasione per distinguersi (P6).

### Roadmap prodotto (in ordine di valore)

Ogni task: annotazioni letterali, test, riga nella tabella Tools del README, audit a 0 FAIL.

- [x] **P1. Errori MCP veri.** Oggi `com_tool` restituisce stringhe `"ERROR: ..."`, che per il client
  sono risposte riuscite. Solleva l'eccezione (FastMCP la trasforma in risultato con `isError: true`)
  così l'agente capisce che il passo è fallito. Aggiorna i test che controllano il testo.
- [x] **P2. Verifica dopo ogni feature.** Dopo `extrude`, `cut_extrude`, `fillet` ecc. controlla gli
  errori di rebuild della feature appena creata (`Feature.GetErrorCode2`) e restituiscili; aggiungi un
  tool READ `check_model` (errori di rebuild, corpi multipli, `Body2.Check3`). Ispirazione: hjbaard.
- [x] **P3. Output strutturato.** Per `get_bounding_box`, `get_mass_properties`, `measure`,
  `get_dimensions` restituisci un `TypedDict`/modello Pydantic: FastMCP genera `outputSchema` e
  l'agente legge numeri, non testo da interpretare.
- [x] **P4. Input validati.** Usa `Annotated[float, Field(gt=0, description="...")]` per diametri,
  profondità, spessori, numero di lati. Schemi più precisi = meno chiamate sbagliate (Slacker perde
  un controllo qualità per questo).
- [x] **P5. Salvataggi sicuri.** `save_document` con percorso assoluto può sovrascrivere qualsiasi
  file. Aggiungi `overwrite: bool = False`; con cartella progetti impostata rifiuta percorsi esterni
  e `..`. Ispirazione: 0xFFD, Slacker.
- [x] **P6. Prompt e risorse MCP.** Prompt `design_part` con il flusso tipico (piano → sketch →
  estrusione → verifica → salvataggio). Risorse READ: `solidworks://active/info`,
  `solidworks://active/feature-tree`, `solidworks://materials`.
- [x] **P7. Modalità sola lettura.** `[safety] read_only = true` nel TOML registra solo i tool READ:
  utile per revisioni di modelli altrui.
- [x] **P8. Assiemi.** `new_assembly`, `insert_component`, `add_mate` (coincident, concentric,
  parallel, distance, angle), `check_interference` (READ). È la lacuna più grande rispetto a 0xFFD e Slacker.
- [x] **P9. Tavole 2D.** `new_drawing` dal pezzo attivo, tre viste standard + isometrica,
  `insert_model_dimensions`, export PDF/DXF.
- [x] **P10. Configurazioni.** `list_configurations` (READ), `activate_configuration`, `add_configuration`.
- [x] **P11. Lamiera.** `base_flange`, `edge_flange`, `export_flat_pattern` (DXF).
- [x] **P12. CLI di supporto.** `solidworks-mcp doctor` (SolidWorks raggiungibile, stessi privilegi,
  modello di parte predefinito impostato, versione) e `solidworks-mcp install --client ...` che scrive
  la configurazione con backup.
- [ ] **P13. Test di integrazione.** Marker `solidworks`: crea una piastra 100×50×10 con foro Ø10 e
  confronta il volume con il calcolo (49 214,6 mm³), saltati se SolidWorks non risponde.
- [ ] **P14. Dividere `server.py`** (2100 righe) in moduli per gruppo di tool. Solo dopo la Fase A,
  mantenendo le annotazioni letterali nei decoratori.

---

## 4. Cose da non fare (fanno perdere punti)

- Dichiarare annotazioni con costanti, funzioni o decoratori personalizzati.
- `os.remove`, `unlink`, `SaveAs3` o `subprocess` dentro un tool READ.
- Descrizioni che dicono "returns", "lists", "image of" quando il codice scrive o cancella.
- Strumenti generici tipo "chiama qualsiasi metodo COM" o "esegui VBA" attivi di default.
- Chiamate di rete, telemetria, lettura di variabili con segreti, binding su `0.0.0.0`.
- Hook di build o script post-install in `pyproject.toml`.

## 5. Fonti

- [Scheda M8ven del tuo server](https://m8ven.ai/mcp/matteobaisini/solidworks-mcp) · [metodologia](https://m8ven.ai/verified/verify/methodology) · [ricerca server SolidWorks](https://m8ven.ai/verified?search=solidworks-mcp)
- Schede M8ven: [blussyya](https://m8ven.ai/mcp/blussyya/solidworks-mcp), [lildebil0](https://m8ven.ai/mcp/lildebil0/solidworks-mcp), [Slacker-LLC](https://m8ven.ai/mcp/Slacker-LLC/solidworks-mcp), [hjbaard](https://m8ven.ai/mcp/hjbaard/SolidWorks-MCP), [0xFFD](https://m8ven.ai/mcp/0xFFD/solidworks-mcp)
- Repository: [blussyya](https://github.com/blussyya/solidworks-mcp), [Slacker-LLC](https://github.com/Slacker-LLC/solidworks-mcp), [hjbaard](https://github.com/hjbaard/SolidWorks-MCP), [0xFFD](https://github.com/0xFFD/solidworks-mcp)
