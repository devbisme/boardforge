# Vendored: kicad-happy

These files are copied from **kicad-happy** by Andrew Klofas, used under the MIT License
(see `LICENSE` in this directory).

- Source: https://github.com/aklofas/kicad-happy
- Version: 2.3.1, commit `06840467ad0f5d76af45f64449b1f5ecb10f052f` (2026-10-05)
- Copied: 2026-10-11, from the installed Claude Code plugin
  (`~/.claude/plugins/cache/kicad-happy/kicad-happy/2.3.1/`)
- Contents: the `skills/` tree, unmodified, minus `__pycache__`; plus `how-it-works.md`.
  The tree layout is kept because the scripts find each other by relative paths.

## Rules for this directory
- Leave the upstream files unmodified, so later versions can be diffed and merged in cleanly.
  boardsmith code calls the scripts as subprocesses or wraps them; it doesn't edit them.
- If a local patch can't be avoided, list it below with the reason.
- To update: copy the new upstream `skills/` over this tree, then update the version and
  commit above.

## Optional dependencies
Most scripts use only the Python standard library. A few datasheet/review validators import
`jsonschema` and `referencing`; boardsmith lists these as optional extras.

## Local patches
None.
