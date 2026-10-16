---
description: Environment — boardforge, KiCad 10, the commands that run the design
alwaysApply: true
---

# Environment

- **boardforge** on `PATH` (`uv tool install boardforge` or `pip install boardforge`). It
  brings SKiDL and schematizer, and finds KiCad 10, Freerouting and ngspice itself.
  `boardforge doctor` checks all of it; run it once at the start, not per phase.
- KiCad library paths are boardforge's business (`[kicad]` in `boardforge.toml` or the user
  config). Don't set `KICAD*_SYMBOL_DIR` by hand.
- Plugin scripts run with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/<script>`.

## Commands

All from the project root.

| Command | Does | Output |
|---|---|---|
| `boardforge sch` | **Runs the design** (C1): SKiDL + SKiDL ERC → netlist → KiCad schematic → KiCad ERC + netlist parity | `build/netlist.json`; ERC detail in `build/reports/skidl.log` and `erc.json` |
| `boardforge review` | B4 gate: `constraints.json` (SPICE paths, rail budgets), datasheet checks, package checks, BOM vs design | `build/reports/review.json`; `-v` prints every finding |
| `boardforge datasheets status` | Per MPN: PDF, extraction, quality, staleness | — |
| `boardforge datasheets sync [--fresh] [--offline]` | Fill `datasheets/` from the shared cache, local PDFs, symbol URLs, LCSC | — |
| `boardforge datasheets add <MPN> <file-or-URL> [--force]` | Add one PDF after checking its text names the part; a scanned PDF needs `--force` after you've checked it by eye | — |
| `boardforge datasheets check <MPN>...` | Validate extractions, fill derived fields, score | exit 1 if invalid |
| `boardforge datasheets show <MPN>...` | Markdown summary of extractions | — |
| `boardforge datasheets paths` | Where kicad-happy's extraction prompts and schemas are | — |
| `boardforge datasheets publish` | Copy finished extractions to the shared cache | — |
| `boardforge run --from c2 --to c3` | Board setup and placement only (seconds) | C3 warnings; `rules/physical.md` |
| `boardforge run --from c4` | Routing, checks, fab outputs (10–45 min) | `build/`; `boardforge status` |
| `boardforge render [--side both]` | Picture of the current board, to check a placement by eye | `build/renders/*.png` |

`boardforge sch` takes 10–20 s. Its one-line summary gives the SKiDL and KiCad ERC counts and
whether the netlist round trip matched; read the log only when a count is non-zero.

A design file is plain SKiDL. boardforge imports it (a package runs as a module), so the
circuit must be built at import time or by the function named in `[design] top`, and an
`if __name__ == "__main__":` block is ignored.
