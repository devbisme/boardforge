"""Plain-text edits of .kicad_pcb files, for what pcbnew's Python API can't do. No pcbnew
import, so both the orchestrator and the board scripts can use it."""

import re
import uuid

# Top-level items whose UUIDs are made stable. KiCad saves each kind sorted by UUID, so
# random UUIDs reorder the file on every run, and with it the order later stages (placement,
# fanout, the Specctra export and Freerouting) see the items in.
KINDS = ("footprint", "segment", "arc", "via")


def stable_uuids(path):
    """Give footprints a UUID derived from their reference, and tracks and vias one derived
    from their content, then sort each kind by UUID as KiCad would. The same board then
    gives the same file. pcbnew's SWIG API can't set a UUID, so the file is rewritten."""
    with open(path) as f:
        text = f.read()
    end = text.rstrip().rfind(")")  # the board's closing paren stays last
    chunks = re.split(r"(?m)^(?=\t\()", text[:end])  # top-level items start with one tab
    groups, seen = {}, {}
    for i, c in enumerate(chunks):
        kind = re.match(r"\t\((\w+)", c)
        kind = kind.group(1) if kind else ""
        if kind not in KINDS:
            continue
        if kind == "footprint":
            ref = re.search(r'\(property "Reference" "([^"]*)"', c)
            key = "footprint/" + (ref.group(1) if ref else c)
        else:
            key = kind + "/" + re.sub(r'\(uuid "[^"]+"\)', "", c)
        seen[key] = seen.get(key, 0) + 1  # identical items (or references) still get distinct UUIDs
        new = str(uuid.uuid5(uuid.NAMESPACE_URL, f"boardsmith/{key}/{seen[key]}"))
        # The item's own UUID is its first one; footprint pads and fields come later.
        c = re.sub(r'\(uuid "[^"]+"\)', f'(uuid "{new}")', c, count=1)
        groups.setdefault(kind, []).append((i, new, c))
    for items in groups.values():
        for i, (_, c) in zip([i for i, _, _ in items], sorted((n, c) for _, n, c in items)):
            chunks[i] = c
    with open(path, "w") as f:
        f.write("".join(chunks) + text[end:])
