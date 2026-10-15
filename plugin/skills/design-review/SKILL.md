---
name: design-review
description: >-
  Three-angle review of a SKiDL circuit — the boardsmith gates (ERC, SPICE paths, datasheet,
  package, BOM, budgets), architecture against requirements, and code quality — aggregated
  into design_review.md with severity-sorted findings and
  fixes. Use before exporting a netlist, after the coder finishes, or when the user asks
  to review, check, or validate a circuit.
---

# Design Review

Runs in the main thread. Unlike a pipeline phase, a review deliberately reads across all
phases — but read the handoffs before the artifacts.

## Read the handoff chain first

Each `## Decisions` section tells you what was chosen *and why*, which is the difference
between a finding and a false positive:

| Handoff | What it saves you from flagging |
|---|---|
| `01_requirements.md` | Judging the design against requirements it never had — constraints are marked `[HARD]`/`[SOFT]` |
| `02_architecture.md` | Re-litigating IC and topology choices made for a stated reason |
| `03_sourcing.md` | Flagging a deliberate substitution, or missing a `⚠️ CUSTOM FP NEEDED` |
| `05_coding.md` | Re-reporting intentional NC pins, open-drain nets, known false positives |
| `06_review.md` | Repeating a gate run whose dispositions are already recorded |

A finding that contradicts a `## Decisions` entry is still valid — but say which decision it
contradicts and why the reasoning does not hold. That is worth far more than a bare flag.

## 1. Electrical rules

If `handoffs/06_review.md` exists and is newer than every file under `circuits/`,
`sourcing/sourced_bom.csv`, `constraints.json` and `datasheets/extracted/`, the verdict is
current: read it and `review_report.md`, and do not re-run anything. Only when it is missing
or stale do you spawn the `erc-reviewer` agent. Re-running a clean gate to confirm it
is clean is the most common waste in this pipeline.

## 2. Architecture sanity

Re-read `SPEC.md`, `architecture/`, and the `## Carried forward` section of every handoff —
deferred questions and known gaps concentrate there. Check:

- Every functional block present in the code?
- Power budget realistic against the `[HARD]` current constraints?
- Net plan complete and consistent with what the code actually connects?
- Known risks from `design_risks.md` (decoupling, protection, EMI, thermal) addressed?

## 3. Code quality

Read `circuits/*.py` and check against `rules/skidl-syntax.md`: `ref`/`value`/`footprint` on
every part, `@SubCircuit` per functional block with a docstring, `C_DECOUP_*` naming, `NC` on
intentionally unused pins, UPPERCASE power / camelCase signal nets, `.drive = POWER` on
supplies, `fields["MPN"]` on every part, no `generate_*` calls (boardsmith writes the outputs).

## 4. Footprints and packages

Read `PKG:pins`, `PKG:<MPN>` and `BOM` in `build/reports/review.json` (current per § 1). A
symbol pin with no pad, a pad count or pitch that disagrees with the datasheet, or a body
variant that disagrees with the BOM is a **HIGH** finding. A part on `DS:coverage`'s list has
no extraction, so its package was never checked against a datasheet: name it, and say
whether it matters (an IC or connector does; a 0603 resistor doesn't). For a missing
footprint, give `validate-footprints.py`'s close match, or generate one with
`scripts/generate-footprint.py` from the datasheet mechanical drawing (`rules/kicad-footprints.md`).

## Output

Write `design_review.md`:

```
# Design Review — <circuit_name>

## Summary
Gates: PASS/WARN/FAIL | Architecture: OK/Issues | Code: OK/Issues
<n> HIGH, <n> MEDIUM, <n> LOW

## Findings
| # | Severity | Area | Issue | Suggested fix |
|---|----------|------|-------|---------------|
```

Every HIGH finding carries a specific SKiDL fix snippet. Every open item in any handoff's
`## Carried forward` is either resolved by the code or appears here as a finding — silently
dropping one is how known gaps reach fabrication. Record `review_status` in
`design_state.json`, then offer to spawn `skidl-coder` with the report if there are HIGH
findings, or declare the circuit ready for the physical pipeline (phase 7) if there are none.
