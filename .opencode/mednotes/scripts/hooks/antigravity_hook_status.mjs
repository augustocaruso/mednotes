#!/usr/bin/env node

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_NAME = "medical-notes-workbench";
const STATUS_SCHEMA = "medical-notes-workbench.antigravity-hook-status-report.v1";
const CURRENT_PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const GEMINI_EXTENSION_PATH_TOKEN = "${" + "extensionPath}";
const GEMINI_EXTENSION_HOME_TOKEN = "~/.gemini/" + "extensions/medical-notes-workbench";
const GEMINI_EXTENSIONS_COMMAND_TOKEN = "gemini " + "extensions ";
const STALE_RUNTIME_TOKENS = [
  GEMINI_EXTENSION_PATH_TOKEN,
  GEMINI_EXTENSION_HOME_TOKEN,
  GEMINI_EXTENSIONS_COMMAND_TOKEN,
];
const EXPECTED_AGENT_MODELS = new Map([
  ["med-chat-triager.md", "Gemini 3.5 Flash (High)"],
  ["med-publish-guard.md", "Gemini 3.5 Flash (High)"],
  ["med-link-graph-curator.md", "Gemini 3.5 Flash (High)"],
  ["med-flashcard-maker.md", "Gemini 3.1 Pro (High)"],
  ["med-knowledge-architect.md", "Gemini 3.1 Pro (High)"],
]);
const EXPECTED_MODEL_LABELS = new Set(EXPECTED_AGENT_MODELS.values());
const HOOK_STATUS_FILE = path.join(appHomeDir(), "antigravity-hooks", "status.json");

function appHomeDir() {
  return process.env.MEDNOTES_HOME || path.join(os.homedir(), ".mednotes");
}

function compactPath(value) {
  const text = String(value || "");
  if (!text) return "";
  const home = os.homedir();
  return text.startsWith(home) ? `~${text.slice(home.length)}` : text;
}

async function readJson(file) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch {
    return null;
  }
}

async function hookFileStatus(file) {
  const data = await readJson(file);
  const serialized = data ? JSON.stringify(data) : "";
  return {
    path: compactPath(file),
    exists: Boolean(data),
    antigravity_schema: Boolean(data && !Object.hasOwn(data, "hooks") && serialized.includes("PreToolUse")),
    has_pre_tool_use: serialized.includes("PreToolUse"),
    has_post_tool_use: serialized.includes("PostToolUse"),
    stale_gemini_schema: Boolean(data && Object.hasOwn(data, "hooks")),
  };
}

function frontmatterModel(text) {
  const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!match) return "";
  const modelLine = match[1].match(/^model:\s*(.+?)\s*$/m);
  if (!modelLine) return "";
  return modelLine[1].replace(/^['"]|['"]$/g, "").trim();
}

async function agentStatus(pluginRoot) {
  const agentsDir = path.join(pluginRoot, "agents");
  try {
    const entries = await fs.readdir(agentsDir, { withFileTypes: true });
    const files = entries
      .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
      .map((entry) => path.join(agentsDir, entry.name))
      .sort();
    const stale_placeholders = [];
    const models = [];
    const unexpected_models = [];
    for (const file of files) {
      const text = await fs.readFile(file, "utf8");
      for (const token of STALE_RUNTIME_TOKENS) {
        if (text.includes(token)) stale_placeholders.push({ path: compactPath(file), token });
      }
      const filename = path.basename(file);
      const model = frontmatterModel(text);
      const expected = EXPECTED_AGENT_MODELS.get(filename) || "";
      models.push({ path: compactPath(file), model, expected });
      if (expected && model !== expected) {
        unexpected_models.push({ path: compactPath(file), model, expected });
      }
    }
    const model_status = files.length > 0 && unexpected_models.length === 0 ? "ok" : "needs_repair";
    return {
      status: files.length > 0 && stale_placeholders.length === 0 && model_status === "ok" ? "ok" : "needs_repair",
      plugin_root: compactPath(pluginRoot),
      count: files.length,
      files: files.map(compactPath),
      stale_placeholders,
      model_status,
      models,
      unexpected_models,
    };
  } catch {
    return {
      status: "missing",
      plugin_root: compactPath(pluginRoot),
      count: 0,
      files: [],
      stale_placeholders: [],
      model_status: "missing",
      models: [],
      unexpected_models: [],
    };
  }
}

async function selectedModelStatus() {
  const settingsPath = path.join(os.homedir(), ".gemini", "antigravity-cli", "settings.json");
  const settings = await readJson(settingsPath);
  const model = typeof settings?.model === "string" ? settings.model : "";
  return {
    path: compactPath(settingsPath),
    status: model ? "selected" : "missing",
    model,
    matches_plugin_agent_models: model ? EXPECTED_MODEL_LABELS.has(model) : false,
  };
}

async function main(argv = process.argv.slice(2)) {
  const json = argv.includes("--json");
  const configHook = path.join(os.homedir(), ".gemini", "config", "plugins", PLUGIN_NAME, "hooks.json");
  const configNestedHook = path.join(os.homedir(), ".gemini", "config", "plugins", PLUGIN_NAME, "hooks", "hooks.json");
  const importHook = path.join(os.homedir(), ".gemini", "antigravity-cli", "plugins", PLUGIN_NAME, "hooks.json");
  const importNestedHook = path.join(
    os.homedir(),
    ".gemini",
    "antigravity-cli",
    "plugins",
    PLUGIN_NAME,
    "hooks",
    "hooks.json",
  );
  const status = await readJson(HOOK_STATUS_FILE);
  const hook_files = [
    await hookFileStatus(configHook),
    await hookFileStatus(configNestedHook),
    await hookFileStatus(importHook),
    await hookFileStatus(importNestedHook),
  ];
  const agents = await agentStatus(CURRENT_PLUGIN_ROOT);
  const selected_model = await selectedModelStatus();
  const installed = hook_files.some((item) => item.exists && item.antigravity_schema);
  const stale = hook_files.some((item) => item.exists && item.stale_gemini_schema);
  const invocation_seen = Boolean(status?.last_hook?.mode);
  const report = {
    schema: STATUS_SCHEMA,
    status:
      installed && invocation_seen && !stale && agents.status === "ok"
        ? "ok"
        : installed && !stale && agents.status === "ok"
          ? "installed_no_invocation_seen"
          : "needs_repair",
    hook_files,
    agents,
    selected_model,
    heartbeat: status || null,
    next_action: invocation_seen
      ? "Hooks Antigravity foram executados neste ambiente."
      : "Abra ou reinicie a TUI e execute uma ferramenta simples, por exemplo um comando shell; depois rode este status novamente.",
  };

  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }

  if (report.status === "ok") {
    const last = report.heartbeat.last_hook;
    process.stdout.write(`Hooks Antigravity: OK\n`);
    process.stdout.write(`Ultima execucao: ${report.heartbeat.updated_at}\n`);
    process.stdout.write(`Hook: ${last.mode} (${last.hook_event_name || "evento desconhecido"})\n`);
    process.stdout.write(`Ferramenta: ${last.tool_name || "desconhecida"}\n`);
    process.stdout.write(`Plugin root: ${last.plugin_root || "desconhecido"}\n`);
    process.stdout.write(`Agents: OK (${report.agents.count} arquivos, sem placeholder Gemini antigo)\n`);
    process.stdout.write(`Agent models: OK (${report.agents.model_status})\n`);
    process.stdout.write(`Modelo selecionado: ${report.selected_model.model || "nao detectado"}\n`);
  } else if (report.status === "installed_no_invocation_seen") {
    process.stdout.write("Hooks Antigravity: instalados, mas nenhuma execucao foi registrada ainda.\n");
    process.stdout.write(`Agents: OK (${report.agents.count} arquivos, sem placeholder Gemini antigo)\n`);
    process.stdout.write(`Agent models: OK (${report.agents.model_status})\n`);
    process.stdout.write(`Modelo selecionado: ${report.selected_model.model || "nao detectado"}\n`);
    process.stdout.write(`${report.next_action}\n`);
  } else {
    process.stdout.write("Hooks Antigravity: precisam de reparo.\n");
    for (const item of hook_files) {
      process.stdout.write(
        `- ${item.path}: ${item.exists ? (item.stale_gemini_schema ? "schema Gemini antigo" : "schema Antigravity") : "ausente"}\n`,
      );
    }
    process.stdout.write(`Agents: ${report.agents.status} (${report.agents.count} arquivos)\n`);
    for (const item of report.agents.stale_placeholders) {
      process.stdout.write(`- ${item.path}: placeholder antigo ${item.token}\n`);
    }
    for (const item of report.agents.unexpected_models) {
      process.stdout.write(`- ${item.path}: modelo ${item.model || "ausente"}, esperado ${item.expected}\n`);
    }
    process.stdout.write(`Modelo selecionado: ${report.selected_model.model || "nao detectado"}\n`);
  }
}

await main();
