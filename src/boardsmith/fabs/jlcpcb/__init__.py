"""JLCPCB adapter: kicad-happy's BOM/CPL translator plus what it leaves out (LCSC numbers
from the parts' ``LCSC`` field, and per-footprint rotation offsets)."""

import os
import subprocess
import sys

from boardsmith.analysis import vendor_script
from boardsmith.fabs.base import FabAdapter, read_csv, write_csv


class Adapter(FabAdapter):
    name = "jlcpcb"

    def bom(self, kicad_bom_csv, out_csv, fields):
        """fields: {ref: {field: value}}; the LCSC column is filled from each part's LCSC field."""
        r = subprocess.run([sys.executable, vendor_script("bom/scripts/translate_bom_pnp.py"), "bom",
                            kicad_bom_csv, "-o", out_csv], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"translate_bom_pnp bom failed: {r.stderr.strip()[-300:]}")
        rows = read_csv(out_csv)
        head = rows[0]
        ci, di = head.index("LCSC Part #"), head.index("Designator")
        missing = []
        for row in rows[1:]:
            refs = [x.strip() for x in row[di].replace(";", ",").split(",") if x.strip()]
            lcsc = sorted({fields.get(ref, {}).get("LCSC", "") for ref in refs} - {""})
            row[ci] = ",".join(lcsc)
            if not lcsc:
                missing.append(row[di].split(",")[0])
        write_csv(out_csv, rows)
        return {"lines": len(rows) - 1, "lines_without_lcsc": len(missing)}

    def cpl(self, kicad_pos_csv, out_csv, footprints, bom_csv=None):
        """footprints: {ref: "lib:name"} for the rotation-offset table."""
        args = [sys.executable, vendor_script("bom/scripts/translate_bom_pnp.py"), "pnp",
                kicad_pos_csv, "-o", out_csv]
        if bom_csv:
            args += ["--bom", bom_csv]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"translate_bom_pnp pnp failed: {r.stderr.strip()[-300:]}")
        rows = read_csv(out_csv)
        head = rows[0]
        ri, di = head.index("Rotation"), head.index("Designator")
        adjusted = []
        for row in rows[1:]:
            off = self.rotation_offset(footprints.get(row[di], ""))
            if off:
                row[ri] = f"{(float(row[ri] or 0) + off) % 360:g}"
                adjusted.append(row[di])
        write_csv(out_csv, rows)
        return {"placements": len(rows) - 1, "rotation_adjusted": adjusted}

    def parts_catalog(self):
        # Free jlcsearch API, no key: stock, price tiers, basic vs. extended.
        return [sys.executable, vendor_script("lcsc/scripts/search_lcsc.py")]
