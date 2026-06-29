import { existsSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// OpenCode imports the project plugin before user agents are invoked. This
// adapter uses that boot point to apply only runtime knobs from the user TOML:
// specialist model ids and reasoning effort. FSM state and workflow policy do
// not live here.
const FIELD_TO_AGENT = new Map([
  ["med_chat_triager", "med-chat-triager"],
  ["med_flashcard_maker", "med-flashcard-maker"],
  ["med_knowledge_architect", "med-knowledge-architect"],
  ["med_link_graph_curator", "med-link-graph-curator"],
  ["med_publish_guard", "med-publish-guard"],
]);
const REASONING_EFFORTS = new Set(["minimal", "low", "medium", "high"]);

export function syncMedNotesOpenCodeUserConfig(projectRoot = opencodeProjectRoot()) {
  if (autoSyncDisabled()) return { status: "disabled", agents: [] };
  const runtimeByAgent = readRuntimeOverrides(projectRoot);
  applyRuntimeOverridesToFiles(projectRoot, runtimeByAgent);
  return {
    status: "synced",
    project_root: projectRoot,
    agents: Object.keys(runtimeByAgent).sort(),
  };
}

export function applyMedNotesOpenCodeRuntimeConfig(config, projectRoot = opencodeProjectRoot()) {
  const runtimeByAgent = readRuntimeOverrides(projectRoot);
  config.agent = config.agent && typeof config.agent === "object" ? config.agent : {};
  for (const [agentId, runtimeConfig] of Object.entries(runtimeByAgent)) {
    const entry = config.agent[agentId] && typeof config.agent[agentId] === "object" ? config.agent[agentId] : {};
    config.agent[agentId] = { ...entry, ...runtimeConfig };
  }
}

function readRuntimeOverrides(projectRoot) {
  const configPath = findConfigPath(projectRoot);
  if (!configPath || !existsSync(configPath)) return {};
  return parseAgentRuntimeConfig(readFileSync(configPath, "utf8"), configPath);
}

function applyRuntimeOverridesToFiles(projectRoot, runtimeByAgent) {
  if (Object.keys(runtimeByAgent).length === 0) return;
  updateOpenCodeJson(path.join(projectRoot, ".opencode", "opencode.json"), runtimeByAgent);
  for (const [agentId, runtimeConfig] of Object.entries(runtimeByAgent)) {
    updateAgentMarkdown(path.join(projectRoot, ".opencode", "agents", `${agentId}.md`), runtimeConfig);
  }
}

function updateOpenCodeJson(configPath, runtimeByAgent) {
  if (!existsSync(configPath)) return;
  const parsed = JSON.parse(readFileSync(configPath, "utf8"));
  parsed.agent = parsed.agent && typeof parsed.agent === "object" ? parsed.agent : {};
  for (const [agentId, runtimeConfig] of Object.entries(runtimeByAgent)) {
    const entry = parsed.agent[agentId] && typeof parsed.agent[agentId] === "object" ? parsed.agent[agentId] : {};
    parsed.agent[agentId] = { ...entry, ...runtimeConfig };
  }
  writeIfChanged(configPath, `${JSON.stringify(parsed, null, 2)}\n`);
}

function updateAgentMarkdown(agentPath, runtimeConfig) {
  if (!existsSync(agentPath)) return;
  let text = readFileSync(agentPath, "utf8");
  if (runtimeConfig.model) {
    text = replaceFrontmatterLine(agentPath, text, "model", runtimeConfig.model);
  }
  if (runtimeConfig.reasoningEffort) {
    text = replaceFrontmatterLine(agentPath, text, "reasoningEffort", runtimeConfig.reasoningEffort);
  }
  writeIfChanged(agentPath, text);
}

function replaceFrontmatterLine(agentPath, text, key, value) {
  const pattern = new RegExp(`^${key}: .+$`, "m");
  if (!pattern.test(text)) {
    throw new Error(`MedNotes OpenCode auto-sync could not find ${key} in ${agentPath}`);
  }
  return text.replace(pattern, `${key}: ${frontmatterScalar(value)}`);
}

function parseAgentRuntimeConfig(text, configPath) {
  // The MedNotes config template emits simple [agents.<name>] TOML sections.
  // Keeping this parser narrow avoids making plugin startup depend on Python or
  // a package install before OpenCode opens.
  const runtimeByAgent = {};
  let currentAgentId = "";
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;

    const section = line.match(/^\[agents\.([A-Za-z0-9_]+)\]$/);
    if (section) {
      currentAgentId = FIELD_TO_AGENT.get(section[1]) || "";
      continue;
    }
    if (line.startsWith("[")) {
      currentAgentId = "";
      continue;
    }
    if (!currentAgentId) continue;

    const assignment = line.match(/^([A-Za-z0-9_]+)\s*=\s*(.+)$/);
    if (!assignment) continue;
    const key = assignment[1];
    const value = parseTomlStringValue(assignment[2], configPath);
    if (key === "model") {
      if (!value) throw new Error(`MedNotes OpenCode config has empty model for ${currentAgentId}`);
      runtimeByAgent[currentAgentId] = { ...(runtimeByAgent[currentAgentId] || {}), model: value };
    }
    if (key === "reasoning_effort") {
      if (!REASONING_EFFORTS.has(value)) {
        throw new Error(`MedNotes OpenCode config has invalid reasoning_effort for ${currentAgentId}: ${value}`);
      }
      runtimeByAgent[currentAgentId] = { ...(runtimeByAgent[currentAgentId] || {}), reasoningEffort: value };
    }
  }
  return runtimeByAgent;
}

function parseTomlStringValue(rawValue, configPath) {
  const value = rawValue.trim();
  if (value.startsWith('"')) {
    let escaped = false;
    for (let index = 1; index < value.length; index += 1) {
      const char = value[index];
      if (escaped) {
        escaped = false;
        continue;
      }
      if (char === "\\") {
        escaped = true;
        continue;
      }
      if (char === '"') return JSON.parse(value.slice(0, index + 1));
    }
    throw new Error(`Invalid quoted TOML string in ${configPath}`);
  }
  if (value.startsWith("'")) {
    const end = value.indexOf("'", 1);
    if (end > 0) return value.slice(1, end);
    throw new Error(`Invalid literal TOML string in ${configPath}`);
  }
  return value.split("#", 1)[0].trim();
}

function findConfigPath(projectRoot) {
  if (process.env.MEDNOTES_CONFIG) return path.resolve(process.env.MEDNOTES_CONFIG);
  if (process.env.MEDNOTES_HOME) return path.resolve(process.env.MEDNOTES_HOME, "config.toml");
  for (let current = path.resolve(projectRoot); ; current = path.dirname(current)) {
    const candidate = path.join(current, "config.toml");
    if (existsSync(candidate)) return candidate;
    if (current === path.dirname(current)) break;
  }
  return path.join(os.homedir(), ".mednotes", "config.toml");
}

function opencodeProjectRoot() {
  const adapterDir = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(adapterDir, "..", "..", "..", "..");
}

function autoSyncDisabled() {
  return /^(0|false|no)$/i.test(process.env.MEDNOTES_OPENCODE_AUTO_SYNC || "");
}

function frontmatterScalar(value) {
  const text = String(value);
  return /^[A-Za-z0-9_./:@-]+$/.test(text) ? text : JSON.stringify(text);
}

function writeIfChanged(filePath, nextText) {
  const current = existsSync(filePath) ? readFileSync(filePath, "utf8") : "";
  if (current !== nextText) writeFileSync(filePath, nextText, "utf8");
}
