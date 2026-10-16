---
description: Phase handoff contract — every pipeline phase ends by writing a handoff file and clearing context
alwaysApply: true
---

# Phase Handoff Contract

The pipeline is **context-isolated**. No phase inherits the conversation of the phase
before it. The *only* thing that crosses a phase boundary is a file on disk.

```
Phase N ──writes──> handoffs/NN_<phase>.md ──read by──> Phase N+1
   │                                                        │
   └── context discarded ────────────────────────────────── ┘
```

Stage order, filenames, and which agent owns which phase: `rules/design-workflow.md`.

## Standard phase protocol

Every sub-agent phase follows this. Its own prompt states only what is specific to it.

1. **Read in.** Your entry point is the preceding handoff file, named in your work order.
   Read it, then the artifacts its `## Artifacts` table points at — those, and nothing
   else. Walk further back up the chain only when the entry point is silent on something
   you need.
2. **Write your handoff skeleton first** — front matter with `status: partial`, every
   mandatory section present (`_None yet._` is fine). A run can be killed at any point by
   an API rate or spend limit, or by running out of context; a skeleton on disk is what
   makes the work so far recoverable instead of lost.
3. **Do the work**, honouring `## Decisions` and `## Do not redo` from upstream. If you
   disagree with an upstream decision, escalate it; do not quietly re-decide it. **Save
   each artifact the moment it is finished** — one datasheet summary, one block file — and
   update the handoff to list it. Never hold finished work in context to write at the end.
4. **Finish the handoff.** Everything the next phase needs must be in it. Assume nobody
   remembers what you were told. Flip `status:` to `complete` only now.
5. **Return a receipt** of ≤10 lines: counts and status. No artifacts, code, tables, or BOM
   rows in the return — the driver reads your handoff for detail.

   **This limit is the single cheapest cost control in the pipeline, and it is the one most
   often ignored.** Your return lands in the driver's context and is then re-read on every
   driver turn for the rest of the run — it is the most expensive text you write. In the
   measured run receipts averaged 40–80 lines against this 10-line limit. The driver spent
   **30 % of the whole run re-reading up to 174 k tokens per turn**, and most of that context
   repeated what was already on disk.

   Concretely: findings, tables, per-item reasoning, file-by-file change lists and
   "two things for the next phase" belong in the handoff *only*. The receipt says what
   happened, in numbers, plus anything that changes what the driver does next — a failure,
   a blocked gate, a decision it must make. If you catch yourself explaining, stop and put
   it in the handoff; the next agent will read it there and the driver will not pay for it
   on every turn.

## Working efficiently

Every turn re-reads your whole context, so cost is roughly *turns × context size*. In
skidl-skills test runs 4–7, over 95 % of tokens were those re-reads, and output was under
1 %.

- **Batch independent reads and checks into one tool call** — one Bash call that runs five
  `grep`s costs one turn; five calls cost five turns, each re-reading everything.
- **Prefer commands and scripts to doing lookups by hand** — `boardforge datasheets
  status|sync|add|check|show`, and `stock-check.py`, `find-symbol.py`, `pdf-table.py` in
  `${CLAUDE_PLUGIN_ROOT}/scripts/`. Each replaces dozens of turns with one.
- **Read PDFs as text first**: `pdftotext -layout -f N -l N`, then `grep` the result. For
  large pin tables, use `pdf-table.py`. Read pages as images (the Read tool's `pages`
  parameter) only for figures, mechanical drawings and pinout diagrams, and only the
  **few pages** that hold them. Every page image stays in your context and is re-read on
  every later turn. In run 6, the sourcer read a 23-page regulator datasheet as images to
  find one number, and two block coders read whole-page screenshots.
- **Keep handoffs under ~150 lines** — the next agent pays to read every one of them on
  every turn.

**Stop instead of guessing** when your entry point is missing, malformed, or contradicts
your work order. A phase built on a guess costs a full rework cycle downstream.

## Handoff file format

Front matter plus six sections. Sections are mandatory; write `_None._` rather than
deleting one.

```markdown
---
phase: 02_architecture
agent: circuit-architect
circuit: <circuit_name>
written: <YYYY-MM-DDTHH:MM:SSZ>
status: complete | partial | failed
revision: 1
next_phase: 03_sourcing
---

# Phase 2 handoff — Architecture

## Decisions
What the next phase must honour and cannot re-derive from the artifacts. One line each,
imperative and specific. Include the *why* whenever a reader could reasonably choose
otherwise.

## Artifacts
| File | Contains | Read it when |
|------|----------|--------------|
| `architecture/net_plan.md` | Every net, type, connected refs | Always — before writing any code |

## Next phase must
Numbered, executable instructions addressed to the next agent by name.

## Carried forward
Unresolved constraints, deferred choices, and user preferences that still bind — each with
the reason it is still open.

## Do not redo
Work already settled. The next phase must not re-litigate it.

## Receipt
3–6 lines, counts and status only. This is what the driver sees.
```

Some phases add one or two extra tables that the driver needs (block manifest, parts by
block, summaries by block); those are specified in the agent that owns them.

Re-invocations **overwrite** their phase's handoff file and bump `revision:`. History lives
in git, not in accumulated files.

## Writing a good handoff

- **Self-contained.** The next agent starts cold. A fact that exists only in your context
  is lost unless you write it down.
- **Point, don't paste.** Reference `sourcing/sourced_bom.md`; never inline it. Handoffs
  stay under ~150 lines.
- **Carry the why.** "TPS62840 over MP2315 — 60nA Iq, spec requires 10µA sleep budget"
  survives contact with the next phase; "chose TPS62840" invites second-guessing.
- **Name names.** Exact ref designators, net names, file paths, MPNs.
- **State what is broken.** A `status: partial` handoff that lists two unsourced parts is
  worth more than a `complete` one that hides them.

## Why this survives a context clear

A sub-agent's context dies when it returns; writing the handoff first is what makes that
safe. The driver stays thin by reading handoffs rather than artifacts. Because all state
lives in `handoffs/` + `design_state.json`, the user can `/clear` at any approval gate
and resume with `/new-circuit`.

## Gate — checked by the driver after every phase

1. `handoffs/<NN>_<phase>.md` exists, with front matter carrying `status:` and `next_phase:`.
2. Every path in its `## Artifacts` table exists on disk.
3. `status:` is `complete`, or `partial` with the gaps named under `## Carried forward`.
   A `partial` whose `## Receipt` is still a placeholder is a run that was **killed**, not
   one that finished with gaps — route it through recovery (`skills/new-circuit/SKILL.md`
   § Recovery), don't advance on it.
4. `design_state.json` records the path under `handoffs.<phase>` and `last_handoff`.
5. **From the coding phase onward, the BOM must agree with the code.** Run it yourself —
   it is a command, it costs no tokens, and it is not the sub-agent's word for anything:

   ```
   boardforge sch && boardforge review
   ```

   A `BOM` result other than PASS is a gate failure. Route it to whoever owns the disagreeing side — a
   wrong BOM cell to `part-sourcer`, a wrong part in the code to the block's coder — and
   never "fix" it by editing the other side to match without deciding which one is right.
   Nine BOM-vs-code drifts reached a gate in the measured run because nothing checked this;
   each cost a narrow re-spawn worth $1–4.

A `status: failed` handoff never advances the pipeline — it routes to recovery.
