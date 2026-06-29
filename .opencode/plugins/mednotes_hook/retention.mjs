import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

import { clampInt } from "./runtime.mjs";

function retentionHours(kind) {
  const specific =
    kind === "error"
      ? process.env.MEDNOTES_HOOK_ERROR_RETENTION_HOURS
      : process.env.MEDNOTES_HOOK_EVENT_RETENTION_HOURS;
  return clampInt(specific ?? process.env.MEDNOTES_HOOK_RETENTION_HOURS, 24, 1, 168);
}

function retentionPolicy(kind) {
  return {
    maxFiles: clampInt(
      kind === "error" ? process.env.MEDNOTES_HOOK_ERROR_MAX_FILES : process.env.MEDNOTES_HOOK_EVENT_MAX_FILES,
      kind === "error" ? 25 : 50,
      0,
      1000,
    ),
    retentionHours: retentionHours(kind),
  };
}

export async function pruneHookEventFiles(dir) {
  await pruneJsonFiles(dir, retentionPolicy("event"));
}

export async function pruneHookErrorFiles(dir) {
  await pruneJsonFiles(dir, retentionPolicy("error"));
}

export function pruneHookEventFilesSync(dir) {
  pruneJsonFilesSync(dir, retentionPolicy("event"));
}

async function pruneJsonFiles(dir, policy) {
  try {
    const entries = await fsp.readdir(dir, { withFileTypes: true });
    const files = [];
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith(".json")) continue;
      const filePath = path.join(dir, entry.name);
      try {
        const stat = await fsp.stat(filePath);
        files.push({ path: filePath, name: entry.name, mtimeMs: stat.mtimeMs });
      } catch {
        // Best effort only.
      }
    }
    await pruneEntries(files, policy, (filePath) => fsp.unlink(filePath));
  } catch {
    // Retention cleanup is observability-only. Never fail a hook because of it.
  }
}

function pruneJsonFilesSync(dir, policy) {
  try {
    const files = [];
    for (const name of fs.readdirSync(dir)) {
      if (!name.endsWith(".json")) continue;
      const filePath = path.join(dir, name);
      try {
        const stat = fs.statSync(filePath);
        if (stat.isFile()) files.push({ path: filePath, name, mtimeMs: stat.mtimeMs });
      } catch {
        // Best effort only.
      }
    }
    pruneEntriesSync(files, policy, (filePath) => fs.unlinkSync(filePath));
  } catch {
    // Retention cleanup is observability-only. Never fail a hook because of it.
  }
}

async function pruneEntries(files, policy, unlink) {
  const victims = retentionVictims(files, policy);
  for (const item of victims) {
    try {
      await unlink(item.path);
    } catch {
      // Another process may have cleaned it already.
    }
  }
}

function pruneEntriesSync(files, policy, unlink) {
  const victims = retentionVictims(files, policy);
  for (const item of victims) {
    try {
      unlink(item.path);
    } catch {
      // Another process may have cleaned it already.
    }
  }
}

function retentionVictims(files, policy) {
  const cutoffMs = Date.now() - policy.retentionHours * 60 * 60 * 1000;
  const sorted = [...files].sort((left, right) => right.mtimeMs - left.mtimeMs || right.name.localeCompare(left.name));
  const victims = [];
  const survivors = [];
  for (const item of sorted) {
    if (item.mtimeMs < cutoffMs) {
      victims.push(item);
    } else {
      survivors.push(item);
    }
  }
  victims.push(...survivors.slice(Math.max(0, policy.maxFiles)));
  return victims;
}
