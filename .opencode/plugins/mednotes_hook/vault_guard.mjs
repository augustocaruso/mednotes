import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { pruneHookEventFilesSync } from "./retention.mjs";
import { allow, deny } from "./runtime.mjs";

const STATE_SUBDIR = path.join(".gemini", "medical-notes-workbench");
const LEASE_SUBDIR = path.join("vault-guard", "leases");
const DENY_REASON =
  "Bloqueei esta alteração porque o vault ainda não tem um ponto de restauração ativo para este run. Rode `uv run python scripts/vault/vault_git.py run-start --agent gemini-cli --workflow <workflow> --json` e repita a operação.";
const DENY_DETAILS = {
  status: "blocked_vault_guard_required",
  blocked_reason: "vault_guard_required",
  human_message: "Bloqueei esta alteração porque ainda não existe ponto de restauração ativo para este run.",
  next_action:
    "Abrir um ponto de restauração para este run e repetir a operação. Faça isso uma vez por lote, não por nota.",
  agent_message:
    "Abra o guard com run-start uma vez por lote no começo do workflow, execute todas as mutações, e feche com run-finish uma vez por lote no final.",
  recovery_command:
    "uv run python scripts/vault/vault_git.py run-start --agent gemini-cli --workflow <workflow> --json",
  required_inputs: ["agent", "workflow"],
  human_decision_required: false,
};
const DENY_DIRECT_GIT_REASON =
  "Bloqueei este comando porque operações Git diretas no vault devem passar por `scripts/vault/vault_git.py`, mesmo com ponto de restauração ativo.";
const DENY_DIRECT_GIT_DETAILS = {
  status: "blocked_direct_mutation_forbidden",
  blocked_reason: "direct_mutation_forbidden",
  human_message: "Operação Git direta no vault bloqueada. Use o fluxo oficial de proteção/restauração do vault.",
  next_action:
    "Use `uv run python scripts/vault/vault_git.py run-finish --agent gemini-cli --workflow <workflow> --title <titulo> --json` para registrar mudanças.",
  agent_message:
    "Não rode git add/commit/push/reset/clean diretamente no vault. Use vault_git.py para preservar restore points, leases e backup online.",
  recovery_command:
    "uv run python scripts/vault/vault_git.py run-finish --agent gemini-cli --workflow <workflow> --title <titulo> --json",
  required_inputs: ["title"],
  human_decision_required: false,
};
const DENY_GENERATED_SCRIPT_REASON =
  "Bloqueei este comando porque scripts de reparo gerados não podem modificar o vault ou artefatos do workflow. Use a rota oficial do Workbench.";
const DENY_GENERATED_SCRIPT_DETAILS = {
  status: "blocked_generated_script_workaround_forbidden",
  blocked_reason: "generated_script_workaround_forbidden",
  human_message:
    "Script de reparo manual bloqueado. Correções de raw chat, coverage, manifest, staged notes, links ou Wiki devem passar pelo CLI oficial.",
  next_action:
    "Use `uv run python scripts/mednotes/wiki/cli.py <comando-oficial> --json` ou bloqueie com error_context se não houver rota oficial.",
  agent_message:
    "Não crie nem execute scripts ad hoc para editar raw chats, coverage, manifests, staged notes, Related Notes, WikiLinks ou notas do vault.",
  recovery_command: "uv run python scripts/mednotes/wiki/cli.py <comando-oficial> --json",
  required_inputs: ["official_workflow_command"],
  human_decision_required: false,
};
const DENY_DIRECT_RAW_REASON =
  "Bloqueei esta alteração porque raw chats não podem ser editados diretamente. Metadados YAML/status só devem ser alterados pelo CLI oficial do Workbench.";
const DENY_DIRECT_RAW_DETAILS = {
  status: "blocked_direct_raw_chat_edit_forbidden",
  blocked_reason: "direct_raw_chat_edit_forbidden",
  human_message:
    "Raw chat bloqueado contra edição direta. O conteúdo do chat é imutável; metadados YAML/status devem passar pelo script oficial.",
  next_action:
    "Use `wiki/cli.py triage`, `wiki/cli.py discard` ou `wiki/cli.py publish-batch` via `scripts/run_python.mjs`, conforme a etapa do workflow.",
  agent_message:
    "Não use write_file, replace, sed, echo, redirecionamento shell ou script ad hoc em Chats_Raw. Se precisar mudar status/YAML, use a porta oficial wiki/cli.py.",
  recovery_command:
    'node "${extensionPath}/scripts/run_python.mjs" "${extensionPath}/scripts/mednotes/wiki/cli.py" <triage|discard|publish-batch> --json',
  required_inputs: ["official_workflow_command"],
  human_decision_required: false,
};
const DENY_WORKFLOW_ARTIFACT_REASON =
  "Bloqueei esta alteração porque artefatos de workflow devem ser gerados pelo CLI oficial do Workbench, não por escrita direta do agente.";
const DENY_WORKFLOW_ARTIFACT_DETAILS = {
  status: "blocked_workflow_artifact_direct_write_forbidden",
  blocked_reason: "workflow_artifact_direct_write_forbidden",
  human_message:
    "Artefato de workflow bloqueado contra edição direta. Plans, manifests, receipts e reports precisam ser gerados pela rota oficial.",
  next_action:
    "Reexecute o comando oficial de wiki/cli.py que gera este artefato; não use write_file, replace, edit ou multiedit em runs/*.json.",
  agent_message:
    "Não fabrique nem edite artefatos de workflow manualmente. Use wiki/cli.py para gerar plans, manifests, receipts, reports e diagnoses.",
  recovery_command: "uv run python scripts/mednotes/wiki/cli.py <comando-oficial> --json",
  required_inputs: ["official_workflow_command"],
  human_decision_required: false,
};
const DENY_INSTALLED_EXTENSION_EDIT_REASON =
  "Bloqueei esta alteração porque o bundle instalado da extensão não é a fonte de verdade. Corrija o arquivo fonte no repositório e reinstale/atualize a extensão.";
const DENY_INSTALLED_EXTENSION_EDIT_DETAILS = {
  status: "blocked_installed_extension_runtime_edit_forbidden",
  blocked_reason: "installed_extension_runtime_edit_forbidden",
  human_message:
    "Edição direta do bundle instalado bloqueada. A correção precisa ser feita no repositório fonte e distribuída pela rota oficial.",
  next_action:
    "Patch canonical source under bundle/ in the repository, then rebuild and reinstall/update the extension.",
  agent_message:
    "Do not edit ~/.gemini/extensions/medical-notes-workbench, ~/.gemini/config/plugins/medical-notes-workbench or Windows equivalents. Report the source file under bundle/.",
  required_inputs: ["canonical_source_patch"],
  human_decision_required: false,
};
const DENY_UNSUPPORTED_TOOL_PARAMETER_REASON =
  "Bloqueei esta chamada porque ela contém parâmetro que não existe no contrato da tool.";
const DENY_PUBLIC_DEV_ESCAPE_REASON =
  "Bloqueei este comando porque escapes de desenvolvedor não podem ser usados em workflow público.";

const WRITE_TOOLS = new Set(["write_file", "write", "replace", "edit", "multiedit"]);
const SHELL_TOOLS = new Set(["run_shell_command", "run_shell", "shelltool", "bash", "shell", "powershell", "pwsh"]);
const UNSUPPORTED_TOOL_PARAMETERS = new Set(["wait_for_previous"]);
const WORKFLOW_ARTIFACT_NAME_RE =
  /(^|[-_])(plan|manifest|receipt|report|diagnosis|trigger-context|trigger_context|run_state)([-_.]|$)/i;

function homeDir() {
  return process.env.HOME || process.env.USERPROFILE || os.homedir();
}

function stateDir() {
  return path.join(homeDir(), STATE_SUBDIR);
}

function readFirstLine(filePath) {
  try {
    return fs
      .readFileSync(filePath, "utf8")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find(Boolean);
  } catch {
    return "";
  }
}

function normalizeForCompare(value) {
  const normalized = path.normalize(String(value || ""));
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

function canonicalPath(value) {
  if (!value) return "";
  const absolute = path.resolve(String(value));
  try {
    return fs.realpathSync.native(absolute);
  } catch {
    const parent = path.dirname(absolute);
    try {
      return path.join(fs.realpathSync.native(parent), path.basename(absolute));
    } catch {
      return absolute;
    }
  }
}

function isInsideOrSame(candidate, root) {
  if (!candidate || !root) return false;
  const left = normalizeForCompare(canonicalPath(candidate));
  const right = normalizeForCompare(canonicalPath(root));
  return left === right || left.startsWith(right.endsWith(path.sep) ? right : `${right}${path.sep}`);
}

function installedExtensionRoot() {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
}

function looksLikeInstalledExtensionRoot(root) {
  const normalized = normalizeForCompare(root).replace(/[\\/]+/g, "/");
  const home = normalizeForCompare(homeDir()).replace(/[\\/]+/g, "/");
  return (
    normalized === `${home}/.gemini/extensions/medical-notes-workbench` ||
    normalized.startsWith(`${home}/.gemini/extensions/medical-notes-workbench/`) ||
    normalized === `${home}/.gemini/config/plugins/medical-notes-workbench` ||
    normalized.startsWith(`${home}/.gemini/config/plugins/medical-notes-workbench/`)
  );
}

function configuredInstalledExtensionRoots() {
  const currentRoot = installedExtensionRoot();
  const roots = [
    path.join(homeDir(), ".gemini", "extensions", "medical-notes-workbench"),
    path.join(homeDir(), ".gemini", "config", "plugins", "medical-notes-workbench"),
  ];
  if (looksLikeInstalledExtensionRoot(currentRoot)) {
    roots.unshift(currentRoot);
  }
  return [...new Set(roots.filter(Boolean).map((root) => path.resolve(root)))];
}

function isInsideInstalledExtensionRoot(candidate) {
  return configuredInstalledExtensionRoots().some((root) => isInsideOrSame(candidate, root));
}

function configuredVaultPath() {
  return readFirstLine(path.join(stateDir(), "vault.path"));
}

function configuredRawDirs() {
  const candidates = [];
  for (const envName of ["MED_RAW_DIR", "MEDNOTES_RAW_DIR"]) {
    const value = process.env[envName];
    if (value) candidates.push(expandConfiguredPath(value));
  }
  const configPath = configuredAppConfigPath();
  const rawDir =
    readTomlPathValue(configPath, "paths", "raw_dir") || readTomlPathValue(configPath, "chat_processor", "raw_dir");
  if (rawDir) candidates.push(expandConfiguredPath(rawDir, configPath));
  return [...new Set(candidates.filter(Boolean).map((candidate) => path.resolve(candidate)))];
}

function configuredAppConfigPath() {
  const configured = process.env.MEDNOTES_CONFIG;
  if (configured) return path.resolve(expandConfiguredPath(configured));
  return path.join(stateDir(), "config.toml");
}

function expandConfiguredPath(value, configPath = "") {
  const text = String(value || "").trim();
  if (!text) return "";
  const expanded = text.startsWith("~") ? path.join(homeDir(), text.slice(1)) : text;
  if (path.isAbsolute(expanded)) return expanded;
  if (configPath) return path.resolve(path.dirname(configPath), expanded);
  return path.resolve(expanded);
}

function readTomlPathValue(configPath, sectionName, keyName) {
  if (!configPath) return "";
  let text = "";
  try {
    text = fs.readFileSync(configPath, "utf8");
  } catch {
    return "";
  }
  let activeSection = "";
  for (const line of text.split(/\r?\n/)) {
    const section = line.match(/^\s*\[([^\]]+)]\s*(?:#.*)?$/);
    if (section) {
      activeSection = section[1].trim();
      continue;
    }
    if (activeSection !== sectionName) continue;
    const match = line.match(new RegExp(`^\\s*${keyName}\\s*=\\s*(['"])(.*?)\\1\\s*(?:#.*)?$`));
    if (match) return unescapeTomlBasicString(match[2], match[1]);
  }
  return "";
}

function unescapeTomlBasicString(value, quote) {
  if (quote === "'") return value;
  return String(value || "")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, "\\");
}

export function normalizedToolName(name) {
  return String(name || "")
    .trim()
    .toLowerCase();
}

export function toolInput(payload) {
  if (!payload || typeof payload !== "object") return {};
  for (const input of [payload.tool_input, payload.toolInput, payload.input, payload.parameters, payload.args]) {
    if (input && typeof input === "object" && Object.keys(input).length > 0) return input;
  }
  return {};
}

export function isWriteTool(toolName) {
  return WRITE_TOOLS.has(normalizedToolName(toolName));
}

export function isShellTool(toolName) {
  return SHELL_TOOLS.has(normalizedToolName(toolName));
}

export function shellLooksMutating(command) {
  const text = String(command || "");
  if (!text.trim()) return false;
  return (
    /\b(uv\s+run\s+python|python3?|node|npm|powershell|pwsh|bash|sh)\b/i.test(text) ||
    /\b(sed\s+-i|perl\s+-pi|rm|del|move|mv|copy|cp|Set-Content|Out-File|Add-Content|Remove-Item|Copy-Item|Move-Item|Rename-Item|New-Item|Clear-Content|tee)\b/i.test(
      text,
    ) ||
    /(^|[^>])>>?($|[^>])/.test(text) ||
    /\bgit\s+(add|commit|push|checkout|reset|clean|revert|merge|rebase)\b/i.test(text)
  );
}

export function shellLooksDirectGitMutation(command) {
  return /\bgit\s+(add|commit|push|checkout|reset|clean|revert|merge|rebase)\b/i.test(String(command || ""));
}

function runsPythonViaUv(command) {
  return /(?:^|\s)uv\s+run(?:\s+(?:--project|--with)\s+(?:"[^"]+"|'[^']+'|\S+))*\s+python\s+/i.test(
    String(command || ""),
  );
}

function runsWikiCliViaRunPythonWrapper(command) {
  const text = String(command || "");
  return (
    /(?:^|\s)node(?:\.exe)?\s+(?:"[^"]*scripts[\\/]run_python\.mjs"|'[^']*scripts[\\/]run_python\.mjs'|\S*scripts[\\/]run_python\.mjs)\s+(?:"[^"]*(?:extension[\\/])?scripts[\\/]mednotes[\\/]wiki[\\/]cli\.py"|'[^']*(?:extension[\\/])?scripts[\\/]mednotes[\\/]wiki[\\/]cli\.py'|\S*(?:extension[\\/])?scripts[\\/]mednotes[\\/]wiki[\\/]cli\.py)\s+/i.test(
      text,
    ) ||
    /(?:^|\s)node(?:\.exe)?\s+(?:"[^"]*scripts[\\/]run_python\.mjs"|'[^']*scripts[\\/]run_python\.mjs'|\S*scripts[\\/]run_python\.mjs)\s+(?:"[^"]*wiki[\\/]cli\.py"|'[^']*wiki[\\/]cli\.py'|\S*wiki[\\/]cli\.py)\s+/i.test(
      text,
    )
  );
}

export function isPrivilegedVaultGitCommand(command) {
  const text = String(command || "");
  if (/vault_(precommit|commit)\.(sh|ps1)\b/.test(text)) return true;
  if (!runsPythonViaUv(text)) return false;
  if (!/scripts[\\/]+vault[\\/]+/.test(text)) return false;
  return /vault_git\.py["']?\s+(setup|run-start|run-finish|timeline|restore-preview|restore-apply|guard-status)\b/.test(
    text,
  );
}

export function isOfficialWorkflowCommand(command) {
  const text = String(command || "");
  return (
    isPrivilegedVaultGitCommand(text) ||
    runsWikiCliViaRunPythonWrapper(text) ||
    (runsPythonViaUv(text) &&
      (/(?:^|\s)uv\s+run(?:\s+(?:--project|--with)\s+(?:"[^"]+"|'[^']+'|\S+))*\s+python\s+["']?[^"'\s]*(?:extension[\\/])?scripts[\\/]mednotes[\\/]wiki[\\/]cli\.py["']?\s+/i.test(
        text,
      ) ||
        /(?:^|\s)uv\s+run(?:\s+(?:--project|--with)\s+(?:"[^"]+"|'[^']+'|\S+))*\s+python\s+["']?[^"'\s]*wiki[\\/]cli\.py["']?\s+/i.test(
          text,
        )))
  );
}

export function shellLooksGeneratedWorkflowBypass(command) {
  const text = String(command || "");
  if (!text.trim() || isOfficialWorkflowCommand(text)) return false;
  const runsInterpreter = /\b(uv\s+run\s+python|python3?|node|npm|powershell|pwsh|bash|sh)\b/i.test(text);
  const mentionsWorkflowArtifact =
    /\b(raw|coverage|manifest|stage(?:d)?|wiki|wikilink|related[-_ ]?notes|linker|vault|note|markdown|frontmatter)\b/i.test(
      text,
    ) ||
    /\b(repair|fix|rewrite|migrate|merge|sync|cleanup|normalize|dedupe|publish)\b/i.test(text) ||
    /\.(py|js|mjs|ps1|sh)\b/i.test(text);
  return runsInterpreter && mentionsWorkflowArtifact;
}

export function targetPathsFromPayload(payload) {
  const input = toolInput(payload);
  const cwd = payload?.cwd ? String(payload.cwd) : process.cwd();
  const candidates = [input.file_path, input.path, input.absolute_path, input.target_path, input.output].filter(
    Boolean,
  );
  return candidates.map((candidate) => path.resolve(cwd, String(candidate)));
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

function shellCommand(input) {
  return firstString(input.command, input.cmd, input.script);
}

function unsupportedToolParameter(input) {
  for (const key of Object.keys(input || {})) {
    if (UNSUPPORTED_TOOL_PARAMETERS.has(key)) return key;
  }
  return "";
}

function shellLooksPublicDevEscape(command) {
  const text = String(command || "");
  return /\bMEDNOTES_ALLOW_DEV_ESCAPE\s*=\s*(?:1|true|yes)\b/i.test(text) || /\b--skip-prompt-eval\b/i.test(text);
}

function commandMentionsVault(command, vaultDir) {
  const text = String(command || "");
  if (!text || !vaultDir) return false;
  const raw = path.resolve(String(vaultDir));
  const real = canonicalPath(raw);
  return text.includes(raw) || (real && text.includes(real));
}

function targetsRawChatMarkdown(candidate, rawDirs) {
  if (!candidate || path.extname(String(candidate)).toLowerCase() !== ".md") return false;
  return rawDirs.some((rawDir) => isInsideOrSame(candidate, rawDir));
}

function commandMentionsPath(command, targetDir) {
  const text = String(command || "");
  if (!text || !targetDir) return false;
  const raw = path.resolve(String(targetDir));
  const real = canonicalPath(raw);
  return text.includes(raw) || (real && text.includes(real));
}

function shellRedirectionTargets(command, cwd) {
  const targets = [];
  const text = String(command || "");
  const redirectPattern = /(?:^|\s)(?:>>?|1>|2>|&>)\s*(?:"([^"]+)"|'([^']+)'|(\S+))/g;
  let match;
  // biome-ignore lint/suspicious/noAssignInExpressions: canonical regex-exec iteration idiom (assign-and-test in while).
  while ((match = redirectPattern.exec(text)) !== null) {
    const value = match[1] || match[2] || match[3] || "";
    if (!value || value.startsWith("&")) continue;
    targets.push(path.resolve(cwd || process.cwd(), value));
  }
  return targets;
}

function shellLooksDirectRawPathMutation(command) {
  return /\b(sed\s+-i|perl\s+-pi|rm|del|move|mv|copy|cp|Set-Content|Out-File|Add-Content|Remove-Item|Copy-Item|Move-Item|Rename-Item|Clear-Content)\b/i.test(
    String(command || ""),
  );
}

function commandWritesConfiguredRawDir(command, cwd, rawDirs) {
  const redirectedTargets = shellRedirectionTargets(command, cwd);
  if (redirectedTargets.some((candidate) => targetsRawChatMarkdown(candidate, rawDirs))) return true;
  return shellLooksDirectRawPathMutation(command) && rawDirs.some((rawDir) => commandMentionsPath(command, rawDir));
}

function targetsConfiguredRawDir(payload, toolName, cwd, command, rawDirs) {
  if (rawDirs.length === 0) return false;
  if (isWriteTool(toolName)) {
    return targetPathsFromPayload(payload).some((candidate) => targetsRawChatMarkdown(candidate, rawDirs));
  }
  if (!isShellTool(toolName) || !shellLooksMutating(command)) return false;
  if (isOfficialWorkflowCommand(command)) return false;
  return rawDirs.some((rawDir) => isInsideOrSame(cwd, rawDir)) || commandWritesConfiguredRawDir(command, cwd, rawDirs);
}

function looksLikeWorkflowArtifactPath(candidate) {
  const normalized = String(candidate || "").replace(/\\/g, "/");
  const name = normalized.split("/").pop() || "";
  return normalized.includes("/runs/") && name.endsWith(".json") && WORKFLOW_ARTIFACT_NAME_RE.test(name);
}

function targetsWorkflowArtifact(payload, toolName) {
  if (!isWriteTool(toolName)) return false;
  return targetPathsFromPayload(payload).some((candidate) => looksLikeWorkflowArtifactPath(candidate));
}

export async function activeLeasesForVault(vaultDir) {
  const leaseDir = path.join(stateDir(), LEASE_SUBDIR);
  let entries = [];
  try {
    entries = fs.readdirSync(leaseDir);
  } catch {
    return [];
  }
  const now = Date.now();
  const active = [];
  for (const entry of entries) {
    if (!entry.endsWith(".json")) continue;
    try {
      const lease = JSON.parse(fs.readFileSync(path.join(leaseDir, entry), "utf8"));
      if (lease.status !== "active") continue;
      if (
        !isInsideOrSame(String(lease.vault_dir || ""), vaultDir) ||
        !isInsideOrSame(vaultDir, String(lease.vault_dir || ""))
      ) {
        continue;
      }
      const expiresAt = Date.parse(String(lease.expires_at || ""));
      if (Number.isFinite(expiresAt) && expiresAt <= now) continue;
      active.push(lease);
    } catch {
      // Ignore malformed leases; hooks must not fail closed because of corrupt local state.
    }
  }
  return active;
}

export async function guardVaultBefore(payload) {
  const toolName =
    payload?.tool_name || payload?.toolName || payload?.name || payload?.tool || payload?.original_request_name;
  const input = toolInput(payload);
  const cwd = payload?.cwd ? String(payload.cwd) : "";
  const command = shellCommand(input);
  const rawDirs = configuredRawDirs();

  const badParam = unsupportedToolParameter(input);
  if (badParam) {
    recordVaultGuardEvent(payload, { status: "blocked", error: "unsupported_tool_parameter" });
    return deny(DENY_UNSUPPORTED_TOOL_PARAMETER_REASON, {
      status: "blocked_unsupported_tool_parameter",
      blocked_reason: "unsupported_tool_parameter",
      bad_param: badParam,
      human_message: "O agente tentou usar um parâmetro inexistente da ferramenta.",
      next_action: "Remova o parâmetro e aguarde o resultado da tool anterior antes de emitir a próxima chamada.",
      agent_message:
        "Do not pass wait_for_previous or other undocumented tool parameters. Sequence by waiting for the previous tool result.",
      required_inputs: [],
      human_decision_required: false,
    });
  }

  if (isShellTool(toolName) && shellLooksPublicDevEscape(command)) {
    recordVaultGuardEvent(payload, { status: "blocked", error: "public_dev_escape_forbidden" });
    return deny(DENY_PUBLIC_DEV_ESCAPE_REASON, {
      status: "blocked_public_dev_escape_forbidden",
      blocked_reason: "public_dev_escape_forbidden",
      human_message: "Escape técnico bloqueado em workflow público.",
      next_action:
        "Pare e retome pela rota oficial com recibo/proveniência tipados; não use MEDNOTES_ALLOW_DEV_ESCAPE nem --skip-prompt-eval.",
      agent_message:
        "Developer escapes are forbidden in public workflows. Report the typed blocker instead of bypassing it.",
      required_inputs: [],
      human_decision_required: false,
    });
  }

  if (
    isWriteTool(toolName) &&
    targetPathsFromPayload(payload).some((candidate) => isInsideInstalledExtensionRoot(candidate))
  ) {
    recordVaultGuardEvent(payload, { status: "blocked", error: "installed_extension_runtime_edit_forbidden" });
    return deny(DENY_INSTALLED_EXTENSION_EDIT_REASON, DENY_INSTALLED_EXTENSION_EDIT_DETAILS);
  }

  if (targetsWorkflowArtifact(payload, toolName)) {
    recordVaultGuardEvent(payload, { status: "blocked", error: "workflow_artifact_direct_write_forbidden" });
    return deny(DENY_WORKFLOW_ARTIFACT_REASON, DENY_WORKFLOW_ARTIFACT_DETAILS);
  }

  if (targetsConfiguredRawDir(payload, toolName, cwd, command, rawDirs)) {
    recordVaultGuardEvent(payload, { status: "blocked", error: "direct_raw_chat_edit_forbidden" });
    return deny(DENY_DIRECT_RAW_REASON, DENY_DIRECT_RAW_DETAILS);
  }

  const vaultDir = configuredVaultPath();
  if (!vaultDir) return allow();

  if (isShellTool(toolName) && isPrivilegedVaultGitCommand(command)) return allow();

  let targetsVault = false;
  if (isWriteTool(toolName)) {
    targetsVault = targetPathsFromPayload(payload).some((candidate) => isInsideOrSame(candidate, vaultDir));
  } else if (isShellTool(toolName) && shellLooksMutating(command)) {
    targetsVault = isInsideOrSame(cwd, vaultDir) || commandMentionsVault(command, vaultDir);
  }

  if (!targetsVault) return allow();
  if (isShellTool(toolName) && shellLooksDirectGitMutation(command)) {
    recordVaultGuardEvent(payload, { status: "blocked", error: "direct_mutation_forbidden" });
    return deny(DENY_DIRECT_GIT_REASON, DENY_DIRECT_GIT_DETAILS);
  }
  if (maintainerBypassEnabled()) {
    recordVaultGuardEvent(payload, { status: "bypassed", error: "maintainer_bypass" });
    return allow({ bypassed: true });
  }
  const active = await activeLeasesForVault(vaultDir);
  if (active.length > 0) {
    if (isShellTool(toolName) && shellLooksGeneratedWorkflowBypass(command)) {
      recordVaultGuardEvent(payload, { status: "blocked", error: "generated_script_workaround_forbidden" });
      return deny(DENY_GENERATED_SCRIPT_REASON, DENY_GENERATED_SCRIPT_DETAILS);
    }
    return allow();
  }
  recordVaultGuardEvent(payload, { status: "blocked", error: "vault_guard_required" });
  return deny(DENY_REASON, DENY_DETAILS);
}

function maintainerBypassEnabled() {
  return (
    process.env.MEDNOTES_VAULT_GUARD_DISABLE === "1" &&
    String(process.env.MEDNOTES_VAULT_GUARD_DISABLE_REASON || "").trim().length > 0
  );
}

function recordVaultGuardEvent(payload, { status, error }) {
  try {
    const event = {
      schema: "medical-notes-workbench.agent-hook-event.v1",
      event_id: crypto.randomUUID ? crypto.randomUUID() : crypto.randomBytes(16).toString("hex"),
      recorded_at: new Date().toISOString(),
      hook_event_name: String(payload?.hook_event_name || "BeforeTool"),
      session_id: String(payload?.session_id || ""),
      transcript_path: compactPath(String(payload?.transcript_path || "")),
      cwd: compactPath(String(payload?.cwd || process.cwd())),
      tool_name: String(payload?.tool_name || ""),
      tool_kind: "vault_guard",
      original_request_name: String(payload?.original_request_name || ""),
      generated_scripts: [],
      command_events: [
        {
          command_family: "vault_guard",
          status,
          error,
          capture_method: "guard-vault-before",
        },
      ],
    };
    const dir = path.join(feedbackRoot(), "hook-events");
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `${event.recorded_at.replace(/[:.]/g, "-")}-${event.event_id}.json`);
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, `${JSON.stringify(event, null, 2)}\n`, "utf8");
    fs.renameSync(tmp, file);
    pruneHookEventFilesSync(dir);
  } catch {
    // Hook event capture must never change the allow/deny decision.
  }
}

function feedbackRoot() {
  const configured = process.env.MEDNOTES_FEEDBACK_DIR || process.env.MEDICAL_NOTES_FEEDBACK_DIR;
  if (configured) return configured;
  return path.join(homeDir(), ".gemini", "medical-notes-workbench", "feedback");
}

function compactPath(value) {
  const text = String(value || "");
  if (!text) return "";
  const home = homeDir();
  return text.startsWith(home) ? `~${text.slice(home.length)}` : text;
}
