---
description: "Roda a linkagem da Wiki_Medicina."
---

<!-- Generated from commands/mednotes/link.toml. Do not edit directly. -->

Rode a linkagem da Wiki_Medicina.
Argumentos do usuário: $ARGUMENTS
Use a skill `link-medical-wiki` e carregue `.opencode/mednotes/skills/obsidian-ops/SKILL.md`.
Responda pelo contrato `.opencode/mednotes/docs/workflow-output-contract.md`.
Invariantes do launcher:
- /mednotes:link é o dono de todo reparo de grafo: vocabulary DB, curadoria semântica, aliases, body linker, Related Notes e validação final. Não mantém índice Dataview.
- `wiki/cli.py run-linker --diagnose` monta plano e salva `link-diagnosis.json`
  sem mutar notas; resolve aliases contextuais determinísticos, mas não abre
  `gemini -p` escondido para ambiguidades médicas reais.
- `wiki/cli.py run-linker --apply --diagnosis <json>` aplica só diagnóstico
  validado; o apply não chama LLM nem recalcula decisões contextuais.
- Se aparecer `vocabulary_curator_batch_plan_path` ou `vocabulary_semantic_ingestion_pending`, continue a curadoria aqui; não encerre como próximo passo manual.
- Na curadoria de vocabulário: Não use `@generalist` nem outro agente intermediário; o agente pai é o único orquestrador; lance `med-link-graph-curator` diretamente por `work_items[]`.
- No apply, `vocabulary_semantic_repair` resolve fila simples antes de linkar; só pare em decisão humana ou erro operacional real.
- `wiki/cli.py related-notes-sync` é o único escritor de `## 🔗 Notas Relacionadas`;
  `run-linker` chama essa fase canônica em vez de heurística própria.
- Modos estreitos: `/mednotes:link-body`, `/mednotes:link-related`; seção
  gerenciada: `related-notes-sync --apply` com recibo e proteção do vault.
- Este workflow não corrige estilo, YAML/status, publicação ou taxonomia.
- Não faça regex manual para linkar notas.
- Não mostre JSON bruto por padrão; resuma links, Related Notes, blockers e próxima ação.
