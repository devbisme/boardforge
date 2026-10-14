# boardsmith

Generate and analyze KiCad designs from a requirements prompt: explore many candidate
architectures, then turn the selected one into a routed, manufacturable board. SKiDL code is
the source of truth; runs headless (KiCad only for viewing/editing results).

**Status:** the physical pipeline (C1–C6: SKiDL → schematic → board → placement → routing →
checks → fab outputs) works. Explore and detail stages are not started. Design:
[`docsrc/design.md`](docsrc/design.md).

## Install (development)
```bash
uv pip install -e .            # or: pip install -e .
boardsmith doctor              # finds KiCad 10, its Python, Freerouting
```
Needs KiCad 10 (kicad-cli and its Python), Freerouting (optional; the built-in A* router
works alone on small boards), and a SKiDL with `.kicad_symdir` support (currently SKiDL's
`boardsmith` branch). Machine-specific paths can go in `~/.config/boardsmith/config.toml`,
using the same `[kicad]` and `[routing.freerouting]` keys as a project file.

## Use
A project is a directory with a SKiDL file and a `boardsmith.toml`:
```bash
cd test/examples/tiny
boardsmith run                 # C1..C6 -> build/
boardsmith run --from c4       # rerun from routing
boardsmith route               # one stage: sch | board | place | route | check | fab
boardsmith status
```
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
- Don't set `lib_search_paths`; boardsmith points SKiDL at KiCad 10's libraries.
- Expose a function that builds the circuit and name it in `[design] top`.

### boardsmith.toml
See `src/boardsmith/config.py` for every key and its default. The main ones:
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

[rules]                        # default net class
track_width = 0.15
clearance = 0.15

[[placement.anchor]]           # floorplan: fixed positions (mm from top-left)
value = "^LTC2291"             # regex on value (or ref = "U3"), optional sheet regex
at = [55.0, 44.0]
rot = 0
edge = "left"                  # optional: keep the offset from this edge when compacting

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

[checks.rail_currents]         # regulator loads (A) for the thermal check; otherwise
"+1V2" = 0.05                  # kicad-happy guesses them from output capacitance
```
Net names in the config are SKiDL names; boardsmith maps them to KiCad's.

## Layout
```
src/boardsmith/         package
  physical.py           C1-C6 orchestration
  pcb/                  board scripts run under KiCad's Python (all pcbnew code is here)
  routers/              router interface: Freerouting (Specctra), A*
  fabs/<fab>/           fab adapters: profile.toml + adapter
  analysis/             analysis-step registry (kicad-happy analyzers by default)
  vendor/kicad_happy/   vendored kicad-happy (MIT)
test/                   pytest; test/examples/ has the tiny and dual-ADC projects
docsrc/                 documentation source (design doc)
```
Tests: `pytest -m "not kicad"` for the fast ones; `pytest` also runs the tiny design
end to end (about a minute).

## Third-party code
`src/boardsmith/vendor/` contains code from other projects, each under its own license.
See [`src/boardsmith/vendor/README.md`](src/boardsmith/vendor/README.md).
