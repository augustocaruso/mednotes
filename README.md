# mednotes

[![CI](https://github.com/augustocaruso/mednotes/actions/workflows/ci.yml/badge.svg)](https://github.com/augustocaruso/mednotes/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-public_export-blue)](CHANGELOG.md)

Public medical-study skills, agents, and hooks for terminal AI workflows.

This repository is generated from a private canonical repository. The public
tree contains only files promoted through the MedNotes allowlist export.

`mednotes` is built for one practical goal: help medical students turn public
study material into clearer, safer, more testable understanding. It does not
process private patient information and should not be used for real-patient
diagnosis or treatment decisions.

## What this repo is

The repository separates source from distribution:

- `core/` is the shared source of truth: skills, agents, and Python hook logic.
- `adapters/antigravity/` builds a plugin bundle for Antigravity CLI.
- `adapters/opencode/` builds an npm package for opencode.
- `dist/` is generated output and is intentionally ignored by Git.

This keeps the medical-study behavior in one place while letting each runtime
have the packaging format it expects.

## Install

### Antigravity

Current local build:

```bash
python3 adapters/antigravity/build.py --output dist/antigravity/mednotes
agy plugin validate dist/antigravity/mednotes
```

Future public install path:

```bash
agy plugin install https://github.com/augustocaruso/mednotes
```

The exact install source is kept in the release checklist because Antigravity's
public plugin distribution surface is still moving.

### opencode

After the npm package is published, add it to `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@augusto/mednotes"]
}
```

Local package build:

```bash
python3 adapters/opencode/build.py --output dist/opencode/package
cd dist/opencode/package
npm pack --dry-run
```

## Verify

Run the full local readiness check:

```bash
python3 scripts/verify.py
```

CI runs the same core checks with `--skip-agy`, because GitHub-hosted runners do
not have Antigravity CLI installed by default.

## Why Gemini CLI is not a target

Gemini CLI extensions are intentionally not maintained as a live adapter here.
Google announced that Antigravity CLI keeps the critical Gemini CLI features,
including Agent Skills, Hooks, Subagents, and Extensions as plugins, while
individual/free/Google AI Pro/Ultra Gemini CLI access transitions on June 18,
2026. This project treats Antigravity as the successor target.

## Safety boundary

This repository uses an allowlist mindset:

- public educational material belongs here
- experiments and WIP belong outside this repo
- patient-identifying information must never be committed or packaged

`core/scripts/public_guard.py` catches obvious private paths and secret markers
before local packaging or release.

## Project docs

- [Architecture](docs/architecture.md)
- [Release process](docs/release.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
