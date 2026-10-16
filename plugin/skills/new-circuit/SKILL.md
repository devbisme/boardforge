---
name: new-circuit
description: >-
  Starts or resumes a PCB circuit design and drives the whole pipeline: interview →
  architecture → sourcing → datasheets → SKiDL code → review gate → board, with an approval
  gate at every stage. Use when the user asks to create a new circuit, start a design,
  add a schematic, or resume an in-progress circuit.
---

# New Circuit — pipeline driver

You are the driver. You run in the main thread, you own `design_state.json`, and you are
the only one who advances a stage. Phases 2–6 run as sub-agents; the interview, the
physical pipeline (phase 7) and every approval gate stay with you, because they need the
user.

Stage order, agents, and models: `rules/design-workflow.md`. Handoff contract and gate:
`rules/phase-handoff.md`. Read both before the first spawn.

Plugin files (`rules/*`, `skills/*`, `scripts/*`) resolve under `${CLAUDE_PLUGIN_ROOT}`.
Project files (`handoffs/*`, `circuits/*`, `architecture/*`) are relative to the project cwd.

## Start

1. Read `design_state.json` (schema: `skills/new-circuit/design_state_schema.json`).
   Missing or corrupt → recreate it from the schema.
2. `stage == "initial"` → phase 1. Otherwise ask: *"Resume **\<circuit_name\>** at phase
   \<N\>, or start fresh?"*
3. On resume, read the one file named by `last_handoff` — that file, not this conversation,
   is the resume point. Report: *"Resuming \<circuit_name\>. Last completed: \<phase\>
   (`<path>`)."*
4. `last_handoff` is null but `stage` is not `initial` → the last phase never passed its
   gate. Do not go forward: name the incomplete phase and offer to re-run it.

## Run a phase

**Phase 1** — conduct the interview yourself, following `skills/new-circuit/interview.md`.

**Phases 2–6** — spawn the agent for the stage with a pointer prompt, nothing more:

```
Read `handoffs/<NN>_<prev_phase>.md` first — it is your complete entry point.
Follow the phase protocol in `rules/phase-handoff.md`.
Circuit: <circuit_name>.  Your handoff output: `handoffs/<NN>_<your_phase>.md`.
<one or two lines of task-specific override, e.g. an escalation reason>
```

Never paste `SPEC.md`, BOM rows, net plans, datasheet summaries, or code into a spawn
prompt, and never pass a `model` override — the agent's frontmatter owns that. If a
sub-agent needs content, its entry-point handoff points at the file and it reads it in its
own context.

**Scope a narrow re-invocation by the change, not by the row count.** "Fix these two BOM
rows" reads as a fence, and a good agent will obey it literally — leaving everything the
change *implies* untouched and correctly flagging it instead. In the measured run a
regulator swap scoped that way left its feedback dividers sized for the retired part's
reference voltage and its inductor at the old value; closing them took a second spawn that
cost as much as the first. Say **"make this change and everything it depends on"**, name the
parts you already know are implicated, and let the agent report anything further it finds.
A re-spawn is far more expensive than an over-wide scope.

**Keep your own context thin.** Read handoffs and `design_state.json`, not artifacts.
Reading one artifact to settle a routing question is allowed and is cheaper than a re-spawn;
carrying that artifact into the next spawn prompt is not. If you find yourself needing an
artifact repeatedly, the previous handoff was inadequate — say so and have it rewritten.

**Your context is the most expensive in the pipeline — and it is you who fills it.** Every
turn re-reads everything the conversation has accumulated, so cost is *turns × context*.
In test runs 4–7 the driver cost **23–40 % of each run** by itself, more than any single
agent type. By the end of a run it was re-reading 250–400 k tokens per turn. Two habits
cause most of it, and both are yours to fix:

- **Do not restate a sub-agent's report back to the user.** It is already in your context;
  paraphrasing it doubles it, permanently. Summarise from the `## Receipt` in a few lines
  and name the file for anything longer.
- **Do not re-derive on-disk facts in prose.** A number you need again is in the handoff.
  Re-stating it in your own message makes you pay for it on every remaining turn.

When a sub-agent returns 60 lines where the contract allows 10, that is a contract breach
worth naming in your next work order to that agent — not something to absorb silently.

## After a phase

1. Run the gate in `rules/phase-handoff.md`.
   - Cosmetic defect (missing front-matter field, missing section header, wrong path
     spelling): fix the handoff file yourself and note it.
   - Substantive defect (missing artifact, empty `## Next phase must`, unexplained
     `partial`): re-invoke the same agent once — *"Your handoff failed the gate: \<specific
     failure\>. Fix only the handoff file — do not redo the phase work."* Fails twice → halt
     and report. Never write another agent's handoff content yourself.
   - **From the coding phase onward, gate item 5 runs `boardforge sch && boardforge review`
     yourself.** It is a command; it costs nothing and it is not the agent's word for
     anything. A `BOM` result other than PASS routes to whichever side is wrong — a bad cell
     to `part-sourcer`, a bad part in the code to that block's coder. Decide which side is
     right before either is edited.
2. Update `design_state.json`: `stage`, the phase's `handoffs.<phase>` path,
   `last_handoff`, and any counter that moved.
3. Summarize to the user from the handoff's `## Receipt`, then ask:
   *"Approve to continue to \<next stage\>? (yes / modify / abort)"* — advance only on an
   explicit yes.
4. Offer the checkpoint, verbatim, when the phase produced large artifacts (architecture,
   sourcing, datasheets, coding) or the conversation has run long:

   > Context checkpoint — phase \<N\> is fully on disk (`handoffs/<NN>_<phase>.md`).
   > You may run `/clear` now and resume with `/new-circuit`; nothing will be lost.

   Never run `/clear` yourself, and never advance a stage in the same message.

## After phase 2 — `boardforge.toml`

Once the architecture is approved, write `boardforge.toml`: the librarian's
`boardforge datasheets` commands and the coders' `boardforge sch` need it. Take the values
from the handoffs, not from artifacts:

```toml
[design]
file = "circuits/<circuit_name>.py"     # modular (>3 blocks): "circuits/<circuit_name>"
title = "<one-line title from SPEC.md>"

[board]
layers = <layer count from 02_architecture.md ## Decisions, else 4>
```

Everything else has a boardforge default; board size, planes and placement anchors are
phase 7's business. The circuit file doesn't exist yet — that's fine. Record `design_file`
in `design_state.json`.

## Stage 5 — coding mode

Count the `## Block manifest` rows in `handoffs/02_architecture.md`. Set `coding_mode`.

**Monolithic (≤3 blocks):** spawn one `skidl-coder`.

**Modular (>3 blocks):**

1. Create `circuits/<circuit_name>/`.
2. Build `block_manifest` in `design_state.json` by joining three handoff tables — no
   artifact reads:

   | Field | Source |
   |---|---|
   | `block_id`, `block_name`, `function_signature`, `interface_nets` | `02_architecture.md` → `## Block manifest` |
   | `parts` (ref designators) | `03_sourcing.md` → `## Parts by block` |
   | `datasheet_summaries` | `04_datasheets.md` → `## Summaries by block` |

   A missing or incomplete table is a gate failure for that phase, not a reason to open the
   artifact.
3. **Group blocks into work orders.** Every coder pays a fixed read-in (rules, three
   handoffs, datasheet summaries) before writing a line, so a 3-part block like `aux_io`
   costs nearly as much as the FPGA block. Put each **complex** block — analog front end,
   ADC/FPGA/MCU, anything with a keystone IC — in its own work order. Pack **simple**
   blocks (connectors, protection, LEDs, straightforward regulators) **2–3 to a work
   order**. Target ≤ 4–5 work orders for a typical design.
4. **Dispatch coders one at a time** (`run_in_background: true`), starting the next when
   the previous reports. **Two** concurrently is the ceiling, and only when the user has
   said throughput matters more than cost; never three or more. Two measured runs make the
   case: 9 parallel coders tripped the account's spend limit together and died together,
   delivering 4 of 9 files for a third of that run's subagent cost; a later run with 3
   parallel opus coders died the same way, and the one that had not yet written a file had
   to restart from zero. Concurrency buys no discount — every coder pays the same read-in
   whenever it runs — it only concentrates the burn, and a limit that fires takes every
   agent in flight with it. Sequential dispatch also lets each finished handoff inform the
   next work order: in that run the `usb_bridge` coder found a ref designator that sourcing
   had filed under `fpga_core`, and the next work order could simply say so. Each work order
   is a pointer — the signatures and ref designators are the only inline data, because they
   are the blocks' contract:

   ```
   Read `handoffs/04_datasheets.md` first, then only the artifacts it points at for your
   blocks. Follow `rules/phase-handoff.md`.
   Circuit: <circuit_name>.
   Block: <block_id> — <block_name>
     Function signature (verbatim): <function_signature>
     Interface nets: <interface_nets>
     Your parts (ref designators only): <parts>
     Unverified keystone facts for this block: <part — fact — assumption to proceed on>
     Output file: circuits/<circuit_name>/<block_id>.py
     Your handoff output: handoffs/05_blocks/<block_id>.md
   [Block: … repeated for each block in this work order]
   ```

   The `Unverified keystone facts` line carries phase 4's open questions (write `none` when
   there are none). A coder that is told a fact is unverified writes the assumption into its
   handoff; one that is handed the same number bare builds on it silently.
5. When all blocks report, spawn `skidl-assembler` pointed at `handoffs/05_blocks/*.md` —
   those handoffs, not the architecture, carry each block's **final** signature.

## Recovery

Caps and routing are in `rules/design-workflow.md` § Recovery loops. Operationally:

- **Sourcing `failed`, or `partial` with entries in `sourcing/sourcing_failures.md`** →
  do not advance to datasheets. Re-invoke `circuit-architect` pointed at
  `handoffs/03_sourcing.md` with the instruction to substitute the failed parts and
  rewrite `handoffs/02_architecture.md` with `revision:` bumped. Then re-run sourcing.
  Count in `sourcing_escalation_count`. A `partial` whose gaps are all **downstream
  work** — footprints to confirm, custom footprints or symbols to build, values to
  compute from a datasheet — is not a sourcing failure: every part is sourced, and the
  architect has nothing to substitute. It passes the gate (gaps named under
  `## Carried forward`) and advances, with those gaps named in the datasheet work order.
- **Datasheets `partial` with rows under `## Unverified keystone facts`** → disposition
  every row *before* you write any block work order, and say which way you went. Either
  (a) re-invoke `datasheet-librarian` for that one fact — *"one part, one fact, do not redo
  the phase"* — or (b) carry it into the block coder's work order as an **open question with
  the assumption named**, never as a settled value. A fact that decides a part's
  compatibility with another part, a rail's voltage, or a pin's polarity is always (a):
  those three are what cost the measured run ≈1 M tokens of rework when they were left to
  phase 5 and 6 to discover.

- **Coding escalation** (`escalation_reason` in the coding handoff) → straight to
  `circuit-architect`; never retry the coder. Count in `architecture_rework_count`.
- **ERC still failing after the coder's 3 attempts** → read `## Carried forward` in
  `handoffs/05_coding.md` (not the raw error log), tell the user the count and error types,
  and offer: retry / escalate to architect / abort. Count in `review_iteration_count`.
- **Review gate FAIL** (phase 6) → `handoffs/06_review.md` → `## Next phase must` names the
  owner of every HARD finding (routing table: `agents/erc-reviewer.md` § Routing). Re-spawn
  each owner with *"make this change and everything it depends on"*, then re-run phase 6.
  Count in `review_iteration_count`.
- **Modular ERC failure** → attribute each error to a block by mapping its ref designators
  through `block_manifest[].parts`: one block's fault → re-spawn that block coder only;
  inter-block wiring → re-spawn the assembler; pin or topology conflict → architect.

**When a sub-agent dies instead of finishing** — the task notification says `failed`, or
its result is a mid-sentence fragment rather than a receipt:

1. **Check the disk before anything else.** Read its handoff (written first, per
   `rules/phase-handoff.md`) and list its artifacts. A kill after real progress is a
   resume, not a restart.
2. **Transient server error** (HTTP 500, 502, 503, 529, "Overloaded", "Internal server
   error"). Not a rate limit and not a phase failure — the agent simply died. Check the
   disk per step 1, then **re-spawn immediately without asking**, narrowing the work order
   to whatever is not already on disk. Do this up to **three times** before treating it as
   a real failure; add a short wait (~60 s) only if two fire back to back. Tell the agent
   in its work order that a previous attempt died this way and whether anything survived,
   so it does not redo finished work. In the measured run the ERC gate was killed twice
   like this before it ever produced a verdict — each death cost a spawn's read-in and
   hours of wall clock for nothing, because the pipeline sat waiting to be told to retry.
3. **API rate or spend limit** (HTTP 429, "spend limit", "session limit resets <time>").
   Re-spawning immediately just dies again — but **parking the pipeline on the user is
   worse**. In the measured run two such kills cost ~17 h of wall clock, and ~10 h of that
   was the limit already reset with the driver idle, waiting for someone to type "keep
   going". Wait it out yourself:

   a. Read the reset time out of the error text and record it in `design_state.json` as
      `blocked_until` (ISO-8601), so a `/clear` or a fresh session still knows why nothing
      is running.
   b. Sleep until that time **plus 5 minutes** without ending your turn. Foreground `sleep`
      is blocked, so use a **background** Bash call — `sleep <seconds>` with
      `run_in_background: true` — whose exit re-invokes you, or the equivalent wait/monitor
      facility your harness offers. Compute the seconds from the clock; do not guess, and do
      not poll in a loop.
   c. On waking, clear `blocked_until` and resume **one agent at a time** per step 1 — disk
      first, because a kill after real progress is a resume, not a restart. Never restart
      with a parallel burst; that is what tripped the limit.
   d. Stop and hand it to the user only when the limit states **no reset time**, names a
      *monthly* ceiling that will not clear inside this session, or fires a second time on
      the same agent after a wait. Then give a diagnostic: what is done, what is pending,
      and when it can restart.
4. **Context exhaustion.** The signature: resuming it returns the *same* result text at
   nearly the same token count (~200 k) with no new files on disk — each resume replays
   the whole transcript and dies at the same spot. **Never resume such an agent again.**
   Spawn a fresh one whose work order lists what is already on disk as *done — do not
   redo*, and narrows the scope to what remains.
5. Do not do the sub-agent's work in the driver thread unless no sub-agent can run at all,
   and then say so to the user. The driver's context is the most expensive in the
   pipeline — every turn re-reads everything the conversation has accumulated.

At any cap, halt and hand the user a consolidated diagnostic. Never start phase 7 until
phase 6 passes its gate (`rules/design-workflow.md` § Pipeline).

## Phase 7 — physical

After the user approves the phase 6 result. Settings, and what each summary line means:
`rules/physical.md` — read it first.

1. **Floorplan.** Write the board size, layers, planes, pours and `[[placement.anchor]]`
   entries into `boardforge.toml` from the architecture's signal flow: every connector on
   its edge with `facing`, the keystone ICs in signal-flow order, and each input's first
   amplifier beside its connector.
2. **Place and look.** Run `boardforge run --from c2 --to c3` (seconds). Fix anything C3
   warns about (unplaced parts, a connector path over 25 mm, congestion), and render the
   placement once to check it by eye. Repeat until C3 is clean.
3. **Route.** Run `boardforge run --from c4` **in the background** (`run_in_background:
   true`); routing takes 10–45 minutes. Don't poll — its exit re-invokes you.
4. Read `boardforge status` and, only for a stage that warned or failed, the report it names
   under `build/reports/`. Record `physical_status` in `design_state.json`.
5. Report: board size and layers, routed or not, DRC errors, release-gate verdict,
   `lines_without_lcsc`, and where the fab outputs are (`build/fab/`). A problem here is a
   `boardforge.toml` setting or a design defect the gates missed: describe it and ask the
   user how to proceed; don't edit circuit code in phase 7.

## design_state.json

You are the only writer — sub-agents return receipts, they do not touch this file. Its full
field list is `skills/new-circuit/design_state_schema.json`; two things the schema cannot
say:

- A phase whose `handoffs.<phase>` entry is `null` has **not** passed its gate, whatever any
  `*_complete` flag says. `last_handoff` is the resume point and must always match the most
  recently gated phase. A skipped datasheet phase records the string `"skipped"`.
- `blocked_until` is set **only** while waiting out an API rate or spend limit (§ Recovery
  step 2) and cleared the moment work resumes. A non-null `blocked_until` on a resume means
  the pipeline is mid-stall, not mid-failure: if the time has passed, resume the named
  agents; if not, wait out the remainder rather than asking the user.
- Each `block_manifest` entry: `block_id`, `block_name`, `filename`, `interface_nets`,
  `parts`, `status` (`pending|in_progress|syntax_ok|erc_fail|done`), `erc_errors_attributed`.
- `review_status` is copied from `build/reports/review.json` and the `boardforge sch`
  summary after each phase 6 run; `boardforge` itself never writes `design_state.json`.
