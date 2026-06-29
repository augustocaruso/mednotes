import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { pruneHookErrorFiles } from "./retention.mjs";
import { clampInt } from "./runtime.mjs";

const SCHEMA = "medical-notes-workbench.agent-hook-error.v1";
const MAX_ERROR_CHARS = clampInt(process.env.MEDNOTES_HOOK_MAX_ERROR_CHARS, 8 * 1024, 512, 64 * 1024);

export async function recordHookError({
  mode = "",
  type = "hook_internal_error",
  severity = "warning",
  payload = {},
  error = null,
  message = "",
  details = {},
} = {}) {
  try {
    const event = buildHookError({ mode, type, severity, payload, error, message, details });
    const dir = path.join(feedbackRoot(), "hook-errors");
    await fs.mkdir(dir, { recursive: true });
    const file = path.join(dir, `${event.recorded_at.replace(/[:.]/g, "-")}-${event.error_id}.json`);
    const tmp = `${file}.tmp`;
    await fs.writeFile(tmp, `${JSON.stringify(event, null, 2)}\n`, "utf8");
    await fs.rename(tmp, file);
    await pruneHookErrorFiles(dir);
  } catch {
    // Last-resort fail-open: hook error capture must never create a hook error loop.
  }
}

export function buildHookError({ mode, type, severity, payload, error, message, details }) {
  const safePayload = payload && typeof payload === "object" ? payload : {};
  const err = error instanceof Error ? error : null;
  return {
    schema: SCHEMA,
    error_id: crypto.randomUUID ? crypto.randomUUID() : crypto.randomBytes(16).toString("hex"),
    recorded_at: new Date().toISOString(),
    mode: cleanCode(mode || "unknown"),
    type: cleanCode(type || "hook_internal_error"),
    severity: cleanCode(severity || "warning"),
    hook_event_name: String(safePayload.hook_event_name || ""),
    tool_name: String(safePayload.tool_name || safePayload.toolName || safePayload.name || ""),
    session_id: String(safePayload.session_id || ""),
    transcript_path: compactPath(String(safePayload.transcript_path || "")),
    cwd: compactPath(String(safePayload.cwd || process.cwd())),
    message: redactOperationalText(message || err?.message || String(error || "unknown hook error"), MAX_ERROR_CHARS),
    stack_tail: err?.stack ? tail(redactOperationalText(err.stack, MAX_ERROR_CHARS), MAX_ERROR_CHARS) : "",
    details: sanitizeDetails(details),
  };
}

function sanitizeDetails(value, depth = 0) {
  if (depth > 4) return "[max-depth]";
  if (!value || typeof value !== "object") {
    return typeof value === "string" ? redactOperationalText(value, 500) : value;
  }
  if (Array.isArray(value)) {
    return value.slice(0, 20).map((item) => sanitizeDetails(item, depth + 1));
  }
  const out = {};
  for (const [key, item] of Object.entries(value).slice(0, 30)) {
    const lower = key.toLowerCase();
    if (["token", "auth_token", "api_key", "apikey", "secret", "password", "authorization", "bearer"].includes(lower)) {
      out[key] = "[redacted]";
    } else if (["content", "markdown", "html", "raw_chat", "note_text"].includes(lower)) {
      out[key] = `[${lower} omitted]`;
    } else {
      out[key] = sanitizeDetails(item, depth + 1);
    }
  }
  return out;
}

function cleanCode(value) {
  return (
    String(value || "unknown")
      .replace(/[^a-zA-Z0-9_.-]/g, "_")
      .toLowerCase()
      .slice(0, 80) || "unknown"
  );
}

function feedbackRoot() {
  const configured = process.env.MEDNOTES_FEEDBACK_DIR || process.env.MEDICAL_NOTES_FEEDBACK_DIR;
  if (configured) return configured;
  return path.join(os.homedir(), ".gemini", "medical-notes-workbench", "feedback");
}

function compactPath(value) {
  const text = String(value || "");
  if (!text) return "";
  const home = os.homedir();
  return text.startsWith(home) ? `~${text.slice(home.length)}` : text;
}

function redactOperationalText(value, maxChars) {
  let text = String(value || "");
  text = text.replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[email]");
  text = text.replace(
    /\b(api[_-]?key|token|secret|password|authorization|bearer)(\s*[:=]\s*)(["']?)[^\s"']+/gi,
    "$1$2[redacted]",
  );
  text = text.replace(/(--(?:api-key|auth-token|token|secret|password)\s+)([^\s"']+)/gi, "$1[redacted]");
  text = text.replace(/https?:\/\/[^\s)>"]+/g, (url) => {
    const index = url.indexOf("?");
    return index === -1 ? url : `${url.slice(0, index)}?[redacted]`;
  });
  text = text.replace(/\b[A-Za-z0-9_=-]{36,}\b/g, "[redacted-token]");
  if (text.length > maxChars) return `${text.slice(0, Math.max(0, maxChars - 3)).trimEnd()}...`;
  return text;
}

function tail(value, maxChars) {
  const text = String(value || "");
  return text.length > maxChars ? text.slice(text.length - maxChars) : text;
}
