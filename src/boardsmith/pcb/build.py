"""C2: build the board from the kicad-cli netlist: stackup, rules, nets, footprints, outline,
mounting holes, inner-layer plane zones. Footprints are left at the board centre for C3.

Job: netlist, pcb, fp_paths, size ([w, h] or null), fill, origin, layers, thickness,
planes {layer: net}, rules {...}, mounting_holes {footprint, inset, net}.
"""

import math

import pcbnew

from boardsmith.netlist import read_kicad_netlist
from boardsmith.pcbfile import stable_uuids
from boardsmith.pcb.common import MM, add_zone, courtyard_rect, load_footprint, pt, run

# Netlist fields that are KiCad's own, not user data.
STD_FIELDS = {"Reference", "Value", "Footprint", "Datasheet", "Description", "Sheetname",
              "Sheetfile", "ki_keywords", "ki_fp_filters", "ki_description"}


def is_dnp(comp):
    """Do-not-populate: a DNP field that isn't "no"/"false"/"0", or the value "DNP"."""
    v = str(comp["fields"].get("DNP", comp["fields"].get("dnp", ""))).strip().casefold()
    return v not in ("", "no", "false", "0", "n") or comp.get("value", "").strip().upper() == "DNP"


def estimate_size(fps, fill, mh_inset):
    """Board [w, h] (mm, 4:3) whose area makes the courtyards cover ``fill`` of it."""
    area = 0.0
    for fp in fps:
        x0, y0, x1, y1 = courtyard_rect(fp)
        area += (x1 - x0) * (y1 - y0)
    total = area / max(fill, 0.05)
    w = math.sqrt(total * 4 / 3) + 2 * mh_inset
    h = total / max(w - 2 * mh_inset, 1) + 2 * mh_inset
    return [math.ceil(w), math.ceil(h)]


def build(job):
    comps, nets = read_kicad_netlist(job["netlist"])
    rules = job["rules"]
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(job["layers"])
    ds = board.GetDesignSettings()
    ds.SetBoardThickness(MM(job["thickness"]))
    ds.m_TrackMinWidth = MM(rules["min_track"])
    ds.m_MinClearance = MM(rules["min_clearance"])
    ds.m_ViasMinSize = MM(rules["min_via_diameter"])
    ds.m_MinThroughDrill = MM(rules["min_drill"])
    ds.m_HoleClearance = MM(rules["hole_clearance"])
    ds.m_CopperEdgeClearance = MM(rules["edge_clearance"])
    # Pads on poured nets also connect through fanout vias or routed tracks, so one thermal
    # spoke is enough (KiCad's default of 2 flags crowded decap pads as errors).
    ds.m_MinResolvedSpokes = rules.get("min_spokes", 1)
    nc = ds.m_NetSettings.GetDefaultNetclass()
    nc.SetClearance(MM(rules["clearance"]))
    nc.SetTrackWidth(MM(rules["track_width"]))
    nc.SetViaDiameter(MM(rules["via_diameter"]))
    nc.SetViaDrill(MM(rules["via_drill"]))

    tb = board.GetTitleBlock()
    tb.SetTitle(job.get("title", ""))
    tb.SetRevision(job.get("revision", ""))
    tb.SetDate(job.get("date", ""))

    netinfo = {}
    for name in nets:
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        netinfo[name] = ni
    pad_net = {(r, p): name for name, nodes in nets.items() for r, p in nodes}

    fps, warnings = [], []
    for ref, c in sorted(comps.items()):
        if ref.startswith("#"):
            continue
        if not c["footprint"]:
            warnings.append(f"{ref} has no footprint; skipped")
            continue
        fp = load_footprint(c["footprint"], job["fp_paths"])
        fp.SetReference(ref)
        fp.SetValue(c["value"])
        fp.SetPath(pcbnew.KIID_PATH(c["sheettstamps"] + c["tstamp"]))
        fp.SetSheetname(c["sheetnames"] or "")
        fp.SetSheetfile(c["fields"].get("Sheetfile", ""))
        for k, fid in (("Datasheet", pcbnew.FIELD_T_DATASHEET),
                       ("Description", pcbnew.FIELD_T_DESCRIPTION)):
            if c["fields"].get(k):  # DRC schematic parity compares these
                fld = fp.GetField(fid)
                fld.SetText(c["fields"][k])
                fld.SetVisible(False)
        for k, v in c["fields"].items():
            if k in STD_FIELDS:
                continue
            fp.SetField(k, v)
            for fld in fp.GetFields():
                if fld.GetName() == k:
                    fld.SetVisible(False)
        for pad in fp.Pads():
            n = pad_net.get((ref, pad.GetNumber()))
            if n:
                pad.SetNet(netinfo[n])
        if is_dnp(c):
            # placed (for a later rework) but not assembled: out of the BOM, CPL and counts
            fp.SetDNP(True)
            fp.SetExcludedFromBOM(True)
            fp.SetExcludedFromPosFiles(True)
        fps.append(fp)

    mh = job.get("mounting_holes") or {}
    inset = float(mh.get("inset", 3.5)) if mh.get("footprint") else 0.0
    size = job.get("size") or estimate_size(fps, job["fill"], inset)
    w, h = size
    ox, oy = job["origin"]

    corners = [(ox, oy), (ox + w, oy), (ox + w, oy + h), (ox, oy + h)]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(MM(0.1))
        board.Add(s)

    for fp in fps:
        fp.SetPosition(pt(ox + w / 2, oy + h / 2))
        board.Add(fp)

    if mh.get("footprint"):
        for i, (x, y) in enumerate(((inset, inset), (w - inset, inset), (inset, h - inset),
                                    (w - inset, h - inset))):
            hole = load_footprint(mh["footprint"], job["fp_paths"])
            hole.SetBoardOnly(True)  # not in the schematic
            hole.SetReference(f"H{i + 1}")
            hole.SetValue("MountingHole")
            hole.SetPosition(pt(ox + x, oy + y))
            if mh.get("net") in netinfo:
                for pad in hole.Pads():
                    pad.SetNet(netinfo[mh["net"]])
            board.Add(hole)

    m = 0.3
    for layer, net in job.get("planes", {}).items():
        lid = board.GetLayerID(layer)
        if lid < 0 or net not in netinfo:
            warnings.append(f"plane {layer}={net}: unknown layer or net; skipped")
            continue
        add_zone(board, lid, netinfo[net], (ox + m, oy + m, ox + w - m, oy + h - m))

    board.Save(job["pcb"])
    stable_uuids(job["pcb"])
    return {"footprints": len(fps), "nets": len(nets), "size": [w, h],
            "size_estimated": not job.get("size"), "warnings": warnings}


if __name__ == "__main__":
    run(build)
