---
description: "Atualiza somente a secao Notas Relacionadas da Wiki_Medicina."
---

<!-- Generated from commands/mednotes/link-related.toml. Do not edit directly. -->

Atualize somente a secao `## 🔗 Notas Relacionadas` no fim das notas da Wiki_Medicina.

Argumentos do usuario: $ARGUMENTS

Use a skill `link-medical-wiki` e carregue `.opencode/mednotes/skills/obsidian-ops/SKILL.md`.
Responda pelo contrato `.opencode/mednotes/docs/workflow-output-contract.md`.

Invariantes do launcher:
- Este comando e o modo estreito de `/mednotes:link` para Related Notes.
- Rode `wiki/cli.py related-notes-sync --dry-run --json` para montar o plano sem
  mutar notas.
- Aplique somente se o dry-run estiver coerente e sem blockers:
  `wiki/cli.py related-notes-sync --apply --receipt <receipt.json> --json`.
- `related-notes-sync` e o unico escritor de `## 🔗 Notas Relacionadas` e usa
  apenas o export do plugin Related Notes.
- Nao rode `run-linker` e nao atualize WikiLinks no corpo do texto.
- Este workflow nao corrige estilo, YAML/status, publicacao ou taxonomia.
- Nao faca regex manual para preencher notas relacionadas.
- Nao mostre JSON bruto por padrao; resuma notas atualizadas, links propostos,
  pendencias acionaveis e proxima acao; cite artefatos tecnicos apenas quando
  forem necessarios para retomar ou auditar.
