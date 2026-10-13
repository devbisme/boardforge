"""kicad-cli DRC/ERC runs, reduced to short summaries (full JSON stays on disk)."""

import collections
import json
import os


# DRC error types a reroute can fix when they involve router copper (tracks, vias).
REPAIRABLE = ("clearance", "track_width", "tracks_crossing", "shorting_items", "hole_clearance",
              "copper_edge_clearance")


def repairable(report):
    """Errors in a raw kicad-cli DRC report that involve tracks or vias and a reroute can fix."""
    return [v for v in report.get("violations", [])
            if v["severity"] == "error" and v["type"] in REPAIRABLE
            and any(i["description"].startswith(("Track", "Via")) for i in v["items"])]


def drc(kicad, pcb, out_json, parity=False, log=None):
    """Run DRC; return {errors, warnings, unconnected, parity, by_type, unconnected_items}.

    Zones are refilled first (in memory; the file isn't changed), so plane connections count
    as connected even on a board whose zones were never filled.

    Unconnected items that involve a zone are not counted as unrouted: they are zone islands
    or unfilled zones, which a refill or the planes resolve, not missing tracks.
    """
    args = ["pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "-o", out_json]
    if parity:
        args.append("--schematic-parity")
    kicad.cli_run(*args, pcb, log=log)
    if not os.path.exists(out_json):
        return {"ok": False, "error": "DRC produced no report"}
    with open(out_json) as f:
        d = json.load(f)
    viol = d.get("violations", [])
    unc = [u for u in d.get("unconnected_items", [])
           if not any(i["description"].startswith("Zone") for i in u["items"])]
    by_type = collections.Counter(f"{v['severity']}:{v['type']}" for v in viol)
    return {
        "ok": True,
        "errors": sum(1 for v in viol if v["severity"] == "error"),
        "warnings": sum(1 for v in viol if v["severity"] == "warning"),
        "unconnected": len(unc),
        "unconnected_zone": len(d.get("unconnected_items", [])) - len(unc),
        "parity": len(d.get("schematic_parity", [])) if parity else None,
        "by_type": dict(by_type.most_common()),
        "unconnected_items": [[i["description"][:60] for i in u["items"]] for u in unc[:20]],
        "error_items": [f"{v['type']}: {v['description'][:80]}" for v in viol
                        if v["severity"] == "error"][:20],
        "parity_items": [p["description"][:90] for p in d.get("schematic_parity", [])[:20]],
    }


def erc(kicad, sch, out_json, log=None):
    kicad.cli_run("sch", "erc", "--format", "json", "--severity-all", "-o", out_json, sch, log=log)
    if not os.path.exists(out_json):
        return {"ok": False, "error": "ERC produced no report"}
    with open(out_json) as f:
        d = json.load(f)
    viol = [v for s in d.get("sheets", []) for v in s.get("violations", [])]
    by_type = collections.Counter(f"{v['severity']}:{v['type']}" for v in viol)
    return {
        "ok": True,
        "errors": sum(1 for v in viol if v["severity"] == "error"),
        "warnings": sum(1 for v in viol if v["severity"] == "warning"),
        "by_type": dict(by_type.most_common()),
        "error_items": [f"{v['type']}: {v['description'][:80]}" for v in viol
                        if v["severity"] == "error"][:20],
    }
