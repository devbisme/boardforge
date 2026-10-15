---
description: SKiDL coding conventions, syntax reference, ERC rules, and footprint validation
paths: 
    - "**/*.py"
alwaysApply: false
---

# SKiDL Conventions

- Always `from skidl import *` at the top
- Use `dest=TEMPLATE` (not `TEMPLATE`) for parts instantiated multiple times
- Net naming: UPPERCASE for power rails (`VCC`, `GND`, `V3V3`), camelCase for signals (`txData`, `csBar`)
- Supply nets require `.drive = POWER` to pass ERC
- Always assign `ref`, `value`, and `footprint` on every Part
- **Every part gets its MPN as a field**: `part.fields["MPN"] = "<MPN from the BOM>"`
  (also `LCSC`, `Manufacturer` when the BOM has them). Keyword arguments to `Part()` and
  plain attributes (`part.MPN = ...`) do **not** become fields, and the review gates find
  each part's datasheet facts, and the BOM check matches each row, by this field.
- Footprints: KiCad format — `'Library:Footprint_Name'` (no `.pretty`)
- `+=` is the only connection operator — never use `=` for net connections
- SKiDL slices `[2:4]` are **inclusive on both ends** (unlike Python)
- Group functional blocks into `@SubCircuit` decorated functions with docstrings
- Use `TEMPLATE` copies for repeated passives; never duplicate Part definitions
- Every `@SubCircuit` gets a docstring: purpose, inputs/outputs, assumptions
- Comment each section with the architecture block or net-plan row it implements
- Decoupling: 100 nF + 10 µF per IC VCC pin, named `C_DECOUP_<IC_REF>`
- Take MPNs, values, and footprint strings from `sourcing/sourced_bom.csv` verbatim —
  never invent a footprint and never hard-code a value that belongs in the BOM
- Don't set `lib_search_paths`; boardsmith points SKiDL at KiCad 10's libraries and the
  project's `symbols/` directory
- Net classes (optional; boardsmith turns them into board rules):
  `power = NetClass("Power", track_width=0.3, clearance=0.15, priority=2)` then
  `net.netclasses = power` (not `net.netclass`, which does nothing)

# SKiDL Syntax Reference
 
```python
from skidl import *  # required in every file
```
 
## Parts
 
```python
R1 = Part('Device', 'R', value='10k', footprint='Resistor_SMD:R_0603_1608Metric')
 
R_t = Part('Device', 'R', dest=TEMPLATE)   # template — not added to circuit
R2, R3 = R_t(2, value='4.7k', footprint='Resistor_SMD:R_0603_1608Metric')
```
 
## Nets & Connections
 
**`+=` is the only way to connect.** Never use `=` for connections.
 
```python
GND  = Net('GND')      # UPPERCASE for power rails
V3V3 = Net('V3V3')
sig  = Net('txData')   # camelCase for signals
 
V3V3 += R1[1]
R1[2] += R2[1]                       # pin-to-pin creates implicit net
U1['VCC', 'GND'] += V3V3, GND        # multi-pin shorthand
V3V3.drive = POWER                   # required on supply nets to pass ERC
```
 
Pin access: `part[1]`, `part['VDD']`, `part['GP0 GP1']`, `part[2:4]` (slices are **inclusive** on both ends).
 
## Buses
 
```python
data_bus = Bus('DATA', 8)            # nets DATA0..DATA7
part['DQ[0:7]'] += data_bus          # connect all 8 lines
data_bus[3:0]                        # reverse slice
```
 
## Series / Parallel Networks
 
```python
VCC & R1 & R2 & GND                 # series
VCC & (R1 | R2) & GND               # parallel
VCC & R1 & outp & Q1['C,E'] & GND   # named tap point inside network
inp & tee(C1 & GND) & L1 & outp     # tee junction
```
 
## Subcircuits
 
```python
# Method 1 — decorator (reusable, returns Interface)
@SubCircuit
def ldo(vin, vout, gnd):
    Reg = Part('Regulator_Linear', 'AMS1117-3.3', footprint='Package_TO_SOT_SMD:SOT-223-3_TabPin2')
    C_in, C_out = Part('Device', 'C', dest=TEMPLATE)(2, value=['100nF', '10uF'])
    vin & Reg['VI'] & C_in & gnd
    vout & Reg['VO'] & C_out & gnd
    return Interface(vin=Reg['VI'], vout=Reg['VO'], gnd=Reg['GND'])
 
# Method 2 — subclass (OO style)
class Ldo(SubCircuit):
    def __init__(self):
        self.initialize()            # do NOT call super().__init__()
        Reg = Part('Regulator_Linear', 'AMS1117-3.3')
        self.vin  = Reg['VI']
        self.vout = Reg['VO']
        self.gnd  = Reg['GND']
        self.finalize()              # required
 
# Method 3 — context manager (one-off grouping, not reusable)
with SubCircuit('power'):
    Reg = Part('Regulator_Linear', 'AMS1117-3.3')
    V12 & Reg['VI'] & GND
```
 
## Cross-File SubCircuits (Modular Mode)

For circuits with >3 blocks, each block lives in its own file within a package directory:

```python
# circuits/my_circuit/power_input.py
from skidl import *

@SubCircuit
def power_input(vin, hv_plus, gnd):
    """Power input stage with reverse polarity protection."""
    D1 = Part('Device', 'D_Schottky', ref='D1', value='SS34',
              footprint='Diode_SMD:D_SMA')
    C_bulk = Part('Device', 'C_Polarized', ref='C1', value='100uF',
                  footprint='Capacitor_SMD:CP_Elec_6.3x7.7')
    vin += D1[1]
    D1[2] += hv_plus
    hv_plus += C_bulk[1]
    C_bulk[2] += gnd

# circuits/my_circuit/__main__.py
from skidl import *
from .power_input import power_input

VIN = Net('VIN'); VIN.drive = POWER
GND = Net('GND'); GND.drive = POWER
HV_PLUS = Net('HV_PLUS')

power_input(vin=VIN, hv_plus=HV_PLUS, gnd=GND, tag='power_input')

def _stabilize_tags():
    """Derive every untagged part's tag from its refdes (see "Stable netlists")."""
    for part in default_circuit.parts:
        if not getattr(part, 'tag', None):
            part.tag = part.ref

_stabilize_tags()   # at import time: boardsmith imports this module, it doesn't run __main__
```

`boardsmith.toml` names the package directory: `[design] file = "circuits/my_circuit"`.
boardsmith runs it as a module (so the relative imports work), runs ERC and writes the
netlist itself — no `ERC()`, `generate_netlist()` or `generate_xml()` in the design. Run it
with `boardsmith sch` (`rules/environment.md`).

Key rules for modular blocks:
- Block files: only `from skidl import *`, no cross-block imports
- No `ERC()`, `generate_netlist()`, or `__main__` in block files
- Function signature is dictated by the block manifest in `handoffs/02_architecture.md`
- Ref designators are pre-assigned per block (no collisions)
- Assembly file (`__main__.py`) creates all nets and calls all blocks
- A block instantiated more than once (e.g. one analog channel ×2) takes an instance
  argument and offsets its refdes by it — `ref=f'U{100*ch + 7}'` gives U107/U207 — so
  every instance has deterministic references. Otherwise SKiDL uniquifies collisions with
  `_1` suffixes (`U8_1`), which breaks the BOM-to-board mapping.

## Stable netlists

SKiDL derives each component's netlist UUID from its `tag`, and invents a **random** tag
when none is set, logging a "Missing tag" warning per part (445 in the measured run).
With random tags every regeneration produces a new netlist. KiCad's "update PCB from
schematic" then treats every part as new and discards placement and routing. Two things
make it reproducible:

- Pass `tag='<block_id>'` on every block call in `__main__.py` (`tag='afe_ch1'`,
  `tag='afe_ch2'` for repeated blocks).
- Call `_stabilize_tags()` (template above) after the last block call, at import time.
  It gives each untagged part its refdes as its tag.

Two runs of unchanged code then produce identical `build/netlist.json` files. Editing a block file also shifts the `SKiDL Line` field (source file:line)
of every part defined below the edit. That is metadata: component UUIDs, values and
connectivity are unchanged, and KiCad keeps placement.

## Exposed pads

A QFN/LFCSP footprint's exposed pad has its own pad number (e.g. `33` on a 32-pin part).
If the symbol has no pin with that number, the pad gets **no net** in the netlist: the
thermal and ground pad ends up unconnected on the board, and neither ERC nor the
footprint validator notices. Connect it (`U['EP'] += gnd`, or the supply the datasheet
names). Generated symbols must include it.

**Modular block file template:**
```python
"""<Block Name> — <one-line description>
Block from: architecture/block_diagram.md
Interface nets: VIN, VOUT, GND
"""
from skidl import *

@SubCircuit
def block_name(vin, vout, gnd):  # signature from block manifest
    """Docstring with Args."""
    # Parts, connections...
```

## ERC & No-Connects
 
```python
Pic[:] += NC          # mark all pins no-connect first…
Pic['VDD'] += V3V3    # …then connect the ones you use (auto-removed from NC)
GND.do_erc = False    # silence a specific net
U1.do_erc = False     # silence all pins on a part
```
 
## Running

boardsmith builds the circuit, runs ERC and writes every output (`boardsmith sch`; the board
and fab files come from `boardsmith run`). A monolithic file either builds the circuit at
import time or exposes a function named in `[design] top`. Don't call `generate_netlist()`,
`generate_xml()` or `generate_pcb()`; an `if __name__ == '__main__':` block is ignored by
boardsmith and may stay for standalone runs.

## ERC Rules (Non-negotiable)

- No floating pins on active ICs — use `NC` (the net object) for intentional no-connects: `U1['NC1'] += NC`
- No power pin conflicts (two `OUT` pins on the same net without isolation)
- Decoupling caps required on every IC VCC pin — 100 nF minimum, named `C_DECOUP_<IC_REF>`
- Pull-up/pull-down resistors required on all open-drain/open-collector pins
- "Insufficient drive current" warnings on power nets without `PWR_FLAG` are expected — not errors
- KiCad's ERC (run by `boardsmith sch` on the generated schematic) must also show 0 errors.
  `.drive = POWER` satisfies SKiDL's ERC only; a supply that enters from a connector or
  through a passive (ferrite, fuse) needs a flag for KiCad's: `Part('power', 'PWR_FLAG')[1] += net`
  — otherwise KiCad reports `power_pin_not_driven`

## Footprint Validation (Non-negotiable)

- Every `footprint='Lib:Name'` string must resolve to an actual `.kicad_mod` file. The
  post-write hook checks each Write/Edit; `boardsmith review` (`PKG:pins`) checks the built
  design, including that every connected symbol pin has a pad of the same number and every
  numbered pad has a pin.
- The package itself (pad count, pitch, thermal pad, body) is checked against the datasheet
  extraction by `boardsmith review` (`PKG:<MPN>`), and the body variant against the BOM's
  `package` column (`BOM`).
- If a part has no standard KiCad footprint: generate one with `scripts/generate-footprint.py`
  - Provide pad positions/sizes from the datasheet mechanical drawing
  - Output goes to `footprints/<LibraryName>.pretty/<FootprintName>.kicad_mod`
  - Add `<LibraryName> = "footprints/<LibraryName>.pretty"` under `[libraries.footprints]`
    in `boardsmith.toml`, and reference it as `footprint='LibraryName:FootprintName'`

## Common Pitfalls
 
- `=` rebinds Python variables — **only `+=` makes connections**
- `dest=TEMPLATE` parts are excluded from the netlist until copied
- SKiDL slices `[2:4]` include both endpoints (unlike Python's `[2:4]`)
- `SubCircuit` subclasses: call `self.initialize()` / `self.finalize()`, never `super().__init__()`
- Supply nets need `.drive = POWER` to avoid `Insufficient drive` ERC errors
- Intentional no-connects use `NC` (the net object), not `NC`
