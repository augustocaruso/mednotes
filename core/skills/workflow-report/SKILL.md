---
name: workflow-report
description: Use when the user asks for /report, post-workflow reflection, execution postmortems, or emailing a detailed workflow report to Augusto.
---

# Workflow Report

## Core Rule

`/report` is a post-workflow audit. It is not a success summary. Reconstruct the
run from the conversation, commands, files, tool outputs, generated artifacts and
your own decisions. Document what actually happened, including messy parts.

Block if you cannot identify the extension `app_version`; the email subject and
body must include it.

## Required Evidence

Read `${extensionPath}/gemini-extension.json` and capture `app_version`.
Use `${extensionPath}` as the installed extension root; fallback
`~/.gemini/extensions/medical-notes-workbench`.

Run this before sending the email:

```bash
uv run python scripts/mednotes/capture_extension_diff.py --send \
  --github-baseline-url https://codeload.github.com/augustocaruso/medical-notes-workbench/zip/refs/heads/gemini-cli-extension
```

If the user requests immediate delivery, add `--flush`. Record `snapshot_path`,
`extension-full.diff`, `capture.zip`, `generated-scripts/`, `send_result` and
any failure reason.

## Report Content

Write a Markdown report in the persistent feedback area, for example:
`~/.mednotes/feedback/workflow-reports/YYYY-MM-DD-HHMM-workflow-report.md`.
Use `${extensionPath}/docs/workflow-output-contract.md` for the final visible
answer after the email step.

Include these sections:

- workflow/run context: command, objective, app_version, date, environment;
- complete chronology of actions, including retries and dead ends;
- all errors, difficulties, percalços, intercorrências and blockers;
- how each problem was diagnosed, bypassed, fixed or left unresolved;
- arquivos modificados, created artifacts, receipts and relevant paths;
- todos os scripts criados by the agent, with path, purpose, risk and whether
  they were executed; attach script files when emailing;
- impacto no output: missing/changed quality, delays, incomplete
  output, extra validation, manual decisions or user-visible consequences;
- First-pass prevention: o que o agente deveria ter feito logo no primeiro ciclo,
  qual instrução/prompt/contrato estava ausente, ambíguo ou fácil de ignorar,
  qual prompt source deve mudar, qual fixture/corpus validaria a prevenção, e
  quais correções foram apenas recuperação pós-erro;
- o que poderia ter sido diferente for a smoother, more efficient and higher
  quality run: prompt, docs, tests, CLI, telemetry, UX, stop rules and tooling;
- open risks and next actions.

Separate facts from inference. Be specific: filenames, command names, exit
codes, blockers, decisions and validation results. Do not paste raw clinical
Markdown, raw chats, HTML, `.env`, tokens, keys or secrets. Redact if needed.

## Email

Send the report by email to Augusto using the available email capability. Do not
invent the address; use the configured Augusto recipient/account. If no email
tool or recipient is available, stop and say exactly what is missing.

Subject format:

```text
[medical-notes-workbench] Workflow report app_version=<version> <workflow-or-topic>
```

Attach:

- the Markdown report;
- `capture.zip`;
- `extension-full.diff`;
- every script the agent created during the workflow;
- any generated-script files under the capture `generated-scripts/` directory.

The body should contain a short executive summary plus the attachment list. It
must say whether the diff email/envelope was sent successfully by
`capture_extension_diff.py --send`.

## Prompt Optimization Handoff

When the run exposed avoidable retries, ignored `next_action`, wrong phase,
manual script workarounds, missing error context, or preventable validation
errors, add a compact `first_pass_prevention_candidates` JSON block in the
Markdown report. This block is input for `prompt-optimization-from-telemetry`;
it is not an active fixture and must not include raw clinical content, full
scripts, HTML, tokens, keys, or `.env` values.

## Final Response

Reply in Portuguese with status, report path, email sent/not sent, app_version,
attachments included, diff snapshot path and any missing evidence.
