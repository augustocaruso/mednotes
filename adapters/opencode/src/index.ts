import type { Plugin } from "@opencode-ai/plugin"

export const MedNotesPlugin: Plugin = async ({ $, directory }) => {
  return {
    "tool.execute.before": async () => {
      await $`python3 ${directory}/core/scripts/public_guard.py --root ${directory} --json`
    },
  }
}

export default MedNotesPlugin
