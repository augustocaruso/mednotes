import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { normalizePayloadForRuntime, renderAntigravityOutput } from "./adapters/antigravity.mjs";
import { ensureAnkiBefore } from "./anki_preflight.mjs";
import { diagnose } from "./diagnostics.mjs";
import {
  captureAgentDirectiveAfterTool,
  guardAgentDirectiveBeforeTool,
  injectAgentDirectiveBeforeAgent,
  validateAgentDirectiveAfterAgent,
} from "./fsm_directive.mjs";
import { recordHookError } from "./hook_errors.mjs";
import { quiet, readPayloadResult, writeJson } from "./runtime.mjs";
import { captureAfterTool } from "./telemetry_capture.mjs";
import { guardVaultBefore } from "./vault_guard.mjs";

const STATUS_SCHEMA = "medical-notes-workbench.antigravity-hook-status.v1";
const STATUS_DIR = path.join(appHomeDir(), "antigravity-hooks");
const STATUS_FILE = path.join(STATUS_DIR, "status.json");
const HOOK_PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

function appHomeDir() {
  return process.env.MEDNOTES_HOME || path.join(os.homedir(), ".mednotes");
}

function compactPath(value) {
  const text = String(value || "");
  if (!text) return "";
  const home = os.homedir();
  return text.startsWith(home) ? `~${text.slice(home.length)}` : text;
}

async function readStatus() {
  try {
    const parsed = JSON.parse(await fs.readFile(STATUS_FILE, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

async function recordHookStatus(mode, payload, output, status = "executed") {
  try {
    const previous = await readStatus();
    const now = new Date().toISOString();
    const previousByMode = previous.by_mode && typeof previous.by_mode === "object" ? previous.by_mode : {};
    const previousLast = previous.last_hook && typeof previous.last_hook === "object" ? previous.last_hook : {};
    const nextByMode = {
      ...previousByMode,
      [mode || "unknown"]: Number(previousByMode[mode || "unknown"] || 0) + 1,
    };
    const eventName = String(payload?.hook_event_name || "");
    const toolName = String(payload?.tool_name || "");
    const cwd = compactPath(payload?.cwd || "");
    const observedToolName = toolName || String(previousLast.tool_name || "");
    const observedCwd = cwd || String(previousLast.cwd || "");
    const next = {
      schema: STATUS_SCHEMA,
      status,
      updated_at: now,
      total_invocations: Number(previous.total_invocations || 0) + 1,
      by_mode: nextByMode,
      last_hook: {
        mode: mode || "unknown",
        hook_event_name: eventName,
        tool_name: observedToolName,
        decision: String(output?.decision || ""),
        cwd: observedCwd,
        plugin_root: compactPath(HOOK_PLUGIN_ROOT),
        conversation_id_seen: Boolean(payload?.session_id),
        transcript_path_seen: Boolean(payload?.transcript_path),
        current_payload_tool_name_seen: Boolean(toolName),
        current_payload_cwd_seen: Boolean(cwd),
      },
      note: "Redacted operational heartbeat only. No command text, note content, raw chat, markdown, HTML, token or secret is stored here.",
    };
    await fs.mkdir(STATUS_DIR, { recursive: true });
    const tmp = `${STATUS_FILE}.tmp`;
    await fs.writeFile(tmp, `${JSON.stringify(next, null, 2)}\n`, "utf8");
    await fs.rename(tmp, STATUS_FILE);
  } catch {
    // Hook status is observability only. Never let it change hook behavior.
  }
}

export async function dispatch(mode, payload) {
  if (mode === "ensure-anki-before") return ensureAnkiBefore(payload);
  if (mode === "guard-vault-before") return guardVaultBefore(payload);
  if (mode === "capture-after-tool") return captureAfterTool(payload);
  if (mode === "capture-agent-directive-after-tool") return captureAgentDirectiveAfterTool(payload);
  if (mode === "inject-agent-directive-before-agent") return injectAgentDirectiveBeforeAgent(payload);
  if (mode === "guard-agent-directive-before-tool") return guardAgentDirectiveBeforeTool(payload);
  if (mode === "validate-agent-directive-after-agent") return validateAgentDirectiveAfterAgent(payload);
  return quiet();
}

export async function run(argv = process.argv.slice(2)) {
  const mode = argv[0] || "";
  if (mode === "diagnose") {
    writeJson(await diagnose());
    return;
  }

  const payloadResult = await readPayloadResult();
  if (payloadResult.error) {
    await recordHookError({
      mode,
      type: payloadResult.error.type,
      message: payloadResult.error.message,
      details: payloadResult.error.details,
    });
  }
  const payload = await normalizePayloadForRuntime(payloadResult.payload, mode);
  const result = await dispatch(mode, payload);
  const output = payload.antigravity_payload_seen ? renderAntigravityOutput(mode, result, payload) : result;
  // Status heartbeat is observability only and fail-open inside recordHookStatus,
  // but tests and diagnostics depend on it being flushed before the one-shot
  // hook process exits.
  await recordHookStatus(mode, payload, output);
  writeJson(output);
}

export async function main(argv = process.argv.slice(2)) {
  try {
    await run(argv);
  } catch (error) {
    await recordHookError({
      mode: argv[0] || "",
      type: "hook_main_error",
      error,
    });
    console.error(`mednotes hook failed open: ${error instanceof Error ? error.message : String(error)}`);
    writeJson(quiet());
    process.exitCode = 0;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
