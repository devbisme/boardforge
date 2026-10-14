# boardsmith — design doc (draft for review)

Status: **approved 2026-10-12; build-order steps 1–2 implemented** (see §7 and §4.8).
Written 2026-10-11 from the runs in
[ai-circuit-design-tests](https://github.com/devbisme/ai-circuit-design-tests) (local: `~/projects/AI/ai-circuit-design-tests`).
All run paths below are relative to that repo.
Name: **boardsmith**.

## 1. Goal

One tool, covering phases 1–14 (spec → fab outputs), that:

1. **Explores widely:** many architectures, a large part catalog.
2. **Converges:** once one alternative is picked, it produces a routed, DRC-clean,
   manufacturable board.
3. Runs **cheaply and quickly**: few tokens and short wall-clock time.
4. Is **easy to install**, can run **autonomously** on request, and needs **no GUI**. KiCad is
   used only to view or edit the results.

Source of truth for the circuit: **SKiDL code**.

## 2. What the runs say (evidence base)

### 2.1 Generator runs on the same prompt (all Opus 5.5)

| Run | Reached | Tokens | Equiv. $ | Wall clock | How the board was actually built |
|---|---|---|---|---|---|
| Konnect 2 | routed, DRC 0 | 164.8M | 61.12 | ~7 h (incl. GUI waits) | ~1 MCP call per edit, plus GUI steps by the user |
| kicad-mcp-server 2 | routed, 4 DRC err | 51.8M | 16.93 | ~2h20m | server functions in bulk from scripts, plus pcbnew scripts |
| kicad-mcp-pro 2 | routed, DRC 0 | 57.7M | 19.42 | ~1h35m | `sch_build_circuit` per sheet, plus scripts |
| eda-agent 2 | routed, DRC 0 | 51.6M | 18.47 | ~1h56m | DesignPlan JSON → label writer, plus scripts |
| kcaa 2 | routed, DRC 0 | 60.1M | 19.38 | ~3h45m (2×36 min Freerouting) | ~2,500 in-process kcaa calls from `design.py` |
| kicad-happy 2 | routed, DRC 0, 100 % MPN | n/a¹ | 29.37 | ~3.5 h | **SKiDL + scripts only**; kicad-happy for review |
| skidl-skills 9 | netlist only | 49.4M | 21.01 | ~2–3 h² | multi-agent SKiDL pipeline |
| Copperhead 1 | BOM only | — | ~33.5 | ~10 h | 208 single-turn sessions |

Sources: each run's `claude_cost.txt`, `README.md` and `executive_summary.md`.
¹ kicad-happy run 2 has no token total in its cost file listing. ² Run 8 was ~2¼ h; run 9's
time comes from its summary and is uncertain. The skidl-skills top-level README cost column
differs from some runs' `claude_cost.txt` (e.g. run 4: $93.45 vs $46.86). I used the cost
files.

**Takeaways**

- **The tool barely matters for the board; the scripts do.** Every routed board came from the
  same pattern: a single-file netlist → label-based schematic writer → kicad-cli netlist parity
  check → scripted placement → locked plane fanout → Freerouting → A* finisher → polish. Each
  MCP server was used for a slice and then worked around. Evidence: the "gaps filled by
  scripts" bullets in every tool README, and memory notes
  [kcaa design gaps], [kicad-mcp-pro design gaps], [eda-agent gaps].
- **Fine-grained MCP editing is expensive.** Konnect, editing one item per call, used about 3×
  the tokens of the script-driven runs. Calling tool code in-process (kcaa, kicad-mcp-server,
  kicad-mcp-pro, eda-agent) was the main way to keep cost down.
- **ERC and DRC passing is a weak measure of quality.** Every run passed both and still had
  spec-level faults:
  - weak anti-aliasing in nearly every run;
  - pin wiring written from memory because datasheet downloads failed;
  - decoupling caps 5–22 mm from their pins (eda-agent 1, kcaa 1).
- **There was little real exploration.** Each run listed options per block and then committed
  to one design. The skidl-skills runs differ from each other mostly through run-to-run noise,
  not deliberate search (skidl-skills README, "Comparison").
- **Nothing was compact.** Boards ranged from 115×85 to 150×90 mm. Copperhead's spec targeted
  ≤100×80 mm, but it never got to layout.

### 2.2 Best component per phase

| Phase | Best performer (evidence) | Keep | Drop / fix |
|---|---|---|---|
| 1 Spec | Copperhead `constraints.json` (16 numeric budgets, ASSUMED tags); skidl-skills `SPEC.md` hard/soft split | Both: prose spec + **machine-checkable constraints file** | — |
| 2 Architecture | Copperhead/skidl-skills per-block criteria → options → choice | Per-block option lists | Single-candidate commit; add real enumeration and scoring (§4.1) |
| 3 Budgets | Copperhead jitter/power/suspend budgets; kcaa `filter_design.py` | Budgets as code checked against constraints | Hand-waved filter order (aliasing missed in 6+ runs) |
| 4 Sourcing | skidl-skills part-sourcer + **pcbparts MCP** (live LCSC stock and price) | pcbparts; kicad-happy distributor skills (DigiKey/Mouser/LCSC/JLC) if keys exist | Stock-KiCad-lib-only restriction (Copperhead, most MCP runs) |
| 5 Datasheets | skidl-skills librarian + `fetch-datasheet.py` (validated PDFs), `pdf-table.py` | Same, plus a **persistent part-fact cache** | Repeated FTDI 403 / analog.com timeouts with no fallback |
| 6 Libraries | skidl-skills: kipart symbols, `generate-footprint.py`; flattened symdirs | kipart; footprint generator | Footprint-vs-package check missing (`validate-footprints.py` only checks the file exists) |
| 7 Capture | SKiDL modular block coders + assembler (skidl-skills); byte-stable netlists | Same | — |
| 8 ERC/parity | kicad-happy 2 `sch_writer.py` + `compare_netlists.py` (0 ERC, exact parity) | Label-based writing (to become schematizer's labels-only mode) + kicad-cli parity gate | SKiDL schematizer output (merged GND/+3V3, 910 violations) |
| 9 Review | skidl-skills **independent ERC reviewer** (found a HIGH defect in 5 runs); kicad-happy suite (SPICE, EMC, thermal, cross-domain; $1.92); eda-agent/kcaa-style netlist + geometry scripts | Reviewer agent with fresh context + kicad-happy scripts + geometry checks | MCP analyzers that gave wrong answers on label-connected designs (kcaa netlist/ratsnest, kicad-mcp-pro gate) |
| 10 PCB setup | kicad-happy 2 `build_pcb.py` (from netlist, no GUI); skidl-skills `generate-design-rules.py` | Build board from netlist in pcbnew Python; rules from a fab profile | kcaa GUI-only `update_pcb_from_schematic` (needed xdotool) |
| 11 Placement | kcaa `place_pcb.py` (signal-flow floorplan, B.Cu decaps, 2.5 mm escape band, collision guard); kicad-happy passives-next-to-pin; `pin_swap.py` | All of these, as one placer | Grid/spiral auto-placement (kicad-mcp-pro scored 1/100; eda-agent spiral decaps → 63+ unrouted) |
| 12 Routing | kicad-happy 2 / Konnect 2: `fanout.py`, `preroute_usb.py`, `dsn_tools.py`, Freerouting 2.4.1, `astar_fix.py`, SES merge | Whole pipeline, with the settings that worked: 0.45/0.2 vias, 0.15 tracks, `-mt 1`, inner layers typed `power` | Plane polygons in DSN (NPE); incremental Freerouting re-runs |
| 13 DRC/DFM | kicad-cli DRC `--schematic-parity`; eda-agent impedance calc; geometry script (decap distances); `add_fiducials.py`; `widen_narrow.py` | All | Silkscreen left dirty in most runs; no test points |
| 14 Outputs | kicad-cli exports + JLC conversion (eda-agent 1, kcaa 1, kicad-happy 2) | Scripted JLC zip/BOM/CPL, renders, `.pcf` | MCP export tools (inch CPL, ungrouped BOM, all 20 layers) |

## 3. Design principles

1. **The LLM decides; scripts execute.** The model writes specs, makes choices and writes SKiDL
   code, and steps in when something fails. Everything mechanical (schematic writing,
   placement, routing, checks, exports) is a deterministic script that returns a short
   summary. This is the main lever on tokens and time.
2. **Coarse tools.** Each tool call does a whole step, e.g. `route(project)`, not
   `add_via(x, y)`. The output is a summary of a few hundred tokens, with full details written
   to disk.
3. **One source of truth per layer.** SKiDL code for the circuit, `constraints.json` for
   requirements, a fab profile for the rules. Everything else is generated from these, never
   edited by hand. Hand edits caused the "partly scripted rebuild" problem in
   kicad-mcp-server 2 and kicad-mcp-pro 2.
4. **Gates measure the spec, not just ERC and DRC.** Each gate checks against
   `constraints.json` and fails loudly when it can't verify something. Following your
   error-handling preference, failures are warnings that are recorded; they don't stop the run.
5. **Files are the interface.** Handoff files plus `pipeline_state.json`, as in skidl-skills.
   That gives resume after a usage-limit stop, autonomy, and inspectable intermediate results.
6. **Headless by construction.** Use kicad-cli and KiCad 10's Python in subprocesses. Each
   mutation runs in a fresh process, which avoids the SWIG stale-wrapper problem
   ([eda-agent gaps]). IPC is optional and only used to refresh a KiCad window if one is open.

## 4. Architecture

```
            ┌────────────── EXPLORE (cheap, wide) ──────────────┐
 prompt ──► │ A1 spec + constraints.json                        │
            │ A2 enumerate N architectures (block × option grid)│
            │ A3 score each: deterministic calculators + quick  │
            │    part lookups → Pareto table                    │
            └──────────────┬────────────────────────────────────┘
                           │ select (user, or auto by weights)
            ┌──────────────▼──────── DETAIL (LLM-heavy) ─────────┐
            │ B1 sourcing + datasheets → part-fact cache         │
            │ B2 symbols/footprints (stock, kipart, generator,   │
            │    LCSC import) + package check                    │
            │ B3 SKiDL block coders + assembler                  │
            │ B4 review gate: reviewer agent + SPICE + budgets   │
            └──────────────┬─────────────────────────────────────┘
            ┌──────────────▼──────── PHYSICAL (scripts) ─────────┐
            │ C1 sch writer → kicad-cli netlist → parity gate    │
            │ C2 board setup from fab profile + net classes      │
            │ C3 placement (floorplan + algorithmic + compaction)│
            │ C4 routing (preroute, fanout, Freerouting, A*)     │
            │ C5 DRC/parity/DFM/geometry/EMC checks              │
            │ C6 fab outputs                                     │
            └──────────────┬─────────────────────────────────────┘
                     loop back to C3 (or B3) on failure
```

### 4.1 Explore (A1–A3): the new part

None of the tested tools did this, so this stage is the least proven.

- **A1:** prose `SPEC.md` plus a `constraints.json` of numeric budgets, each tagged
  HARD/SOFT/ASSUMED (from Copperhead). Catch contradictions early. Every run caught the USB
  throughput contradiction, so this already works.
- **A2:** for each block, list the option families: ADC type, memory, controller, bridge, AFE
  topology, power tree, layer count. Generate **N candidate architectures**, around 4–8,
  pruned for compatibility. Each one is a small structured file: blocks, key part *classes*,
  interfaces. A cheaper model (Sonnet/Haiku) can do the enumeration. Only key ICs are named,
  not passives.
- **A3:** score each candidate with **deterministic calculators**, not prose:
  - throughput and memory depth;
  - power budget against the USB limits;
  - anti-alias attenuation at Nyquist versus ENOB (this would have caught the most common
    defect in the runs);
  - clock jitter budget;
  - key-part cost and stock from pcbparts;
  - board-area estimate from summed courtyard areas × fill factor;
  - layer count and fab tier;
  - JLC basic vs. extended part count.

  The output is a Pareto table (`candidates.md`).
  - **Default (decided 2026-10-11):** the run stops here so you can pick a candidate. To pick
    one later, run `boardsmith select <id>`; the run then continues from `pipeline_state.json`.
  - In autonomous mode (`--auto`), it auto-selects using weights from the prompt and records
    why it chose that candidate.

Cost control: A3 is mostly calculator scripts plus one batch of part lookups, so exploring 8
candidates should cost far less than detailing one. *Estimate, unverified.*

### 4.2 Detail (B1–B4)

- Reuse skidl-skills' agents (architect, sourcer, librarian, block coder, assembler,
  reviewer), handoff files, `validate-bom.py`, and Sonnet subagents for the mechanical work.
- **Part-fact cache:** use kicad-happy's `datasheets/` skill (§4.7), not a new one. It already
  extracts pin tables, ratings, operating conditions and application circuits into per-MPN
  JSON, with schemas, scoring and validation. boardsmith only adds a cross-project location
  for the cache, which is checked before any fetch.
  - Your tests turned the cache off so runs would be independent. In the tool it should be
    on by default, with a `--fresh` switch.
  - It fixes two problems from the runs: facts that disagree between runs (ADS5231 minimum
    clock), and datasheet fetches that fail every time.
- **Datasheet fallbacks:** manufacturer URL → LCSC-hosted PDF (kicad-happy
  `fetch_datasheet_lcsc.py`, no key) → other distributor fetchers (keys needed) → flag as
  "from memory". Whether the LCSC copy works for FTDI and ADI parts is *unverified*.
- **Libraries:**
  - KiCad 10 stock libs, read natively. **Fix SKiDL's `.kicad_symdir` loading** rather than
    flattening libraries in every run.
  - kipart for missing symbols, and the footprint generator.
  - LCSC symbol/footprint import (e.g. easyeda2kicad) to widen the catalog. This is *new and
    untested here.*
  - Add a **package check** (footprint pad count, pitch and body size against the datasheet
    package) to catch the mirrored BNC and USB-C SH/S1 class of errors.
- **B4 review gate:**
  1. ERC.
  2. **Datasheet verification:** kicad-happy `datasheet_verify.py` compares the extracted
     specs with the connections: voltage violations, missing external parts, decoupling.
  3. The independent reviewer agent, with fresh context and datasheets.
  4. A **SPICE check of every analog block** against `constraints.json` (filter attenuation,
     headroom), using kicad-happy's spice scripts. The pass/fail limits come from boardsmith's
     constraints; kicad-happy alone only checks that a circuit is internally consistent
     (§4.7).
  5. Budget recheck with the real parts.

  Failures go back to B3 with the specific findings.

### 4.3 Physical (C1–C6): harden the scripts that worked

The starting point is kicad-happy run 2's `scripts/`, together with routing pieces from
eda-agent, kcaa and Konnect. Turn it into a package with tests.

- **C1:** schematizer in labels-only mode, then kicad-cli netlist, then pin-for-pin compare.
  - **Decided (2026-10-11):** the schematic is written by **schematizer** in a new
    **labels-only mode**. There is no separate writer package. schematizer is already a
    PyPI package that reads the same JSON, so boardsmith lists it as a dependency and it
    installs automatically.
  - Labels-only mode differs from schematizer's normal mode in three ways:
    1. Every net is stubbed, so each pin gets a power symbol, a global or local label, or a
       no-connect flag.
    2. Parts are placed on a simple deterministic grid instead of at random positions.
    3. There is no wire routing.
  - Prerequisite: fix schematizer's KiCad 10 file writing. With `auto_stub=True`, kicad-happy
    run 2 got merged GND/+3V3, dropped pins, an old version stamp, single-level `/uuid` sheet
    paths and 910 ERC violations. The cause hasn't been found and is probably in code shared
    with normal mode. The rules and fixes in kicad-happy run 2's `scripts/sch_writer.py`
    (0 ERC errors, exact netlist match) are the reference.
  - The **netlist-match check stays in boardsmith**, not schematizer. It needs `kicad-cli`, and
    schematizer has no KiCad dependency. The check exports a netlist with kicad-cli and
    compares it to the JSON `nets`.
  - The input is the **`skidl-generic-netlist` v1 JSON** produced by
    `skidl.schematic_netlist.build_generic_netlist`.
  - Why the format fits:
    - Symbols are embedded (`draw_cmds` + per-unit pin geometry), so no KiCad libraries are
      read at write time. This avoids the `extends`, private-property and symdir bugs seen in
      the runs.
    - Each pin carries its `unit`, which avoids the multi-unit bugs.
    - Nets have an `nc` flag (no-connects), and parts have a `hiertuple` (sheets).
    - Output is sorted, so it's deterministic.
    - The schema is versioned, and readers must ignore unknown keys.
    - The parity check can compare the kicad-cli netlist straight against the JSON `nets`.
  - Additions needed. All are additive keys, so the schema stays at v1 and schematizer
    ignores them:
    - `components[].fields`: MPN, manufacturer, LCSC#, DNP. Today only `value` and
      `footprint` are carried, and the BOM and CPL need these.
    - `nets[].attrs` (optional): net class, width, diff-pair partner, impedance. Used by C2.
      A sidecar file is the alternative.
  - Labels-only rules:
    - Net scope comes from `hiertuple`: a net on several sheets gets a global label, a net on
      one sheet gets a local label.
    - Power symbols are chosen by name against the vendored power library, the same rule
      schematizer uses. Other rails get labels.
  - SKiDL still needs its `.kicad_symdir` loading fixed, because it reads the libraries when
    it builds the document.
  - Readable schematic layout is a secondary goal. The schematic is for humans to view;
    connectivity comes from the netlist.
- **C2:**
  - Board built straight from the netlist in pcbnew Python, with no GUI.
  - Stackup and rules come from the selected **fab adapter** (§4.5). JLCPCB is the only one
    at first.
  - Net classes come from **net attributes declared in SKiDL**. This avoids the BUCK_SW miss
    caused by sheet-prefixed net names.
  - Save the `.kicad_pro` properly, since `board.Save()` wipes net classes.
- **C3 placement** is the weakest area in every run and the main lever on area:
  1. LLM floorplan: block regions in signal-flow order, analog/digital separation, connector
     edges. This is one short decision.
  2. Algorithmic placement inside each region:
     - ICs first;
     - decaps on the same side within N mm of their pin, or on the back side under the IC;
     - a 2.5 mm escape band around fine-pitch parts;
     - passives next to the pin they serve;
     - courtyard collision checks.
  3. FPGA/MCU pin swap to untangle buses (`pin_swap.py`). It writes the pin map back into the
     SKiDL source.
  4. **Compaction loop:** shrink the outline and region spacing step by step, scoring each
     step with a fast congestion estimate (ratsnest crossings and pin density per cell). Keep
     the smallest layout that still passes the routability estimate. Confirm with a real route.
- **C4 routing:** the steps below run through the router interface (§4.6). Only step 4
  depends on which router is used.
  1. Preroute critical nets: USB diff pair as a coupled pair at the target impedance, and the
     ADC clock.
  2. Locked plane fanout.
  3. Build a router-neutral `RouteJob` (see §4.6).
  4. Main router. The default is Freerouting, run from the bundled binary (no Java version
     trap) with `-mt 1`, small vias and 0.15 mm tracks. Its adapter handles the DSN details:
     plane polygons stripped, inner layers typed `power`, plane nets cut, SES merge.
  5. Finishing router: the A* router with rip-up, for whatever the main router left
     unrouted. Then polish: widen necked tracks, remove duplicate vias, outer pours, refill.
  6. If more than a threshold of connections stay unrouted, go back to C3 with a slightly
     larger area or a different swap. This loop runs without the LLM.
- **C5:** kicad-cli DRC `--schematic-parity` with the fab profile rules, then schematic↔PCB
  geometry checks with kicad-happy `cross_verify.py` / `cross_analysis.py`: diff-pair length,
  decoupling placement, power trace width, bus skew, thermal vias. boardsmith adds only what
  those miss:
  - decap distance per IC pin, with a hard threshold. On the Konnect board, kicad-happy run 1
    didn't report the 5–22 mm decaps that the eda-agent and kcaa geometry scripts found;
    whether `cross_verify` catches them is *unverified*;
  - clock route length;
  - digital traces crossing the AFE region.

  Then the kicad-happy PCB/EMC/thermal analyzers. The thermal analyzer gets real per-rail
  currents from the power budget; without them it assumed rated currents and produced
  false positives. Their findings are triaged by a short LLM
  pass that keeps a list of known false-positive patterns (e.g. EMC plane-gap findings,
  label-connected "isolated" parts).
- **C6:**
  - Gerber zip with fab layers only, checked with kicad-happy `analyze_gerbers.py`;
  - BOM and pick-and-place in the fab's formats, from the fab adapter. For JLCPCB, it uses
    kicad-happy `translate_bom_pnp.py`: LCSC numbers, pick-and-place in mm. That script passes
    rotations through unchanged, so boardsmith adds the per-footprint rotation offsets
    (*a gap* in every run);
  - fiducials, test points on key rails and signals, silkscreen cleanup;
  - 3D renders and a schematic PDF;
  - FPGA constraint file;
  - **release gate:** kicad-happy `fab_release_gate.py` gives a pass/fail "ready for fab"
    verdict from all the analyzer outputs.

### 4.4 Packaging and install

- **One Python package** (`uv tool install` / pip):
  - a CLI that runs each stage, or the whole pipeline (`boardsmith run --auto`);
  - library code for the scripts.
- **A Claude Code plugin** (skills + agents + script hooks) that drives the LLM stages and
  calls the CLI. An optional **small MCP server** exposes the same coarse steps for other
  clients, about 10–15 tools, not 289.
- **External dependencies:**
  - KiCad 10 (kicad-cli + its Python);
  - SKiDL;
  - Freerouting (bundled binary, fetched by `boardsmith setup`);
  - ngspice (optional);
  - kicad-happy scripts: **vendored** in `src/boardsmith/vendor/kicad_happy/` (MIT; version
    2.3.1, commit `0684046`; unmodified; attribution in `VENDORED.md`). A few validators need
    the optional `jsonschema` package.
  - Note: the scripts from kicad-happy **run 2** (`sch_writer.py`, `build_pcb.py`,
    `fanout.py`, `astar_fix.py`, …) aren't kicad-happy code. Claude wrote them during your
    test session, so they're yours to reuse directly.
- `boardsmith doctor` checks the environment against the pitfalls these tests kept hitting:
  - KiCad 9 vs. 10 kicad-cli;
  - the `KICAD_SYMBOL_DIR` override;
  - stale global `sym-lib-table` (use project-local tables);
  - the Java version.

### 4.5 Fab adapters (decided 2026-10-11: JLCPCB only at first, others easy to add)

Everything that depends on the fab sits behind one interface, so the rest of the pipeline
never names a fab. Adding a fab means adding one directory, `boardsmith/fabs/<fab>/`:

- **`profile.toml`** (data; TOML rather than YAML because Python's standard library reads
  it, including the KiCad Python that the board scripts run under, which has no PyYAML):
  - capability tiers: minimum track/space, drill sizes, annular ring, via types, board
    thickness, copper weights;
  - standard stackups with impedance data;
  - layer-count limits;
  - assembly limits: sides, smallest package;
  - DFM thresholds.
- **`adapter.py`** (code, a small class) with these methods:
  - `design_rules(tier, stackup)` → `.kicad_dru` plus net-class defaults;
  - `parts_catalog()` → sourcing back end. For JLCPCB: kicad-happy `search_lcsc.py` (free
    jlcsearch API, no key; stock, price tiers, basic vs. extended), with pcbparts MCP as an
    option. The script costs fewer tokens than an MCP server; how their results compare is
    *unverified*;
  - `gerber_set()` → layer list, naming and zip layout;
  - `bom(rows)` and `cpl(rows)` → the fab's files (column order, units, rotation-offset
    table);
  - `dfm_checks(board)` → fab-specific checks beyond KiCad DRC;
  - `estimate_cost(board, bom)` → rough price for A3/B scoring. Optional; may return
    "unknown".

A base class supplies defaults for anything a fab doesn't override, such as the plain
kicad-cli Gerber export. The fab is chosen once in `constraints.json` (`"fab": "jlcpcb"`).
Exploration (A3) uses the adapter's catalog and cost estimate, so it ranks candidates for the
chosen fab.

### 4.6 Router interface (decided 2026-10-11: Freerouting default, other routers pluggable)

Like the fab adapters (§4.5), each router plugs in through one interface, so the rest of the
pipeline never names a router. Routers live in `boardsmith/routers/<name>.py`.

- **`RouteJob`** (router-neutral input, built by boardsmith):
  - path to a scratch copy of the board (prerouted and fanned-out items are locked);
  - which nets to route, and which to skip or treat as plane-connected;
  - layer roles: signal, plane, or avoid;
  - rules per net class (width, clearance, via size) from the fab adapter;
  - diff pairs and length or skew targets;
  - time budget, thread count and random seed.
- **`RouteResult`** (output):
  - the routed board (or tracks and vias to merge);
  - the list of unrouted connections;
  - statistics: completion %, via count, total length, run time;
  - a log path.
- **`Router` class:**
  - `capabilities()`: which features the router supports, among planes, diff pairs, length
    matching, incremental routing and multithreading. The pipeline uses this to decide what
    to do itself, e.g. it does the plane fanout when the router can't use planes.
  - `available()`: a dependency check, which `boardsmith doctor` uses.
  - `route(job) -> RouteResult`.
- **Specctra base class:** a router that reads DSN and writes SES only has to supply its
  command line. The base class handles DSN export, plane handling, the SES merge (keeping
  locked items) and the import. KiCad's own `ExportSpecctraDSN` / `ImportSpecctraSES` do the
  file conversion.
- **Router chain:** set in config as a main router plus a finishing router, e.g.
  `["freerouting", "astar"]`. Each router gets the previous one's unrouted list as its job.
  The placement feedback loop (C4 step 6) only looks at `RouteResult`, so it works with any
  router.
- **Implementations at first:**
  - `freerouting`: the Specctra base class plus the bundled binary;
  - `astar`: the internal grid A* router with rip-up. It's the finisher, and works as a main
    router for small boards.
- **Possible later implementations** (not evaluated): other Specctra-capable routers, cloud
  routers with an API, or a native boardsmith router.

### 4.7 kicad-happy integration

kicad-happy (MIT, by Andrew Klofas) is **vendored**: a frozen copy of its files lives in
`src/boardsmith/vendor/kicad_happy/` with its license and a `VENDORED.md` that records the
source commit. It isn't installed as a separate dependency, for three reasons:
- It isn't a Python package (no `pyproject.toml`); it's a Claude Code plugin of scripts run by
  path, so pip couldn't install it automatically.
- A vendored copy is pinned and ships with boardsmith.
- Installing it as a plugin would also register its 11 skills with Claude. Their triggers
  overlap other skills (kicad-happy `setup.md`), so Claude might run them unprompted and
  spend tokens.

The cost of vendoring is that updates are manual: copy the new upstream tree over the old one,
then update `VENDORED.md`.

**Comparison with boardsmith's own components**

kicad-happy is better at what it already does:
- datasheet extraction and caching;
- analyzer breadth: EMC, thermal, Gerber, SPICE model generation, tolerance, parasitics,
  lifecycle;
- key-free LCSC search with low token cost.

Ties: JLCPCB BOM/CPL formatting (neither applies rotation offsets) and datasheet downloads
(both failed on FTDI and analog.com in the runs).

boardsmith's advantage is that it has data kicad-happy can only infer:
- **Connectivity from the source.** kicad-happy works out connections by parsing KiCad files
  with heuristics, which caused false positives (run 2, PP-001: a filter resistor not treated
  as a DC path). boardsmith has the SKiDL netlist plus the kicad-cli match check.
- **Pass/fail against requirements.** On the Konnect board, kicad-happy reported 36 of 36 SPICE
  subcircuits passing while the anti-alias filter was far too weak for 12 bits. boardsmith's
  `constraints.json` provides the limits.
- **Real operating data.** Per-rail currents from the power budget fix the thermal false
  positives (run 2: TS-001/002 at 121–132 °C).

So boardsmith mostly **feeds kicad-happy better inputs** rather than replacing it.

**Analysis-step registry (how to override kicad-happy components)**

Each analysis step is a registry entry, following the same pattern as the fab adapters (§4.5)
and router interface (§4.6). Its default implementation is a vendored kicad-happy script run as
a subprocess, and boardsmith can replace any entry with its own implementation.
- The scripts communicate through JSON files with a documented schema
  (`skills/kicad/references/output-schema.md`). Downstream consumers (`fab_release_gate.py`,
  `diff_analysis.py`, `cross_analysis.py`) only read that JSON. So a replacement only has to
  write the same JSON.
- Overrides work **one script at a time**, not one function at a time. Each script puts its own
  directory first on `sys.path`, so replacing a function inside one would mean patching the
  vendored files. `VENDORED.md` forbids that, except as a recorded local patch.
- Config chooses an implementation per step, e.g. `thermal: kicad_happy` or
  `thermal: boardsmith`.

**Components used, by stage**

| kicad-happy component | Where boardsmith uses it |
|---|---|
| `datasheets/` (extraction, cache, scoring) | B1 part-fact cache |
| `datasheets/datasheet_verify.py` | B4 review gate |
| `lcsc/search_lcsc.py`, `fetch_datasheet_lcsc.py` | JLCPCB `parts_catalog()`, B1 datasheet fallback |
| `spice/` (simulator, templates, models) | B4 SPICE gate, with limits from `constraints.json` |
| `kicad/analyze_schematic.py` | B4 / C1, on the written schematic |
| `kicad/cross_verify.py`, `cross_analysis.py` | C5 geometry checks |
| `kicad/analyze_pcb.py`, `emc/`, `analyze_thermal.py` | C5 |
| `kicad/analyze_gerbers.py` | C6 Gerber check |
| `bom/translate_bom_pnp.py` | JLCPCB `bom()` / `cpl()` (plus boardsmith rotation offsets) |
| `kicad/fab_release_gate.py` | C6 final gate |

**Deferred (cheap to add later)**
- `spice_tolerance.py`: Monte Carlo over component tolerances, e.g. whether the filter still
  meets spec with real parts.
- `extract_parasitics.py`: post-layout R/L/C for a SPICE re-run after C4.
- `spice_model_generator.py`: behavioral models for parts with no vendor model.
- `what_if.py`: change values and re-run the affected checks. Useful for A3 scoring and the B4
  rework loop.
- `diff_analysis.py`: shows what changed between iterations, so a rework loop can tell whether
  it made things better or worse.
- `lifecycle_audit.py`: obsolete/NRND/EOL checks. Needs distributor API keys.
- The structured deep review (`kicad/review/`): compare against skidl-skills' reviewer agent.
- `jlcpcb/` and `pcbway/` reference docs: source data for the fab profiles, and a head start on
  a PCBWay adapter.
- Report helpers (`summarize_findings.py`, `export_issues.py`).
- Not vendored: the GitHub Action (`action.yml`) for PR-based review.

**Upstream fixes to propose** (better to fix them there than to work around them here):
- resistors as DC paths in power-path checks;
- thermal analysis that accepts supplied rail currents;
- JLCPCB rotation offsets in `translate_bom_pnp.py`.

### 4.8 Implementation notes (build-order steps 1–2, 2026-10-13)

**Step 1** (SKiDL and schematizer) is committed on each repo's `boardsmith` branch: SKiDL
`c7a52b85`, schematizer `ca39a8e`. Neither is pushed or released, so boardsmith currently needs
both installed from those branches. The dual-ADC design reaches 0 ERC errors and an exact
netlist match (172 of 172 nets).

**Step 2** (C1–C6 plus the analysis-step registry) is in `src/boardsmith/`. Test projects:
`test/examples/tiny` (runs end to end in pytest, about a minute) and
`test/examples/dual_adc_usb` (kicad-happy run 2's SKiDL file and floorplan).

Dual-ADC results:

| Run | Routing (C4) | C5 DRC | Release gate | Wall clock |
|---|---|---|---|---|
| 3 (before the fixes below) | 984 s; Freerouting left 8 unrouted, A* got to 4; 2 clearance errors | 4 unconnected, 2 errors | FAIL (3) | 1108 s |
| 4 (`--from c4`) | 259 s; Freerouting alone, 0 unrouted | 0 errors, 0 unconnected, 0 parity | WARN | 335 s |
| 5 (clean build) | 334 s; Freerouting left 1; A* routed it three times, each with 3–4 clearance errors | 1 unconnected | FAIL (routing; fab tier from 0.09 mm tracks) | 458 s |
| 7 (clean) | 278 s; Freerouting 0 unrouted + 4 errors (thinned copper); A* repaired them | 1 unconnected (cleanup removed a live track) | WARN† | 395 s |
| 8 (clean) | 388 s; Freerouting 3 + 2 errors; A* left 1 (DA10) | 2 unconnected (cleanup broke a via chain) | FAIL | 507 s |
| 9 (clean) | 390 s; Freerouting 1; A* finished | 0 errors, 0 unconnected | WARN | 517 s |
| 10 (clean), then C4–C6 again | 260 / 252 s; Freerouting 2 / 1; A* finished both | 0 errors, 0 unconnected | WARN | 387 / 335 s |
| 11 (clean, repeatable C1–C4), then C4–C6 again | 419 / 441 s; Freerouting 1 (identical output); A* left 1 (DA10) | 1 unconnected | FAIL‡ | 537 / 515 s |

Each run was fixed before the next: A* clearance (after 5), Freerouting narrowing and the
board minimums (after 5), cleanup connectivity (after 7 and 8), stable UUIDs (after 9 and
10), gate routing status (after 11). With the final code, routing completeness depends on
the placement: of the clean placements, runs 7, 9 and 10 routed completely and runs 8 and
11 left one ADC data line (DA10, U12 → U14) in the congested ADC-to-FPGA bus. C1–C4 now
give the same board on every run, so a given design and config routes the same way each
time; the remaining fix is placement (step 3: floorplan and FPGA pin swap).

† kicad-happy counted the partly routed net as routed. ‡ Reported as WARN by the run;
FAIL after the gate fix below (rerun of C6).

**Where the implementation differs from §4.3–4.7**
- Config is `boardsmith.toml` in the project directory, merged over defaults and an optional
  `~/.config/boardsmith/config.toml` for machine paths (KiCad, Freerouting). Fab profiles
  are TOML too (§4.5). Net names in config are SKiDL names; C1 writes the SKiDL→KiCad map.
- The floorplan is not an LLM step yet: `[[placement.anchor]]` entries fix the main parts
  (run 2's anchor table), and C3 is run 2's placer with its design-specific tables moved into
  config. The placement upgrade is step 3.
- Each board stage saves a snapshot (`build/work/built.kicad_pcb`, `placed.kicad_pcb`), so any
  stage can be rerun (`boardsmith run --from c4`). A lock file allows one run per build
  directory.
- C1 runs SKiDL in a subprocess started in `build/work/`, because SKiDL opens its `.log` and
  `.erc` files in the current directory when it is imported.
- Stable UUIDs (`boardsmith/pcbfile.py`): KiCad saves footprints, tracks and vias sorted by
  UUID, and new items get random ones, so the file order changed on every run. That changed
  C3's placement (30–33 parts on the bottom side across runs 5–9) and the order of the
  locked fanout wires in Freerouting's input. C2 (footprints, from the reference) and C4
  before routing (tracks and vias, from their content) rewrite the saved file with derived
  UUIDs, since pcbnew's Python API can't set one. C1–C3 now give identical boards.
- C2 sets the minimum thermal-relief spokes to 1; KiCad's default of 2 flagged crowded decap
  pads as errors.
- DRC always runs with `--refill-zones`; otherwise the zones saved in the file are stale and
  the unconnected count is wrong.
- C4:
  - A* prerouting uses per-net width and clearance from the net classes.
  - Fanout skips a plane-net pad when a through-hole pad of the same net and footprint lies
    inside it (thermal vias in an exposed pad). Without this, a GND via in front of a
    WSON-12's pins blocked the routes out of them.
  - Freerouting's narrowing is off (`routing.freerouting.neckdown`). It has two mechanisms:
    neck-down at pins (0.075 mm tracks at 0.5 mm-pitch pads; widening them afterwards caused
    clearance errors) and retrying failed connections at `neck_width_um` (default 0.1 mm),
    which boardsmith sets to the default track width. It still thins some wires near pins to
    0.75–0.9 of their width, so the board's minimum track width and clearance are the
    smallest values a net class uses, never the fab's lower limits. DRC then flags thinned
    copper and the A* finisher repairs it. The chain goes on to the finisher whenever a
    router leaves DRC errors, not only unrouted connections.
  - The A* finisher first removes copper that DRC finds too thin, too close or crossing
    (`repairs` passes), then routes the unconnected pairs. Rip-up costs grow with how often a
    net has been ripped up before, and pairs that already forced rip-ups go last. This
    stopped two nets ripping each other up on every pass. A* keeps each obstacle at the
    larger of the two nets' clearances, as KiCad does; with only the routed net's clearance,
    0.1 mm-class data lines came 0.14 mm from default-class copper (run 5).
  - Finishing adds outer-layer pours with stitching vias on a grid (`board.stitch_pitch`),
    fiducials, and a "name Rev X" silkscreen label. Leaf copper (a track with a free end that
    touches at most one other item, or a via with one connection) is removed until none is
    left; locked copper is never removed. Connections count where copper overlaps, as in
    KiCad: A* joins tracks partway along others and places vias overlapping other copper.
    Counting only track ends removed a whole data line in run 7 and a via chain in run 8. If
    cleanup still leaves more unconnected items than the router chain did, C4 restores the
    board from before cleanup and warns.
  - The placement feedback loop (§4.3 C4 step 6) only grows the board when its size was
    estimated (`routing.grow_retries`); with a fixed size it reports the unrouted count.
- C5 thermal: `[checks.rail_currents]` declares regulator loads. boardsmith patches them into
  a copy of the schematic analysis (LDO dissipation, switcher loss, power-budget loads) that
  the thermal and cross-verify steps read. kicad-happy otherwise estimates LDO load from output
  capacitance (about 0.2 A for 20 µF), which caused run 2's TS-001/002 false positives. A
  power budget from the detail stage should fill this table later.
- C6 release gate: boardsmith gives `fab_release_gate.py` a copy of the PCB analysis with
  two corrections; the vendored files stay unmodified.
  - The footprint count leaves out board-only footprints (fiducials, mounting holes), which
    have no schematic symbol.
  - The routing status comes from C5's kicad-cli DRC. kicad-happy counts a net as routed
    once it has any copper, so a partly routed net passed the gate (run 11).
- KiCad 10 SWIG: deleting items while iterating a board's track list leaves the list stale.
  The board scripts take a Python snapshot of the list and remove items afterwards.

**Deferred or not done**
- Routing congestion on the ADC-to-FPGA data bus: some placements leave one data line that
  neither Freerouting nor A* rip-up completes (runs 8, 11). Step 3 (placement, FPGA pin
  swap) is the fix; the A* rip-up loop moves the shortage between bus nets rather than
  resolving it.
- Pin swap and compaction (step 3).
- FPGA constraint file and test points (C6).
- The digital-traces-over-AFE check (C5).
- `boardsmith setup` fetching Freerouting (step 6); for now `doctor` finds an installed copy.
- Diff pairs: `RouteJob.diff_pairs` exists but the pipeline doesn't fill it and no router
  uses it. The USB pair is prerouted by A* as two separate nets of the same width, not as a
  coupled pair at a target impedance.
- The JLCPCB rotation-offset table is kicad-happy's "typical" offsets and is unverified;
  C6 warns when it applies one.
- Known warnings on the dual-ADC board: 4 `via_dangling` (a signal via with copper on one
  layer only), 34 decaps farther than 3 mm from their pin, 61 BOM lines without an LCSC
  number, and the fab-capability warning for the deliberate 0.1 mm "Fine" net class, which
  needs JLCPCB's advanced process.

### 4.9 Step 3: placement upgrade (decided 2026-10-13)

Decisions:
- **Floorplan:** `[[placement.region]]` entries in `boardsmith.toml` (sheet or value regex →
  rectangle, optional board edge). A person or the agent driving boardsmith writes them;
  boardsmith makes no LLM calls, so runs stay repeatable. Anchors stay for exact positions.
- **Pin swap:** `[[placement.pin_swap]]` names the part, its swappable pin groups and each
  group's partner part. C3 writes `{SKiDL net: pin}` to a JSON file that the SKiDL design
  reads (as `dual_adc_usb.py` already did), and when the map changes, C1–C3 run again once.
  The SKiDL source isn't rewritten.
- **Order:** fix the measured failures first: pin swap (the DA10 congestion), decap
  placement (32–34 decaps over 3 mm), a congestion estimate; then regions and compaction.

Pin-swap rule: the signals going to the partner are ordered around the partner's outline
(clockwise from the widest gap between them) and the group's pins around the part's own outline
the other way; an order-preserving assignment then picks the pins (shortest total distance).
Run 2's `pin_swap.py` ordered signals by angle seen from the FPGA centre, which reverses the
pads on the ADC's top and bottom edges: their traces leave upwards or downwards and then turn,
so those groups crossed (DA6–DA11 and OFA went to FPGA pins 1–9 in reverse). When some partner
pads face away from the part (the SDRAM's far TSOP row), the whole group keeps run 2's
order seen from the part's centre: those traces drop through vias under the package, and the
outline order sent them around its ends (runs 12–13 left SDRAM nets unrouted).

Results so far (dual-ADC board, same anchors as §4.8):

| Run | Change | Routing (C4) | Decaps > 3 mm | Gate |
|---|---|---|---|---|
| 11 | (step 2 baseline) | 1 unrouted (DA10) | 32 | FAIL |
| 12 | pin swap, outline order for every group | 3 (DA7, DA9, SD_A11) | 32 | FAIL |
| 12b | + QFN ground pins stub into the exposed pad | 2 (SDRAM) | 32 | FAIL |
| 13 / 13b | + decaps first, back-side fallback; 13b: through-hole pins block the back | 13: shorts; 13b: 2 | 10 | FAIL |
| 14 | run 2 order when partner pads face away; back-side fallback for plane decaps only | 2 (DB3, CFG_MISO) | 27 | FAIL |
| 14b, 15 (clean) | + fanout vias keep out of other ICs' escape bands | **0**, Freerouting alone, 240 s | 27 | WARN |

What each failure turned out to be:
- The unrouted ADC lines were boxed in at the pin row, not short of channel space further
  out: fanout vias of the ADC's own ground pins (12b), then a back-side decap's vias under
  the ADC (14), sat in the lane its signal pins escape through. Ground pins next to the
  exposed pad now connect inwards to it, and vias for other parts' pads stay 1.2 mm from a
  fine-pitch IC's signal pads when there is room elsewhere.
- Run 13's shorts: the placer didn't count the pins of front through-hole parts (BNCs, USB-B,
  regulator thermal vias) as taken on the back, and treated a fuse between two rails as a
  decap. C3 now runs DRC on the placed board and warns about shorts and overlaps before
  routing (`reports/drc_placed.json`).
- Moving decaps on non-plane rails to the back added a routed via at a crowded pin row
  (+3V3_ADC, 13b), so the fallback is limited to plane decaps. Of the 27 decaps still over
  3 mm, most are on non-plane rails or 1–2.2 µF.

Also added: `reports/congestion.json` (RUDY demand per 2.5 mm cell, ratsnest crossings) in
C3, for the compaction loop; `[[placement.region]]` (floorplan regions, below).

**Compaction** (`[placement.compact]`). The board shrinks in uniform steps (default 5 %, down
to 0.6). Each step reruns C2 and C3 without the pin swap (about 9 s on the dual-ADC board) and
passes if every part is placed, the placed-board DRC finds no overlaps, no anchor overlaps
or leaves more parts away from their partners than at full size, and at most `max_over`
congestion cells exceed `max_util`. The smallest passing size is placed for real (pin swap
included) and routed; if routing leaves connections, the scale steps back up and C2–C4 run
again (`route_retries`). Anchor positions and region rectangles scale with the board; an
anchor with `edge` keeps its offset from that edge (connectors), and may slide only along it
when it has to clear another part.

| Board | Trials | Chosen | Stopped by | Route |
|---|---|---|---|---|
| dual-ADC, 130 × 90 | 1.0, 0.95, 0.9, 0.85 | 0.9: 117 × 81 (−19 % area) | 0.85: USB-B would leave its edge | run 16: Freerouting alone, 0 unrouted, 0 DRC errors, gate WARN; track 3958 mm (−10 %), 595 vias (−16 %) |
| tiny (estimated 29 × 22 at fill 0.12) | 1.0 … 0.75 | 0.8: 23.2 × 17.6 | 0.75: a part unplaced | complete |

On the dual-ADC board the congestion estimate stays at 0.9–0.95 for every size: its peak is
the ADC's own pin field, and the limit is the anchors running into each other. The anchor
table (run 2's, for 130 × 90) is the real constraint; regions in place of most anchors would
let the placer move ICs closer together.

**Regions** (`[[placement.region]]`). Tested with the dual-ADC board anchored only by its
four connectors (BNCs, USB-B, GPIO header, each tied to its edge) and one region per sheet
(rectangles in mm on 130 × 90: AP2112 74,14–92,28; TLV62569 112,50–128,64; front ends
16,8–46,34 and 16,56–46,82; power 14,36–46,52; ADC 46,32–66,56; FPGA 68,28–100,72;
SDRAM 102,26–126,54; USB 84,64–108,90).
- Shelf-packing each region's ICs from its top-left corner, in sheet order, ignored
  connectivity: 678 ratsnest crossings at full size against 352 for the anchor table, front
  ends at the top of their regions away from the BNCs, the USB ESD part away from the
  connector. Compaction stopped at 0.95 (a part no longer fit its region).
- Now each region IC goes at the centroid of its connections to parts already placed
  (signal nets only; rails and ground would pull everything to the regulators), clamped into
  its region, in the best of four rotations, most-connected first. Parts with no region are
  still shelf-packed. Full size: 356 crossings, 3629 mm ratsnest (anchors: 352).

| Floorplan | Compacted to | Route (Freerouting, then A*) | Track | Vias | Decaps > 3 mm |
|---|---|---|---|---|---|
| anchor table (run 16) | 0.9 (USB-B stops 0.85) | 0 unrouted, Freerouting alone | 3958 mm | 595 | 26 |
| 4 connectors + regions | 0.9 (USB-B stops 0.85) | 0 unrouted (Freerouting 1, A* finished it), 0 DRC errors, gate WARN | 3864 mm | 648 | 25 |

So a floorplan of regions does about as well as run 2's hand-tuned anchor table. The
regions config isn't kept in the repo: it would share the design's `fpga_pinmap.json`.

Freerouting 2.4.1 reads `gui.enabled` from `~/.config/freerouting/freerouting.json`; with it
on and a display present, the CLI routed, exited 0 and left an empty session file on the
dual-ADC board (the tiny board still saved), leaving A* the whole board. boardsmith now
passes `--gui.enabled=false` and reports an empty session file as a router failure.

**Congestion calibration** (`max_util`). The regions floorplan above (USB-B no longer tied
to its edge) placed and routed at forced scales, Freerouting alone, no compaction search:

| Scale | Placement check | RUDY max | p95 | Crossings | Unrouted | Freerouting |
|---|---|---|---|---|---|---|
| 0.9 (anchor table) | fits | 0.95 | 0.37 | 352 | 0 | 240 s |
| 0.9 (regions) | fits | 0.99 | 0.37 | 416 | 1 | 263 s |
| 0.85 | a part outside its region | 0.97 | 0.37 | 387 | 1 | 792 s |
| 0.8 | a part outside its region | 0.88 | 0.42 | 423 | 4 | 1111 s |
| 0.75 | anchor overlap | 1.02 | 0.44 | 541 | 5 (+3 DRC) | 634 s |
| 0.7 | DRC overlaps | 0.99 | 0.51 | 653 | 11 (+24 DRC) | 1677 s |

- The peak cell doesn't predict routing: it's the ADC pin field at every scale (0.88–1.02),
  and the 0.8 board, with the lowest peak, left 4 connections. No `max_util` separates these.
- The failed connections aren't in the hot cells. They are local: VBUS between the ESD part
  and the fuse (0.85); the FT2232H crystal, VPHY and a strap resistor (0.8); a charge-pump
  cap 15 mm from its IC (0.75); the FT2232H–FPGA bus (0.7). Shrinking squeezes passives away
  from their pins and parts out of their regions; 2.5 mm RUDY over MST edges can't see that.
- p95 and crossings rise with the failures (0.37 → 0.51, 387 → 653) but too gently to set a
  threshold from one design (0.9 passes at 0.37–0.41, 0.8 fails at 0.42).
- The placement checks already stop compaction where routing starts to fail (0.85 here:
  region overflow; anchor overlaps on the anchor table). Router time triples first.

So `max_util` stays at 1.0 as a backstop for boards whose channels really are full (none
yet), and `reports/congestion.json` gains a histogram (cells over 0.3 … 0.9) for later
designs. A better predictor of these failures is how far C3 had to move passives from their
targets; that, not RUDY, is the next calibration candidate.

Still to do in step 3: a decap rule for the non-plane rails.

## 5. Expected gains (estimates, not measurements)

| Criterion | Baseline in runs | Expected | Why |
|---|---|---|---|
| Install | 30–60 min, env workarounds per tool (`setup.md` files) | One install + `doctor` | No GUI and no IPC dependence; paths configured explicitly |
| Tokens / $ | ~50–60M, $17–19 for a routed board (script-heavy runs) | Lower, maybe ~half | Mechanical steps become scripts the model doesn't rewrite each run; the part-fact cache saves datasheet reads |
| Wall clock | 1.5–3.75 h | ~1 h | Fewer turns; better placement → shorter Freerouting runs (two 36-min runs in kcaa 2) |
| Exploration | 1 architecture per run | 4–8 scored candidates | A2/A3 |
| Part breadth | stock KiCad libs, often model memory | LCSC catalog + generated libs | pcbparts + import + kipart |
| Area | 115×85 to 150×90 mm | Smaller, amount unknown | Compaction loop; B.Cu decaps |
| Routing | 100 % after scripted rescue | 100 % with no manual work | Same pipeline, with the placement feedback loop |
| DFM | silkscreen, decap distance and LCSC fields often missing | Checked and gated | C5/C6 |

The token and time numbers are guesses. The way to check them is to build a minimal version
and run it on the dual-ADC prompt.

## 6. Risks

- **pcbnew SWIG bindings** are deprecated in favor of IPC. I believe removal is planned after
  KiCad 10, *but I haven't confirmed the timeline*. Mitigation: keep board mutation in one
  module, so it can be retargeted to IPC (needs a running KiCad instance, possibly headless —
  *unverified*) or to direct s-expression writing.
- **The routing pipeline is fragile.** It was tuned on one design with TQFP-144 and QFN-64
  parts. BGAs, 6-layer boards and dense RF layouts are untested.
- **The routability estimate for compaction may be poorly calibrated.** If it is, the loop
  costs Freerouting time instead of saving it.
- **Exploration quality depends on the calculators.** A missing calculator (e.g. EMI,
  thermal) means candidates are ranked on an incomplete basis.
- **The validation gap remains.** None of the designs has been checked by a human yet.
  Matching the old runs' ERC/DRC results says little; the real test is your validation
  checklist.

## 7. Proposed build order

The plan is top-down design and bottom-up build: start with the parts proven in the runs,
then add the new ones.

1. **SKiDL and schematic writer** (done; §4.8):
   - SKiDL: `.kicad_symdir` loading, plus the additive `fields`/`attrs` keys in
     `build_generic_netlist`.
   - schematizer: fix the KiCad 10 writer bugs, then add labels-only mode (all nets stubbed,
     grid placement, no routing). Done when the dual-ADC design reaches 0 ERC errors and an
     exact netlist match.
2. **Physical pipeline package (C1–C6)** plus the analysis-step registry (§4.7) (done;
   §4.8): harden the
   kicad-happy run 2 session scripts. Test it by
   regenerating an existing run's board from its SKiDL file with zero manual steps.
3. **Placement upgrade:** floorplan + algorithmic placer + compaction (pin swap,
   escape-aware fanout, decap ordering, compaction and regions done; §4.9).
4. **Detail stage:** port skidl-skills' agents, wire in kicad-happy's datasheet cache and
   `datasheet_verify`, and add the constraint-driven SPICE gate.
5. **Explore stage:** calculators, candidate enumeration, Pareto table.
6. **Packaging:** CLI, plugin, small MCP, `doctor`. Then rerun the dual-ADC prompt and compare
   against §2.1.

## 8. Open questions for you

1. ~~Name and location~~ — decided: `boardsmith`, its own repo.
2. ~~Where the schematic writer lives~~ — decided: a labels-only mode in schematizer, which
   is a boardsmith dependency. The netlist-match check lives in boardsmith.
3. ~~Selection gate~~ — decided: stop after A3 for your pick; `--auto` auto-selects.
4. ~~Fab target~~ — decided: JLCPCB only for now, behind a fab-adapter interface (§4.5) so
   others are easy to add.
5. ~~kicad-happy~~ — decided and done: MIT-licensed, vendored unmodified with attribution
   (`src/boardsmith/vendor/kicad_happy/`).
6. ~~Freerouting~~ — decided: keep it as the default main router, behind a router
   interface (§4.6) so other routers can be swapped in.

No open questions remain. Approved 2026-10-12.

## Sources

- Per-tool READMEs: `skidl-skills-tests/`, `konnect-tests/`, `kicad-happy-tests/`,
  `kicad-mcp-server-tests/`, `kicad-mcp-pro-tests/`, `eda-agent-tests/`,
  `kicad-ai-assistant-tests/`.
- Run summaries: `*/dual-adc-usb-*/executive_summary.md`; costs from `*/claude_cost.txt`.
- Scripts: `kicad-happy-tests/dual-adc-usb-2/scripts/`, `eda-agent-tests/dual-adc-usb-2/scripts/`,
  `kicad-ai-assistant-tests/dual-adc-usb-2/scripts/`, `konnect-tests/dual-adc-usb-2/routing/`.
- Tool-gap notes from these sessions (Claude memory): freerouting-kicad10-plane-workflow,
  skidl-schematic-and-routing-pipeline, kcaa-design-gaps, kicad-mcp-pro-design-gaps,
  eda-agent-kicad-gaps, kicad-mcp-server-editing-gaps.
- skidl-skills plugin layout: `~/projects/AI/skills/skidl-skills/` (agents, scripts,
  pcbparts MCP reference).
