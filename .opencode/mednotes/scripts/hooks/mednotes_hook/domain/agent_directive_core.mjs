// Pure hook-domain contract logic. This module validates and renders FSM
// directives, but receives runtime-specific redaction context from adapters.
export const DIRECTIVE_SCHEMA = "medical-notes-workbench.agent-directive.v1";
export const WORKFLOW_RUN_RECORD_SCHEMA = "medical-notes-workbench.workflow-run-record.v1";
export const KNOWN_FSM_SCHEMAS = new Set([
  "medical-notes-workbench.fix-wiki-fsm-result.v1",
  "medical-notes-workbench.flashcards-fsm-result.v1",
  "medical-notes-workbench.link-fsm-result.v1",
  "medical-notes-workbench.link-related-fsm-result.v1",
  "medical-notes-workbench.process-chats-fsm-result.v1",
  "medical-notes-workbench.setup-fsm-result.v1",
  "medical-notes-workbench.history-fsm-result.v1",
]);
const KNOWN_DIRECTIVE_CARRIER_SCHEMAS = new Set([
  ...KNOWN_FSM_SCHEMAS,
  "medical-notes-workbench.fix-wiki-agent-stdout-report.v1",
  "medical-notes-workbench.plan-output-receipt.v1",
  "medical-notes-workbench.specialist-task-runner-result.v1",
  "medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1",
]);

const MAX_CONTEXT_CHARS = 1800;

export function isOfficialWorkflowPayload(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  // Run records are local observability snapshots. They may contain a copied
  // directive for audit, but they are not the live FSM payload for enforcement.
  if (String(value.schema || "") === WORKFLOW_RUN_RECORD_SCHEMA) return false;
  if (!KNOWN_FSM_SCHEMAS.has(String(value.schema || ""))) return false;
  return isAgentDirective(value.agent_directive);
}

export function isAgentDirectiveCarrierPayload(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  // Carriers may be official CLI stdout/receipt contracts, but run records stay
  // observability-only snapshots and must not inject live hook state.
  if (String(value.schema || "") === WORKFLOW_RUN_RECORD_SCHEMA) return false;
  if (!KNOWN_DIRECTIVE_CARRIER_SCHEMAS.has(String(value.schema || ""))) return false;
  return isAgentDirective(value.agent_directive);
}

export function isAgentDirective(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const control = value.control;
  const capabilities = control && typeof control === "object" ? control.capabilities : null;
  if (
    !(
      value.schema === DIRECTIVE_SCHEMA &&
      typeof value.workflow === "string" &&
      typeof value.run_id === "string" &&
      control &&
      typeof control === "object" &&
      typeof control.status === "string" &&
      typeof control.state === "string" &&
      capabilities &&
      typeof capabilities === "object" &&
      typeof capabilities.continue === "boolean" &&
      typeof capabilities.final_report === "boolean"
    )
  ) {
    return false;
  }
  return directiveStatusShapeIsValid(control, capabilities);
}

export function contextFromDirective(directive) {
  if (!isAgentDirective(directive)) return "";
  const control = directive.control || {};
  const capabilities = control.capabilities || {};
  const effects = Array.isArray(control.effects)
    ? control.effects
        .map((effect) => String(effect?.kind || ""))
        .filter(Boolean)
        .join(", ")
    : "";
  const limits = control.limits && typeof control.limits === "object" ? control.limits : {};
  const lines = [
    "MEDNOTES FSM CONTEXT",
    `workflow: ${safeLine(directive.workflow)}`,
    `status: ${safeLine(control.status)}`,
    `state: ${safeLine(control.state)}`,
    `capabilities: continue=${String(Boolean(capabilities.continue))}; final_report=${String(Boolean(capabilities.final_report))}`,
  ];
  if (effects) lines.push(`effects: ${safeLine(effects)}`);
  lines.push(
    `limits: raw_content=${String(limits.raw_content !== false)}; absolute_paths=${String(limits.absolute_paths !== false)}; ad_hoc_scripts=${String(limits.ad_hoc_scripts !== false)}`,
  );
  const resume = safeLine(control.resume || "");
  if (resume) lines.push(`resume: ${resume}`);
  for (const instruction of Array.isArray(directive.instructions) ? directive.instructions.slice(0, 12) : []) {
    const line = safeLine(instruction);
    if (line) lines.push(`instruction: ${line}`);
  }
  const context = lines.join("\n");
  return context.length <= MAX_CONTEXT_CHARS ? context : `${context.slice(0, MAX_CONTEXT_CHARS - 3).trimEnd()}...`;
}

export function contextFromWorkflowIntent(intent) {
  const workflow = safeLine(intent?.workflow || "");
  if (!workflow) return "";
  const lines = [
    "MEDNOTES WORKFLOW INTENT",
    `workflow: ${workflow}`,
    "stage: before_workflow_payload",
    "instruction: load only the official packaged workflow skill if needed.",
    "instruction: follow the official public workflow route, then wait for official JSON.",
    "instruction: do not probe permissions, list directories, call MCP, invoke subagents, schedule timers or self-debug before the workflow payload.",
    "instruction: if a tool schema fails, retry the same necessary tool without invented metadata arguments.",
  ];
  const context = lines.join("\n");
  return context.length <= MAX_CONTEXT_CHARS ? context : `${context.slice(0, MAX_CONTEXT_CHARS - 3).trimEnd()}...`;
}

export function controlStatus(directive) {
  return String(directive?.control?.status || "");
}

export function directiveAllowsFinalReport(directive) {
  return isAgentDirective(directive) && Boolean(directive?.control?.capabilities?.final_report);
}

export function directiveAllowsPauseReport(directive) {
  const capabilities = directive?.control?.capabilities;
  if (capabilities?.continue !== false) return false;
  const status = controlStatus(directive);
  return status === "waiting_external" || status === "waiting_human" || status === "blocked" || status === "failed";
}

export function limitIsFalse(directive, name) {
  return directive?.control?.limits?.[name] === false;
}

function safeLine(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/\/(?:private\/)?(?:var|tmp|Users|home)\/[^\s]+/g, "[path]")
    .slice(0, 240)
    .trim();
}

function directiveStatusShapeIsValid(control, capabilities) {
  const status = String(control.status || "");
  const effects = Array.isArray(control.effects) ? control.effects : [];
  const blockers = Array.isArray(control.blockers)
    ? control.blockers.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  const resume = String(control.resume || "").trim();
  if (status === "waiting_agent") {
    if (capabilities.continue !== true) return false;
    if (capabilities.final_report !== false) return false;
    if (!effects.length) return false;
  }
  if (status === "completed" || status === "completed_with_warnings") {
    if (capabilities.final_report !== true) return false;
  }
  if (["waiting_human", "waiting_external", "blocked", "failed"].includes(status)) {
    if (!blockers.length && !resume) return false;
  }
  return true;
}
