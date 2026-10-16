# boardforge

Generate and analyze KiCad designs from a requirements prompt: explore many candidate
architectures, then turn the selected one into a routed, manufacturable board. SKiDL code is
the source of truth; runs headless (KiCad only for viewing/editing results).

This project was called **boardsmith** until 2026-10-16; the name is taken on PyPI by an
unrelated tool. An old `boardsmith.toml` is still read (with a warning), and old
`constraints.json` files keep working.

**Status:** the physical pipeline (C1–C6: SKiDL → schematic → board → placement → routing →
checks → fab outputs) and the review gate (B4) work. The detail stage's agents are a Claude
Code plugin ([`plugin/`](plugin/README.md)), run end to end twice on the dual-ADC design. The explore stage is not
started. Design: [`docsrc/design.md`](docsrc/design.md).

## Install (development)
```bash
uv pip install -e .            # or: pip install -e .
boardforge doctor              # finds KiCad 10, its Python, Freerouting
```
Needs KiCad 10 (kicad-cli and its Python), Freerouting (optional; the built-in A* router
works alone on small boards), and a SKiDL with `.kicad_symdir` support (currently SKiDL's
`boardforge` branch). Machine-specific paths can go in `~/.config/boardforge/config.toml`,
using the same `[kicad]` and `[routing.freerouting]` keys as a project file.

## Use
A project is a directory with a SKiDL file and a `boardforge.toml`:
```bash
cd test/examples/tiny
boardforge run                 # C1..C6 -> build/
boardforge run --from c4       # rerun from routing
boardforge route               # one stage: sch | board | place | route | check | fab
boardforge status
boardforge render --side both  # build/renders/*.svg/png of the current board
boardforge review              # B4: constraints.json against the design (after C1)
boardforge datasheets sync     # part-fact cache: datasheet PDFs and extractions
```
C3 places multi-pin parts next to what they connect to (through up to three series
passives), and warns when a connector's signal reaches its first IC from more than
`[placement] connector_path_mm` (25) away, such as an unbuffered input crossing the board.
C3 also reports a routability estimate (`reports/congestion.json`: routing demand per 2.5 mm
cell, ratsnest crossings) and, with compaction, the sizes it tried (`reports/compaction.json`). Each stage prints a short summary; details go to `build/reports/`, and
`build/pipeline_state.json` records every stage. Outputs:
- `build/kicad/`: KiCad project (labels-only schematic, board, net classes, lib tables)
- `build/fab/`: Gerber zip, JLCPCB BOM and CPL
- `build/docs/`: schematic PDF, 3D renders
- `build/reports/`: parity, ERC, DRC, geometry checks, kicad-happy analyses

### SKiDL conventions
- Use `part.fields["MPN"] = ...` (and `LCSC`, `Manufacturer`, `Tolerance`, ...). Keyword
  arguments to `Part()` and plain attributes (`part.MPN = ...`) don't become fields.
- Net classes: `NetClass("Power", track_width=0.3, clearance=0.15, priority=2)` and
  `net.netclasses = power` (not `net.netclass`, which does nothing). Keys are KiCad's
  `.kicad_pro` names; in SKiDL the larger `priority` wins.
- Don't set `lib_search_paths`; boardforge points SKiDL at KiCad 10's libraries.
- Expose a function that builds the circuit and name it in `[design] top`, or build it at
  import time. `[design] file` may also be a package directory (`circuits/<name>/` with
  `__main__.py`); it runs as a module, so relative imports work. The project directory and
  its `symbols/` are on SKiDL's library path.

### boardforge.toml
See `src/boardforge/config.py` for every key and its default. The main ones:
```toml
[design]
file = "tiny.py"
top = "build"                  # function that builds the circuit

[fab]
name = "jlcpcb"
tier = "standard"              # profile tier: standard (1-2 layers) | multilayer

[board]
size = [130, 90]               # mm; omit to estimate from courtyard area (fill)
layers = 4
planes = { "In1.Cu" = "GND", "In2.Cu" = "+3V3" }
pours = ["GND"]                # outer-layer pours, stitched with vias

[rules]                        # default net class; lowered to the pad gap of a finer-pitch
track_width = 0.15             # footprint (0.4 mm QFN: 0.10), down to the fab tier minimum
clearance = 0.15

[[placement.anchor]]           # floorplan: fixed positions (mm from top-left)
value = "^LTC2291"             # regex on value (or ref = "U3"), optional sheet regex
at = [55.0, 44.0]
rot = 0
edge = "left"                  # optional: keep the offset from this edge when compacting

[[placement.anchor]]           # a connector: rotation from the footprint, mating side out
ref = "J2"
at = [12.5, 22.0]
facing = "left"
edge = "left"

[placement.compact]            # shrink the board while everything still fits, then
enabled = true                 # confirm by routing (stepping back up if it fails)
min_scale = 0.6
max_stretched = 5              # stop when 5 more passives than at full size end up
stretch_mm = 8.0               # over 8 mm from their spot (C3 summary: stretch)

[[placement.region]]           # floorplan: unanchored ICs of a sheet go in this area, near
sheet = "front_end"            # the parts they connect to; regex on sheet (and/or value);
area = [0.0, 0.0, 0.4, 1.0]    # fractions of the board (or rect = [x0, y0, x1, y1] mm)

[[placement.pin_swap]]         # FPGA/MCU pin swap; C3 writes the map, the design reads it
part = "^ICE40"
map = "fpga_pinmap.json"       # {SKiDL net: pin}; C1-C3 rerun when it changes
[[placement.pin_swap.group]]
pins = [1, 2, 3, 4, 7, 8]      # swappable pins
partner = "^LTC2291"           # the part these signals go to

[routing]
chain = ["freerouting", "astar"]
preroute = ["USB_DP", "USB_DM"]

[checks.rail_currents]         # regulator loads (A) for the thermal check, overriding
"+1V2" = 0.05                  # constraints.json rail_current; else kicad-happy guesses
```
Net names in the config are SKiDL names; boardforge maps them to KiCad's.

### Review gate (B4)
`boardforge review` checks `constraints.json` (project root) against the design after C1 and
writes `build/reports/review.json`: PASS / FAIL / NOT CHECKED per constraint. A HARD failure
fails the gate; SOFT and ASSUMED failures, and anything not checked, warn.
```json
{"schema": "boardforge-constraints", "version": 1, "constraints": [
  {"id": "ALIAS-A", "kind": "ac_gain", "tag": "ASSUMED", "why": "content at Nyquist 40 dB down",
   "in": "VIN_A", "out": ["AIN_AP", "AIN_AN"], "at_hz": 5e6, "ref_hz": 1e3, "max_db": -40}]}
```
`ac_gain` runs an ngspice testbench of the whole path, built from the SKiDL netlist. Op-amps
and fully-differential amplifiers need a model (unmodelled ICs are open circuits):
```toml
[review]
dc = { VCM = 0.9 }             # DC volts for nets the netlist can't give
[review.models."^THS4521"]     # regex on value / MPN / symbol
type = "fda"                   # or "opamp"
gbw_hz = 93e6                  # overridden by a part-fact cache extraction
pins = { outp = 4, outn = 5 }  # roles the symbol's pin names don't give
```
`rail_current` (`net`, `max_a`: the load on the rail) is checked against the rated output
current (`regulator.iout_max`) of the part that makes the rail, found by its power-output pin
on the net or behind one series inductor or ferrite bead. The same loads feed the C5 thermal
check.
`review` also checks each part that has a datasheet extraction (see Part-fact cache), as
`DS:<MPN>`: the voltage across its supply pins against the absolute maximum (error) and
recommended range (warning), and its datasheet power pins connected. `DS:coverage` lists the
parts without an extraction. `PKG:pins` checks that every symbol pin on a net has a footprint
pad (and every numbered pad has a pin), and `PKG:<MPN>` compares the footprint with the
datasheet package: pad count, pitch, thermal pad, pin numbers and body size. `BOM` diffs the
sourced BOM (`[review] bom`, default `sourcing/sourced_bom.csv`) against the design: MPN,
LCSC number, value (numerically), footprint, symbol, and the `package` column against the
footprint's body variant.

### Part-fact cache
Datasheet PDFs and their structured extractions, in kicad-happy's layout
(`datasheets/manifest.json`, `<MPN>.pdf`, `extracted/<MPN>.json`), kept twice: in the project
(`datasheets/`, what the analyzers and `review` read) and in a cache shared by all projects
(`~/.cache/boardforge/datasheets/`). Parts are the netlist's `MPN` fields; ICs, diodes and
crystals by default (`prefixes`).
```bash
boardforge datasheets status   # per part: PDF, extraction, quality, staleness, in the cache
boardforge datasheets sync     # copy from the shared cache, then find missing PDFs
boardforge datasheets add MPN FILE_OR_URL   # one PDF, after checking it names the part
boardforge datasheets check MPN...  # validate extractions, fill derived fields, score
boardforge datasheets show MPN...   # markdown summary of extractions
boardforge datasheets paths    # kicad-happy's extraction prompts, schemas, examples
boardforge datasheets publish  # copy the project's extractions to the shared cache
```
Before there is a netlist, the parts come from the sourced BOM CSV.
A PDF is kept only if its text names the part; a scanned one needs `add --force` after a
look by eye. `sync` looks for a missing PDF in the `search` directories (a file named after the part,
`OPA810.pdf` for `OPA810IDR`), then downloads it from the symbol's datasheet URL, then
LCSC. A download whose text doesn't name the part is discarded (needs `pdftotext`). `--fresh`
skips the shared cache, `--offline` the downloads. Extraction itself is LLM work: an agent
writes `extracted/<MPN>.json` following kicad-happy's prompts and schemas (`paths`), then
`check` validates it and fills in the PDF hash, page count and quality score. `publish` takes
only extractions that match their PDF and pass kicad-happy's schema (needs `jsonschema`), and
keeps a shared entry unless the project's is newer and scores no lower (`--force`).
`review` takes op-amp GBW and slew rate from the extractions.
```toml
[datasheets]
search = ["~/datasheets"]
cache = "~/.cache/boardforge/datasheets"   # "" = no shared cache
```

## Layout
```
src/boardforge/         package
  physical.py           C1-C6 orchestration
  pcb/                  board scripts run under KiCad's Python (all pcbnew code is here)
  routers/              router interface: Freerouting (Specctra), A*
  fabs/<fab>/           fab adapters: profile.toml + adapter
  analysis/             analysis-step registry (kicad-happy analyzers by default)
  review/               B4 gate: constraints.json, SPICE path testbenches, datasheet, package and BOM checks
  datasheets.py         part-fact cache: status, sync, add, check, show, publish
plugin/                 Claude Code plugin: the detail-stage agents and skills (from skidl-skills)
  vendor/kicad_happy/   vendored kicad-happy (MIT)
test/                   pytest; test/examples/ has the tiny and dual-ADC projects
docsrc/                 documentation source (design doc)
```
Tests: `pytest -m "not kicad"` for the fast ones; `pytest` also runs the tiny design
end to end (about a minute).

## Third-party code
`src/boardforge/vendor/` contains code from other projects, each under its own license.
See [`src/boardforge/vendor/README.md`](src/boardforge/vendor/README.md). The plugin is
adapted from skidl-skills (MIT): [`plugin/ATTRIBUTION.md`](plugin/ATTRIBUTION.md).
