#!/usr/bin/env python3
"""PreToolUse:Bash guard — refuse shell commands that destroy existing design files.

Replaces an LLM `prompt` hook that ran a model call before *every* Bash command (710
times in one measured pipeline run) and still misjudged cases, e.g. treating the
creation of a brand-new file as an overwrite. This check is deterministic, instant,
and free.

Protected: any EXISTING file or directory under a path component named `datasheets`,
`circuits`, `sourcing`, `symbols` or `footprints`. `build/` is not protected: boardforge
regenerates it. A command is blocked when it would delete or
overwrite one — never merely for mentioning it. Creating new files there is allowed.

Scope, stated plainly: this reads the shell command text. It sees `rm`, `mv`, `cp`,
`>`, `sed -i`, `tee`, `truncate`, `dd of=`, `find -delete`, `curl -o`, `wget -O` and
friends. It cannot see a write performed *inside* a script (`python3 -c
"open(p, 'w')"`); circuit files are meant to be edited with the Write/Edit tools,
which is what triggers the post-write validator.

Protocol: JSON on stdin (`tool_input.command`, `cwd`). Exit 2 blocks the call and
feeds stderr back to Claude; exit 0 allows it.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import sys
from pathlib import Path

PROTECTED = {"datasheets", "circuits", "sourcing", "symbols", "footprints"}

# Commands whose every path operand is destroyed or overwritten if it exists.
DESTROY_ALL = {"rm", "rmdir", "unlink", "shred", "truncate"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "{", "}"}
REDIRECT_OVERWRITE = {">", ">|", "&>"}        # `>>` appends and is allowed


def protected(path: Path) -> bool:
    """True if `path` exists and lives in (or is) a protected directory."""
    try:
        p = path.resolve()
    except OSError:
        return False
    if not p.exists():
        return False
    return bool(PROTECTED & set(p.parts[:-1])) or (p.is_dir() and p.name in PROTECTED)


def expand(token: str, base: Path) -> list[Path]:
    """Resolve a path operand against `base`, expanding globs."""
    token = os.path.expanduser(token)
    p = token if os.path.isabs(token) else str(base / token)
    hits = glob.glob(p) if any(c in token for c in "*?[") else [p]
    return [Path(h) for h in hits]


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies: they are data (often Python), not shell to analyse."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        if m:
            term = m.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                i += 1
        i += 1
    return "\n".join(out)


def split_commands(tokens: list[str]) -> list[list[str]]:
    cmds, cur = [], []
    for t in tokens:
        if t in SEPARATORS or t == "\n":
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


def victims(argv: list[str], base: Path) -> tuple[list[Path], Path]:
    """Paths this simple command would destroy, and the cwd after it runs."""
    hits: list[Path] = []

    # Redirections anywhere in the command: `> file`, `2> file`, `&> file`.
    clean: list[str] = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t in REDIRECT_OVERWRITE or re.fullmatch(r"\d>", t):
            if i + 1 < len(argv) and not argv[i + 1].startswith("&"):
                hits += expand(argv[i + 1], base)
            i += 2
            continue
        if t in (">>", "<", "<<", "<<<") or re.fullmatch(r"\d>>|\d>&\d?|>&\d?", t):
            i += 2 if t in (">>", "<", "<<<") else 1
            continue
        clean.append(t)
        i += 1

    # Skip leading env assignments and wrappers (sudo, env, nice, time, command).
    while clean and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", clean[0])
                     or clean[0] in {"sudo", "env", "nice", "time", "command", "nohup"}):
        clean = clean[1:]
    if not clean:
        return hits, base

    name, args = os.path.basename(clean[0]), clean[1:]
    operands = [a for a in args if not a.startswith("-")]

    if name == "cd":
        target = operands[0] if operands else str(Path.home())
        new = Path(os.path.expanduser(target))
        return hits, (new if new.is_absolute() else base / new)

    if name in DESTROY_ALL:
        for a in operands:
            hits += expand(a, base)
    elif name in ("mv", "cp", "install", "rsync"):
        dest_i = next((k for k, a in enumerate(args) if a in ("-t", "--target-directory")), None)
        if dest_i is not None and dest_i + 1 < len(args):
            dest, srcs = args[dest_i + 1], [a for a in operands if a != args[dest_i + 1]]
        elif len(operands) >= 2:
            dest, srcs = operands[-1], operands[:-1]
        else:
            dest, srcs = None, []
        if name == "mv":                      # moving a file out deletes it
            for s in srcs:
                hits += expand(s, base)
        if dest is not None:
            for d in expand(dest, base):
                if d.is_dir():                # cp/mv INTO a dir overwrites dir/<name>
                    for s in srcs:
                        hits.append(d / Path(s).name)
                else:
                    hits.append(d)
    elif name in ("sed", "perl", "ruby") and any(re.fullmatch(r"-i\S*|--in-place\S*", a) for a in args):
        # The script expression is the first non-option operand unless -e/-f supplied it.
        has_e = any(a in ("-e", "-f") or a.startswith("--expression") for a in args)
        files = operands if has_e else operands[1:]
        for f in files:
            hits += expand(f, base)
    elif name == "tee" and not any(a in ("-a", "--append") for a in args):
        for a in operands:
            hits += expand(a, base)
    elif name == "dd":
        for a in args:
            if a.startswith("of="):
                hits += expand(a[3:], base)
    elif name == "find" and ("-delete" in args or any(
            a == "-exec" and k + 1 < len(args) and os.path.basename(args[k + 1]) in DESTROY_ALL
            for k, a in enumerate(args))):
        roots = []
        for a in args:
            if a.startswith("-") or a in ("(", ")", "!"):
                break
            roots.append(a)
        for r in roots or ["."]:
            hits += expand(r, base)
    elif name in ("curl", "wget"):
        flags = ("-o", "--output") if name == "curl" else ("-O", "--output-document")
        for k, a in enumerate(args):
            if a in flags and k + 1 < len(args):
                hits += expand(args[k + 1], base)
            elif any(a.startswith(f + "=") for f in flags if f.startswith("--")):
                hits += expand(a.split("=", 1)[1], base)

    return hits, base


def check(command: str, cwd: str) -> list[Path]:
    base = Path(cwd or os.getcwd())
    text = strip_heredocs(command)
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        # Unparseable (unbalanced quotes). Fall back to a coarse textual check rather
        # than silently allowing an rm against a protected directory.
        if re.search(r"\b(rm|shred|truncate)\b[^|;&]*\b(" + "|".join(PROTECTED) + r")/", text):
            return [Path("<unparseable command touching a protected directory>")]
        return []
    bad: list[Path] = []
    for argv in split_commands(tokens):
        hits, base = victims(argv, base)
        bad += [h for h in hits if protected(h)]
    return bad


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0                              # not our input shape — do not interfere
    command = (data.get("tool_input") or {}).get("command", "")
    if not command:
        return 0
    bad = check(command, data.get("cwd", ""))
    if not bad:
        return 0
    names = sorted({str(p) for p in bad})[:8]
    print(
        "Blocked: this command would delete or overwrite existing design files:\n  "
        + "\n  ".join(names)
        + "\n\nFiles under datasheets/, circuits/, sourcing/, symbols/ and footprints/ are "
        "protected. Edit circuit files with the Write/Edit tools (that runs the validator), "
        "add datasheet PDFs with `boardforge datasheets add`, write new files under a new "
        "name, or record stale files in your handoff. If the user really wants this removed, "
        "ask them to run the command themselves with the `!` prefix.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
