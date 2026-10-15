# boardsmith plugin for Claude Code

Agents and skills that take a circuit from a conversation to a routed KiCad board:
requirements → architecture → sourcing → datasheets → SKiDL code → review gate → board.
Every deterministic step is a `boardsmith` command, so the agents decide and the scripts
check. Adapted from Nick Kraakman's skidl-skills (MIT); see `ATTRIBUTION.md`.

## Install

```bash
uv tool install boardsmith          # or: pip install boardsmith
boardsmith doctor                   # KiCad 10, Freerouting, ngspice
claude plugin marketplace add devbisme/boardsmith
claude plugin install boardsmith@boardsmith
claude mcp add --transport http pcbparts https://pcbparts.dev/mcp   # recommended: live part data
```

## Use

In an empty project directory: `claude`, then `/new-circuit`. It interviews you, then runs
each phase as a sub-agent and asks for approval between phases. All state is on disk
(`handoffs/`, `design_state.json`), so `/clear` is safe at any approval gate; `/new-circuit`
resumes.

| Phase | Who | Checked by |
|---|---|---|
| 1 requirements | you + the driver | — |
| 2 architecture, `constraints.json` | `circuit-architect` | the review gate, later |
| 3 sourcing | `part-sourcer` | `BOM` gate |
| 4 datasheets → extractions | `datasheet-librarian` | `boardsmith datasheets check` |
| 5 SKiDL code | `skidl-coder` / block coders + assembler | `boardsmith sch` (SKiDL + KiCad ERC, parity) |
| 6 review | `erc-reviewer` | `boardsmith review` (SPICE paths, datasheet, package, BOM, budgets) |
| 7 board | the driver: floorplan, then place and route (`rules/physical.md`) | C3's placement warnings, DRC, release gate |

Other skills: `/design-review`, `/find-part`. The rules every agent follows are in `rules/`.
