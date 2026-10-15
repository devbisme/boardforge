---
name: find-part
description: >-
  Searches for electronic parts by MPN, description, or specification using the
  pcbparts MCP (live JLCPCB stock, pricing, KiCad symbols) with a fallback to the
  skidl-part-search CLI. Applies sourcing rules: JLCPCB tier priority, stock thresholds,
  lifecycle checks, and single-source flags. Use when selecting parts for a BOM,
  verifying a specific MPN, sourcing an IC or passive, or when the user asks to
  find, search, check, or confirm a component.
---

# Find Part

This skill is the single source of truth for part-lookup tools and sourcing rules —
the architect and part-sourcer invoke it rather than carrying their own copies.

## When to use
- Selecting components for a new circuit BOM
- Verifying stock, pricing, or lifecycle of a candidate MPN
- Retrieving a KiCad symbol/footprint for a specific part
- Invoked via `/find-part`, or by the circuit-architect and part-sourcer agents

## Procedure

### Step 1 — Preferred: pcbparts MCP (live JLCPCB data)

For any query (MPN or description, e.g. "3.3V LDO 500mA SOT-23-5"):

| Task | Tool |
|------|------|
| Description search | `mcp__pcbparts__jlc_search` |
| Exact LCSC# / MPN | `mcp__pcbparts__jlc_get_part` |
| Stock check | `mcp__pcbparts__jlc_stock_check` |
| Cross-ref (DigiKey) | `mcp__pcbparts__digikey_get_part` |
| Cross-ref (Mouser) | `mcp__pcbparts__mouser_get_part` |
| KiCad symbol + footprint | `mcp__pcbparts__cse_get_kicad` |

If `pcbparts` MCP is not connected, warn before continuing, then use Step 2's
`stock-check.py` for stock — never one `curl` per part:
```bash
echo "⚠️ pcbparts MCP not connected. Install: claude mcp add --transport http pcbparts https://pcbparts.dev/mcp"
```

### Step 2 — Fallback: SKiDL local library search

Use when pcbparts MCP is unavailable or a local KiCad symbol check is needed (SKiDL's
`skidl-part-search`, if it is on PATH; `find-symbol.py` in Step 3 always works):
```bash
skidl-part-search "<query>"          # all terms must match
skidl-part-search --limit 20 "<query>"
skidl-part-search 'opamp (low-noise|dip-8)'  # OR within terms
skidl-part-search --interactive      # browse with pin details
```

### Step 2b — Fallback: batch stock check (no MCP)

Every part in **one** call — MPNs, LCSC numbers, or a whole BOM table:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/stock-check.py AD9235BCPZ-40 C653327 ...
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/stock-check.py --bom sourcing/sourced_bom.md
```
Prints a markdown table (stock, first price break, package, Basic/Preferred/Extended,
FAIL <100 / WARN <500) from the open JLCPCB mirror. It has **no lifecycle data** — check
EOL/NRND separately for critical ICs. Querying parts one at a time is what took the
measured sourcing phase 55 calls and 53 minutes; this does 53 parts in under 2 s.

### Step 3 — Symbol availability (always, for every active part)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/find-symbol.py AD9235BCPZ-40 THS4551IRGTR ...
```
Reports `EXACT` / `WILDCARD` / `PREFIX` / `MFR-PREFIX` → `Lib:Symbol`, or `MISSING`.
A missing symbol found here costs one generation step in the datasheet phase; found at
coding time it cost a block coder its whole budget. A `PREFIX` match ignores the MPN's
suffix — confirm the package matches before trusting it.

## Sourcing Rules (always apply)

**JLCPCB tier priority:**
1. Basic — use whenever possible
2. Preferred — strongly preferred for production
3. Extended — only if no alternative (adds manual placement surcharge)

**Mandatory checks (flag anything that fails):**
- Stock > 100 units (warn if < 500)
- Correct voltage, current, and package ratings
- Not EOL / NRND
- Single-source → flag explicitly

**Package preference for passives:** 0402 > 0603 > 0805 > 1206

**Decision heuristics:**
- Jellybean passives → cheapest Basic part meeting spec
- Critical ICs (MCU, regulator, ADC) → Preferred tier + second-source check
- Regulated/automotive designs → AEC-Q100/Q200 when required

## Output

For each part found, produce one row in this format:
```
MPN | LCSC# | Stock | Unit Price | Package | JLCPCB Tier | KiCad Symbol | KiCad Footprint | Notes
```
`KiCad Symbol` is `Lib:Symbol` from Step 3, or `⚠️ SYMBOL NEEDED` when none exists.

## Validation

- Do not use a part without confirming stock > 100 units
- Do not leave the KiCad footprint field blank before handing the part to skidl-coder
- Single-source and Extended-tier parts must have a `Notes` entry explaining the risk
