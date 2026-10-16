"""``boardforge render``: 2D pictures of the current board (placed or routed) for checking
a placement by eye. Copper, silkscreen and outline per side, with the pours left out so the
tracks show; SVG from boardforge's own kicad-cli (an older one on PATH can't read KiCad 10
boards), plus PNG when inkscape or rsvg-convert is installed."""

import os
import shutil
import subprocess

LAYERS = {"top": "F.Cu,F.Silkscreen,F.Fab,Edge.Cuts", "bottom": "B.Cu,B.Silkscreen,B.Fab,Edge.Cuts"}


def strip_zones(text):
    """The board file without its zones (pours and planes)."""
    out, i = [], 0
    while True:
        j = text.find("\n\t(zone", i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        depth, k = 0, j + 1
        while True:
            c = text[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        i = k + 1


def render(proj, kicad, sides=("top",), pours=False, dpi=150):
    """Write build/renders/<side>.svg (and .png); returns the files written."""
    pcb = proj.kfile(".kicad_pcb")
    if not os.path.exists(pcb):
        raise FileNotFoundError("no board yet: run `boardforge run --to c3` first")
    outdir = os.path.join(proj.out, "renders")
    os.makedirs(outdir, exist_ok=True)
    src = pcb
    if not pours:
        src = os.path.join(proj.work, "render.kicad_pcb")
        with open(pcb) as f, open(src, "w") as g:
            g.write(strip_zones(f.read()))
    files = []
    for side in sides:
        svg = os.path.join(outdir, f"{side}.svg")
        args = ["pcb", "export", "svg", "--layers", LAYERS[side], "--mode-single",
                "--fit-page-to-board", "--exclude-drawing-sheet", "-o", svg, src]
        if side == "bottom":
            args.insert(-1, "--mirror")
        kicad.cli_run(*args, check=True)
        files.append(svg)
        png = svg[:-4] + ".png"
        if shutil.which("rsvg-convert"):
            cmd = ["rsvg-convert", "-d", str(dpi), "-p", str(dpi), "-b", "white", "-o", png, svg]
        elif shutil.which("inkscape"):
            cmd = ["inkscape", svg, "--export-type=png", f"--export-dpi={dpi}",
                   "--export-background=white", "--export-background-opacity=1", "-o", png]
        else:
            continue
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            files.append(png)
    return files
