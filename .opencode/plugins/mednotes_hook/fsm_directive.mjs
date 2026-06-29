import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {
  firstObject,
  isAgyRuntime,
  normalizedToolName,
  runtimeFromPayload,
  sessionIdFromPayload,
  toolCommandLine,
  toolInput,
} from "./adapters/harness_payload.mjs";
import {
  contextFromDirective,
  contextFromWorkflowIntent,
  controlStatus,
  directiveAllowsFinalReport,
  directiveAllowsPauseReport,
  isAgentDirective,
  isAgentDirectiveCarrierPayload,
  limitIsFalse,
} from "./domain/agent_directive_core.mjs";
import { deny, quiet } from "./runtime.mjs";

const CARD_SCHEMA = "medical-notes-workbench.workflow-hook-directive-card.v1";
const INTENT_SCHEMA = "medical-notes-workbench.workflow-hook-intent.v1";
const SCRIPT_EXTENSIONS = new Set([".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".cmd", ".bat"]);
const UNSUPPORTED_TOOL_PARAMETERS = new Set(["wait_for_previous"]);
const AGY_TOOL_METADATA_PARAMETERS = new Set(["toolAction", "toolSummary", "IsSkillFile"]);
const PRE_PAYLOAD_BLOCKED_TOOLS = new Set([
  "ask_permission",
  "call_mcp_tool",
  "grep_search",
  "invoke_agent",
  "invoke_subagent",
  "list_dir",
  "list_directory",
  "list_permissions",
  "read_url_content",
  "schedule",
]);
const PUBLIC_WORKFLOWS = new Set([
  "/flashcards",
  "/mednotes:fix-wiki",
  "/mednotes:history",
  "/mednotes:link",
  "/mednotes:link-related",
  "/mednotes:process-chats",
  "/mednotes:setup",
]);
const MUTATING_TOOLS = new Set([
  "bash",
  "edit",
  "multiedit",
  "multi_replace_file_content",
  "powershell",
  "pwsh",
  "replace",
  "replace_file_content",
  "run_command",
  "run_shell",
  "run_shell_command",
  "shell",
  "shelltool",
  "write",
  "write_file",
  "write_to_file",
]);
const TERMINAL_STATUSES = new Set(["completed", "completed_with_warnings"]);
const MAX_TASK_LOG_BYTES = 2 * 1024 * 1024;
const SPECIALIST_WORK_ITEM_RAW_CONTENT_KEYS = new Set([
  "content",
  "html",
  "markdown",
  "note_text",
  "raw",
  "raw_chat",
  "raw_chat_content",
  "raw_markdown",
  "raw_markdown_content",
]);
const SPECIALIST_WORK_ITEM_SAFE_TEXT_KEYS = new Set([
  "agent",
  "attestation_created_by",
  "coverage_path",
  "expected_model",
  "item_type",
  "missing_specialist_task_run_receipt_action",
  "model_policy",
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
  "target_kind",
  "target_path",
  "taxonomy",
  "temp_output",
  "temp_output_path",
  "title",
  "work_id",
  "write_markdown_to",
  "write_policy",
]);

export async function captureAgentDirectiveAfterTool(payload) {
  const sessionId = sessionIdFromPayload(payload);
  const responsePayload = officialWorkflowPayloadFromToolResponse(payload);
  let workflowPayload = isAgentDirectiveCarrierPayload(responsePayload) ? responsePayload : {};
  if (!isAgentDirectiveCarrierPayload(workflowPayload)) {
    const artifactPayload = await officialWorkflowPayloadFromToolArtifact(payload);
    workflowPayload = isAgentDirectiveCarrierPayload(artifactPayload) ? artifactPayload : {};
  }
  const directive = directiveFromWorkflowPayload(workflowPayload);
  if (!directive) return quiet();

  if (!sessionId) return quiet();

  if (directiveAllowsFinalReport(directive) && TERMINAL_STATUSES.has(controlStatus(directive))) {
    await clearDirectiveCard(sessionId);
    return quiet();
  }

  const now = new Date();
  const card = {
    schema: CARD_SCHEMA,
    session_id: sessionId,
    runtime: runtimeFromPayload(payload),
    captured_at: now.toISOString(),
    expires_at: new Date(now.getTime() + ttlMs()).toISOString(),
    source: {
      hook_event: String(payload?.hook_event_name || "AfterTool"),
      tool_name: String(payload?.tool_name || ""),
      payload_hash: `sha256:${sha256(canonicalJson(workflowPayload))}`,
    },
    directive,
    enforcement: {
      mode: "p0",
      p0_blocking_enabled: true,
      ...expectedContinuationEnforcement(workflowPayload),
    },
  };
  await writeDirectiveCard(card);
  await clearWorkflowIntent(sessionId);
  return quiet();
}

export async function injectAgentDirectiveBeforeAgent(payload) {
  const sessionId = sessionIdFromPayload(payload);
  const card = await readActiveDirectiveCard(sessionId);
  if (!card) {
    const intent = (await readActiveWorkflowIntent(sessionId)) || (await recordWorkflowIntentFromPayload(payload));
    if (!intent) return quiet();
    const context = contextFromWorkflowIntent(intent);
    if (!context) return quiet();
    return {
      suppressOutput: true,
      hookSpecificOutput: {
        additionalContext: context,
      },
    };
  }
  const context = contextFromDirectiveCard(card, payload);
  if (!context) return quiet();
  return {
    suppressOutput: true,
    hookSpecificOutput: {
      additionalContext: context,
    },
  };
}

export async function guardAgentDirectiveBeforeTool(payload) {
  const globalGuard = guardAgyOperationalToolContract(payload);
  if (globalGuard) return globalGuard;

  const card = await readActiveDirectiveCard(sessionIdFromPayload(payload));
  if (!card) {
    const intent = await readActiveWorkflowIntent(sessionIdFromPayload(payload));
    if (!intent) return quiet();
    return guardWorkflowIntentBeforeTool(payload, intent);
  }
  const directive = card.directive;
  const input = toolInput(payload);
  const unsupported = firstUnsupportedParameter(input);
  if (unsupported) {
    return denyP0({
      blockedReason: "unsupported_tool_parameter",
      reason: `Bloqueei a chamada porque o parâmetro \`${unsupported}\` não existe no contrato da tool.`,
      agentMessage: `Remova \`${unsupported}\`; agent hooks não aceitam parâmetros de ferramenta inventados.`,
      directiveField: "tool.parameters",
    });
  }
  if (toolRunsRetiredSpecialistRunner(payload)) {
    return denyP0({
      blockedReason: "retired_specialist_runner_command",
      reason: "run-specialist-task foi removido da superfície pública; a FSM deve expor call_specialist_model.",
      agentMessage:
        "Consuma agent_directive.control.effects[].payload.current_batch_items pelo harness atual; não chame run-specialist-task.",
      directiveField: "agent_directive.control.effects",
    });
  }

  const status = controlStatus(directive);
  if (status === "waiting_human" && toolLooksMutating(payload)) {
    return denyP0({
      blockedReason: "human_decision_required",
      reason: "human_decision_required: Bloqueei a mutação porque a FSM aguarda decisão humana.",
      agentMessage: "Peça a decisão humana exposta pelo workflow oficial antes de aplicar ou publicar.",
      directiveField: "agent_directive.control.status=waiting_human",
    });
  }
  if (status === "waiting_external" && toolLooksMutating(payload)) {
    return denyP0({
      blockedReason: "external_resource_wait_required",
      reason: "Bloqueei a mutação porque a FSM aguarda recurso externo.",
      agentMessage: "Aguarde a retomada oficial; não repita mutações enquanto o workflow está em waiting_external.",
      directiveField: "agent_directive.control.status=waiting_external",
    });
  }
  if ((status === "blocked" || status === "failed") && !toolRunsVaultGuardFinish(payload)) {
    return denyP0({
      blockedReason: "workflow_blocked_no_continuation",
      reason: "Bloqueei a chamada porque a FSM registrou bloqueio sem continuação executável.",
      agentMessage: [
        "Feche a proteção do vault se houver lease ativa e reporte o bloqueio em linguagem pública; ",
        "não abra novo subagente, não rode nova conferência e não investigue código para contornar o bloqueio.",
      ].join(""),
      directiveField: `agent_directive.control.status=${status}`,
    });
  }
  if (!directiveAllowsFinalReport(directive) && toolRunsAgentReportValidation(payload)) {
    return denyP0({
      blockedReason: "agent_report_validation_premature",
      reason: [
        "agent_report_validation_premature: Bloqueei validate-agent-run-report porque ",
        "agent_directive.control.capabilities.final_report=false. ",
        "Siga agent_directive.control.effects antes de validar ou concluir relatório final.",
      ].join(""),
      agentMessage:
        "Continue pelo agent_directive.control.effects da FSM; validação de relatório final só depois de final_report=true.",
      directiveField: "agent_directive.control.capabilities.final_report=false",
    });
  }
  if (limitIsFalse(directive, "ad_hoc_scripts") && !toolCommandLine(payload) && toolCreatesAdHocScript(payload)) {
    return denyP0({
      blockedReason: "ad_hoc_script_forbidden",
      reason: "Bloqueei script ad hoc porque a FSM exige rota oficial.",
      agentMessage: "Use a rota oficial do Workbench; agent_directive.control.limits.ad_hoc_scripts=false.",
      directiveField: "agent_directive.control.limits.ad_hoc_scripts=false",
    });
  }
  const opencodeTaskPromptGuard = guardOpenCodeTaskPromptContract(payload, directive);
  if (opencodeTaskPromptGuard) return opencodeTaskPromptGuard;
  if (
    limitIsFalse(directive, "raw_content") &&
    toolCanCarryOperationalRawContent(payload) &&
    toolContainsRawContent(input)
  ) {
    return denyP0({
      blockedReason: "raw_content_forbidden",
      reason: "Bloqueei conteúdo bruto porque a FSM proíbe colar Markdown/chat/HTML no payload da ferramenta.",
      agentMessage: "Passe paths, hashes e work_item tipado; agent_directive.control.limits.raw_content=false.",
      directiveField: "agent_directive.control.limits.raw_content=false",
    });
  }
  const effectGuard = guardWaitingAgentConcreteEffects(payload, directive);
  if (effectGuard) return effectGuard;
  if (limitIsFalse(directive, "ad_hoc_scripts") && toolCreatesAdHocScript(payload)) {
    return denyP0({
      blockedReason: "ad_hoc_script_forbidden",
      reason: "Bloqueei script ad hoc porque a FSM exige rota oficial.",
      agentMessage: "Use a rota oficial do Workbench; agent_directive.control.limits.ad_hoc_scripts=false.",
      directiveField: "agent_directive.control.limits.ad_hoc_scripts=false",
    });
  }
  return quiet();
}

function contextFromDirectiveCard(card, payload) {
  const baseContext = contextFromDirective(card.directive);
  const runtimeContext = opencodeSpecialistTaskContext(card.directive, payload);
  const context = [baseContext, runtimeContext].filter(Boolean).join("\n");
  return context.length <= 2600 ? context : `${context.slice(0, 2597).trimEnd()}...`;
}

function opencodeSpecialistTaskContext(directive, payload) {
  if (runtimeFromPayload(payload) !== "opencode") return "";
  if (!directiveHasCurrentBatchItems(directive)) return "";
  return [
    "MEDNOTES OPENCODE EFFECT ROUTE",
    "instruction: call the native task tool once per current_batch_items member.",
    'instruction: task.prompt must be exactly JSON.stringify({"current_batch_items":[one item from agent_directive.control.effects[].payload.current_batch_items]}); no prose, labels, Markdown, target_path lines or duplicated root fields.',
    "instruction: after task completes, run the official finalizer directly; do not read hook-state metadata.",
  ].join("\n");
}

function directiveHasCurrentBatchItems(directive) {
  const effects = Array.isArray(directive?.control?.effects) ? directive.control.effects : [];
  return effects.some((effect) => {
    const payload = effect?.payload && typeof effect.payload === "object" ? effect.payload : {};
    return Array.isArray(payload.current_batch_items) && payload.current_batch_items.length > 0;
  });
}

function guardOpenCodeTaskPromptContract(payload, directive) {
  if (runtimeFromPayload(payload) !== "opencode") return null;
  if (normalizedToolName(payload) !== "task") return null;
  if (!directiveHasCurrentBatchItems(directive)) return null;
  const prompt = String(toolInput(payload).prompt || "");
  const parsed = parseWholeJsonObject(prompt);
  const isPureSingleItemPacket =
    parsed &&
    Object.keys(parsed).length === 1 &&
    Array.isArray(parsed.current_batch_items) &&
    parsed.current_batch_items.length === 1;
  if (isPureSingleItemPacket) return null;
  return denyP0({
    blockedReason: "opencode_task_prompt_contract_violation",
    reason: [
      "opencode_task_prompt_contract_violation: Bloqueei a task porque o prompt do OpenCode ",
      'precisa ser JSON puro com raiz {"current_batch_items":[...]} e exatamente um item vindo ',
      "de agent_directive.control.effects[].payload.current_batch_items.",
    ].join(""),
    agentMessage: [
      'Repita a mesma task com prompt exatamente JSON.stringify({"current_batch_items":[item]}); ',
      "sem prosa, sem rótulos Contract/Target, sem Markdown e sem campos duplicados no root.",
    ].join(""),
    directiveField: "agent_directive.control.effects[].payload.current_batch_items",
  });
}

function guardAgyOperationalToolContract(payload) {
  if (!isAgyRuntime(payload)) return null;
  if (toolWritesArtifactMetadataToWorkbenchTemp(payload)) {
    return denyP0({
      blockedReason: "agy_artifact_metadata_forbidden_for_operational_output",
      reason:
        "Bloqueei ArtifactMetadata em arquivo operacional do Workbench porque o AGY trata isso como artifact path e rejeita o temp_output.",
      agentMessage: [
        "Repita write_to_file sem ArtifactMetadata. O TargetFile operacional em mednotes-home/tmp/agent-work ",
        "não é artifact do AGY; ele deve ser escrito como arquivo comum.",
      ].join(""),
      directiveField: "subagent_output_contract.write_markdown_to=temp_output",
    });
  }
  return null;
}

async function guardWorkflowIntentBeforeTool(payload, intent) {
  const input = toolInput(payload);
  if (await isOfficialTaskLogRead(payload, intent)) return quiet();
  if (isAllowedPrePayloadTool(payload, intent)) return quiet();

  const unsupportedMetadata = firstUnsupportedParameter(input, 0, AGY_TOOL_METADATA_PARAMETERS);
  if (unsupportedMetadata) {
    return denyP0({
      blockedReason: "unsupported_tool_parameter",
      reason: `Bloqueei a chamada porque \`${unsupportedMetadata}\` e metadado inventado para esta ferramenta AGY; remova esse campo e repita a mesma ferramenta.`,
      agentMessage: `Remova \`${unsupportedMetadata}\` e repita a mesma ferramenta sem metadados inventados; depois siga a rota oficial de ${intent.workflow}.`,
      directiveField: "workflow_intent.before_payload.tool.parameters",
    });
  }

  const toolName = normalizedToolName(payload);
  if (PRE_PAYLOAD_BLOCKED_TOOLS.has(toolName) || !isOfficialPrePayloadCommand(payload, intent)) {
    return denyP0({
      blockedReason: "workflow_payload_missing",
      reason: `Bloqueei ${toolName || "tool"} porque ${intent.workflow} ainda nao emitiu payload oficial.`,
      agentMessage: `Nao use ${toolName || "esta ferramenta"} para auto-debug antes do payload oficial. Carregue a skill oficial se necessario, abra run-start e execute a rota publica do workflow.`,
      directiveField: "workflow_intent.stage=before_workflow_payload",
    });
  }
  return quiet();
}

export async function validateAgentDirectiveAfterAgent(payload) {
  const card = await readActiveDirectiveCard(sessionIdFromPayload(payload));
  if (!card) return quiet();
  const directive = card.directive;
  const hookEvent = String(payload?.hook_event_name || payload?.hookEventName || "").toLowerCase();
  if (!directiveAllowsFinalReport(directive) && hookEvent === "stop" && !directiveAllowsPauseReport(directive)) {
    return denyP0({
      blockedReason: "final_report_forbidden_by_agent_directive",
      reason: "agent_directive.control.capabilities.final_report=false; o workflow ainda não autoriza relatório final.",
      agentMessage: "Continue pela rota oficial antes de declarar conclusão.",
      directiveField: "agent_directive.control.capabilities.final_report=false",
    });
  }
  return quiet();
}

export function officialWorkflowPayloadFromToolResponse(payload) {
  const response = firstObject(payload?.tool_response, payload?.toolResponse, payload?.response, payload?.result);
  if (isAgentDirectiveCarrierPayload(response)) return response;
  for (const value of responseCandidates(response)) {
    const parsed = parseWholeJsonObject(value);
    if (isAgentDirectiveCarrierPayload(parsed)) return parsed;
    const embedded = parseOfficialWorkflowPayloadFromText(value);
    if (isAgentDirectiveCarrierPayload(embedded)) return embedded;
    if (looksLikeAgyTaskLogViewOutput(payload, value)) {
      const embeddedFromView = parseOfficialWorkflowPayloadFromText(stripViewFileLineNumbers(value));
      if (isAgentDirectiveCarrierPayload(embeddedFromView)) return embeddedFromView;
    }
  }
  return {};
}

async function officialWorkflowPayloadFromToolArtifact(payload) {
  if (!toolLooksLikeFileRead(payload)) return {};
  const target = toolTargetPath(payload);
  if (!isAgyTaskLogPath(target)) return {};
  try {
    const stats = await fs.stat(target);
    if (!stats.isFile() || stats.size > MAX_TASK_LOG_BYTES) return {};
    const text = await fs.readFile(target, "utf8");
    const artifactPayload = parseWholeJsonObject(text);
    if (!isCorrelatedTaskLogArtifact(payload, artifactPayload)) return {};
    return parseOfficialWorkflowPayloadFromText(text);
  } catch {
    return {};
  }
}

function isCorrelatedTaskLogArtifact(payload, artifactPayload) {
  // Reading a task log by path is not enough to establish ownership. AGY task
  // logs are accepted only when the file proves it belongs to this exact tool
  // call and response; otherwise a stale log could inject a directive card.
  const artifactCall = firstObject(artifactPayload?.toolCall, artifactPayload?.tool_call);
  const artifactResponse = firstObject(artifactPayload?.toolResponse, artifactPayload?.tool_response);
  const currentCall = firstObject(payload?.toolCall, payload?.tool_call);
  const currentResponse = firstObject(
    payload?.toolResponse,
    payload?.tool_response,
    payload?.response,
    payload?.result,
  );
  const artifactCallId = stableToolId(artifactCall);
  const artifactResponseId = stableToolId(artifactResponse);
  const currentCallId = stableToolId(currentCall);
  const currentResponseId = stableToolId(currentResponse);
  return Boolean(
    currentCallId && currentResponseId && artifactCallId === currentCallId && artifactResponseId === currentResponseId,
  );
}

function stableToolId(value) {
  const raw = value?.id || value?.toolCallId || value?.tool_call_id || value?.callId || value?.call_id || "";
  return typeof raw === "string" && raw.trim() ? raw.trim() : "";
}

function directiveFromWorkflowPayload(payload) {
  if (!isAgentDirectiveCarrierPayload(payload)) return null;
  const directive = payload.agent_directive;
  if (!isAgentDirective(directive)) return null;
  return JSON.parse(canonicalJson(directive));
}

function expectedContinuationEnforcement(payload) {
  // Enforcement card data is metadata about the current FSM payload only. It
  // must not synthesize runtime-specific continuation tasks from legacy
  // diagnostic_context, run records, phase names, or workflow-specific payloads.
  void payload;
  return {};
}

function responseCandidates(response) {
  const candidates = [];
  if (!response || typeof response !== "object") return candidates;
  for (const key of ["returnDisplay", "llmContent", "stdout", "output", "content", "text"]) {
    const value = response[key];
    if (typeof value === "string") candidates.push(value);
  }
  return candidates;
}

function parseWholeJsonObject(value) {
  const text = String(value || "").trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function parseOfficialWorkflowPayloadFromText(value) {
  // The name is kept for stable callers: the parser now returns any official
  // directive carrier, not only root FSM payloads.
  const text = String(value || "");
  let index = 0;
  while (index < text.length) {
    const start = text.indexOf('{"schema"', index);
    if (start < 0) return {};
    const candidate = balancedJsonObjectAt(text, start);
    if (!candidate) {
      index = start + 1;
      continue;
    }
    const parsed = parseWholeJsonObject(candidate);
    if (isAgentDirectiveCarrierPayload(parsed)) return parsed;
    index = start + 1;
  }
  return {};
}

function looksLikeAgyTaskLogViewOutput(payload, value) {
  if (!toolLooksLikeFileRead(payload)) return false;
  const text = String(value || "");
  return text.includes("/.system_generated/tasks/task-") && text.includes(".log") && text.includes('{"schema"');
}

function stripViewFileLineNumbers(value) {
  return String(value || "").replace(/(^|\n)\s*\d+:\s+\{/g, "$1{");
}

function balancedJsonObjectAt(text, start) {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === '"') {
        inString = false;
      }
      continue;
    }
    if (char === '"') {
      inString = true;
    } else if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }
  return "";
}

function denyP0({ blockedReason, reason, agentMessage, directiveField }) {
  return deny(reason, {
    status: "blocked_agent_directive_enforcement",
    blocked_reason: blockedReason,
    agent_message: agentMessage,
    directive_field: directiveField,
  });
}

function appHomeDir() {
  return process.env.MEDNOTES_HOME || path.join(os.homedir(), ".mednotes");
}

function stateDir() {
  return path.join(appHomeDir(), "hook-state", "fsm-directive");
}

function cardPath(sessionId) {
  return path.join(stateDir(), `${sessionId}.json`);
}

function intentStateDir() {
  return path.join(appHomeDir(), "hook-state", "workflow-intent");
}

function intentPath(sessionId) {
  return path.join(intentStateDir(), `${sessionId}.json`);
}

async function writeDirectiveCard(card) {
  await fs.mkdir(stateDir(), { recursive: true });
  const file = cardPath(card.session_id);
  const tmp = `${file}.tmp`;
  await fs.writeFile(tmp, `${JSON.stringify(card, null, 2)}\n`, "utf8");
  await fs.rename(tmp, file);
}

async function clearDirectiveCard(sessionId) {
  try {
    await fs.unlink(cardPath(sessionId));
  } catch {
    // Stale or missing card is already safe.
  }
}

async function readActiveDirectiveCard(sessionId) {
  if (!sessionId) return null;
  try {
    const parsed = JSON.parse(await fs.readFile(cardPath(sessionId), "utf8"));
    if (!parsed || parsed.schema !== CARD_SCHEMA || !isAgentDirective(parsed.directive)) return null;
    const expiresAt = Date.parse(String(parsed.expires_at || ""));
    if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
      await clearDirectiveCard(sessionId);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

async function recordWorkflowIntentFromPayload(payload) {
  // pre-payload guard only: this records that a public workflow was invoked
  // before official JSON exists. It does not create agent_directive and cannot
  // satisfy waiting_agent continuation after a real FSM payload is captured.
  const sessionId = sessionIdFromPayload(payload);
  if (!sessionId) return null;
  const workflow = await workflowFromPayload(payload);
  if (!workflow) return null;
  const now = new Date();
  const intent = {
    schema: INTENT_SCHEMA,
    session_id: sessionId,
    runtime: runtimeFromPayload(payload),
    workflow,
    stage: "before_workflow_payload",
    captured_at: now.toISOString(),
    expires_at: new Date(now.getTime() + ttlMs()).toISOString(),
    source: {
      hook_event: String(payload?.hook_event_name || payload?.hookEventName || "PreInvocation"),
      transcript_seen: Boolean(payload?.transcript_path),
    },
  };
  await writeWorkflowIntent(intent);
  return intent;
}

async function writeWorkflowIntent(intent) {
  await fs.mkdir(intentStateDir(), { recursive: true });
  const file = intentPath(intent.session_id);
  const tmp = `${file}.tmp`;
  await fs.writeFile(tmp, `${JSON.stringify(intent, null, 2)}\n`, "utf8");
  await fs.rename(tmp, file);
}

async function readActiveWorkflowIntent(sessionId) {
  if (!sessionId) return null;
  try {
    const parsed = JSON.parse(await fs.readFile(intentPath(sessionId), "utf8"));
    if (!parsed || parsed.schema !== INTENT_SCHEMA || !PUBLIC_WORKFLOWS.has(String(parsed.workflow || ""))) {
      return null;
    }
    const expiresAt = Date.parse(String(parsed.expires_at || ""));
    if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
      await clearWorkflowIntent(sessionId);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

async function clearWorkflowIntent(sessionId) {
  try {
    await fs.unlink(intentPath(sessionId));
  } catch {
    // Stale or missing intent is already safe.
  }
}

async function workflowFromPayload(payload) {
  const direct = workflowFromText(
    [
      payload?.prompt,
      payload?.user_input,
      payload?.userInput,
      payload?.message,
      payload?.content,
      payload?.request,
      payload?.userRequest,
    ]
      .filter((value) => typeof value === "string")
      .join("\n"),
  );
  if (direct) return direct;
  return workflowFromText(await transcriptUserText(payload?.transcript_path));
}

async function transcriptUserText(transcriptPath) {
  const file = String(transcriptPath || "");
  if (!file) return "";
  try {
    const stats = await fs.stat(file);
    if (!stats.isFile() || stats.size > 2 * 1024 * 1024) return "";
    const text = await fs.readFile(file, "utf8");
    const excerpts = [];
    for (const line of text.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const record = JSON.parse(trimmed);
        if (!record || typeof record !== "object" || Array.isArray(record)) continue;
        const source = String(record.source || record.role || record.type || "");
        if (/USER/i.test(source)) excerpts.push(String(record.content || record.text || record.message || ""));
      } catch {
        excerpts.push(trimmed);
      }
      if (excerpts.join("\n").length > 12000) break;
    }
    return excerpts.join("\n");
  } catch {
    return "";
  }
}

function workflowFromText(value) {
  const text = String(value || "");
  for (const workflow of PUBLIC_WORKFLOWS) {
    if (new RegExp(`${escapeRegExp(workflow)}(?:\\s|$)`).test(text)) return workflow;
  }
  return "";
}

function ttlMs() {
  const hours = Number(process.env.MEDNOTES_FSM_HOOK_TTL_HOURS || "6");
  const safeHours = Number.isFinite(hours) ? Math.min(24, Math.max(1, hours)) : 6;
  return safeHours * 60 * 60 * 1000;
}

function canonicalJson(value) {
  return JSON.stringify(sortJson(value));
}

function sortJson(value) {
  if (Array.isArray(value)) return value.map(sortJson);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, sortJson(item)]),
  );
}

function sha256(value) {
  return crypto
    .createHash("sha256")
    .update(String(value || ""), "utf8")
    .digest("hex");
}

function toolLooksMutating(payload) {
  const name = normalizedToolName(payload);
  if (MUTATING_TOOLS.has(name)) return true;
  const command = toolCommandLine(payload);
  return /\b(write|replace|edit|apply|publish|rm|mv|cp|sed|perl|python|node|bash|pwsh|powershell)\b/i.test(command);
}

function toolRunsRetiredSpecialistRunner(payload) {
  return /\brun-specialist-task\b/.test(toolCommandLine(payload));
}

function toolCanCarryOperationalRawContent(payload) {
  const name = normalizedToolName(payload);
  if (["todo", "todowrite", "todo_write", "todoread", "todo_read"].includes(name)) return false;
  if (
    [
      "bash",
      "run_shell_command",
      "task",
      "invoke_subagent",
      "send_message",
      "write_file",
      "write_to_file",
      "edit",
      "replace",
    ].includes(name)
  )
    return true;
  return toolLooksMutating(payload);
}

function guardWaitingAgentConcreteEffects(payload, directive) {
  if (controlStatus(directive) !== "waiting_agent") return null;
  const routes = concreteEffectRoutes(directive);
  if (!routes.length) {
    if (!toolLooksOperationalWhileEffectPending(payload)) return null;
    return denyP0({
      blockedReason: "agent_directive_effect_required",
      reason: [
        "agent_directive_effect_required: Bloqueei a chamada porque a FSM está em waiting_agent, ",
        "mas agent_directive.control.effects não contém rota concreta executável. ",
        "Trate isso como contract_gap do produtor da FSM; não continue por texto de resume, fase ou histórico.",
      ].join(""),
      agentMessage:
        "A FSM precisa emitir agent_directive.control.effects com payload executável antes de qualquer continuação agentica.",
      directiveField: "agent_directive.control.effects",
    });
  }
  if (toolMatchesConcreteEffectRoute(payload, routes)) return null;
  const defineSubagentGuard = guardPackagedDefineSubagentRoute(payload, routes);
  if (defineSubagentGuard) return defineSubagentGuard;
  if (!toolLooksOperationalWhileEffectPending(payload)) return null;
  return denyP0({
    blockedReason: "agent_directive_effect_required",
    reason: [
      "agent_directive_effect_required: Bloqueei a chamada porque a FSM já expôs efeito(s) ",
      "executável(is) em agent_directive.control.effects. Execute a rota tipada do efeito atual ",
      "ou aguarde novo payload oficial; não rederive a próxima ação por diagnóstico, fase ou histórico.",
    ].join(""),
    agentMessage:
      "Use somente a rota concreta exposta por agent_directive.control.effects para continuar este estado waiting_agent.",
    directiveField: "agent_directive.control.effects",
  });
}

function guardPackagedDefineSubagentRoute(payload, routes) {
  if (normalizedToolName(payload) !== "define_subagent") return null;
  if (!routes.some((route) => route.kind === "define_subagent")) return null;
  return denyP0({
    blockedReason: "packaged_agent_template_required",
    reason: [
      "packaged_agent_template_required: Bloqueei define_subagent porque TypeName sozinho ",
      "nao prova que o agente empacotado completo foi usado. Leia o template oficial e passe ",
      "o prompt com packaged_agent_template_contract antes de definir o subagente.",
    ].join(""),
    agentMessage:
      "Use o template empacotado completo e preserve packaged_agent_template_contract no prompt de define_subagent.",
    directiveField: "agent_directive.control.effects[].payload.harness_routes",
  });
}

function concreteEffectRoutes(directive) {
  const routes = [];
  const effects = Array.isArray(directive?.control?.effects) ? directive.control.effects : [];
  for (const effect of effects) {
    if (!effect || typeof effect !== "object" || Array.isArray(effect)) continue;
    const payload =
      effect.payload && typeof effect.payload === "object" && !Array.isArray(effect.payload) ? effect.payload : {};
    const commandFamily = String(payload.command_family || effect.target || "").trim();
    const args = Array.isArray(payload.arguments) ? payload.arguments.map((item) => String(item || "")) : [];
    if (commandFamily && args.length) routes.push({ kind: "command", commandFamily, args });
    addCommandRoute(routes, payload.apply_command);
    const finalizers = Array.isArray(payload.receipt_finalizers) ? payload.receipt_finalizers : [];
    for (const finalizer of finalizers) addCommandRoute(routes, finalizer);
    addHarnessRoutes(routes, payload.harness_routes);
    const items = Array.isArray(payload.current_batch_items) ? payload.current_batch_items : [];
    if (items.length > 0) {
      routes.push({
        kind: "current_batch_items",
        currentBatchItemsJson: canonicalJson({ current_batch_items: items }),
      });
      // The FSM authorizes the whole specialist batch. Harnesses such as
      // OpenCode may execute that batch as one task per member, so each member
      // is an allowed concrete route without becoming a new policy decision.
      for (const item of items) {
        if (!item || typeof item !== "object" || Array.isArray(item)) continue;
        routes.push({
          kind: "single_current_batch_item",
          currentBatchItemJson: canonicalJson(item),
        });
        routes.push({
          kind: "single_current_batch_item",
          currentBatchItemJson: canonicalJson({ current_batch_items: [item] }),
        });
      }
    }
  }
  return routes;
}

function addCommandRoute(routes, value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const commandFamily = String(value.command_family || "").trim();
  const args = Array.isArray(value.arguments) ? value.arguments.map((item) => String(item || "")) : [];
  if (commandFamily && args.length) routes.push({ kind: "command", commandFamily, args });
}

function addHarnessRoutes(routes, value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const agy = value.antigravity_cli && typeof value.antigravity_cli === "object" ? value.antigravity_cli : {};
  const templateCandidates = Array.isArray(agy.template_path_candidates) ? agy.template_path_candidates : [];
  const templatePaths = templateCandidates.map((candidate) => normalizePathText(candidate)).filter(Boolean);
  for (const candidate of templateCandidates) {
    const normalized = normalizePathText(candidate);
    if (normalized) routes.push({ kind: "template_path", path: normalized });
  }
  const agentName = String(agy.agent_name || "").trim();
  if (agy.define_subagent_source === "packaged_agent_template_only" && agentName) {
    routes.push({
      kind: "define_subagent",
      agentName,
      templatePaths,
      promptContract: String(agy.prompt_contract || "single_current_batch_items_json").trim(),
      packagedTemplateContract: String(
        agy.packaged_agent_template_contract || "medical-notes-workbench.packaged-agent-template.v1",
      ).trim(),
    });
  }
}

function toolMatchesConcreteEffectRoute(payload, routes) {
  const command = toolCommandLine(payload);
  if (command) {
    const invocation = parseSingleCommandInvocation(command);
    for (const route of routes) {
      if (route.kind !== "command") continue;
      if (invocation && commandInvocationMatchesRoute(invocation, route)) return true;
    }
  }
  const embedded = toolEmbeddedJsonObjects(payload);
  for (const route of routes) {
    if (route.kind !== "current_batch_items") continue;
    if (embedded.some((item) => canonicalJson(item) === route.currentBatchItemsJson)) return true;
  }
  for (const route of routes) {
    if (route.kind !== "single_current_batch_item") continue;
    if (embedded.some((item) => canonicalJson(item) === route.currentBatchItemJson)) return true;
  }
  const target = normalizePathText(toolTargetPath(payload));
  for (const route of routes) {
    if (route.kind !== "template_path") continue;
    if (target && target === route.path) return true;
  }
  if (normalizedToolName(payload) === "define_subagent") {
    for (const route of routes) {
      if (route.kind === "define_subagent" && packagedDefineSubagentMatchesRoute(payload, route)) return true;
    }
  }
  return false;
}

function parseSingleCommandInvocation(command) {
  // Command effects authorize one concrete CLI invocation. Text probes,
  // shell chaining and interpreter snippets must not satisfy a route.
  if (!commandLooksLikeSingleRouteInvocation(command)) return null;
  if (/(`|\$\(|>|<|\n|\r)/.test(command)) return null;
  const tokens = shellWords(command);
  if (!tokens?.length) return null;
  if (tokens.some((token) => shellControlToken(token))) return null;
  if (tokens.some((token) => interpreterExecutionFlag(token))) return null;
  const first = commandTokenBasename(tokens[0]);
  return {
    tokens,
    normalized: tokens.map((token) => normalizeCommandToken(token)),
    first,
  };
}

function shellWords(command) {
  const words = [];
  let current = "";
  let quote = "";
  let escaped = false;
  for (const char of String(command || "")) {
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) {
        quote = "";
      } else {
        current += char;
      }
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      continue;
    }
    if (/\s/.test(char)) {
      if (current) {
        words.push(current);
        current = "";
      }
      continue;
    }
    current += char;
  }
  if (escaped || quote) return null;
  if (current) words.push(current);
  return words;
}

function commandInvocationMatchesRoute(invocation, route) {
  const family = normalizeCommandFamily(route.commandFamily);
  if (!family || !commandExecutableCanHostRoute(invocation, family)) return false;
  const familyIndex = routeFamilyTokenIndex(invocation, family);
  if (familyIndex < 0) return false;
  return routeArgsMatch(invocation.normalized, route.args, familyIndex + 1);
}

function commandExecutableCanHostRoute(invocation, family) {
  if (invocation.first === family) return true;
  return ["uv", "python", "python3", "node", "run_python.mjs", "cli.py", "mednotes"].includes(invocation.first);
}

function routeFamilyTokenIndex(invocation, family) {
  for (let index = 0; index < invocation.normalized.length; index += 1) {
    const token = invocation.normalized[index];
    if (!tokenMatchesCommandFamily(token, family)) continue;
    if (index === 0) return index;
    if (previousTokenCanIntroduceSubcommand(invocation.normalized[index - 1])) return index;
  }
  return -1;
}

function previousTokenCanIntroduceSubcommand(token) {
  return ["cli.py", "mednotes", "wiki"].includes(commandTokenBasename(token));
}

function routeArgsMatch(tokens, expectedArgs, startIndex) {
  let cursor = startIndex;
  for (let index = 0; index < expectedArgs.length; index += 1) {
    const expected = normalizeCommandToken(expectedArgs[index]);
    if (!expected) continue;
    const nextExpected = index + 1 < expectedArgs.length ? normalizeCommandToken(expectedArgs[index + 1]) : "";
    const found = findExpectedArg(tokens, expected, nextExpected, cursor);
    if (!found) return false;
    cursor = found.index + 1;
    if (found.consumedNext) index += 1;
  }
  return cursor >= tokens.length;
}

function shellControlToken(token) {
  return /[;&|]/.test(String(token || ""));
}

function interpreterExecutionFlag(token) {
  const value = String(token || "");
  return value === "-c" || value === "--command" || value === "-e" || /^-[^-]*[ce]/.test(value);
}

function findExpectedArg(tokens, expected, nextExpected, startIndex) {
  for (let index = startIndex; index < tokens.length; index += 1) {
    if (tokens[index] === expected) return { index, consumedNext: false };
    if (expected.startsWith("--") && nextExpected && tokens[index] === `${expected}=${nextExpected}`) {
      return { index, consumedNext: true };
    }
  }
  return null;
}

function tokenMatchesCommandFamily(token, family) {
  const base = commandTokenBasename(token);
  return token === family || base === family;
}

function normalizeCommandFamily(value) {
  return commandTokenBasename(value);
}

function normalizeCommandToken(value) {
  return normalizePathText(value).trim();
}

function commandTokenBasename(value) {
  return path.posix.basename(normalizeCommandToken(value)).toLowerCase();
}

function packagedDefineSubagentMatchesRoute(payload, route) {
  const input = toolInput(payload);
  const typeName = String(input.TypeName || input.typeName || input.name || input.Name || "").trim();
  if (typeName !== route.agentName) return false;
  const prompt = String(input.Prompt || input.prompt || input.SystemPrompt || input.systemPrompt || "");
  if (!prompt) return false;
  if (!prompt.includes(route.agentName)) return false;
  if (!prompt.includes(route.packagedTemplateContract)) return false;
  const templatePath = toolTemplatePath(payload);
  if (templatePath && route.templatePaths?.length && !route.templatePaths.includes(templatePath)) return false;
  return true;
}

function toolTemplatePath(payload) {
  const input = toolInput(payload);
  return normalizePathText(
    input.TemplatePath ||
      input.templatePath ||
      input.SourcePath ||
      input.sourcePath ||
      input.TemplateFile ||
      input.templateFile ||
      "",
  );
}

function toolEmbeddedJsonObjects(payload) {
  const input = toolInput(payload);
  const candidates = [
    input.prompt,
    input.Prompt,
    input.message,
    input.Message,
    input.content,
    input.Content,
    input.text,
  ];
  for (const subagent of subagentToolInputs(input)) {
    candidates.push(subagent.Prompt, subagent.prompt);
  }
  const objects = [];
  for (const candidate of candidates) {
    if (typeof candidate !== "string") continue;
    const parsed = parseWholeJsonObject(candidate);
    if (parsed && Object.keys(parsed).length > 0) objects.push(parsed);
  }
  return objects;
}

function commandLooksLikeSingleRouteInvocation(command) {
  // A command route authorizes one official continuation command, not a shell
  // probe chained before or after it.
  return !/(;|\n|\|)/.test(command);
}

function subagentToolInputs(input) {
  const subagents = Array.isArray(input.Subagents)
    ? input.Subagents
    : Array.isArray(input.subagents)
      ? input.subagents
      : [];
  return subagents.filter((item) => item && typeof item === "object" && !Array.isArray(item));
}

function toolLooksOperationalWhileEffectPending(payload) {
  const name = normalizedToolName(payload);
  if (
    [
      "glob",
      "grep",
      "grep_search",
      "define_subagent",
      "invoke_agent",
      "invoke_subagent",
      "list_dir",
      "list_directory",
      "read",
      "read_file",
      "search_file_content",
      "send_message",
      "task",
      "todo",
      "todowrite",
      "todo_write",
      "view_file",
    ].includes(name)
  ) {
    return true;
  }
  return Boolean(toolCommandLine(payload)) || toolLooksMutating(payload);
}

function toolRunsAgentReportValidation(payload) {
  return /\bvalidate-agent-run-report\b/.test(toolCommandLine(payload));
}

function toolRunsVaultGuardFinish(payload) {
  const command = toolCommandLine(payload);
  return /\bvault_git\.py["']?\s+run-finish\b/.test(command);
}

function toolWritesArtifactMetadataToWorkbenchTemp(payload) {
  const name = normalizedToolName(payload);
  if (name !== "write_to_file" && name !== "write_file") return false;
  const input = toolInput(payload);
  const hasArtifactMetadata =
    Object.hasOwn(input, "ArtifactMetadata") ||
    Object.hasOwn(input, "artifactMetadata") ||
    Object.hasOwn(input, "artifact_metadata");
  if (!hasArtifactMetadata) return false;
  return isWorkbenchAgentWorkTempPath(toolTargetPath(payload));
}

function isWorkbenchAgentWorkTempPath(value) {
  const normalized = normalizePathText(value);
  return normalized.includes("/mednotes-home/tmp/agent-work/") || normalized.includes("/.mednotes/tmp/agent-work/");
}

function isAllowedPrePayloadTool(payload, intent) {
  const name = normalizedToolName(payload);
  const target = toolTargetPath(payload);
  if ((name === "view_file" || name === "read_file") && isOfficialSkillRead(target, intent)) return true;
  if (isOfficialPrePayloadCommand(payload, intent)) return true;
  return false;
}

async function isOfficialTaskLogRead(payload, intent) {
  if (!toolLooksLikeFileRead(payload)) return false;
  const target = toolTargetPath(payload);
  if (!isAgyTaskLogPath(target)) return false;
  return transcriptReferencesOfficialTaskLog(payload, intent, target);
}

async function transcriptReferencesOfficialTaskLog(payload, intent, target) {
  const transcriptPath = String(payload?.transcript_path || "");
  if (!transcriptPath) return false;
  try {
    const stats = await fs.stat(transcriptPath);
    if (!stats.isFile() || stats.size > MAX_TASK_LOG_BYTES) return false;
    const text = await fs.readFile(transcriptPath, "utf8");
    const normalizedTarget = normalizePathText(target);
    if (!text.includes(normalizedTarget) && !text.includes(`file://${normalizedTarget}`)) return false;
    void intent;
    return officialWorkflowCommandPattern().test(text);
  } catch {
    return false;
  }
}

function officialWorkflowCommandPattern() {
  return /\b(mednotes|flashcards|wiki)[^\n]+--json\b/i;
}

function toolLooksLikeFileRead(payload) {
  const name = normalizedToolName(payload);
  return name === "read" || name === "view_file" || name === "read_file";
}

function toolTargetPath(payload) {
  const input = toolInput(payload);
  return String(
    input.file_path ||
      input.filePath ||
      input.path ||
      input.target_file ||
      input.targetFile ||
      input.TargetFile ||
      input.AbsolutePath ||
      input.SearchDirectory ||
      input.searchDirectory ||
      input.DirectoryPath ||
      input.directoryPath ||
      "",
  );
}

function isAgyTaskLogPath(target) {
  const normalized = normalizePathText(target);
  return (
    (normalized.includes("/.gemini/antigravity-cli/brain/") &&
      normalized.includes("/.system_generated/tasks/") &&
      /\/task-[^/]+\.log$/.test(normalized)) ||
    (normalized.includes("/.system_generated/tasks/") && /\/task-[^/]+\.log$/.test(normalized))
  );
}

function isOfficialSkillRead(target, intent) {
  const normalized = normalizePathText(target);
  if (!normalized.endsWith("/SKILL.md")) return false;
  if (!normalized.includes("/skills/")) return false;
  const skillName =
    normalized
      .split("/skills/")
      .pop()
      ?.replace(/\/SKILL\.md$/, "") || "";
  if (isSharedOfficialSkill(skillName)) return true;
  const tokens = workflowTokens(intent);
  return tokens.length > 0 && tokens.every((token) => skillName.includes(token));
}

function isOfficialPrePayloadCommand(payload, intent) {
  const command = toolCommandLine(payload);
  if (!command) return false;
  const invocation = parseSingleCommandInvocation(command);
  if (!invocation) return false;
  if (isOfficialVaultRunStartInvocation(invocation)) return true;
  return isOfficialWorkbenchInvocation(invocation, intent);
}

function workflowTokens(intent) {
  return String(intent?.workflow || "")
    .replace(/^\//, "")
    .split(/[:/_-]+/)
    .map((item) => item.trim().toLowerCase())
    .filter((item) => item && !["mednotes", "medical"].includes(item));
}

function isSharedOfficialSkill(skillName) {
  return ["obsidian-cli", "obsidian-markdown", "obsidian-ops"].includes(String(skillName || ""));
}

function normalizePathText(value) {
  return String(value || "")
    .replace(/^["']|["']$/g, "")
    .replace(/^file:\/+/, "/")
    .replace(/\\/g, "/")
    .replace(/\/+/g, "/");
}

function firstUnsupportedParameter(value, depth = 0, unsupported = UNSUPPORTED_TOOL_PARAMETERS) {
  if (!value || typeof value !== "object" || depth > 4) return "";
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstUnsupportedParameter(item, depth + 1, unsupported);
      if (found) return found;
    }
    return "";
  }
  for (const [key, item] of Object.entries(value)) {
    if (unsupported.has(key)) return key;
    const found = firstUnsupportedParameter(item, depth + 1, unsupported);
    if (found) return found;
  }
  return "";
}

function toolCreatesAdHocScript(payload) {
  const input = toolInput(payload);
  const _name = normalizedToolName(payload);
  const filePath = String(
    input.file_path || input.filePath || input.path || input.target_file || input.targetFile || "",
  );
  if (filePath && SCRIPT_EXTENSIONS.has(path.extname(filePath).toLowerCase())) return true;
  const command = toolCommandLine(payload);
  if (!command) return false;
  if (isOfficialWorkbenchCommand(command) || toolRunsVaultGuardFinish(payload)) return false;
  return (
    /\b(python3?|node|bash|sh|pwsh|powershell)\b/i.test(command) && /\.(py|js|mjs|cjs|sh|ps1|cmd|bat)\b/i.test(command)
  );
}

function isOfficialWorkbenchCommand(command) {
  const invocation = parseSingleCommandInvocation(command);
  return Boolean(invocation && isOfficialWorkbenchInvocation(invocation, null));
}

function isOfficialVaultRunStartInvocation(invocation) {
  const scriptIndex = invocation.normalized.findIndex((token) => commandTokenBasename(token) === "vault_git.py");
  if (scriptIndex < 0 || !officialScriptInvocationHostIsValid(invocation, scriptIndex)) return false;
  return scriptIndex >= 0 && invocation.normalized[scriptIndex + 1] === "run-start";
}

function isOfficialWorkbenchInvocation(invocation, intent) {
  if (invocation.normalized.at(-1) !== "--json") return false;
  const scriptIndex = invocation.normalized.findIndex((token) => isOfficialWikiCliScriptToken(token));
  if (scriptIndex < 0) return false;
  if (!officialScriptInvocationHostIsValid(invocation, scriptIndex)) return false;
  const expected = workflowSubcommand(intent);
  if (!expected) return true;
  return invocation.normalized.slice(scriptIndex + 1, -1).includes(expected);
}

function officialScriptInvocationHostIsValid(invocation, scriptIndex) {
  const tokens = invocation.normalized;
  if (scriptIndex === 0) return ["cli.py", "vault_git.py"].includes(invocation.first);
  if (scriptIndex === 1 && invocation.first === "run_python.mjs") return true;
  if (scriptIndex === 2 && invocation.first === "node" && commandTokenBasename(tokens[1]) === "run_python.mjs") {
    return true;
  }
  if (
    scriptIndex === 3 &&
    invocation.first === "uv" &&
    tokens[1] === "run" &&
    ["python", "python3"].includes(commandTokenBasename(tokens[2]))
  ) {
    return true;
  }
  if (
    scriptIndex === 4 &&
    invocation.first === "uv" &&
    tokens[1] === "run" &&
    commandTokenBasename(tokens[2]) === "node" &&
    commandTokenBasename(tokens[3]) === "run_python.mjs"
  ) {
    return true;
  }
  return false;
}

function isOfficialWikiCliScriptToken(token) {
  return normalizeCommandToken(token).endsWith("scripts/mednotes/wiki/cli.py");
}

function workflowSubcommand(intent) {
  const workflow = String(intent?.workflow || "").trim();
  if (!workflow) return "";
  if (workflow === "/flashcards") return "flashcards";
  if (workflow.startsWith("/mednotes:")) return workflow.slice("/mednotes:".length);
  return workflow.replace(/^\//, "");
}

function toolContainsRawContent(value, depth = 0) {
  if (!value || typeof value !== "object" || depth > 4) return false;
  if (Array.isArray(value)) return value.some((item) => toolContainsRawContent(item, depth + 1));
  if (specialistBatchEmbedsRawContent(value)) return true;
  for (const [key, item] of Object.entries(value)) {
    const lower = key.toLowerCase();
    if (
      [
        "content",
        "prompt",
        "message",
        "markdown",
        "html",
        "raw",
        "raw_chat",
        "raw_markdown",
        "raw_markdown_content",
        "note_text",
      ].includes(lower) &&
      typeof item === "string"
    ) {
      if (textLooksLikeJsonPayload(item)) {
        try {
          if (toolContainsRawContent(JSON.parse(item), depth + 1)) return true;
        } catch {
          if (textLooksLikeRawContent(item)) return true;
        }
      } else if (textLooksLikeRawContent(item)) {
        return true;
      }
    }
    if (item && typeof item === "object" && toolContainsRawContent(item, depth + 1)) return true;
  }
  return false;
}

function specialistBatchEmbedsRawContent(value) {
  // Specialist work items may be valid JSON routes but still violate the FSM
  // raw-content contract; detect that before the harness launches a subagent.
  const items = Array.isArray(value.current_batch_items)
    ? value.current_batch_items
    : Array.isArray(value.currentBatchItems)
      ? value.currentBatchItems
      : [];
  return items.some((item) => specialistWorkItemEmbedsRawContent(item));
}

function specialistWorkItemEmbedsRawContent(value, parentKey = "") {
  if (Array.isArray(value)) return value.some((item) => specialistWorkItemEmbedsRawContent(item, parentKey));
  if (!value || typeof value !== "object") {
    if (typeof value !== "string") return false;
    const key = String(parentKey || "").toLowerCase();
    if (SPECIALIST_WORK_ITEM_SAFE_TEXT_KEYS.has(key)) return false;
    return textLooksLikeRawContent(value);
  }
  for (const [key, item] of Object.entries(value)) {
    const normalizedKey = String(key || "").toLowerCase();
    if (SPECIALIST_WORK_ITEM_RAW_CONTENT_KEYS.has(normalizedKey)) return true;
    if (specialistWorkItemEmbedsRawContent(item, normalizedKey)) return true;
  }
  return false;
}

function textLooksLikeJsonPayload(value) {
  const text = String(value || "").trim();
  return (text.startsWith("{") && text.endsWith("}")) || (text.startsWith("[") && text.endsWith("]"));
}

function textLooksLikeRawContent(value) {
  const text = String(value || "");
  if (/<(?:!doctype|html|body|article|section)\b/i.test(text)) return true;
  if (/(^|\n)---\s*\n[\s\S]{0,200}\n---\s*\n#\s+/m.test(text)) return true;
  if (/(^|\n)#\s+[^\n]+\n/.test(text) && /\n##\s+/.test(text)) return true;
  if (/\bChats_Raw\b|\bWiki_Medicina\b|\[Chat Original\]|gemini\.google\.com\/app\//i.test(text)) return true;
  return false;
}

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
