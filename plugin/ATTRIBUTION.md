# Attribution

Most of this plugin is adapted from **skidl-skills** by Nick Kraakman
(<https://github.com/nickkraakman/skidl-skills>), MIT License, Copyright 2026 Nick Kraakman.
The license is in `LICENSE-skidl-skills`. Source commit: `82cf010f8a7d42ed2afee69805a7300764eb4d69`
(2026-09-25, "Tune pipeline for Claude Opus 5.5").

## What came from skidl-skills, and how it changed

| Here | From | Changes |
|---|---|---|
| `agents/*.md` (7 agents) | `agents/` | Run the design, ERC, review gates and datasheet cache through `boardforge` commands; the architect writes `constraints.json`; the librarian writes kicad-happy extractions plus summaries; the reviewer runs `boardforge review` and routes each gate result |
| `skills/new-circuit/` | same | Writes `boardforge.toml` after phase 2; phase 6 is the review gate; new phase 7 runs the physical pipeline; state file renamed `design_state.json` (`build/pipeline_state.json` is boardforge's) |
| `skills/design-review/`, `skills/erc-rules/`, `skills/find-part/` | same | boardforge commands, KiCad's ERC alongside SKiDL's |
| `rules/phase-handoff.md` | same | Gate item 5 is `boardforge review`'s `BOM` result |
| `rules/design-workflow.md`, `project-layout.md`, `environment.md` | same | Rewritten for boardforge's project layout and commands |
| `rules/skidl-syntax.md`, `kicad-footprints.md` | same | `fields["MPN"]` on every part; no `generate_*`/`ERC()` in the design; PWR_FLAG for KiCad's ERC; project footprint libraries via `boardforge.toml` |
| `scripts/stock-check.py`, `pdf-table.py`, `generate-footprint.py` | `scripts/` | Unchanged |
| `scripts/find-symbol.py`, `validate-footprints.py` | `scripts/` | KiCad 10 library locations from boardforge (`kicadlibs.py`); `.kicad_symdir` libraries; footprint strings on the command line |
| `scripts/guard-bash.py`, `post-write.sh`, `run-hook.sh` | `scripts/` | Protected directories; state directory name |
| `scripts/session-start.sh`, `stop-check.py` | `scripts/` | Rewritten for boardforge (`boardforge status`, C1's ERC result) |
| `hooks/hooks.json` | same | Unchanged |

## New here (not from skidl-skills)

`rules/physical.md` (phase 7: floorplan, placement checks, reading the physical pipeline's
results) and `scripts/kicadlibs.py`.

## Moved into boardforge itself

- `validate-bom.py` → the `BOM` result of `boardforge review`
  (`src/boardforge/review/bom_check.py`; its value, refdes, symbol and package helpers are
  copied unchanged).
- `fetch-datasheet.py`, `part-cache.py`, `partdoc.py` → `boardforge datasheets sync|add|publish`
  (identity-checked PDFs, a shared cache, kicad-happy's layout).
- `generate-design-rules.py` → boardforge's fab profiles and board setup (C2).

## Not carried over

`settings.json` (permission allow-list), `.claude/`, example images.
