export function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

export function firstObject(...values) {
  for (const value of values) {
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  }
  return {};
}

export function normalizedArgs(args) {
  const input = args && typeof args === "object" ? { ...args } : {};
  const command = firstString(input.command, input.cmd, input.script, input.CommandLine);
  const cwd = firstString(input.cwd, input.Cwd, input.workingDirectory, input.WorkingDirectory);
  const target = firstString(
    input.file_path,
    input.filePath,
    input.path,
    input.TargetFile,
    input.AbsolutePath,
    input.SearchDirectory,
    input.DirectoryPath,
  );
  if (command) input.command = command;
  if (cwd) input.cwd = cwd;
  if (target) {
    input.file_path = target;
    input.target_file = target;
  }
  return input;
}

export function isAgyRuntime(payload) {
  return (
    Boolean(payload?.antigravity_payload_seen) ||
    Boolean(payload?.hookEventName || payload?.conversationId || payload?.workspacePaths)
  );
}

export function runtimeFromPayload(payload) {
  if (isAgyRuntime(payload)) return "antigravity";
  if (
    payload?.opencode_payload_seen ||
    payload?.opencodePayloadSeen ||
    String(payload?.runtime || "").toLowerCase() === "opencode" ||
    String(payload?.harness || "").toLowerCase() === "opencode"
  ) {
    return "opencode";
  }
  return "gemini-cli";
}

export function isOpenCodeRuntime(payload, card = null) {
  return runtimeFromPayload(payload) === "opencode" || String(card?.runtime || "") === "opencode";
}

export function sessionIdFromPayload(payload) {
  return cleanFileStem(String(payload?.session_id || payload?.conversation_id || payload?.conversationId || ""));
}

export function cleanFileStem(value) {
  return String(value || "")
    .replace(/[^a-zA-Z0-9_.-]/g, "_")
    .slice(0, 120);
}

export function toolInput(payload) {
  return firstObject(payload?.tool_input, payload?.toolInput, payload?.input, payload?.parameters, payload?.args);
}

export function normalizedToolName(payload) {
  return String(payload?.tool_name || payload?.toolName || payload?.name || payload?.tool || "")
    .trim()
    .toLowerCase();
}

export function toolCommandLine(payload) {
  const input = toolInput(payload);
  return String(input.command || input.CommandLine || input.commandLine || input.cmd || input.script || "");
}

export function hookEventNameFromPayload(payload) {
  return String(payload?.hook_event_name || payload?.hookEventName || "");
}

export function normalizeHookEvent(payload, card = null) {
  const runtime = card && String(card.runtime || "") ? String(card.runtime) : runtimeFromPayload(payload);
  return {
    runtime,
    eventName: hookEventNameFromPayload(payload),
    sessionId: sessionIdFromPayload(payload),
    toolName: normalizedToolName(payload),
    toolInput: toolInput(payload),
    toolResponse: firstObject(payload?.tool_response, payload?.toolResponse, payload?.response, payload?.result),
    cwd: String(payload?.cwd || ""),
    transcriptPath: String(payload?.transcript_path || payload?.transcriptPath || ""),
  };
}
