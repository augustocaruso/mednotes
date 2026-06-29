import fs from "node:fs/promises";

import { firstObject, firstString, normalizedArgs } from "./harness_payload.mjs";

const TRANSCRIPT_READ_LIMIT_BYTES = 2 * 1024 * 1024;

export function isAntigravityPayload(payload) {
  return Boolean(
    payload &&
      typeof payload === "object" &&
      (payload.toolCall || payload.hookEventName || payload.conversationId || payload.workspacePaths),
  );
}

export function defaultAntigravityHookEventName(mode) {
  if (mode === "capture-after-tool" || mode === "capture-agent-directive-after-tool") return "PostToolUse";
  if (mode === "inject-agent-directive-before-agent") return "PreInvocation";
  if (mode === "validate-agent-directive-after-agent") return "Stop";
  return "PreToolUse";
}

export async function normalizePayloadForRuntime(payload, mode) {
  const raw = payload && typeof payload === "object" ? payload : {};
  if (!isAntigravityPayload(raw)) return raw;
  const transcriptStep = await antigravityTranscriptStep(raw);
  const toolCall = firstObject(raw.toolCall, transcriptStep.toolCall, transcriptStep.tool_call, transcriptStep.toolUse);
  const args = normalizedArgs(toolCall.args || raw.tool_input || raw.toolInput || raw.parameters || raw.args || {});
  const toolName = firstString(
    toolCall.name,
    raw.tool_name,
    raw.toolName,
    raw.name,
    raw.tool,
    transcriptStep.tool_name,
    transcriptStep.toolName,
  );
  const cwd = firstString(args.cwd, raw.cwd, Array.isArray(raw.workspacePaths) ? raw.workspacePaths[0] : "");
  return {
    ...raw,
    hook_event_name: raw.hookEventName || raw.hook_event_name || defaultAntigravityHookEventName(mode),
    session_id: raw.conversationId || raw.session_id || "",
    transcript_path: raw.transcriptPath || raw.transcript_path || "",
    artifact_directory_path: raw.artifactDirectoryPath || raw.artifact_directory_path || "",
    cwd,
    tool_name: toolName,
    original_request_name: toolName,
    tool_input: args,
    tool_response: normalizedToolResponse(
      raw.toolResponse,
      raw.toolResult,
      raw.response,
      raw.result,
      transcriptStep.toolResponse,
      transcriptStep.toolResult,
      transcriptStep.response,
      transcriptStep.result,
      transcriptStep.output,
      transcriptStep.content,
    ),
    antigravity_payload_seen: true,
  };
}

export function renderAntigravityOutput(mode, result, payload) {
  const output = result && typeof result === "object" ? result : {};
  if (mode === "capture-after-tool" || mode === "capture-agent-directive-after-tool") return {};
  const eventName = String(payload?.hook_event_name || "");
  const context = output?.hookSpecificOutput?.additionalContext;
  if (eventName === "PreInvocation" && typeof context === "string" && context.trim()) {
    return {
      injectSteps: [
        {
          ephemeralMessage: context,
        },
      ],
    };
  }
  if (eventName === "Stop") {
    if (output.decision === "deny") {
      return {
        decision: "continue",
        reason: [output.agent_message, output.reason].filter(Boolean).join(" "),
      };
    }
    return {};
  }
  if (eventName === "PreToolUse") {
    if (!output.decision) return { decision: "allow" };
    const details = [output.blocked_reason, output.directive_field, output.agent_message].filter(Boolean).join(" ");
    const reason = [output.reason, details].filter(Boolean).join(" ");
    return {
      decision: output.decision,
      ...(reason ? { reason } : {}),
    };
  }
  if (!output.decision || output.decision === "allow") return {};
  return output;
}

async function antigravityTranscriptStep(raw) {
  const transcriptPath = firstString(raw.transcriptPath, raw.transcript_path);
  if (!transcriptPath) return {};
  try {
    const stats = await fs.stat(transcriptPath);
    if (!stats.isFile() || stats.size > TRANSCRIPT_READ_LIMIT_BYTES) return {};
    const targetStepIdx = stepIndexFromPayload(raw);
    const text = await fs.readFile(transcriptPath, "utf8");
    let fallback = {};
    for (const line of text.split(/\r?\n/)) {
      const record = parseJsonLine(line);
      if (!Object.keys(record).length) continue;
      if (
        record.toolCall ||
        record.toolResponse ||
        record.toolResult ||
        record.response ||
        record.result ||
        record.output
      ) {
        fallback = record;
      }
      if (targetStepIdx && stepIndexFromTranscriptRecord(record) === targetStepIdx) return record;
    }
    return fallback;
  } catch {
    return {};
  }
}

function normalizedToolResponse(...values) {
  for (const value of values) {
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
    if (typeof value === "string" && value.trim()) return { returnDisplay: value };
  }
  return {};
}

function stepIndexFromPayload(payload) {
  return firstString(
    payload?.stepIdx,
    payload?.stepIndex,
    payload?.step_idx,
    payload?.toolStepIdx,
    payload?.toolUseStepIdx,
  );
}

function stepIndexFromTranscriptRecord(record) {
  return firstString(
    record?.stepIdx,
    record?.stepIndex,
    record?.step_idx,
    record?.idx,
    record?.index,
    record?.toolStepIdx,
    record?.toolUseStepIdx,
  );
}

function parseJsonLine(line) {
  const text = String(line || "").trim();
  if (!text) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}
