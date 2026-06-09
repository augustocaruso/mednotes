# Architecture

MedNotes uses one source tree and multiple distribution adapters.

## Core

`core/` contains the behavior that should not be duplicated:

- `core/skills/`: reusable agent skills
- `core/agents/`: agent personas and operating instructions
- `core/scripts/`: Python scripts invoked by runtime hooks

The first shared script is `public_guard.py`, which checks for obvious private
or secret-bearing material before packaging or release.

## Antigravity adapter

`adapters/antigravity/` builds a plugin bundle:

```text
plugin.json
hooks.json
hooks/hooks.json
skills/
agents/
scripts/
```

The builder copies from `core/` into `dist/antigravity/mednotes`. Generated
bundles are release artifacts, not source files.

## opencode adapter

`adapters/opencode/` builds an npm package directory:

```text
package.json
README.md
src/index.ts
core/
```

The TypeScript plugin is intentionally thin. It locates the packaged
`core/scripts/public_guard.py` relative to its own module URL and calls it with
the active project directory.

## Distribution rule

No adapter owns medical-study behavior. Adapters only translate the host's
packaging and hook API into calls against `core/`.
