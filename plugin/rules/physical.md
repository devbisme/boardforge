---
description: Phase 7 — boardsmith.toml board, placement and rules settings, and how to read the physical pipeline's results
alwaysApply: false
---

# Physical pipeline (phase 7)

`boardsmith run --from c2` turns the reviewed design into a board:

| Stage | Does |
|---|---|
| C2 | board setup: outline, stackup, rules, planes |
| C3 | placement |
| C4 | routing: Freerouting, then A* |
| C5 | checks: DRC, geometry, analyses |
| C6 | fab outputs |

`boardsmith status` gives one line per stage. Each summary is in `build/pipeline_state.json`
and the details are in `build/reports/`. Routing a board of about 180 parts takes 10–45
minutes, so get placement right before routing: `boardsmith run --from c2 --to c3` takes
seconds.

Everything below goes in `boardsmith.toml`. Positions are mm from the board's top-left
corner. Net names are SKiDL names.

## Before the first run: a floorplan from the architecture

Write it from `architecture/block_diagram.md`'s signal flow, before routing anything. In the
dual-ADC run, the three attempts without a floorplan cost about 2 h of routing:
- no anchors left a BNC unplaced;
- a guessed rotation put the USB-C connector backwards.

```toml
[board]
size = [100, 80]                   # omit to estimate from the parts' area (fill = 0.35)
layers = 4
planes = { "In1.Cu" = "GND", "In2.Cu" = "+3V3" }   # solid inner planes
pours = ["GND"]                    # outer-layer ground pour, stitched with vias

[[placement.anchor]]               # every connector on its edge
ref = "J2"
at = [12.5, 20.0]                  # the footprint origin (often pin 1, not the body edge)
facing = "left"                    # rotation chosen so the mating side faces this edge
edge = "left"                      # keep the offset from this edge if the board shrinks

[[placement.anchor]]               # the keystone ICs, in signal-flow order
ref = "U13"                        # or value = "^ADS5231" (regex; optional sheet regex)
at = [45.0, 40.0]
rot = 0
```

- **Connectors:**
  - Anchor every one with `facing` and `edge`; don't guess `rot`.
  - To get the `at` coordinate across the edge, take the distance from the footprint origin
    to the body's front face from the footprint file. Look in its `descr` or the F.Fab
    outline; KiCad's connectors say where the board edge goes.
- **ICs:**
  - Anchor the few that set the signal flow (ADC, FPGA/MCU, the USB bridge), with room
    between them for the parts that connect them.
  - Everything else follows its connections. Multi-pin parts are placed next to what they
    connect to, through up to three series passives. Passives go next to the pin they serve.
- **Input chains:** to keep an input's front end beside its connector, anchor its first
  amplifier there too. A buffer belongs at the jack; a fully-differential driver belongs at
  the ADC.
- **Regions** (`[[placement.region]]` with `sheet`/`value` regexes and
  `area = [x0, y0, x1, y1]` fractions) confine a whole block, such as the analog section or
  the power supplies, to part of the board.
- **Fine-pitch parts need nothing.** When a footprint's pads are closer than
  `[rules] clearance` (a 0.4 mm-pitch QFN at 0.10 mm), boardsmith lowers the board
  clearance to that gap, down to the fab tier's minimum, and says so in C2. Traces escaping
  those pins need it. The fab check judges spacing against the board's own fab tier
  (JLCPCB 4-layer: 0.09 mm), so this doesn't move the board into a costlier tier.

## Read C3 before routing

The C3 summary and its warnings show:

- **`unplaced`, `anchor_overlaps`:** fix the anchors.
- **`<conn>.<pin> (<net>) reaches <IC> NN mm away through …`:** a connector signal travels
  more than 25 mm (`[placement] connector_path_mm`) to its first IC. For an analog input,
  that is an unbuffered high-impedance trace across the board. Anchor that IC by its
  connector, or move the connector.
- **`stretch`:** passives pushed far from their pins (`worst` lists them).
- **"N placement cells need more routing than they have":** congestion
  (`reports/congestion.json`). Spread the anchors or enlarge the board.

To see the placement before routing, render it:

```bash
kicad-cli pcb export svg --layers F.Fab,F.Silkscreen,Edge.Cuts,F.Cu --mode-single --fit-page-to-board \
    --exclude-drawing-sheet -o /tmp/place.svg build/kicad/<name>.kicad_pcb
```

Then convert it to PNG (inkscape or rsvg-convert) and look at it.

## After routing

- **C4:** `unrouted` must be 0. If the board size was estimated, boardsmith grows it and
  retries by itself.
- **C5:**
  - DRC `errors` must be 0. Group the errors in `reports/drc.json` by type and part; they
    usually point at one footprint or one anchor.
  - `decaps.far` lists decoupling capacitors over 3 mm from their pin.
- **C6:**
  - The release gate is in `reports/analysis/release_gate.json`. Its DFM tier is judged
    against the board's own fab tier.
  - The fab files are in `build/fab/`. `lines_without_lcsc` counts BOM lines JLCPCB can't
    place: those parts need an `LCSC` field in the code.

Change `boardsmith.toml`, never circuit code, in phase 7. A defect that needs a circuit
change goes back to the user.
