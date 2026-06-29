import plugin, { MedNotesFSM, server } from "./mednotes_hook/adapters/opencode_plugin.mjs";
import { syncMedNotesOpenCodeUserConfig } from "./mednotes_hook/adapters/opencode_user_config_sync.mjs";

syncMedNotesOpenCodeUserConfig();

export { MedNotesFSM, server };
export default plugin;
