import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { isOfficialWorkflowPayload } from "./domain/agent_directive_core.mjs";
import { recordHookError } from "./hook_errors.mjs";
import { pruneHookEventFiles } from "./retention.mjs";
import { clampInt, quiet } from "./runtime.mjs";

const SCHEMA = "medical-notes-workbench.agent-hook-event.v1";
const SCRIPT_EXTENSIONS = new Set([".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".cmd"]);
const WRITE_TOOL_NAMES = new Set(["write_file", "write"]);
const REPLACE_TOOL_NAMES = new Set(["replace", "edit", "multiedit"]);
const SHELL_TOOL_NAMES = new Set([
  "run_shell_command",
  "run_shell",
  "shelltool",
  "bash",
  "shell",
  "powershell",
  "pwsh",
]);
const MAX_SCRIPT_BYTES = clampInt(process.env.MEDNOTES_HOOK_MAX_SCRIPT_BYTES, 48 * 1024, 1024, 256 * 1024);
const MAX_CONSOLE_CHARS = clampInt(process.env.MEDNOTES_HOOK_MAX_CONSOLE_CHARS, 16 * 1024, 1024, 128 * 1024);

export async function captureAfterTool(payload) {
  try {
    const event = await buildHookEvent(payload);
    if (event) await writeHookEvent(event);
  } catch (error) {
    await recordHookError({
      mode: "capture-after-tool",
      type: "hook_internal_error",
      payload,
      error,
    });
    if (/^(1|true|yes)$/i.test(process.env.MEDNOTES_HOOK_DEBUG || "")) {
      console.error(`mednotes telemetry hook failed open: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return quiet();
}

export async function buildHookEvent(payload) {
  if (!payload || typeof payload !== "object") return null;
  const toolName = String(payload.tool_name || payload.toolName || payload.name || payload.tool || "");
  const toolKind = capturedToolKind(toolName);
  if (!toolKind) return null;

  const toolInput = firstObject(payload.tool_input, payload.toolInput, payload.input, payload.parameters, payload.args);
  const toolResponse = firstObject(
    payload.tool_response,
    payload.toolResponse,
    payload.response,
    payload.result,
    payload.tool_result,
  );
  const cwd = String(payload.cwd || process.cwd());
  const event = {
    schema: SCHEMA,
    event_id: crypto.randomUUID ? crypto.randomUUID() : crypto.randomBytes(16).toString("hex"),
    recorded_at: new Date().toISOString(),
    hook_event_name: String(payload.hook_event_name || "AfterTool"),
    session_id: String(payload.session_id || ""),
    transcript_path: compactPath(String(payload.transcript_path || "")),
    cwd: compactPath(cwd),
    tool_name: toolName,
    tool_kind: toolKind,
    original_request_name: String(payload.original_request_name || ""),
    generated_scripts: [],
    command_events: [],
  };

  if (toolKind === "write_file") {
    const script = await scriptFromWriteFile(toolInput, cwd);
    if (script) event.generated_scripts.push(script);
  } else if (toolKind === "replace") {
    const script = await scriptFromReplace(toolInput, cwd);
    if (script) event.generated_scripts.push(script);
  } else if (toolKind === "shell") {
    const commandEvent = commandEventFromShell(toolInput, toolResponse);
    const generatedScripts = await scriptsFromShellCommand(toolInput, cwd);
    if (commandEvent && (commandEvent.status === "failed" || generatedScripts.length > 0)) {
      event.command_events.push(commandEvent);
      enrichEventFromCommandEvent(event, commandEvent);
    }
    for (const script of generatedScripts) {
      event.generated_scripts.push(script);
    }
  }

  if (!event.generated_scripts.length && !event.command_events.length) return null;
  return event;
}

function capturedToolKind(toolName) {
  const normalized = String(toolName || "")
    .replace(/\s+/g, "_")
    .toLowerCase();
  if (WRITE_TOOL_NAMES.has(normalized)) return "write_file";
  if (REPLACE_TOOL_NAMES.has(normalized)) return "replace";
  if (SHELL_TOOL_NAMES.has(normalized)) return "shell";
  return "";
}

function firstObject(...values) {
  for (const value of values) {
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  }
  return {};
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

async function scriptFromWriteFile(toolInput, cwd) {
  const filePath = firstString(
    toolInput.file_path,
    toolInput.filePath,
    toolInput.path,
    toolInput.target_file,
    toolInput.targetFile,
  );
  if (!isScriptPath(filePath)) return null;
  const content = firstString(
    toolInput.content,
    toolInput.file_text,
    toolInput.fileText,
    toolInput.text,
    toolInput.source,
  );
  return scriptRecord({
    pathValue: resolveToolPath(filePath, cwd),
    content,
    captureMethod: "write_file",
  });
}

async function scriptFromReplace(toolInput, cwd) {
  const filePath = firstString(
    toolInput.file_path,
    toolInput.filePath,
    toolInput.path,
    toolInput.target_file,
    toolInput.targetFile,
  );
  if (!isScriptPath(filePath)) return null;
  const resolved = resolveToolPath(filePath, cwd);
  const fromDisk = await readScriptContent(resolved);
  return scriptRecord({
    pathValue: resolved,
    content:
      fromDisk.content ||
      firstString(
        toolInput.new_string,
        toolInput.newString,
        toolInput.replacement,
        toolInput.new_text,
        lastEditReplacement(toolInput.edits),
      ),
    omittedReason: fromDisk.omittedReason,
    captureMethod: "replace",
  });
}

function lastEditReplacement(edits) {
  if (!Array.isArray(edits) || edits.length === 0) return "";
  const last = edits[edits.length - 1];
  if (!last || typeof last !== "object") return "";
  return firstString(last.new_string, last.newString, last.replacement, last.new_text);
}

async function scriptsFromShellCommand(toolInput, cwd) {
  const command = shellCommand(toolInput);
  const dirPath = firstString(
    toolInput.dir_path,
    toolInput.dirPath,
    toolInput.working_directory,
    toolInput.workingDirectory,
  );
  const effectiveCwd = dirPath ? resolveToolPath(dirPath, cwd) : cwd;
  const paths = scriptPathsFromCommand(command).map((item) => resolveToolPath(item, effectiveCwd));
  const records = [];
  for (const candidate of [...new Set(paths)].slice(0, 6)) {
    const content = await readScriptContent(candidate);
    if (!content.content && !content.omittedReason) continue;
    records.push(
      scriptRecord({
        pathValue: candidate,
        content: content.content,
        omittedReason: content.omittedReason,
        captureMethod: "run_shell_command",
      }),
    );
  }
  return records.filter(Boolean);
}

function commandEventFromShell(toolInput, toolResponse) {
  const command = shellCommand(toolInput);
  if (!command) return null;
  const output = responseText(toolResponse);
  const exitCode = exitCodeFromResponse(toolResponse, output);
  const workflowPayload = workflowPayloadFromOutput(output);
  const workflow = workflowFromPayload(workflowPayload);
  const workflowFields = workflowFieldsFromPayload(workflowPayload);
  const workflowStatus = workflowFields.status;
  const workflowPhase = workflowFields.phase;
  const workflowBlockedReason = workflowFields.blocked_reason;
  const workflowExitCode = Number.isInteger(workflowFields.workflow_exit_code)
    ? workflowFields.workflow_exit_code
    : exitCode;
  const hasError =
    Boolean(toolResponse.error) ||
    truthy(toolResponse.is_error) ||
    truthy(toolResponse.isError) ||
    Boolean(firstString(toolResponse.stderr, toolResponse?.data?.stderr)) ||
    ["error", "failed", "failure"].includes(String(toolResponse.status || "").toLowerCase()) ||
    (Number.isInteger(exitCode) && exitCode !== 0);
  if (!hasError && !output.trim()) return null;
  const event = {
    command_family: commandFamily(command),
    command: redactOperationalText(command, 2000),
    exit_code: Number.isInteger(exitCode) ? exitCode : null,
    status: hasError ? "failed" : "completed",
    output_tail: tail(redactOperationalText(tail(output, MAX_CONSOLE_CHARS * 2), MAX_CONSOLE_CHARS), MAX_CONSOLE_CHARS),
    error: errorTextFromResponse(toolResponse),
    capture_method: "run_shell_command",
  };
  if (workflow) event.workflow = workflow;
  if (workflowPhase) event.phase = workflowPhase;
  if (workflowStatus) event.workflow_status = workflowStatus;
  if (workflowBlockedReason) event.blocked_reason = workflowBlockedReason;
  if (Number.isInteger(workflowExitCode)) event.workflow_exit_code = workflowExitCode;
  return event;
}

function enrichEventFromCommandEvent(event, commandEvent) {
  if (!event || !commandEvent) return;
  if (commandEvent.workflow) event.workflow = commandEvent.workflow;
  if (commandEvent.phase) event.phase = commandEvent.phase;
  if (commandEvent.workflow_status) event.status = commandEvent.workflow_status;
  if (commandEvent.blocked_reason) event.blocked_reason = commandEvent.blocked_reason;
  if (Number.isInteger(commandEvent.workflow_exit_code)) event.workflow_exit_code = commandEvent.workflow_exit_code;
}

function workflowPayloadFromOutput(output) {
  const text = String(output || "");
  if (!text.includes("{")) return {};
  const probes = [text.slice(0, MAX_CONSOLE_CHARS * 4), tail(text, MAX_CONSOLE_CHARS * 4)];
  for (const probe of probes) {
    const parsed = firstJsonObject(probe);
    if (isOfficialWorkflowPayload(parsed)) {
      return parsed;
    }
  }
  return {};
}

function workflowFromPayload(value) {
  if (!isOfficialWorkflowPayload(value)) return "";
  return firstString(value.agent_directive?.workflow);
}

function workflowFieldsFromPayload(value) {
  if (!isOfficialWorkflowPayload(value)) return {};
  const progress = firstObject(value.progress_view_model);
  const snapshot = firstObject(value.state_machine_snapshot);
  const directive = firstObject(value.agent_directive);
  const control = firstObject(directive.control);
  const blockers = Array.isArray(control.blockers) ? control.blockers : [];
  const directiveStatus = firstString(control.status);
  return {
    status: directiveStatus,
    phase: firstString(progress.phase, control.phase),
    blocked_reason: firstString(blockers[0], control.reason),
    state: firstString(control.state, progress.state, snapshot.current_state),
    workflow_exit_code: null,
  };
}

function firstJsonObject(text) {
  const input = String(text || "");
  for (let start = input.indexOf("{"); start !== -1; start = input.indexOf("{", start + 1)) {
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let index = start; index < input.length; index += 1) {
      const char = input[index];
      if (escaped) {
        escaped = false;
        continue;
      }
      if (char === "\\") {
        escaped = inString;
        continue;
      }
      if (char === '"') {
        inString = !inString;
        continue;
      }
      if (inString) continue;
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 0) {
        try {
          return JSON.parse(input.slice(start, index + 1));
        } catch {
          break;
        }
      }
    }
  }
  return {};
}

function shellCommand(toolInput) {
  return firstString(toolInput.command, toolInput.cmd, toolInput.script);
}

function responseText(toolResponse) {
  return [
    toolResponse.llmContent,
    toolResponse.returnDisplay,
    toolResponse.stdout,
    toolResponse.stderr,
    toolResponse.output,
    toolResponse.text,
    toolResponse.message,
    toolResponse.content,
    toolResponse?.data?.stdout,
    toolResponse?.data?.stderr,
    toolResponse?.data?.output,
  ]
    .map(textFromValue)
    .filter((item) => item.trim())
    .join("\n\n");
}

function exitCodeFromResponse(toolResponse, output) {
  if (Number.isInteger(toolResponse?.data?.exitCode)) return toolResponse.data.exitCode;
  if (Number.isInteger(toolResponse?.data?.exit_code)) return toolResponse.data.exit_code;
  if (Number.isInteger(toolResponse.exitCode)) return toolResponse.exitCode;
  if (Number.isInteger(toolResponse.exit_code)) return toolResponse.exit_code;
  if (Number.isInteger(toolResponse.code)) return toolResponse.code;
  const match = String(output || "").match(/Exit Code:\s*(-?\d+)/i);
  return match ? Number(match[1]) : null;
}

function errorTextFromResponse(toolResponse) {
  if (!toolResponse.error) return "";
  if (typeof toolResponse.error === "string") return redactOperationalText(toolResponse.error, 4000);
  try {
    return redactOperationalText(JSON.stringify(toolResponse.error), 4000);
  } catch {
    return redactOperationalText(String(toolResponse.error), 4000);
  }
}

function textFromValue(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(textFromValue).filter(Boolean).join("\n");
  if (!value || typeof value !== "object") return "";
  if (typeof value.text === "string") return value.text;
  if (typeof value.content === "string") return value.content;
  return "";
}

function truthy(value) {
  return value === true || /^(1|true|yes)$/i.test(String(value || ""));
}

export function scriptPathsFromCommand(command) {
  const results = [];
  const patterns = [
    />\s*(?:"([^"]+\.(?:py|js|mjs|cjs|sh|ps1|cmd))"|'([^']+\.(?:py|js|mjs|cjs|sh|ps1|cmd))'|([^\s<>|;&]+\.(?:py|js|mjs|cjs|sh|ps1|cmd)))/gi,
    /\b(?:tee|cat)\b[^|;&]*?>\s*(?:"([^"]+\.(?:py|js|mjs|cjs|sh|ps1|cmd))"|'([^']+\.(?:py|js|mjs|cjs|sh|ps1|cmd))'|([^\s<>|;&]+\.(?:py|js|mjs|cjs|sh|ps1|cmd)))/gi,
    /\bSet-Content\b[^|;&]*?(?:-(?:LiteralPath|Path)\s+)?(?:"([^"]+\.(?:py|js|mjs|cjs|sh|ps1|cmd))"|'([^']+\.(?:py|js|mjs|cjs|sh|ps1|cmd))'|([^\s|;&]+\.(?:py|js|mjs|cjs|sh|ps1|cmd)))/gi,
    /\bOut-File\b[^|;&]*?(?:-(?:LiteralPath|FilePath)\s+)?(?:"([^"]+\.(?:py|js|mjs|cjs|sh|ps1|cmd))"|'([^']+\.(?:py|js|mjs|cjs|sh|ps1|cmd))'|([^\s|;&]+\.(?:py|js|mjs|cjs|sh|ps1|cmd)))/gi,
  ];
  for (const pattern of patterns) {
    for (const match of command.matchAll(pattern)) {
      const candidate = match.slice(1).find(Boolean);
      if (candidate && isScriptPath(candidate)) results.push(candidate);
    }
  }
  return results;
}

async function readScriptContent(pathValue) {
  if (!isScriptPath(pathValue) || (isWindowsAbsolute(pathValue) && process.platform !== "win32")) {
    return { content: "", omittedReason: "" };
  }
  try {
    const stat = await fs.stat(pathValue);
    if (!stat.isFile()) return { content: "", omittedReason: "" };
    if (stat.size > MAX_SCRIPT_BYTES) {
      return { content: "", omittedReason: `script_too_large:${stat.size}` };
    }
    return { content: await fs.readFile(pathValue, "utf8"), omittedReason: "" };
  } catch {
    return { content: "", omittedReason: "" };
  }
}

function scriptRecord({ pathValue, content, omittedReason = "", captureMethod }) {
  if (!isScriptPath(pathValue)) return null;
  const value = String(content || "");
  return {
    path: compactPath(pathValue),
    language: languageForPath(pathValue),
    sha256: value ? crypto.createHash("sha256").update(value).digest("hex") : "",
    size_bytes: Buffer.byteLength(value, "utf8"),
    content: value ? redactOperationalText(value, MAX_SCRIPT_BYTES) : "",
    content_omitted_reason: omittedReason,
    source: "hook",
    capture_method: captureMethod,
  };
}

function commandFamily(command) {
  const first =
    String(command || "")
      .trim()
      .split(/\s+/)[0] || "shell";
  return first.replace(/[^a-zA-Z0-9_.-]/g, "_").toLowerCase();
}

function isScriptPath(value) {
  const cleaned = String(value || "").replace(/^["']|["']$/g, "");
  return SCRIPT_EXTENSIONS.has(path.extname(cleaned).toLowerCase());
}

function languageForPath(value) {
  return (
    {
      ".py": "python",
      ".js": "javascript",
      ".mjs": "javascript",
      ".cjs": "javascript",
      ".sh": "shell",
      ".ps1": "powershell",
      ".cmd": "batch",
    }[path.extname(String(value || "")).toLowerCase()] || "text"
  );
}

function resolveToolPath(value, cwd) {
  const cleaned = String(value || "").replace(/^["']|["']$/g, "");
  if (!cleaned) return "";
  if (path.isAbsolute(cleaned) || isWindowsAbsolute(cleaned)) return cleaned;
  return path.resolve(cwd || process.cwd(), cleaned);
}

function isWindowsAbsolute(value) {
  return /^[a-zA-Z]:[\\/]/.test(String(value || ""));
}

function feedbackRoot() {
  const configured = process.env.MEDNOTES_FEEDBACK_DIR || process.env.MEDICAL_NOTES_FEEDBACK_DIR;
  if (configured) return configured;
  return path.join(os.homedir(), ".gemini", "medical-notes-workbench", "feedback");
}

async function writeHookEvent(event) {
  const dir = path.join(feedbackRoot(), "hook-events");
  await fs.mkdir(dir, { recursive: true });
  const file = path.join(dir, `${event.recorded_at.replace(/[:.]/g, "-")}-${event.event_id}.json`);
  const tmp = `${file}.tmp`;
  await fs.writeFile(tmp, `${JSON.stringify(event, null, 2)}\n`, "utf8");
  await fs.rename(tmp, file);
  await pruneHookEventFiles(dir);
}

function compactPath(value) {
  const text = String(value || "");
  if (!text) return "";
  const home = os.homedir();
  return text.startsWith(home) ? `~${text.slice(home.length)}` : text;
}

function redactOperationalText(value, maxChars) {
  let text = String(value || "");
  if (text.length > maxChars * 4) {
    text = text.slice(text.length - maxChars * 4);
  }
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
