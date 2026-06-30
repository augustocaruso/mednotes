#!/usr/bin/env node
import { existsSync } from "node:fs";
import { copyFile, cp, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PACKAGE_SPEC = "mednotes-opencode";
const OPENCODE_SCHEMA = "https://opencode.ai/config.json";
const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PACKAGE_OPENCODE = findPackagedOpenCodeDir();
const GENERATED_CONFIG = path.join(PACKAGE_OPENCODE, "opencode.json");
const MEDNOTES_RUNTIME_REF = ".opencode/mednotes";
const REQUIRED_INSTALL_FILES = [
  ["commands", "mednotes", "status.md"],
  ["agents", "med-knowledge-architect.md"],
  ["mednotes", "AGENTS.md"],
  ["plugins", "mednotes-fsm.mjs"],
];

function findPackagedOpenCodeDir() {
  const candidates = [
    path.join(PACKAGE_ROOT, ".opencode"),
    path.join(PACKAGE_ROOT, "manifests", "opencode-plugin", ".opencode"),
  ];
  for (const candidate of candidates) {
    if (existsSync(path.join(candidate, "opencode.json"))) {
      return candidate;
    }
  }
  return candidates[0];
}

function usage() {
  return [
    "Usage:",
    "  mednotes-opencode install [--dry-run] [--config <path>] [--plugin <specifier>]",
    "  mednotes-opencode doctor [--config <path>]",
  ].join("\n");
}

function configPathFromEnv() {
  if (process.env.OPENCODE_CONFIG) {
    return process.env.OPENCODE_CONFIG;
  }
  if (process.env.OPENCODE_CONFIG_DIR) {
    return path.join(process.env.OPENCODE_CONFIG_DIR, "opencode.json");
  }
  if (process.platform === "win32") {
    const appData = process.env.APPDATA ?? path.join(os.homedir(), "AppData", "Roaming");
    return path.join(appData, "opencode", "opencode.json");
  }
  const xdg = process.env.XDG_CONFIG_HOME ?? path.join(os.homedir(), ".config");
  return path.join(xdg, "opencode", "opencode.json");
}

function parseArgs(argv) {
  const args = [...argv];
  const command = args.shift() ?? "install";
  const options = {
    command,
    configPath: configPathFromEnv(),
    dryRun: false,
    pluginSpec: PACKAGE_SPEC,
  };
  while (args.length > 0) {
    const flag = args.shift();
    switch (flag) {
      case "--dry-run":
        options.dryRun = true;
        break;
      case "--config":
        options.configPath = args.shift();
        break;
      case "--plugin":
        options.pluginSpec = args.shift();
        break;
      case "-h":
      case "--help":
        options.command = "help";
        break;
      default:
        throw new Error(`Unknown argument: ${flag}`);
    }
  }
  if (!options.configPath) {
    throw new Error("--config requires a path");
  }
  if (!options.pluginSpec) {
    throw new Error("--plugin requires a specifier");
  }
  return options;
}

async function readConfig(configPath) {
  if (!existsSync(configPath)) {
    return { $schema: OPENCODE_SCHEMA, plugin: [] };
  }
  const raw = await readFile(configPath, "utf8");
  const parsed = JSON.parse(raw);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("OpenCode config must be a JSON object");
  }
  return parsed;
}

function normalizePlugins(value, pluginSpec) {
  const existing = Array.isArray(value) ? value : [];
  const preserved = existing.filter((entry) => {
    if (typeof entry !== "string") {
      return true;
    }
    return !entry.includes("mednotes-opencode") && !entry.includes("mednotes-fsm.mjs");
  });
  return [...preserved, pluginSpec];
}

function normalizeInstructions(value, instructionPath) {
  const existing = Array.isArray(value) ? value : [];
  const preserved = existing.filter((entry) => {
    if (typeof entry !== "string") {
      return true;
    }
    return !entry.includes("mednotes/AGENTS.md") && !entry.includes("mednotes\\AGENTS.md");
  });
  return [...preserved, instructionPath];
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

async function readGeneratedConfig() {
  return JSON.parse(await readFile(GENERATED_CONFIG, "utf8"));
}

function localPluginSpec(configDir) {
  return pathToFileURL(path.join(configDir, "plugins", "mednotes-fsm.mjs")).href;
}

function requiredInstallFilesPresent(configDir) {
  return REQUIRED_INSTALL_FILES.every((parts) => existsSync(path.join(configDir, ...parts)));
}

async function copyOpenCodeSurface(configDir, dryRun) {
  const installs = [
    ["commands", "commands"],
    ["agents", "agents"],
    ["mednotes", "mednotes"],
    ["plugins", "plugins"],
    ["mednotes.generated.json", "mednotes.generated.json"],
  ];
  if (dryRun) {
    return installs.map(([, target]) => path.join(configDir, target));
  }
  for (const [source, target] of installs) {
    const sourcePath = path.join(PACKAGE_OPENCODE, source);
    const targetPath = path.join(configDir, target);
    await mkdir(path.dirname(targetPath), { recursive: true });
    await cp(sourcePath, targetPath, { recursive: true, force: true });
  }
  await rewriteRuntimeReferences(path.join(configDir, "commands"), path.join(configDir, "mednotes"));
  await rewriteRuntimeReferences(path.join(configDir, "agents"), path.join(configDir, "mednotes"));
  return installs.map(([, target]) => path.join(configDir, target));
}

async function rewriteRuntimeReferences(root, runtimeRoot) {
  if (!existsSync(root)) {
    return;
  }
  const entries = await readdir(root, { withFileTypes: true });
  for (const entry of entries) {
    const entryPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      await rewriteRuntimeReferences(entryPath, runtimeRoot);
      continue;
    }
    if (!entry.isFile() || !entry.name.endsWith(".md")) {
      continue;
    }
    const current = await readFile(entryPath, "utf8");
    const next = current.replaceAll(MEDNOTES_RUNTIME_REF, runtimeRoot);
    if (next !== current) {
      await writeFile(entryPath, next, "utf8");
    }
  }
}

function mergeAgentConfig(config, generatedConfig) {
  const currentAgent = objectValue(config.agent);
  const generatedAgent = objectValue(generatedConfig.agent);
  const nextAgent = { ...currentAgent };
  for (const [agentId, runtimeConfig] of Object.entries(generatedAgent)) {
    nextAgent[agentId] = { ...objectValue(currentAgent[agentId]), ...objectValue(runtimeConfig) };
  }
  return nextAgent;
}

async function install(options) {
  const configPath = path.resolve(options.configPath);
  const configDir = path.dirname(configPath);
  const generatedConfig = await readGeneratedConfig();
  const pluginSpec = options.pluginSpec === PACKAGE_SPEC ? localPluginSpec(configDir) : options.pluginSpec;
  const assetsWereMissing = !requiredInstallFilesPresent(configDir);
  const config = await readConfig(configPath);
  const before = `${JSON.stringify(config, null, 2)}\n`;
  config.$schema = typeof config.$schema === "string" ? config.$schema : OPENCODE_SCHEMA;
  config.plugin = normalizePlugins(config.plugin, pluginSpec);
  config.instructions = normalizeInstructions(config.instructions, path.join(configDir, "mednotes", "AGENTS.md"));
  config.agent = mergeAgentConfig(config, generatedConfig);
  const after = `${JSON.stringify(config, null, 2)}\n`;
  const changed = before !== after || assetsWereMissing;
  const backupPath = `${configPath}.bak.${new Date().toISOString().replace(/[:.]/g, "-")}`;
  const installedPaths = await copyOpenCodeSurface(configDir, options.dryRun);

  if (changed && !options.dryRun) {
    await mkdir(configDir, { recursive: true });
    if (existsSync(configPath)) {
      await copyFile(configPath, backupPath);
    }
    await writeFile(configPath, after, "utf8");
  }

  return {
    status: changed ? "updated" : "already_configured",
    config_path: configPath,
    plugin: pluginSpec,
    dry_run: options.dryRun,
    installed_paths: installedPaths,
    backup_path: changed && !options.dryRun && existsSync(backupPath) ? backupPath : null,
  };
}

async function doctor(options) {
  const configPath = path.resolve(options.configPath);
  const configDir = path.dirname(configPath);
  const config = await readConfig(configPath);
  const plugin = Array.isArray(config.plugin) ? config.plugin : [];
  const pluginConfigured = plugin.some(
    (entry) => typeof entry === "string" && (entry.includes("mednotes-opencode") || entry.includes("mednotes-fsm.mjs")),
  );
  const surfaceConfigured = requiredInstallFilesPresent(configDir);
  return {
    status: pluginConfigured && surfaceConfigured ? "configured" : pluginConfigured ? "incomplete" : "missing",
    config_path: configPath,
    plugin,
    surface_configured: surfaceConfigured,
  };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  switch (options.command) {
    case "help":
      console.log(usage());
      return 0;
    case "install":
      console.log(JSON.stringify(await install(options), null, 2));
      return 0;
    case "doctor":
      console.log(JSON.stringify(await doctor(options), null, 2));
      return 0;
    default:
      throw new Error(`Unknown command: ${options.command}\n${usage()}`);
  }
}

main()
  .then((exitCode) => {
    process.exitCode = exitCode;
  })
  .catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
