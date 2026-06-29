---
description: "Cria flashcards no Anki a partir de notas, pastas, tags Obsidian ou texto."
---

<!-- Generated from commands/flashcards.toml. Do not edit directly. -->

Crie flashcards médicos no Anki a partir do escopo indicado.

Argumentos do usuário: $ARGUMENTS

Use a skill `create-medical-flashcards`. Antes de ler notas/pastas do vault,
carregue `.opencode/mednotes/skills/obsidian-ops/SKILL.md`.
Use também `.opencode/mednotes/docs/workflow-output-contract.md` para responder
com resumo legível, status com emoji e próxima ação.

Invariantes do launcher:
- `/flashcards` é o único comando público de cards.
- Use o MCP global `anki-mcp`; não crie `/twenty_rules` local nem peça ao usuário para executá-lo.
- Resolva fontes com `flashcard_sources.py` antes de ler notas.
- O modo padrão é preview-first; só grave direto se o usuário pedir `--create`,
  `--direct`, `--yes`, `--no-preview`, "criar diretamente" ou equivalente.
- Não use conteúdo fora das fontes resolvidas como base factual.
- Não adicione tags Anki.
- Não mostre JSON bruto por padrão; use `flashcard_report.py` quando houver
  dados estruturados e resuma fontes, candidatos, duplicados, criados e bloqueios.
