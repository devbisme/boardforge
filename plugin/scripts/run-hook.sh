#!/bin/bash
# Launch a Python hook script with a directly-resolved interpreter.
#
#   run-hook.sh <script.py>        (stdin is passed through untouched)
#
# Hooks fire on every tool call, so interpreter start-up is paid hundreds of times per
# pipeline run. Version-manager shims (pyenv, asdf) add ~0.5 s per call — measured 18x
# slower than calling the interpreter directly. session-start.sh resolves the real
# interpreter once and caches its path; this reads the cache and execs it. With no
# cache (or a stale one) it falls back to whatever `python3` is on PATH.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="${TMPDIR:-/tmp}/boardsmith-plugin/python"
PY="python3"
if [ -r "$CACHE" ]; then
    CANDIDATE="$(cat "$CACHE")"
    [ -x "$CANDIDATE" ] && PY="$CANDIDATE"
fi
exec "$PY" "$HERE/$1"
