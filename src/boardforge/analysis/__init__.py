"""Analysis-step registry (design doc §4.7).

Each step (schematic, pcb, thermal, emc, cross_verify, gerbers, release_gate) has one or
more implementations. The default is the vendored kicad-happy script, run as a subprocess.
Steps talk through JSON files with kicad-happy's schema
(``skills/kicad/references/output-schema.md``), so a replacement only has to write the same
JSON. Choose an implementation per step in boardforge.toml:

    [checks.steps]
    thermal = "boardforge"

Register a replacement with ``@register("thermal", "boardforge")`` on a function
``(ctx) -> output path``.
"""

import json
import os
import subprocess
import sys

VENDOR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor", "kicad_happy", "skills")
REGISTRY = {}

# Steps in dependency order; each lists the outputs it reads.
ORDER = ["schematic", "pcb", "thermal", "emc", "cross_verify", "gerbers", "release_gate"]


def vendor_script(rel):
    return os.path.join(VENDOR, rel)


def register(step, impl):
    def deco(fn):
        REGISTRY.setdefault(step, {})[impl] = fn
        return fn
    return deco


class Context:
    """What a step needs: input files and where outputs go. ``outputs`` fills as steps run."""

    def __init__(self, sch, pcb, outdir, gerber_dir=None, rail_currents=None, drc_report=None,
                 fab_limits=None):
        self.sch, self.pcb, self.outdir, self.gerber_dir = sch, pcb, outdir, gerber_dir
        # {"name": "jlcpcb/multilayer", min_track, min_clearance, min_drill, annular_ring}:
        # the fab tier the board was set up for; DFM findings are re-judged against it.
        self.fab_limits = fab_limits
        self.drc_report = drc_report  # kicad-cli DRC JSON: the authority on unrouted connections
        self.rail_currents = dict(rail_currents or {})  # {KiCad net: A}
        self.outputs = {}
        self.loads = []  # [(regulator ref, rail, A)] patched by schematic_with_loads()
        os.makedirs(outdir, exist_ok=True)

    def out(self, step):
        return os.path.join(self.outdir, f"{step}.json")

    def schematic_with_loads(self):
        """The schematic analysis with declared rail currents in place of kicad-happy's load
        guesses (LDO dissipation, switcher rail loads). Same file if nothing is declared."""
        src = self.outputs["schematic"]
        if not self.rail_currents:
            return src
        with open(src) as f:
            sch = json.load(f)
        self.loads = apply_rail_currents(sch, self.rail_currents)
        path = self.out("schematic_loads")
        with open(path, "w") as f:
            json.dump(sch, f)
        return path


def apply_rail_currents(sch, currents):
    """Patch declared load currents into a kicad-happy schematic analysis, in place.
    Returns [(regulator ref, rail, amps)] for the regulators changed."""
    patched = []
    for reg in sch.get("findings", []):
        if reg.get("detector") != "detect_power_regulators":
            continue
        rail = reg.get("output_rail")
        if rail not in currents:
            continue
        amps = float(currents[rail])
        pd = reg.get("power_dissipation")
        if isinstance(pd, dict) and "dropout_V" in pd:  # LDO
            pd["estimated_iout_A"] = round(amps, 4)
            pd["estimated_pdiss_W"] = round(pd["dropout_V"] * amps, 4)
            pd["_iout_provenance"] = {"source": "boardforge_config", "confidence": "declared",
                                      "basis": "checks.rail_currents"}
        elif isinstance(pd, dict) and pd.get("efficiency_assumed") and pd.get("vout_V"):  # switcher
            eta = pd["efficiency_assumed"]
            pd["estimated_iout_A"] = round(amps, 4)
            pd["estimated_pdiss_W"] = round(pd["vout_V"] * amps * (1 / eta - 1), 4)
            pd["_iout_provenance"] = {"source": "boardforge_config", "confidence": "declared",
                                      "basis": "checks.rail_currents"}
        reg["estimated_iout_A"] = round(amps, 4)
        patched.append((reg.get("ref"), rail, amps))
    rails = sch.get("power_budget", {}).get("rails", {})
    for rail, amps in currents.items():
        if isinstance(rails.get(rail), dict):
            rails[rail]["estimated_load_mA"] = round(float(amps) * 1000, 1)
    return patched


def _vendor(script, *args, timeout=900):
    r = subprocess.run([sys.executable, vendor_script(script), *map(str, args)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode not in (0, 1):  # some analyzers exit 1 when they report errors
        raise RuntimeError(f"{os.path.basename(script)} exit {r.returncode}: {r.stderr.strip()[-400:]}")


@register("schematic", "kicad_happy")
def _schematic(ctx):
    _vendor("kicad/scripts/analyze_schematic.py", ctx.sch, "-o", ctx.out("schematic"))
    return ctx.out("schematic")


@register("pcb", "kicad_happy")
def _pcb(ctx):
    _vendor("kicad/scripts/analyze_pcb.py", ctx.pcb, "--full", "-o", ctx.out("pcb"))
    return ctx.out("pcb")


@register("thermal", "kicad_happy")
def _thermal(ctx):
    _vendor("kicad/scripts/analyze_thermal.py", "-s", ctx.schematic_with_loads(), "-p",
            ctx.outputs["pcb"], "-o", ctx.out("thermal"))
    return ctx.out("thermal")


@register("emc", "kicad_happy")
def _emc(ctx):
    _vendor("emc/scripts/analyze_emc.py", "-s", ctx.outputs["schematic"], "-p", ctx.outputs["pcb"],
            "-o", ctx.out("emc"))
    return ctx.out("emc")


@register("cross_verify", "kicad_happy")
def _cross(ctx):
    args = ["-s", ctx.schematic_with_loads(), "-p", ctx.outputs["pcb"], "-o", ctx.out("cross_verify")]
    if "thermal" in ctx.outputs:
        args += ["-t", ctx.outputs["thermal"]]
    _vendor("kicad/scripts/cross_verify.py", *args)
    return ctx.out("cross_verify")


@register("gerbers", "kicad_happy")
def _gerbers(ctx):
    _vendor("kicad/scripts/analyze_gerbers.py", ctx.gerber_dir, "-o", ctx.out("gerbers"))
    return ctx.out("gerbers")


# kicad-happy DFM-001 parameter -> the fab-tier minimum it is held to
DFM_LIMITS = {"track_spacing": "min_clearance", "track_width": "min_track", "via_drill": "min_drill",
              "pad_drill": "min_drill", "annular_ring": "annular_ring"}


def dfm_for_fab(pcb, limits):
    """Re-judge kicad-happy's DFM tier against the board's own fab tier. Its 'standard' limits
    are JLCPCB's 1-2 layer ones (0.127 mm spacing), so a 4-layer board at JLCPCB's 0.09 mm
    multilayer minimum was reported as needing the 'advanced' process. Findings within the
    tier are dropped; the tier becomes 'standard' (meaning: this fab tier) when none are left.
    Returns the parameters dropped."""
    if not limits:
        return []
    dropped, keep = [], []
    for f in pcb.get("findings", []):
        par = f.get("parameter") if f.get("rule_id") == "DFM-001" else None
        lim = limits.get(DFM_LIMITS.get(par, ""))
        if lim is not None and isinstance(f.get("actual_mm"), (int, float)) and f["actual_mm"] >= lim - 1e-6:
            dropped.append(par)
            continue
        keep.append(f)
    pcb["findings"] = keep
    left = [f for f in keep if f.get("rule_id") == "DFM-001" and f.get("parameter") in DFM_LIMITS]
    dfm = pcb.setdefault("dfm_summary", {})
    if dropped and not left:
        dfm["dfm_tier"] = "standard"
    dfm["judged_against"] = limits.get("name")
    return dropped


def routing_from_drc(pcb, drc_report):
    """Replace kicad-happy's routing status with KiCad's: kicad-happy counts a net as routed
    once it has any copper, so a partly routed net passes. Nets come from the DRC report's
    unconnected items (``Pad 1 [NET] of U1``)."""
    nets = sorted({i["description"].split("[", 1)[1].split("]", 1)[0]
                   for u in drc_report.get("unconnected_items", []) for i in u.get("items", [])
                   if "[" in i.get("description", "")})
    conn = pcb.setdefault("connectivity", {})
    conn["unrouted_count"] = len(nets)
    conn["routing_complete"] = not nets
    conn["unrouted"] = [{"net_name": n, "source": "kicad-cli DRC"} for n in nets]
    return nets


@register("release_gate", "kicad_happy")
def _gate(ctx):
    # The gate compares schematic parts with *all* PCB footprints; board-only ones
    # (fiducials, mounting holes) have no schematic symbol, and the schematic count leaves
    # out DNP parts, so both are left out of the count.
    # Its DFM tier is re-judged against the board's fab tier (dfm_for_fab).
    with open(ctx.outputs["pcb"]) as f:
        pcb = json.load(f)
    fps = pcb.get("footprints", [])
    pcb.setdefault("statistics", {})["footprint_count"] = sum(
        1 for fp in fps if not fp.get("board_only") and not fp.get("dnp"))
    if ctx.drc_report and os.path.exists(ctx.drc_report):
        with open(ctx.drc_report) as f:
            routing_from_drc(pcb, json.load(f))
    dfm_for_fab(pcb, ctx.fab_limits)
    pcb_gate = ctx.out("pcb_for_gate")
    with open(pcb_gate, "w") as f:
        json.dump(pcb, f)
    args = ["-s", ctx.outputs["schematic"], "-p", pcb_gate, "-o", ctx.out("release_gate")]
    for step, flag in (("gerbers", "-g"), ("thermal", "-t"), ("emc", "-e")):
        if step in ctx.outputs:
            args += [flag, ctx.outputs[step]]
    _vendor("kicad/scripts/fab_release_gate.py", *args)
    return ctx.out("release_gate")


def summarize(path, top=5):
    """Counts by severity plus the first few errors, from kicad-happy's common envelope."""
    with open(path) as f:
        d = json.load(f)
    s = d.get("summary", {}) if isinstance(d, dict) else {}
    out = {"by_severity": s.get("by_severity")}
    findings = [x for x in d.get("findings", []) if not x.get("suppressed")]
    errs = [x for x in findings if x.get("severity") == "error"]
    out["errors"] = [f"{x.get('rule_id', '?')}: {(x.get('summary') or x.get('title') or '')[:90]}"
                     for x in errs[:top]]
    if "overall_status" in d:  # release gate
        out = {"status": d["overall_status"], **{k: d.get("summary", {}).get(k)
                                                 for k in ("pass", "warn", "fail", "skip")}}
        out["not_passed"] = [f"{c.get('status')}: {c.get('check_id')}: {c.get('message', '')[:80]}"
                             for c in d.get("checks", []) if c.get("status") in ("warn", "fail")][:top]
    return out


def run_steps(ctx, steps, choice=None):
    """Run the named steps in dependency order. A failure is recorded and later steps that
    need its output are skipped. Returns {step: summary}."""
    choice = choice or {}
    results = {}
    for step in [s for s in ORDER if s in steps]:
        impl = choice.get(step, "kicad_happy")
        fn = REGISTRY.get(step, {}).get(impl)
        if fn is None:
            results[step] = {"error": f"no implementation {impl!r}"}
            continue
        try:
            path = fn(ctx)
            ctx.outputs[step] = path
            results[step] = summarize(path)
        except KeyError as e:
            results[step] = {"skipped": f"needs {e.args[0]} output"}
        except Exception as e:  # noqa: BLE001 - recorded, run continues
            results[step] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    return results
