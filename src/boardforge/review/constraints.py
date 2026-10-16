"""constraints.json v1 (§4.10): machine-checkable requirements, each tagged HARD/SOFT/ASSUMED.

    {"schema": "boardforge-constraints", "version": 1,
     "constraints": [{"id": "AAF-A", "kind": "ac_gain", "tag": "HARD", "why": "...",
                      "in": "VIN_A", "out": ["AIN_AP", "AIN_AN"], "at_hz": 5e6,
                      "ref_hz": 1e3, "max_db": -20}, ...]}

Net names are SKiDL names. Unknown kinds are kept and reported as not checked.
"""

import json
import os

KINDS = {
    "ac_gain": {"required": ["in", "out", "at_hz"], "one_of": ["min_db", "max_db"]},
    "rail_current": {"required": ["net", "max_a"], "one_of": []},
}
TAGS = ("HARD", "SOFT", "ASSUMED")


def path(proj):
    """The project's constraints.json ([review] constraints)."""
    return os.path.join(proj.root, proj.cfg.get("review", {}).get("constraints", "constraints.json"))


def rail_loads(proj):
    """{net: A} from the valid rail_current constraints."""
    cs, _ = load(path(proj))
    return {c["net"]: c["max_a"] for c in cs if c.get("kind") == "rail_current" and not c.get("invalid")}


def load(path):
    """(constraints, problems). A missing file gives ([], [])."""
    if not os.path.exists(path):
        return [], []
    with open(path) as f:
        doc = json.load(f)
    problems = []
    if doc.get("schema") not in ("boardforge-constraints", "boardsmith-constraints"):  # old name
        problems.append(f"{path}: schema is {doc.get('schema')!r}, expected 'boardforge-constraints'")
    if doc.get("version", 1) > 1:
        problems.append(f"{path}: version {doc['version']} is newer than this boardforge (1)")
    out, seen = [], set()
    for i, c in enumerate(doc.get("constraints", [])):
        cid = c.get("id") or f"#{i}"
        if cid in seen:
            problems.append(f"constraint {cid}: duplicate id")
        seen.add(cid)
        c = {**c, "id": cid, "tag": c.get("tag", "HARD")}
        if c["tag"] not in TAGS:
            problems.append(f"constraint {cid}: tag {c['tag']!r} is not one of {TAGS}")
        spec = KINDS.get(c.get("kind"))
        if spec:
            missing = [k for k in spec["required"] if k not in c]
            if spec["one_of"] and not any(k in c for k in spec["one_of"]):
                missing.append(" or ".join(spec["one_of"]))
            if missing:
                problems.append(f"constraint {cid}: {c['kind']} needs {', '.join(missing)}")
                c["invalid"] = True
        out.append(c)
    return out, problems
