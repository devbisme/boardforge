---
description: KiCad footprint rules for creating and validating footprints
paths: 
    - "**/*.kicad_mod"
alwaysApply: false
---

# KiCad Footprint Rules

## Footprint String Conventions

Always verify footprint strings match **exact** KiCad library filenames before generating a netlist.
Common traps on this project:

| Wrong | Correct |
|---|---|
| `Package_TO_SOT_THT:TO-247-4_Horizontal` | `Package_TO_SOT_THT:TO-247-4_Horizontal_TabUp` |
| `Package_TO_SOT_THT:TO-247-2` | `Package_TO_SOT_THT:TO-247-2_Horizontal_TabUp` |
| `Resistor_SMD:R_MELF_MMB0207` | `Resistor_SMD:R_MELF_MMB-0207` (dash, not nothing) |
| `Varistor:RV_Disc_D21.5mm_W6.1mm_P10mm` | `Varistor:RV_Disc_D21.5mm_W11.4mm_P10mm` (check width) |

To verify footprint strings exist (KiCad's libraries and the project's `footprints/`):
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/validate-footprints.py Package_SO:SOIC-8_3.9x4.9mm_P1.27mm ...
```

## Custom Footprint Libraries

When a part has no KiCad footprint (unusual modules, custom ICs):

1. Create `footprints/<LibraryName>.pretty/<FootprintName>.kicad_mod`
   (`scripts/generate-footprint.py`)
2. Add it to `boardsmith.toml`:
   ```toml
   [libraries.footprints]
   LibraryName = "footprints/LibraryName.pretty"
   ```
   boardsmith writes it into the project's fp-lib-table; no KiCad preferences needed.
3. Reference in SKiDL as `'LibraryName:FootprintName'`

## .kicad_mod File Format Rules

**Do NOT use semicolons (`;`) as comments.** The KiCad S-expression parser rejects files with semicolons — the footprint silently fails to load ("not found in any libraries").

**`(hide yes)` must go inside `(effects ...)`,** not as a sibling of `(layer ...)`.

Correct property with hidden text:
```scheme
(property "Datasheet" "https://..."
  (at 0 0 0)
  (layer "F.Fab")
  (effects (font (size 1 1) (thickness 0.15)) (hide yes))
)
```

Correct pad definitions:
```scheme
(pad "1" thru_hole rect (at 0 0) (size 1.9 1.9) (drill 1.2)
  (layers "*.Cu" "*.Mask")
)
(pad "2" thru_hole oval (at 2.54 0) (size 1.9 1.9) (drill 1.2)
  (layers "*.Cu" "*.Mask")
)
```

Key rules:
- Pin 1 pad: `rect`; all others: `oval`
- No inline comments of any kind (no `;`, no `#`)
- Courtyard (`F.CrtYd`) stroke: 0.05mm; Fab/SilkS: 0.1–0.12mm
- THT pad size: drill + 0.7mm annular ring (e.g. drill 1.2 → pad 1.9×1.9)

## Always Read the Package Drawing — Do Not Trust Text Summaries

Datasheet text descriptions of pin arrangements may be wrong or ambiguous.
**Always read the actual mechanical drawing and recommended footprint diagram to verify the pin layout.**

Example: Murata MGJ6D was described in the datasheet text as "single row of 8 pins" but the
mechanical drawing clearly shows **two 2×2 groups** separated by the component body.
