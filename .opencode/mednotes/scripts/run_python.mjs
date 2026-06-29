#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, rmSync, unlinkSync } from "node:fs";
import os from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
if (args.length === 0) {
  console.error("usage: node scripts/run_python.mjs <script.py> [args...]");
  process.exit(2);
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(scriptDir, "..");
const uv = process.env.UV || "uv";
const env = { ...process.env };
env.PYTHONDONTWRITEBYTECODE = env.PYTHONDONTWRITEBYTECODE || "1";

const appHome = env.MEDNOTES_HOME || join(os.homedir(), ".mednotes");

const installedExtension = existsSync(join(projectRoot, "gemini-extension.json"));
const installedAntigravityPlugin = existsSync(join(projectRoot, "plugin.json"));
const installedBundle = installedExtension || installedAntigravityPlugin;
const projectLockPath = join(projectRoot, "uv.lock");
const projectLockExisted = existsSync(projectLockPath);
const buildDir = join(projectRoot, "build");
const buildDirExisted = existsSync(buildDir);
const eggInfoBefore = new Set(discoverEggInfoDirs(projectRoot));
const usePersistentEnv =
  env.MEDNOTES_USE_PERSISTENT_UV_ENV === "1" || (env.MEDNOTES_USE_PERSISTENT_UV_ENV !== "0" && installedBundle);

if (!env.UV_PROJECT_ENVIRONMENT && usePersistentEnv) {
  mkdirSync(appHome, { recursive: true });
  env.UV_PROJECT_ENVIRONMENT = join(appHome, ".venv");
}

const [scriptArg, ...scriptArgs] = args;
if (scriptArg.startsWith("-")) {
  console.error(
    `run_python.mjs expects a Python script path as the first argument; module mode is not supported: ${scriptArg}`,
  );
  console.error(
    "Use an explicit script path, for example: node scripts/run_python.mjs src/enricher/__main__.py --help",
  );
  process.exit(2);
}

const resolvedScript = isAbsolute(scriptArg) || existsSync(scriptArg) ? scriptArg : join(projectRoot, scriptArg);

const result = spawnSync(
  uv,
  ["run", "--no-editable", "--project", projectRoot, "python", resolvedScript, ...scriptArgs],
  {
    env,
    stdio: "inherit",
    shell: false,
  },
);

cleanupGeneratedPythonMetadata(projectRoot, eggInfoBefore, buildDirExisted);

if (installedExtension && !projectLockExisted && existsSync(projectLockPath)) {
  try {
    unlinkSync(projectLockPath);
  } catch {
    // Runtime cleanup is best-effort; command exit status remains authoritative.
  }
}

if (result.error?.code === "ENOENT") {
  console.error("Could not find uv. Install uv first, or run scripts/reset_windows_python_uv.ps1 on Windows.");
  process.exit(127);
}

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}

process.exit(result.status ?? 1);

function discoverEggInfoDirs(root) {
  const candidates = [join(root, "src"), join(root, "bundle", "src")];
  const found = [];
  for (const candidate of candidates) {
    if (!existsSync(candidate)) continue;
    for (const entry of readdirSync(candidate, { withFileTypes: true })) {
      if (entry.isDirectory() && entry.name.endsWith(".egg-info")) {
        found.push(resolve(candidate, entry.name));
      }
    }
  }
  return found;
}

function cleanupGeneratedPythonMetadata(root, existingEggInfoDirs, existingBuildDir) {
  // uv/setuptools may create build metadata beside generated runtime sources.
  // Clean only metadata that did not exist before this wrapper invocation.
  for (const eggInfoDir of discoverEggInfoDirs(root)) {
    if (!existingEggInfoDirs.has(eggInfoDir)) {
      rmSync(eggInfoDir, { recursive: true, force: true });
    }
  }
  const runtimeBuildDir = join(root, "build");
  if (!existingBuildDir && existsSync(runtimeBuildDir)) {
    rmSync(runtimeBuildDir, { recursive: true, force: true });
  }
}
