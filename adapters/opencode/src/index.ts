import type { Plugin } from "@opencode-ai/plugin"
import { fileURLToPath } from "node:url"

const publicGuardPath = fileURLToPath(
  new URL("../core/scripts/public_guard.py", import.meta.url),
)

export const MedNotesPlugin: Plugin = async ({ $, directory }) => {
  return {
    "tool.execute.before": async () => {
      await $`python3 ${publicGuardPath} --root ${directory} --json`
    },
  }
}

export default MedNotesPlugin
