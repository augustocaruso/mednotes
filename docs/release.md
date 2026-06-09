# Release Process

Releases are tag-driven and produce two artifacts:

- an Antigravity plugin zip
- an opencode npm package tarball

## Local preflight

```bash
python3 scripts/verify.py
```

This runs tests, builds both adapters, validates the Antigravity bundle with the
local `agy` binary, dry-runs the npm package, and checks the public guard.

## Create a release

```bash
git tag v0.1.0
git push origin v0.1.0
```

The GitHub release workflow builds artifacts and publishes them to the release.

## Publish npm

The release workflow creates the npm tarball but does not publish it. Publishing
requires an npm token and should be enabled only after the package scope is
confirmed.

## Antigravity install source

The intended public install command is:

```bash
agy plugin install https://github.com/augustocaruso/mednotes
```

Before the first public release, verify the exact source format against the
current Antigravity CLI version and update this file if the command changes.
