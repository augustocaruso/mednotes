---
description: "Atualiza somente WikiLinks no corpo da Wiki_Medicina."
---

<!-- Generated from commands/mednotes/link-body.toml. Do not edit directly. -->

Atualize somente os WikiLinks no corpo das notas da Wiki_Medicina.

Argumentos do usuario: $ARGUMENTS

Use a skill `link-medical-wiki` e carregue `.opencode/mednotes/skills/obsidian-ops/SKILL.md`.
Responda pelo contrato `.opencode/mednotes/docs/workflow-output-contract.md`.

Invariantes do launcher:
- Este comando e o modo estreito de `/mednotes:link` para corpo do texto.
- Rode `wiki/cli.py run-linker --diagnose --no-related-notes --json` para montar
  o plano sem mutar notas.
- Aplique somente se o diagnostico estiver coerente e sem blockers:
  `wiki/cli.py run-linker --apply --no-related-notes --diagnosis <json> --json`.
- O apply nao chama LLM nem recalcula decisoes contextuais.
- Nao chame `related-notes-sync` e nao reescreva `## 🔗 Notas Relacionadas`.
- Este workflow nao corrige estilo, YAML/status, publicacao ou taxonomia.
- Nao faca regex manual para linkar notas.
- Nao mostre JSON bruto por padrao; resuma links no corpo, vocabulario,
  blockers, warnings e proxima acao.
