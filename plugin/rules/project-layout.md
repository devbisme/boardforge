---
description: Project directory structure for boardsmith design projects
alwaysApply: true
---

# Project Layout

The project directory is both the design workspace and the boardsmith project (it holds
`boardsmith.toml`). Run every `boardsmith` command from it.

- `SPEC.md` — captured design requirements
- `constraints.json` — numeric requirements the review gate checks (`ac_gain`,
  `rail_current`), each tagged `HARD`/`SOFT`/`ASSUMED`. Written by the architect (phase 2)
  from the requirements. Format: `agents/circuit-architect.md` § constraints.json.
- `boardsmith.toml` — `[design] file` (the circuit), board and fab settings, project
  footprint libraries (`[libraries.footprints]`). Written by the driver after phase 2.
- `architecture/` — block diagram, net plan, IC selection, skeleton BOM, design risks
- `sourcing/` — `sourced_bom.md` (human-readable), `sourced_bom.csv` (diffed against the
  built design by the `BOM` review gate; the two must agree), `sourcing_failures.md`
- `datasheets/` — the part-fact cache, kicad-happy's layout:
  - `<MPN>.pdf` and `manifest.json` — PDFs, put there by `boardsmith datasheets sync|add`
    (never by hand: they check the PDF names the part)
  - `extracted/<MPN>.json` — structured extraction (pinout, ratings, package, category
    facts). The review gates read it. Validated by `boardsmith datasheets check`.
  - `<MPN>_SUMMARY.md` — the coders' view: generated tables plus hand-written facts
- `circuits/` — SKiDL circuit files. Two modes:
  - **Monolithic** (≤3 blocks): `circuits/<name>.py`
  - **Modular** (>3 blocks): `circuits/<name>/` — a Python package:
    - `__init__.py` — empty package marker
    - `__main__.py` — assembly: imports blocks, creates nets, calls blocks
    - `<block_id>.py` — one `@SubCircuit` function per functional block
- `symbols/` — symbols generated for parts the KiCad libraries lack
  (`symbols/<circuit_name>.kicad_sym`); boardsmith puts this directory on SKiDL's library
  path.
- `footprints/<Lib>.pretty/<Name>.kicad_mod` — generated footprints; each library is listed
  in `boardsmith.toml` `[libraries.footprints]` as `<Lib> = "footprints/<Lib>.pretty"`.
- `handoffs/` — one file per pipeline phase, the **only** channel between phases:
  - `01_requirements.md`, `02_architecture.md`, `03_sourcing.md`, `04_datasheets.md`,
    `05_coding.md`, `06_review.md`
  - `05_blocks/<block_id>.md` — one per block in modular mode
  - Format and contract: `rules/phase-handoff.md`. Commit these — they are the design's
    reasoning record, and they make `/clear` safe at any approval gate.
- `design_state.json` — the driver's pipeline state (schema in
  `skills/new-circuit/design_state_schema.json`). Not `build/pipeline_state.json`, which is
  boardsmith's record of its own C1–C6 stages.
- `build/` — everything boardsmith generates: `netlist.json`, `kicad/` (schematic, board),
  `reports/` (ERC, parity, `review.json`, DRC, analysis), fab outputs. Regenerated freely;
  don't edit it.
