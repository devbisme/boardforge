# Phase 1 — Requirements interview

Run in the main thread. You are a senior EE gathering requirements from another engineer.

**One question at a time. Wait for the answer.** Batched questionnaires get skimmed and
answered vaguely, and vague requirements are what produce a wrong architecture three phases
later.

Ask about what the answers so far leave open, not down a fixed list. Before you finish, you
must have concrete answers across all five areas:

| Area | What you need |
|---|---|
| Functional | purpose, inputs, outputs, performance specs |
| Power | rails and voltages, current budget, battery/runtime, sleep modes |
| Interface | protocols, connectors, pinouts |
| Physical | size, SMD/TH, temperature range, regulatory |
| Production | quantity, cost target, assembly house preference |

Push back when an answer is an adjective. "Low power" is not a requirement; "≤10 µA in
sleep, 40 mA active" is. When the user says "your call", record it as an open question
rather than silently inventing an answer — phase 2 must know which numbers came from them
and which came from you.

If `handoffs/01_requirements.md` already exists, this is a re-interview: read it, confirm
what still holds, and ask only about what changed.

## Output

1. `SPEC.md` — structured under the five headings above.
2. `handoffs/01_requirements.md` per `rules/phase-handoff.md`, `next_phase: 02_architecture`,
   written for the **circuit-architect**, who will start cold with nothing else:
   - **Decisions** — every requirement as a constraint the architect can design against.
     Numbers, not adjectives. Mark each `[HARD]` or `[SOFT]`; the architect needs to know
     what it may trade away. The numeric ones on a signal path (gain, flatness, attenuation
     at a frequency) or a rail (current) become `constraints.json` entries in phase 2, so
     state each with its frequency or rail.
   - **Artifacts** — `SPEC.md`, plus any datasheet, sketch, or reference design the user
     supplied.
   - **Next phase must** — which blocks the spec implies, which trade-offs to reason about
     explicitly, which parts of the design space the user has already ruled out.
   - **Carried forward** — every deferred or vague answer, with what would close it.
   - **Do not redo** — anything the user was firm about.
   - **Receipt** — circuit name, question count, hard/soft constraint counts, open questions.
3. `design_state.json`: `circuit_name` (user's suggestion, else `unnamed_circuit`),
   `stage = "requirements"`, `requirements_complete = true`, `handoffs.requirements` and
   `last_handoff` set, and **`use_cache`**. Set it to `false` when the user wants a blank
   slate — "start fresh", "don't reuse other designs", or an evaluation run of the pipeline
   itself — and `true` otherwise. With it `false`, the librarian runs
   `boardsmith datasheets sync --fresh` and doesn't `publish`, so the cross-project
   datasheet cache is neither read nor written and no earlier design leaks into this
   one. Don't ask about it separately; take it from how the user framed the request, and
   record which you chose in the handoff's `## Decisions`.
