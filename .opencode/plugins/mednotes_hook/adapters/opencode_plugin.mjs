import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {
  captureAgentDirectiveAfterTool,
  guardAgentDirectiveBeforeTool,
  injectAgentDirectiveBeforeAgent,
} from "../fsm_directive.mjs";
import { applyMedNotesOpenCodeRuntimeConfig } from "./opencode_user_config_sync.mjs";

const OPENCODE_TASK_METADATA_SCHEMA = "medical-notes-workbench.opencode-specialist-task-metadata.v1";
const OPENCODE_SPECIALIST_RAW_CONTENT_KEYS = new Set([
  "content",
  "markdown",
  "raw_chat",
  "raw_chat_content",
  "raw_markdown",
  "raw_markdown_content",
  "note_text",
  "html",
]);
const OPENCODE_SPECIALIST_SAFE_TEXT_KEYS = new Set([
  "agent",
  "attestation_created_by",
  "expected_model",
  "item_type",
  "model_policy",
  "missing_specialist_task_run_receipt_action",
  "phase",
  "preferred_model_tier",
  "raw_file",
  "required_model_tier",
  "rewrite_prompt",
  "schema",
  "source_work_id",
  "specialist_task_run_receipt_path",
  "staged_title",
  "target_hash_before",
  "target_path",
  "target_kind",
  "temp_output",
  "temp_output_path",
  "title",
  "coverage_path",
  "taxonomy",
  "work_id",
  "write_markdown_to",
  "write_policy",
]);

function hookPayload(input, output = {}, eventName) {
  const toolState = firstObjectValue(output?.state, input?.state, output?.part?.state, input?.part?.state);
  return {
    hook_event_name: eventName,
    runtime: "opencode",
    opencode_payload_seen: true,
    session_id: String(input?.sessionID || ""),
    tool_name: opencodeToolName(input, output),
    tool_input: firstObjectValue(output?.args, input?.args, toolState.input, output?.input, input?.input),
    tool_response: {
      title: String(output?.title || toolState.title || ""),
      output: String(output?.output || toolState.output || ""),
      metadata: firstObjectValue(output?.metadata, input?.metadata, toolState.metadata),
    },
  };
}

function opencodeToolName(input, output = {}) {
  return String(
    input?.tool || output?.tool || input?.toolName || output?.toolName || input?.part?.tool || output?.part?.tool || "",
  ).toLowerCase();
}

function denialMessage(result) {
  if (result?.decision !== "deny") return "";
  return [result.blocked_reason, result.directive_field, result.reason, result.agent_message]
    .filter(Boolean)
    .join(" | ");
}

function appHomeDir() {
  return path.resolve(process.env.MEDNOTES_HOME || path.join(os.homedir(), ".mednotes"));
}

function safeFileStem(value) {
  return (
    String(value || "")
      .replace(/[^a-zA-Z0-9_.-]/g, "_")
      .slice(0, 120) || "unknown"
  );
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function firstObjectValue(...values) {
  for (const value of values) {
    const candidate = objectValue(value);
    if (Object.keys(candidate).length > 0) return candidate;
  }
  return {};
}

function sha256Text(value) {
  return `sha256:${crypto
    .createHash("sha256")
    .update(String(value || ""))
    .digest("hex")}`;
}

function parseJsonObjectFromText(value) {
  const text = String(value || "").trim();
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    // Continue below for prompts that wrap the JSON work item with small prose.
  }
  const start = text.indexOf("{");
  if (start < 0) return {};
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const char = text[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (char === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (char === "{") depth += 1;
    if (char === "}") {
      depth -= 1;
      if (depth === 0) {
        try {
          const parsed = JSON.parse(text.slice(start, index + 1));
          return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch {
          return {};
        }
      }
    }
  }
  return {};
}

function typedWorkItemFromPrompt(prompt) {
  const packet = parseJsonObjectFromText(prompt);
  const candidates = [
    packet,
    objectValue(packet.work_item),
    objectValue(packet.workItem),
    ...(Array.isArray(packet.current_batch_items) ? packet.current_batch_items : []),
    ...(Array.isArray(packet.currentBatchItems) ? packet.currentBatchItems : []),
  ];
  for (const candidate of candidates) {
    const workItem = objectValue(candidate);
    if (!workItem.work_id || !workItem.temp_output) continue;
    if (isStyleRewriteWorkItem(workItem) || isArchitectWorkItem(workItem)) return workItem;
  }
  return {};
}

function isStyleRewriteWorkItem(workItem) {
  if (!workItem.target_hash_before) return false;
  if (!workItem.subagent_output_contract || typeof workItem.subagent_output_contract !== "object") return false;
  return true;
}

function isArchitectWorkItem(workItem) {
  const expected = objectValue(workItem.expected_output_schema);
  if (String(workItem.phase || "") !== "architect") return false;
  if (String(workItem.agent || "") !== "med-knowledge-architect") return false;
  if (String(workItem.item_type || "") !== "meaning_work_item") return false;
  if (String(expected.schema || "") !== "medical-notes-workbench.architect-output.v1") return false;
  if (!workItem.raw_file) return false;
  return true;
}

function architectOutputFromTaskResponse(outputText) {
  const payload = parseJsonObjectFromText(outputText);
  if (String(payload.schema || "") !== "medical-notes-workbench.architect-output.v1") {
    return {};
  }
  return payload;
}

async function writeArchitectOutputArtifact(workItem, outputText) {
  if (!isArchitectWorkItem(workItem)) return;
  const artifact = architectOutputFromTaskResponse(outputText);
  if (!artifact.schema) return;
  if (String(artifact.temp_output_path || "") !== String(workItem.temp_output || "")) return;
  const baseDir = path.join(appHomeDir(), "hook-state", "opencode-task-output");
  const byWorkIdPath = path.join(baseDir, "by-work-id", `${safeFileStem(workItem.work_id)}.json`);
  const text = `${JSON.stringify(artifact, null, 2)}\n`;
  await fs.mkdir(path.dirname(byWorkIdPath), { recursive: true });
  await fs.writeFile(byWorkIdPath, text, "utf8");
}

function looksLikeRawContent(prompt) {
  const text = String(prompt || "");
  return (
    /(^|\n)---\s*\n[\s\S]{0,200}\n---\s*\n#\s+/m.test(text) ||
    /\bChats_Raw\b|\bWiki_Medicina\b|\[Chat Original\]|gemini\.google\.com\/app\//i.test(text) ||
    /<(?:!doctype|html|body|article|section)\b/i.test(text)
  );
}

function promptEmbedsRawContent(prompt) {
  const packet = parseJsonObjectFromText(prompt);
  const items = Array.isArray(packet.current_batch_items) ? packet.current_batch_items : [];
  if (items.length === 1 && Object.keys(packet).length === 1) {
    return workItemEmbedsRawContent(items[0]);
  }
  return looksLikeRawContent(prompt);
}

function workItemEmbedsRawContent(value, parentKey = "") {
  if (Array.isArray(value)) return value.some((item) => workItemEmbedsRawContent(item, parentKey));
  if (!value || typeof value !== "object") {
    if (typeof value !== "string") return false;
    const key = String(parentKey || "").toLowerCase();
    if (OPENCODE_SPECIALIST_SAFE_TEXT_KEYS.has(key)) return false;
    return looksLikeRawContent(value);
  }
  for (const [key, item] of Object.entries(value)) {
    const normalizedKey = String(key || "").toLowerCase();
    if (OPENCODE_SPECIALIST_RAW_CONTENT_KEYS.has(normalizedKey)) return true;
    if (workItemEmbedsRawContent(item, normalizedKey)) return true;
  }
  return false;
}

async function writeNativeTaskMetadata(input, output = {}) {
  const toolName = String(input?.tool || output?.tool || input?.part?.tool || output?.part?.tool || "").toLowerCase();
  if (toolName !== "task") return;
  const state = firstObjectValue(output?.state, input?.state, output?.part?.state, input?.part?.state);
  const status = String(state.status || output?.status || input?.status || "").toLowerCase();
  if (status && status !== "completed") return;
  const args = firstObjectValue(output?.args, input?.args, state.input, output?.input, input?.input);
  const prompt = String(args?.prompt || "");
  const workItem = typedWorkItemFromPrompt(prompt);
  if (!workItem.work_id) return;
  const metadata = firstObjectValue(
    output?.metadata,
    input?.metadata,
    state.metadata,
    output?.part?.state?.metadata,
    input?.part?.state?.metadata,
  );
  const model = objectValue(metadata.model);
  const taskSessionId = String(metadata.sessionId || "");
  const parentSessionId = String(metadata.parentSessionId || input?.sessionID || "");
  const providerId = String(model.providerID || "");
  const modelId = String(model.modelID || "");
  if (!taskSessionId || !parentSessionId || !providerId || !modelId) return;

  const payload = {
    schema: OPENCODE_TASK_METADATA_SCHEMA,
    work_id: String(workItem.work_id),
    task_id: taskSessionId,
    parent_session_id: parentSessionId,
    specialist_session_id: taskSessionId,
    provider_id: providerId,
    model_id: modelId,
    model_tier: "specialist",
    tool_sequence: ["task"],
    prompt_contract: "single_current_batch_items_json",
    raw_content_embedded: promptEmbedsRawContent(prompt),
    capture_source: "opencode_tool_execute_after",
    capture_session_id: String(input?.sessionID || parentSessionId),
    tool_call_id: String(
      input?.callID ||
        input?.callId ||
        output?.callID ||
        output?.callId ||
        input?.part?.callID ||
        output?.part?.callID ||
        "",
    ),
    tool_prompt_sha256: sha256Text(prompt),
    tool_response_sha256: sha256Text(output?.output || state.output || input?.output || ""),
    captured_at: new Date().toISOString(),
  };
  const baseDir = path.join(appHomeDir(), "hook-state", "opencode-task-metadata");
  const byWorkIdPath = path.join(baseDir, "by-work-id", `${safeFileStem(workItem.work_id)}.json`);
  const bySessionPath = path.join(
    baseDir,
    "by-session",
    safeFileStem(parentSessionId),
    `${safeFileStem(workItem.work_id)}.json`,
  );
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  await fs.mkdir(path.dirname(byWorkIdPath), { recursive: true });
  await fs.writeFile(byWorkIdPath, text, "utf8");
  await fs.mkdir(path.dirname(bySessionPath), { recursive: true });
  await fs.writeFile(bySessionPath, text, "utf8");
  await writeArchitectOutputArtifact(workItem, String(output?.output || state.output || input?.output || ""));
}

export const server = async () => ({
  config: async (config) => {
    applyMedNotesOpenCodeRuntimeConfig(config);
  },
  "experimental.chat.system.transform": async (input, output) => {
    const result = await injectAgentDirectiveBeforeAgent({
      hook_event_name: "BeforeAgent",
      runtime: "opencode",
      opencode_payload_seen: true,
      session_id: String(input?.sessionID || ""),
    });
    const context = result?.hookSpecificOutput?.additionalContext;
    if (context) output.system.push(context);
  },
  "tool.execute.before": async (input, output) => {
    const result = await guardAgentDirectiveBeforeTool(hookPayload(input, output, "PreToolUse"));
    const message = denialMessage(result);
    if (message) throw new Error(`mednotes_fsm_hook_blocked: ${message}`);
  },
  "tool.execute.after": async (input, output) => {
    await writeNativeTaskMetadata(input, output);
    await captureAgentDirectiveAfterTool(hookPayload(input, output, "AfterTool"));
  },
});

export const MedNotesFSM = server;
MedNotesFSM.id = "medical-notes-workbench-fsm";
MedNotesFSM.server = server;

export default MedNotesFSM;
