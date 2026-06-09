# Contributing

Thanks for helping improve MedNotes.

## Ground rules

- Do not include patient-identifying information.
- Keep `core/` as the source of truth for shared behavior.
- Keep runtime-specific behavior inside `adapters/`.
- Add or update tests for behavior changes.
- Run `python3 scripts/verify.py` before opening a pull request.

## Local workflow

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verify.py
```

If you do not have Antigravity CLI installed, use this only for CI-like checks:

```bash
python3 scripts/verify.py --skip-agy
```

## Release workflow

Releases are tag-driven. See [docs/release.md](docs/release.md).
