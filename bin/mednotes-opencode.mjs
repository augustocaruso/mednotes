#!/usr/bin/env node
import { existsSync } from "node:fs";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const PACKAGE_SPEC = "mednotes-opencode";
const OPENCODE_SCHEMA = "https://opencode.ai/config.json";

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

async function install(options) {
  const configPath = path.resolve(options.configPath);
  const config = await readConfig(configPath);
  const before = `${JSON.stringify(config, null, 2)}\n`;
  config.$schema = typeof config.$schema === "string" ? config.$schema : OPENCODE_SCHEMA;
  config.plugin = normalizePlugins(config.plugin, options.pluginSpec);
  const after = `${JSON.stringify(config, null, 2)}\n`;
  const changed = before !== after;
  const backupPath = `${configPath}.bak.${new Date().toISOString().replace(/[:.]/g, "-")}`;

  if (changed && !options.dryRun) {
    await mkdir(path.dirname(configPath), { recursive: true });
    if (existsSync(configPath)) {
      await copyFile(configPath, backupPath);
    }
    await writeFile(configPath, after, "utf8");
  }

  return {
    status: changed ? "updated" : "already_configured",
    config_path: configPath,
    plugin: options.pluginSpec,
    dry_run: options.dryRun,
    backup_path: changed && !options.dryRun && existsSync(backupPath) ? backupPath : null,
  };
}

async function doctor(options) {
  const configPath = path.resolve(options.configPath);
  const config = await readConfig(configPath);
  const plugin = Array.isArray(config.plugin) ? config.plugin : [];
  return {
    status: plugin.some((entry) => typeof entry === "string" && entry.includes("mednotes-opencode"))
      ? "configured"
      : "missing",
    config_path: configPath,
    plugin,
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
