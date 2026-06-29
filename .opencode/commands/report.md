---
description: "Envia relatório reflexivo pós-workflow por email."
---

<!-- Generated from commands/report.toml. Do not edit directly. -->

Gere e envie um relatório pós-workflow.

Argumentos do usuário: $ARGUMENTS

Use a skill `workflow-report`.
Use `.opencode/mednotes/docs/workflow-output-contract.md` para a resposta final.

Invariantes do launcher:
- Rode somente após um workflow ou quando o usuário pedir relatório da execução atual.
- O relatório precisa incluir `app_version`, erros, dificuldades, obstáculos,
  contornos, arquivos modificados, scripts criados, impacto no output e melhorias.
- Este é um envio manual explícito com recibo tipado: preserve o resultado de
  `manual-extension-diff-capture.v1`, `workflow-telemetry-envelope.v1` e `send-result.json`;
  não trate `/report` como reativação de telemetria automática.
- Inclua uma seção `First-pass prevention`: o que o agente deveria ter feito
  logo na primeira tentativa, qual prompt/contrato/guardrail teria evitado o
  problema, e qual fixture validaria essa prevenção.
- Capture o diff local vs GitHub com `uv run python scripts/mednotes/capture_extension_diff.py --send --github-baseline-url https://codeload.github.com/augustocaruso/medical-notes-workbench/zip/refs/heads/gemini-cli-extension`.
- Envie email para Augusto com relatório e anexos criados (`extension-full.diff`, `capture.zip`, scripts).
- Não inclua conteúdo clínico bruto, `.env`, tokens, chaves ou raw chats.
