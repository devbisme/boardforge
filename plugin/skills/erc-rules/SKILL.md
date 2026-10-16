---
name: erc-rules
description: >-
  Interprets SKiDL and KiCad ERC output from `boardforge sch`, classifies errors vs
  warnings, and provides concrete fix patterns (pin conflicts, floating pins, undriven
  power pins, decoupling, NC usage). Use when running `boardforge sch`, reviewing ERC
  results, fixing ERC errors, or performing any design review that includes electrical
  rule checking.
---

# SKiDL ERC Rules

## Where the messages are

`boardforge sch` runs two ERCs and prints both counts:
- **SKiDL ERC** on the circuit: messages in `build/reports/skidl.log`.
- **KiCad ERC** on the schematic boardforge generates: `build/reports/erc.json`
  (`by_type`, `error_items`). `lib_symbol_mismatch` warnings are expected (boardforge's
  project-local library tables) — ignore them. `power_pin_not_driven` errors mean a supply
  reaches a power input only through a connector or passive: add
  `Part('power', 'PWR_FLAG')[1] += net`.

Both must be 0 errors; the netlist parity line must say `ok`.

## When to use
- Before the review gate and the physical pipeline
- After any circuit code change
- During design review (errors must be 0 before proceeding)

## Procedure

**Drive strength order** (PWR > OUT > BIDIR > TRI > OC > IN > NC)

**Must-fix errors — treat as blockers:**
- Output-to-output conflict (two OUT pins on same net)
- Floating input on active IC
- Power pin conflict
- Unconnected pin on IC (unless `NC`)

**Warnings — investigate, suppress only with justification:**
- Open-drain needs external pull-up → add resistor or `erc_assert()`
- Multi-driver bus (I2C, SPI) → expected; suppress with `erc_assert()`

**Mandatory decoupling:**
Every IC VCC pin → 100 nF minimum, named `C_DECOUP_*`

**Intentional NC:**
```python
U1['NC1', 'NC2'] += NC
```

**Warning suppression:**
```python
erc_assert("expected open-drain", net)
```

**ERC run pattern:** `boardforge sch` from the project root — the design file needs no
`ERC()` call of its own.

## Output

Produce this summary line after every ERC run:
```
"X errors, Y warnings, Z notes. Circuit is [PASS/FAIL]."
```

## Validation

- Never declare a circuit ready until errors == 0
- Suppressed warnings must have a comment explaining why
