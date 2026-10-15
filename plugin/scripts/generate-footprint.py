#!/usr/bin/env python3
"""Generate a KiCad 8 .kicad_mod footprint file from explicit datasheet dimensions.

The script handles SMD and THT pad types. The agent reads the mechanical drawing
from the datasheet and provides pad positions and sizes as CLI arguments.

Usage:
    python generate-footprint.py \\
        --name "Converter_DCDC_Murata_MGJ6DxxxxxxSC_SMD" \\
        --library "ProjectLocal" \\
        --description "Murata MGJ6 6W isolated DC-DC, 7-pad SMD" \\
        --pads '[{"number":1,"x":0,"y":0,"width":3.38,"height":2.41}, ...]' \\
        --body-width 27.94 --body-height 19.05

Pad object fields:
    number    — pad number (int or string)
    x, y      — pad center in mm (float), relative to footprint origin
    width     — pad width in mm (float)
    height    — pad height in mm (float)
    shape     — "rect" (default), "roundrect", "circle", "oval"
    type      — "smd" (default) or "tht"
    drill     — drill diameter in mm (required if type == "tht")

Output:
    footprints/<library>.pretty/<name>.kicad_mod
    (relative to current working directory)
"""
import argparse
import json
import math
import pathlib
import sys
from datetime import datetime


KICAD_VERSION = "20241229"
GENERATOR_VERSION = "9.0"


def fmt(v):
    """Format a float to at most 4 decimal places, stripping trailing zeros."""
    return f"{v:.4f}".rstrip("0").rstrip(".")


def generate_kicad_mod(name, description, tags, pads, body_width, body_height, courtyard_clearance=0.25):
    """Return a .kicad_mod file content string."""

    # Compute bounding box from pad centers + half-pad-size
    all_x = [p["x"] for p in pads]
    all_y = [p["y"] for p in pads]
    min_x = min(all_x) - max(p["width"] for p in pads) / 2
    max_x = max(all_x) + max(p["width"] for p in pads) / 2
    min_y = min(all_y) - max(p["height"] for p in pads) / 2
    max_y = max(all_y) + max(p["height"] for p in pads) / 2

    # Use body dimensions for silkscreen/fab if provided, else pad bounding box
    bw2 = body_width / 2
    bh2 = body_height / 2
    cx = (min(all_x) + max(all_x)) / 2
    cy = (min(all_y) + max(all_y)) / 2

    # Courtyard: body + clearance
    cyd_x1 = fmt(cx - bw2 - courtyard_clearance)
    cyd_y1 = fmt(cy - bh2 - courtyard_clearance)
    cyd_x2 = fmt(cx + bw2 + courtyard_clearance)
    cyd_y2 = fmt(cy + bh2 + courtyard_clearance)

    # Fab layer: exact body outline
    fab_x1 = fmt(cx - bw2)
    fab_y1 = fmt(cy - bh2)
    fab_x2 = fmt(cx + bw2)
    fab_y2 = fmt(cy + bh2)

    # Silkscreen: slightly outside fab (0.11mm line, placed at fab boundary)
    ref_y = fmt(cy - bh2 - 1.5)
    val_y = fmt(cy + bh2 + 1.5)
    ref_x = fmt(cx)
    val_x = fmt(cx)

    lines = []
    lines.append(f'(footprint "{name}"')
    lines.append(f'  (version {KICAD_VERSION})')
    lines.append(f'  (generator "pcbnew")')
    lines.append(f'  (generator_version "{GENERATOR_VERSION}")')
    lines.append(f'  (layer "F.Cu")')
    if description:
        lines.append(f'  (descr "{description}")')
    if tags:
        lines.append(f'  (tags "{tags}")')

    # Reference property
    lines.append(f'  (property "Reference" "REF**"')
    lines.append(f'    (at {ref_x} {ref_y})')
    lines.append(f'    (layer "F.SilkS")')
    lines.append(f'    (effects (font (size 1 1) (thickness 0.15)))')
    lines.append(f'  )')

    # Value property
    lines.append(f'  (property "Value" "{name}"')
    lines.append(f'    (at {val_x} {val_y})')
    lines.append(f'    (layer "F.Fab")')
    lines.append(f'    (effects (font (size 1 1) (thickness 0.15)))')
    lines.append(f'  )')

    # Fab layer — body outline
    lines.append(f'  (fp_rect')
    lines.append(f'    (start {fab_x1} {fab_y1}) (end {fab_x2} {fab_y2})')
    lines.append(f'    (layer "F.Fab")')
    lines.append(f'    (stroke (width 0.1) (type solid))')
    lines.append(f'  )')

    # Pin 1 marker on fab layer (small triangle at pin 1 pad center)
    p1 = next((p for p in pads if str(p["number"]) == "1"), pads[0])
    tri_size = 0.4
    lines.append(f'  (fp_line')
    lines.append(f'    (start {fmt(p1["x"] - tri_size)} {fmt(p1["y"] + tri_size)})')
    lines.append(f'    (end {fmt(p1["x"] + tri_size)} {fmt(p1["y"] + tri_size)})')
    lines.append(f'    (layer "F.Fab") (stroke (width 0.1) (type solid))')
    lines.append(f'  )')
    lines.append(f'  (fp_line')
    lines.append(f'    (start {fmt(p1["x"] - tri_size)} {fmt(p1["y"] + tri_size)})')
    lines.append(f'    (end {fmt(p1["x"])} {fmt(p1["y"] - tri_size)})')
    lines.append(f'    (layer "F.Fab") (stroke (width 0.1) (type solid))')
    lines.append(f'  )')
    lines.append(f'  (fp_line')
    lines.append(f'    (start {fmt(p1["x"] + tri_size)} {fmt(p1["y"] + tri_size)})')
    lines.append(f'    (end {fmt(p1["x"])} {fmt(p1["y"] - tri_size)})')
    lines.append(f'    (layer "F.Fab") (stroke (width 0.1) (type solid))')
    lines.append(f'  )')

    # Silkscreen — body outline (same as fab for simplicity)
    lines.append(f'  (fp_rect')
    lines.append(f'    (start {fab_x1} {fab_y1}) (end {fab_x2} {fab_y2})')
    lines.append(f'    (layer "F.SilkS")')
    lines.append(f'    (stroke (width 0.12) (type solid))')
    lines.append(f'  )')

    # Courtyard
    lines.append(f'  (fp_rect')
    lines.append(f'    (start {cyd_x1} {cyd_y1}) (end {cyd_x2} {cyd_y2})')
    lines.append(f'    (layer "F.CrtYd")')
    lines.append(f'    (stroke (width 0.05) (type solid))')
    lines.append(f'  )')

    # Pads
    for pad in pads:
        num = pad["number"]
        px = fmt(pad["x"])
        py = fmt(pad["y"])
        pw = fmt(pad["width"])
        ph = fmt(pad["height"])
        shape = pad.get("shape", "rect")
        pad_type = pad.get("type", "smd")

        if pad_type == "tht":
            drill = pad.get("drill", 1.0)
            first_shape = "rect" if str(num) == "1" else "circle"
            lines.append(f'  (pad "{num}" thru_hole {first_shape}')
            lines.append(f'    (at {px} {py})')
            lines.append(f'    (size {pw} {ph})')
            lines.append(f'    (drill {fmt(drill)})')
            lines.append(f'    (layers "*.Cu" "*.Mask")')
        else:
            first_shape = "rect" if str(num) == "1" else shape
            lines.append(f'  (pad "{num}" smd {first_shape}')
            lines.append(f'    (at {px} {py})')
            lines.append(f'    (size {pw} {ph})')
            lines.append(f'    (layers "F.Cu" "F.Paste" "F.Mask")')
        lines.append(f'  )')

    lines.append(f'  (embedded_fonts no)')
    lines.append(f')')

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Generate a KiCad .kicad_mod footprint from datasheet dimensions"
    )
    parser.add_argument("--name", required=True, help="Footprint name (used as filename)")
    parser.add_argument("--library", required=True, help="Library name (output dir: footprints/<library>.pretty/)")
    parser.add_argument("--description", default="", help="Human-readable description")
    parser.add_argument("--tags", default="", help="Space-separated search tags")
    parser.add_argument(
        "--pads",
        required=True,
        help='JSON array of pad objects: [{"number":1,"x":0,"y":0,"width":3.38,"height":2.41}, ...]',
    )
    parser.add_argument("--body-width", type=float, required=True, help="Component body width in mm")
    parser.add_argument("--body-height", type=float, required=True, help="Component body height in mm")
    parser.add_argument(
        "--output-dir",
        default="footprints",
        help="Root output directory (default: footprints/)",
    )
    parser.add_argument(
        "--courtyard-clearance",
        type=float,
        default=0.25,
        help="Courtyard clearance in mm (default: 0.25)",
    )
    args = parser.parse_args()

    try:
        pads = json.loads(args.pads)
    except json.JSONDecodeError as e:
        print(f"Error: invalid --pads JSON: {e}")
        sys.exit(1)

    required_fields = {"number", "x", "y", "width", "height"}
    for i, pad in enumerate(pads):
        missing = required_fields - set(pad.keys())
        if missing:
            print(f"Error: pad[{i}] missing fields: {', '.join(sorted(missing))}")
            sys.exit(1)

    content = generate_kicad_mod(
        name=args.name,
        description=args.description,
        tags=args.tags,
        pads=pads,
        body_width=args.body_width,
        body_height=args.body_height,
        courtyard_clearance=args.courtyard_clearance,
    )

    out_dir = pathlib.Path(args.output_dir) / f"{args.library}.pretty"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.name}.kicad_mod"
    out_file.write_text(content)
    print(f"✓ Generated: {out_file}")
    print(f"  Use in SKiDL: footprint='{args.library}:{args.name}'")


if __name__ == "__main__":
    main()
