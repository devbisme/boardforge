---
description: Environment — boardsmith, KiCad 10, the commands that run the design
alwaysApply: true
---

# Environment

- **boardsmith** on `PATH` (`uv tool install boardsmith` or `pip install boardsmith`). It
  brings SKiDL and schematizer, and finds KiCad 10, Freerouting and ngspice itself.
  `boardsmith doctor` checks all of it; run it once at the start, not per phase.
- KiCad library paths are boardsmith's business (`[kicad]` in `boardsmith.toml` or the user
  config). Don't set `KICAD*_SYMBOL_DIR` by hand.
- Plugin scripts run with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/<script>`.

## Commands

All from the project root.

| Command | Does | Output |
|---|---|---|
| `boardsmith sch` | **Runs the design** (C1): SKiDL + SKiDL ERC → netlist → KiCad schematic → KiCad ERC + netlist parity | `build/netlist.json`; ERC detail in `build/reports/skidl.log` and `erc.json` |
| `boardsmith review` | B4 gate: `constraints.json` (SPICE paths, rail budgets), datasheet checks, package checks, BOM vs design | `build/reports/review.json`; `-v` prints every finding |
| `boardsmith datasheets status` | Per MPN: PDF, extraction, quality, staleness | — |
| `boardsmith datasheets sync [--fresh] [--offline]` | Fill `datasheets/` from the shared cache, local PDFs, symbol URLs, LCSC | — |
| `boardsmith datasheets add <MPN> <file-or-URL> [--force]` | Add one PDF after checking its text names the part; a scanned PDF needs `--force` after you've checked it by eye | — |
| `boardsmith datasheets check <MPN>...` | Validate extractions, fill derived fields, score | exit 1 if invalid |
| `boardsmith datasheets show <MPN>...` | Markdown summary of extractions | — |
| `boardsmith datasheets paths` | Where kicad-happy's extraction prompts and schemas are | — |
| `boardsmith datasheets publish` | Copy finished extractions to the shared cache | — |
| `boardsmith run --from c2 --to c3` | Board setup and placement only (seconds) | C3 warnings; `rules/physical.md` |
| `boardsmith run --from c4` | Routing, checks, fab outputs (10–45 min) | `build/`; `boardsmith status` |

`boardsmith sch` takes 10–20 s. Its one-line summary gives the SKiDL and KiCad ERC counts and
whether the netlist round trip matched; read the log only when a count is non-zero.

A design file is plain SKiDL. boardsmith imports it (a package runs as a module), so the
circuit must be built at import time or by the function named in `[design] top`, and an
`if __name__ == "__main__":` block is ignored.
