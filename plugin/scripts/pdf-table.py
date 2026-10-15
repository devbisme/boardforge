#!/usr/bin/env python3
"""Extract a column-aligned table (pinouts, ball maps) from a PDF into CSV.

Large vendor pin tables — FPGA packages, MCUs, BGAs — are where the datasheet phase
spends the most turns and makes its costliest mistakes: reading them page by page into
context is slow, and hand-counting 88 pins across banks is how a 48-pin I/O budget got
reported as 71. This script does it deterministically in one call.

    pdf-table.py UG803.pdf --pages 7-17 --cols "Pin Name,BANK,QN88P" --key QN88P
    pdf-table.py UG803.pdf --pages 7-17 --cols "Pin Name,BANK,QN88P" --key QN88P \\
                 --summary-by BANK            # per-bank pin counts: the I/O budget
    pdf-table.py ... --out pins.csv           # CSV file instead of stdout

How it works: `pdftotext -layout` keeps columns aligned. Every line that contains all
the requested column labels is treated as a header and re-measured, because vendor PDFs
shift column positions from page to page. Each text token on a row is assigned to the
column whose header span contains the token's start — every header cell is measured,
not only the requested ones, so unrequested columns cannot bleed into requested ones.
Rows whose `--key` value does not look like a pin/ball ID are skipped: that drops pins
not bonded out in the package, page footers, and cells such as "GND" notes.

Wrapped cells: a first-column name too long for its cell spills onto the lines above
and below the row (`IOL5A/JTAGSEL_N/LPLL_T` / `_in`). When a keyed row has an empty
first column, those fragments are stitched back together.

Column labels match by prefix, so "QN88P" matches a header cell reading "QN88P[2]".
Requires `pdftotext` (poppler-utils).
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from collections import Counter

SLACK = 2          # values often start a character or two left of their header


def pdf_lines(pdf: str, pages: str | None) -> list[str]:
    cmd = ["pdftotext", "-layout"]
    if pages:
        first, _, last = pages.partition("-")
        cmd += ["-f", first, "-l", last or first]
    cmd += [pdf, "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("pdftotext not found — install poppler-utils")
    except subprocess.CalledProcessError as e:
        sys.exit(f"pdftotext failed: {e.stderr.strip() or e}")
    return out.splitlines()


def words(line: str) -> list[tuple[int, str]]:
    """(start column, text) for every whitespace-separated word on a data row."""
    return [(m.start(), m.group()) for m in re.finditer(r"\S+", line)]


def header_spans(line: str, labels: list[str]) -> list[tuple[str, int]] | None:
    """Every column boundary on a header line, as (label, start), if it names all
    `labels`; otherwise None.

    A boundary goes at EVERY header word, not only at the requested labels: vendor
    tables often separate header cells by a single space ("QN88[1] QN88P[2] MG100P[2]"),
    and an unmeasured column's values would bleed into its left neighbour. The only
    words skipped are the continuation words of a requested multi-word label ("Name" in
    "Pin Name"), which owns its whole span.
    """
    starts: dict[int, str] = {}
    owned: list[range] = []
    for label in labels:
        m = re.search(r"(?<!\S)" + re.escape(label), line)
        if not m:
            return None
        starts[m.start()] = label
        owned.append(range(m.start() + 1, m.start() + len(label)))
    for pos, word in words(line):
        if not any(pos in r for r in owned):
            starts.setdefault(pos, word)
    return sorted(((lab, pos) for pos, lab in starts.items()), key=lambda x: x[1])


def add_label_lines(spans: list[tuple[str, int]], lines: list[str], i: int,
                    labels: list[str], key: str, key_re: re.Pattern) -> list[tuple[str, int]]:
    """Add boundaries from multi-line header labels on the lines just above/below the
    header ("Configuration" / "Function" stacked around it). Only positions past the
    first column are taken, so note text such as "[3] The pin is..." cannot split it."""
    extra = dict((pos, lab) for lab, pos in spans)
    first_end = spans[1][1] if len(spans) > 1 else 0
    for j in (i - 1, i + 1):
        if not (0 <= j < len(lines)) or header_spans(lines[j], labels):
            continue
        if key_re.fullmatch(assign(lines[j], spans).get(key, "")):
            continue                                  # a data row, not a label line
        for pos, word in words(lines[j]):
            if pos >= first_end:
                extra.setdefault(pos, word)
    return sorted(((lab, pos) for pos, lab in extra.items()), key=lambda x: x[1])


def assign(line: str, spans: list[tuple[str, int]]) -> dict[str, str]:
    """Map each word on the row to the column whose header span contains its start."""
    row: dict[str, str] = {}
    for pos, text in words(line):
        label = None
        for lab, start in spans:                    # last column starting at/before pos
            if pos >= start - SLACK:
                label = lab
            else:
                break
        if label is not None:
            row[label] = (row.get(label, "") + " " + text).strip()
    return row


def first_col_text(line: str, spans: list[tuple[str, int]]) -> str:
    end = spans[1][1] - SLACK if len(spans) > 1 else len(line)
    return " ".join(t for p, t in words(line) if p < end)


def extract(lines: list[str], labels: list[str], key: str,
            key_re: re.Pattern) -> list[dict[str, str]]:
    rows, spans = [], None
    first_label = labels[0]

    def keyed(i: int) -> bool:
        return 0 <= i < len(lines) and bool(key_re.fullmatch(assign(lines[i], spans).get(key, "")))

    for i, line in enumerate(lines):
        h = header_spans(line, labels)
        if h:
            spans = add_label_lines(h, lines, i, labels, key, key_re)
            continue
        if not spans or not line.strip():
            continue
        row = assign(line, spans)
        if not key_re.fullmatch(row.get(key, "")):
            continue                               # footers, notes, unbonded pins
        if not row.get(first_label):
            # Wrapped name: stitch the first-column text of the non-data lines around it
            parts = [first_col_text(lines[j], spans) for j in (i - 1, i + 1)
                     if 0 <= j < len(lines) and not keyed(j) and not header_spans(lines[j], labels)]
            name = "".join(p for p in parts if p)
            if name:
                row[first_label] = name
        rows.append({lab: row.get(lab, "") for lab in labels})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--pages", help="page or range, e.g. 7-17")
    ap.add_argument("--cols", required=True,
                    help='comma-separated header labels, first = row name, e.g. "Pin Name,BANK,QN88P"')
    ap.add_argument("--key", help="column that must be non-empty for a row to count "
                                   "(default: last of --cols)")
    ap.add_argument("--key-pattern", default=r"[A-Z]{0,2}\d{1,3}",
                    help="regex a --key value must fully match (default: pin number or "
                         "ball ID like 12, A1, K10). Filters page footers and notes.")
    ap.add_argument("--summary-by", metavar="COL", help="also print row counts per value of COL")
    ap.add_argument("--out", help="write CSV here instead of stdout")
    args = ap.parse_args()

    labels = [c.strip() for c in args.cols.split(",") if c.strip()]
    if len(labels) < 2:
        sys.exit("--cols needs at least two labels")
    key = args.key or labels[-1]
    if key not in labels:
        sys.exit(f"--key {key!r} must be one of --cols")

    rows = extract(pdf_lines(args.pdf, args.pages), labels, key, re.compile(args.key_pattern))
    if not rows:
        sys.exit("no rows found — check --cols labels against the PDF's header text "
                 "(`pdftotext -layout -f N -l N file.pdf - | less`)")

    # Sort numerically by key when it is a pin number
    if all(re.fullmatch(r"\d+", r.get(key, "")) for r in rows):
        rows.sort(key=lambda r: int(r[key]))
    # Vendor PDFs often repeat a table (different section, different spacing). Drop
    # exact repeats silently; a key appearing with DIFFERENT values is a real conflict.
    seen, unique = set(), []
    for r in rows:
        sig = tuple(r.get(lab, "") for lab in labels)
        if sig not in seen:
            seen.add(sig)
            unique.append(r)
    rows = unique
    dupes = [v for v, n in Counter(r[key] for r in rows).items() if n > 1]

    fh = open(args.out, "w", newline="") if args.out else sys.stdout
    w = csv.DictWriter(fh, fieldnames=labels, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    if args.out:
        fh.close()

    err = sys.stderr
    print(f"{len(rows)} rows" + (f" -> {args.out}" if args.out else ""), file=err)
    if dupes:
        print(f"WARNING: {key} values with conflicting rows (check by hand): {dupes[:10]}", file=err)
    if all(re.fullmatch(r"\d+", r.get(key, "")) for r in rows):
        nums = {int(r[key]) for r in rows}
        missing = sorted(set(range(1, max(nums) + 1)) - nums)
        if missing:
            print(f"WARNING: {key} numbers missing from 1..{max(nums)}: {missing[:20]}", file=err)
    if args.summary_by:
        c = Counter(r.get(args.summary_by, "") or "(blank)" for r in rows)
        print(f"rows per {args.summary_by}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())), file=err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
