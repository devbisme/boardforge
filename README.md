# boardsmith

Generate and analyze KiCad designs from a requirements prompt: explore many candidate
architectures, then turn the selected one into a routed, manufacturable board. SKiDL code is
the source of truth; runs headless (KiCad only for viewing/editing results).

**Status:** design phase. See [`docsrc/design.md`](docsrc/design.md). No code yet.

## Layout
```
src/boardsmith/   Python package (not started)
test/             pytest unit tests; test/examples/ for integrated runs
docsrc/           documentation source (design doc lives here)
docs/             compiled docs (GitHub Pages)
```

## Third-party code
`src/boardsmith/vendor/` contains code from other projects, each under its own license.
See [`src/boardsmith/vendor/README.md`](src/boardsmith/vendor/README.md).
