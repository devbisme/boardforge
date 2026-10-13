"""Fab adapter interface (design doc §4.5). The rest of the pipeline never names a fab.

A fab is a directory ``boardsmith/fabs/<name>/`` with ``profile.toml`` (data) and an
``__init__.py`` defining ``Adapter`` (a ``FabAdapter`` subclass). The base class supplies
defaults for everything, so a new fab can start as a profile plus an empty subclass.
"""

import csv
import os
import re
import tomllib


class FabAdapter:
    name = "generic"

    def __init__(self, tier=None):
        here = os.path.join(os.path.dirname(__file__), self.name)
        with open(os.path.join(here, "profile.toml"), "rb") as f:
            self.profile = tomllib.load(f)
        self.tier_name = tier or self.profile.get("default_tier")
        tiers = self.profile.get("tiers", {})
        if self.tier_name not in tiers:
            raise ValueError(f"{self.name}: unknown tier {self.tier_name!r}; known: {sorted(tiers)}")
        self.tier = tiers[self.tier_name]

    # ---- C2
    def design_rules(self, layers):
        """Board minimums (mm) for the tier, checked against the layer-count limits."""
        t = self.tier
        notes = []
        lo, hi = t.get("layers", [1, 64])
        if not lo <= layers <= hi:
            notes.append(f"{layers} layers is outside tier {self.tier_name!r} ({lo}-{hi})")
        rules = {k: t[k] for k in ("min_track", "min_clearance", "min_via_diameter", "min_drill",
                                   "hole_clearance", "edge_clearance", "annular_ring")}
        return rules, notes

    def check_rules(self, rules):
        """Warnings for net-class values below the tier minimums."""
        t, out = self.tier, []
        for key, minimum in (("track_width", "min_track"), ("clearance", "min_clearance"),
                             ("via_diameter", "min_via_diameter"), ("via_drill", "min_drill")):
            if key in rules and rules[key] < t[minimum] - 1e-9:
                out.append(f"{key} {rules[key]} < {self.name} {self.tier_name} minimum {t[minimum]}")
        ring = (rules.get("via_diameter", 0) - rules.get("via_drill", 0)) / 2
        if "via_diameter" in rules and ring < t["annular_ring"] - 1e-9:
            out.append(f"via annular ring {ring:.3f} < minimum {t['annular_ring']}")
        return out

    # ---- C6
    def gerber_layers(self, copper_layers):
        inner = [f"In{i}.Cu" for i in range(1, copper_layers - 1)]
        return ["F.Cu", *inner, "B.Cu", "F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.SilkS",
                "B.SilkS", "Edge.Cuts"]

    def drill_args(self):
        return ["--format", "excellon", "--excellon-units", "mm", "--excellon-separate-th",
                "--generate-map", "--map-format", "gerberx2"]

    def bom(self, kicad_bom_csv, out_csv, fields):
        """Fab BOM from kicad-cli's grouped BOM. Default: copy it."""
        with open(kicad_bom_csv) as f, open(out_csv, "w") as g:
            g.write(f.read())
        return {}

    def cpl(self, kicad_pos_csv, out_csv, footprints, bom_csv=None):
        """Fab pick-and-place from kicad-cli's position file. Default: copy it."""
        with open(kicad_pos_csv) as f, open(out_csv, "w") as g:
            g.write(f.read())
        return {}

    def rotation_offset(self, footprint):
        """Degrees to add to KiCad's rotation for this footprint (profile table, first match)."""
        for row in self.profile.get("rotation_offsets", []):
            if re.search(row["match"], footprint):
                return row["offset"]
        return 0

    def dfm_checks(self, stats):
        """Fab-specific checks beyond DRC, from measure.py's board stats."""
        out = []
        lim = self.profile.get("limits", {})
        w, h = stats.get("board_mm", [0, 0])
        if lim.get("min_board") and min(w, h) < lim["min_board"]:
            out.append(f"board {w}x{h} mm is below the minimum {lim['min_board']} mm")
        if lim.get("max_board") and (max(w, h) > max(lim["max_board"]) or min(w, h) > min(lim["max_board"])):
            out.append(f"board {w}x{h} mm exceeds {lim['max_board']} mm")
        if stats.get("parts_bottom") and not lim.get("assembly_both_sides", True):
            out.append(f"{stats['parts_bottom']} parts on the bottom, but assembly is top-only")
        return out

    def parts_catalog(self):
        """Sourcing back end: a command (list) that searches parts, or None."""
        return None

    def estimate_cost(self, stats, bom_rows):
        return "unknown"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
