"""Project configuration: boardsmith.toml merged over defaults.

All paths in the result are absolute. Net names are the SKiDL names; stages translate them
to KiCad names with the map written by C1 (``Project.net_map``).
"""

import copy
import os
import tomllib
from dataclasses import dataclass, field

CONFIG_NAME = "boardsmith.toml"
# Machine-level settings (KiCad and Freerouting paths) shared by all projects.
USER_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "boardsmith", "config.toml"
)

DEFAULTS = {
    "design": {
        "file": None,  # SKiDL source, relative to the project dir
        "top": None,  # function that builds the circuit; None = the file builds it on import
        "name": None,  # project/file stem; default = design file stem
        "title": "",
    },
    "kicad": {
        # Auto-detected when empty (see boardsmith.kicad).
        "cli": "",
        "python": "",
        "symbol_dir": "",
        "footprint_dir": "",
    },
    # Extra libraries: {nickname = "path/to/lib.pretty"}; they override stock ones.
    "libraries": {"footprints": {}},
    "fab": {"name": "jlcpcb", "tier": "multilayer", "renders": True},
    "board": {
        "size": None,  # [w, h] mm; None = estimate from courtyard area
        "fill": 0.35,  # courtyard area / board area when the size is estimated
        "layers": 4,
        "thickness": 1.6,
        "origin": [50.0, 50.0],  # board top-left corner in KiCad coordinates (mm)
        "planes": {},  # {"In1.Cu": "GND", ...}: inner layers that are solid planes
        "pours": [],  # nets poured on the outer layers after routing
        "stitch_pitch": 5.0,  # mm grid for pour stitching vias (0 = none)
        "mounting_holes": {
            "footprint": "MountingHole:MountingHole_3.2mm_M3_Pad_Via",
            "inset": 3.5,
            "net": "",
        },
        "fiducials": True,  # plus a "<name> Rev <revision>" silkscreen label
        "revision": "A",
    },
    "rules": {
        # Default net class. Anything not given comes from the fab tier minimums.
        "track_width": 0.15,
        "clearance": 0.15,
        "via_diameter": 0.45,
        "via_drill": 0.2,
        "edge_clearance": 0.3,
    },
    "placement": {
        "anchor": [],  # [{ref|value, sheet, at=[x,y], rot, side}]
        # Floorplan: [{sheet, value (regexes), rect = [x0, y0, x1, y1] mm from the top-left
        # corner | area = the same as fractions of the board}]. Unanchored ICs are packed
        # into the first region that matches; their passives follow them.
        "region": [],
        "ic_gap": 6.0,  # mm between packed ICs
        # Compaction: shrink the board (and the anchor/region positions with it) in steps of
        # ``step`` down to ``min_scale`` while every part fits, nothing overlaps and at most
        # ``max_over`` congestion cells exceed ``max_util``; then route, and step back up
        # (at most ``route_retries`` times) if routing leaves connections.
        "compact": {"enabled": False, "step": 0.05, "min_scale": 0.6, "max_util": 1.0,
                    "max_over": 0, "route_retries": 2},
        "bottom_decap_owners": "",  # regex on owner value: their decaps go on B.Cu
        # Other decaps that can't get within checks.decap_max_mm on the front may go on B.Cu
        # (off by default: parts on both sides cost more to assemble).
        "decap_bottom": False,
        # Pin swap (FPGA/MCU I/O): [{part (value regex) | ref, map (JSON file the design reads,
        # project-relative), group = [{pins = [...], partner = value regex}]}]. C3 writes
        # {SKiDL net: pin} to the map; when it changes, C1-C3 run again.
        "pin_swap": [],
    },
    "routing": {
        "chain": ["freerouting", "astar"],
        "preroute": [],  # nets routed first (A*) and locked, e.g. USB D+/D-
        "preroute_width": 0.2,
        "freerouting": {"path": "", "max_passes": 40, "threads": 1, "timeout": 3600, "neckdown": False},
        "astar": {"passes": 10, "max_expansions": 600000, "repairs": 3},
        # Board size estimated and connections left unrouted: loosen the fill and redo C2-C4.
        "grow_retries": 1,
    },
    "checks": {
        "decap_max_mm": 3.0,
        "length_nets": [],  # report the routed length of these nets (e.g. clocks)
        "analyzers": ["schematic", "pcb", "thermal", "emc", "cross_verify"],
        "steps": {},  # per-step implementation override, e.g. {thermal = "boardsmith"}
        # Load current (A) per regulator output rail, e.g. {"+1V2" = 0.05}. Replaces the
        # thermal analyzer's guess (kicad-happy estimates LDO load from output capacitance).
        "rail_currents": {},
    },
    "output": {"dir": "build"},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Project:
    """A boardsmith project: config plus the derived output paths."""

    root: str
    cfg: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root=".", overrides=None):
        """Defaults <- user config (machine paths) <- project config <- overrides."""
        root = os.path.abspath(root)
        path = os.path.join(root, CONFIG_NAME)
        cfg = DEFAULTS
        for p in (USER_CONFIG, path):
            if os.path.exists(p):
                with open(p, "rb") as f:
                    cfg = _merge(cfg, tomllib.load(f))
        if overrides:
            cfg = _merge(cfg, overrides)
        d = cfg["design"]
        if not d["file"]:
            raise ValueError(f"{path}: [design] file is required")
        d["file"] = os.path.join(root, d["file"])
        d["name"] = d["name"] or os.path.splitext(os.path.basename(d["file"]))[0]
        return cls(root=root, cfg=cfg)

    # ---- paths
    @property
    def name(self):
        return self.cfg["design"]["name"]

    @property
    def out(self):
        return os.path.join(self.root, self.cfg["output"]["dir"])

    def path(self, *parts):
        p = os.path.join(self.out, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    @property
    def kicad_dir(self):
        return os.path.join(self.out, "kicad")

    def kfile(self, ext):
        """A file of the KiCad project, e.g. kfile('.kicad_pcb')."""
        return self.path("kicad", self.name + ext)

    @property
    def netlist_json(self):
        return self.path("netlist.json")

    @property
    def net_map_file(self):
        return self.path("net_map.json")

    @property
    def state_file(self):
        return self.path("pipeline_state.json")

    @property
    def work(self):
        p = os.path.join(self.out, "work")
        os.makedirs(p, exist_ok=True)
        return p

    @property
    def reports(self):
        p = os.path.join(self.out, "reports")
        os.makedirs(p, exist_ok=True)
        return p
