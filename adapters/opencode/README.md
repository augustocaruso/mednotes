# @augusto/mednotes

OpenCode adapter for MedNotes.

The package exposes a thin TypeScript plugin. The actual shared logic lives in
the packaged `core/` directory and is generated from the repository root during
the release build.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@augusto/mednotes"]
}
```
