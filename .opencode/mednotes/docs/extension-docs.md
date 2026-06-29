# Medical Notes Workbench Extension Docs

These files preserve durable contracts and methodology for Medical Notes
Workbench workflows. They are reference material for commands, runbook skills
and subagents; they are not activatable Gemini skills.

The distributed Gemini CLI extension is sourced from `bundle/` only. If a
runtime document, script, package, or example must ship to users, place its
source under `bundle/` before building.

## Fonte canônica

- `knowledge-architect.md`: Padrão Ouro, formato de nota, taxonomia, footer,
  artefatos Gemini e regras estruturais da `Wiki_Medicina`.
- `semantic-linker.md`: vocabulary DB, body linker, desambiguação contextual,
  Related Notes, reference repair e graph validation.
- `atomicity-splitting-policy.md`: política canônica para decidir split de nota
  a partir do `semantic_signal` do corpo e dos gates do vocabulary DB.
- `flashcard-ingestion.md`: modelos Anki, preview-first, idempotência,
  deeplinks Obsidian, tags e pipeline local de flashcards.
- `anki-mcp-twenty-rules.md`: cópia upstream metodológica do prompt MCP
  `/twenty_rules`; regras locais ficam em `flashcard-ingestion.md`.
- `vault-version-control.md`: pontos de restauração, histórico, rollback e
  versionamento Git invisível ao usuário.
- `workflow-output-contract.md`: resposta final visível dos workflows públicos.

agents e skills devem referenciar estes docs e não repetir contratos longos.
Use duplicação só para sentinelas operacionais críticas testadas ou para
comandos mínimos necessários no runtime context.

`obsidian-ops` é a skill operacional que deve ser carregada antes de qualquer
interação com vault Obsidian. Ela aponta para estes docs e para as CLIs
canônicas em vez de carregar políticas próprias de Git, taxonomia ou linker.
As skills vendorizadas `${extensionPath}/skills/obsidian-cli/SKILL.md` e
`${extensionPath}/skills/obsidian-markdown/SKILL.md` entram como apoio de
ferramenta/sintaxe, sem substituir os contratos médicos do Workbench.

Put workflow sequence and operational branching in activatable skills,
commands, docs, agents, or scripts. Keep `GEMINI.md` as compact routing kernel
and load these documents only when a workflow needs their contract.
