---
description: Design pipeline stages, agents, coding modes, recovery loops, hooks
alwaysApply: true
---

# Design Workflow

`/new-circuit` drives the pipeline **from the main thread**. Phases 2–6 run as sub-agents.
Phase 1, phase 7 and every approval gate stay in the main thread, because they need user
turns — a sub-agent cannot hold a conversation with the user.

**Phases are context-isolated.** The only thing that crosses a phase boundary is a handoff
file on disk. The contract is `rules/phase-handoff.md`.

**Deterministic work is a `boardsmith` command.** Running the design, ERC, the review gates,
the datasheet cache, placement, routing and fab outputs are all CLI commands
(`rules/environment.md`). Agents call them; they never re-implement a check in prose.

## Pipeline

Single source of truth for stage order, handoff files, and artifacts. Do not restate it
elsewhere — link here.

| # | Phase | Runs as | Reads | Writes handoff | Artifacts |
|---|-------|---------|-------|----------------|-----------|
| 1 | requirements | main thread (`skills/new-circuit/interview.md`) | the user | `handoffs/01_requirements.md` | `SPEC.md` |
| 2 | architecture | `circuit-architect` | `01_requirements.md` | `handoffs/02_architecture.md` | `architecture/*`, `constraints.json` |
| 3 | sourcing | `part-sourcer` | `02_architecture.md` | `handoffs/03_sourcing.md` | `sourcing/sourced_bom.md` **+ `sourced_bom.csv`** |
| 4 | datasheets | `datasheet-librarian` | `03_sourcing.md` | `handoffs/04_datasheets.md` | `datasheets/*` (PDFs, `extracted/<MPN>.json`, `<MPN>_SUMMARY.md`) |
| 5 | coding | `skidl-coder` **or** `skidl-block-coder`×N + `skidl-assembler` | `04_datasheets.md` | `handoffs/05_coding.md` (+ `05_blocks/<id>.md`) | `circuits/*` |
| 6 | review | `erc-reviewer` | `05_coding.md` | `handoffs/06_review.md` | `review_report.md`, `build/reports/review.json` — **gate below** |
| 7 | physical | main thread (`rules/physical.md`: floorplan, `boardsmith run`) | `06_review.md` | — | `boardsmith.toml` floorplan, `build/` (board, DRC, fab zip) |
| — | design review (optional) | main thread (`/design-review`) | `06_review.md` | — | `design_review.md` |

**Phase 6 gate:** `boardsmith sch` reports 0 SKiDL ERC errors, 0 KiCad ERC errors and exact
netlist parity, **and** `boardsmith review` has no HARD failure (status not `FAIL`), with the
`BOM` result PASS.

Phase 4 is skippable: if the sourcing handoff's `## Next phase must` names no part needing
a datasheet (all-passive designs), the driver records `datasheets: "skipped"` in
`design_state.json` and phase 5 reads `handoffs/03_sourcing.md` as its entry point.

## Stage 5 mode

Decided from the `## Block manifest` row count in `handoffs/02_architecture.md`:

- **≤3 blocks → monolithic**: one `skidl-coder` → `circuits/<name>.py`
- **>3 blocks → modular**: `skidl-block-coder` work orders — complex blocks alone,
  simple blocks 2–3 per order — dispatched **one at a time**, then `skidl-assembler` →
  `circuits/<name>/__main__.py`. Grouping and the dispatch rule are in
  `skills/new-circuit/SKILL.md` § Stage 5.

## Agents

| Agent | Model | Effort | Role |
|---|---|---|---|
| `circuit-architect` | opus | high | Topology and IC selection → `architecture/*`, numeric `constraints.json`; re-invoked on escalation |
| `part-sourcer` | sonnet | medium | Real in-stock MPNs + footprints → `sourcing/sourced_bom.*` |
| `datasheet-librarian` | opus | medium | Datasheet PDFs, extraction JSON + `<MPN>_SUMMARY.md` per significant part |
| `skidl-coder` | opus | medium | Monolithic circuit code; runs `boardsmith sch` and fixes ERC (≤3 attempts) |
| `skidl-block-coder` | opus | medium | One `@SubCircuit` block file; syntax-check only |
| `skidl-assembler` | opus | medium | Wires blocks into `__main__.py`; runs `boardsmith sch` |
| `erc-reviewer` | opus | high | Independent gate: `boardsmith sch` + `boardsmith review` + checklist → `review_report.md` |

Each agent's frontmatter is authoritative for model and effort. Never pass a `model`
override when spawning, or the two drift apart. `opus` resolves to Claude Opus 5.5.

- **Model.** Use opus wherever a wrong fact turns into rework. Use sonnet only for catalogue
  lookup whose output a later gate checks mechanically (the sourcer's BOM is diffed against
  the design by the `BOM` gate). The librarian is on opus: in skidl-skills runs 5–7, the
  keystone facts it missed caused the most expensive reworks, and its extractions now feed
  the datasheet, package and budget gates directly.
- **Effort.** `high` is kept for the two agents whose misses cost a full rework cycle: the
  architect, which does arithmetic nothing downstream re-derives, and the independent
  reviewer, which is the gate that catches what the scripted checks can't. The others run
  at `medium`. Raise one agent at a time, and only on evidence from a run.
- **CLAUDE.md.** Every agent sets `omitClaudeMd: true`. Each one gets everything it needs from
  its work order and the handoffs.

## Skills

| Command | Purpose |
|---|---|
| `/new-circuit` | Start or resume the pipeline (this is the driver) |
| `/design-review` | Three-angle review: gates + architecture + code quality |
| `/find-part` | Part search and sourcing rules — owns the pcbparts MCP tool list |
| `erc-rules` | ERC message classification and fix patterns (auto-loaded when reading ERC output) |

## Recovery loops

Automatic, driven from the main thread:

- **Sourcing failure** (`status: partial`/`failed` on 03) → re-invoke `circuit-architect`,
  then re-run sourcing. Cap: 2 rounds → ask the user.
- **Review failure** (phase 6) → route each finding to its owner (table in
  `agents/erc-reviewer.md` § Routing). Cap: 3 attempts per block, 9 total → ask the user.
- **Coding escalation** (`pin_count_mismatch`, `unsupported_mode`, `fundamental_conflict`) →
  straight to `circuit-architect`, never a coder retry. Cap: 2 reworks → ask the user.
- **Physical failure** (phase 7: unrouted nets after boardsmith's own retries, DRC errors,
  release gate) → report to the user with `boardsmith status` and the named report file;
  a placement or routing problem is boardsmith's to fix, not an agent's.
- **A sub-agent killed mid-run** (rate/spend limit, or context exhaustion) is not a phase
  failure and doesn't count against these caps. Check its on-disk handoff, then resume or
  re-spawn narrowed, per `skills/new-circuit/SKILL.md` § Recovery.

A re-invoked phase overwrites its handoff and bumps `revision:`, so no phase ever reads a
stale one.

## Hooks (automatic)

All four are plain scripts — no model calls. Python hooks go through `scripts/run-hook.sh`,
which execs the interpreter that `session-start.sh` resolved once.

- **PreToolUse (Bash)** — `guard-bash.py` blocks commands that would delete or overwrite an
  **existing** file under `datasheets/`, `circuits/`, `sourcing/`, `symbols/` or
  `footprints/`. Creating new files there is allowed, and `build/` is not protected
  (boardsmith regenerates it). It parses the command text, so a write done inside a script
  is out of its sight; edit circuit files with Write/Edit.
- **PostToolUse (Write|Edit)** — syntax-checks every `circuits/**.py` and checks that its
  footprint strings resolve. Package variants and pad counts are the `BOM` and `PKG` gates'
  job (`boardsmith review`), not this hook's.
- **SessionStart** — caches the interpreter path, writes the session-start marker, then
  checks that `boardsmith` is installed and prints the pipeline status.
- **Stop** — `stop-check.py` warns (non-blocking `systemMessage`) when circuit code changed
  this session and `boardsmith sch` hasn't run since. It warns rather than blocks: a blocking
  Stop hook with a persistent condition forces Claude to continue indefinitely.
