---
name: create-medical-flashcards
description: Cria flashcards médicos no Anki a partir de notas, pastas, tags Obsidian ou texto, usando Twenty Rules, flashcard_pipeline.py e o MCP global anki-mcp. Use com /flashcards.
---

# Skill: create-medical-flashcards

Resposta visível: `${extensionPath}/docs/workflow-output-contract.md`.

## Quando usar

Use para `/flashcards`: Markdown, múltiplos arquivos, diretórios, globs, tags
Obsidian, filtros em linguagem natural ou texto/briefing colado.

## Fonte canônica

- Metodologia: `${extensionPath}/docs/anki-mcp-twenty-rules.md`.
- Regras locais, modelos, preview, idempotência, deeplink e tags:
  `${extensionPath}/docs/flashcard-ingestion.md`.
- Templates Anki (HTML/CSS): `${extensionPath}/docs/anki-templates/`.
- Resolver fontes: `${extensionPath}/scripts/mednotes/flashcard_sources.py`.
- Modelos: `${extensionPath}/scripts/mednotes/flashcards/install_models.py`.
- Plano/aplicação: `${extensionPath}/scripts/mednotes/flashcard_pipeline.py`.
- Relatórios: `${extensionPath}/scripts/mednotes/flashcard_report.py`.
- Saída visível: `${extensionPath}/docs/workflow-output-contract.md`.
- Deeplink/tag Obsidian: `${extensionPath}/scripts/mednotes/obsidian_note_utils.py`.

## Invariantes runtime

- `/flashcards` é preview-first. Modo direto só com `--create`, `--direct`,
  `--yes`, `--no-preview`, "criar diretamente", "sem preview" ou equivalente.
- Use apenas o MCP global `anki-mcp` já configurado. As tools aparecem como
  `mcp_anki-mcp_*`; não use nomes crus como `addNotes`.
- Não crie `/twenty_rules` local e não peça ao usuário para executá-lo. Leia a
  cópia local `anki-mcp-twenty-rules.md`.
- Leia fontes resolvidas antes de raciocinar. Não use conteúdo externo como base
  factual dos cards.
- Não adicione tags Anki. A tag Obsidian `anki` só vem depois de sucesso real
  no Anki e apenas nas notas com pelo menos um card aceito.
- Pare antes de escrever se modelos, Anki MCP, confirmação ou plano bloquearem.
  Mais de 40 candidatos exige confirmação.
- Trate `flashcard_pipeline.py prepare` como fonte de verdade. Se ele retornar
  FSM `blocked`, `waiting_human` ou `failed`, não chame Anki nem resuma como
  concluído.

## Fluxo

1. Resolva o escopo antes de ler notas:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/flashcard_sources.py" resolve --scope "<args>" --dry-run --skip-tag anki
   ```

   Omita `--skip-tag anki` só se o usuário pedir refazer/regenerar notas já
   marcadas. Se `summary.requires_confirmation` ou escopo amplo, mostre:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/flashcard_sources.py" preview --scope "<args>" --dry-run --skip-tag anki
   ```

2. Use `manifest.notes` como lista final. Leia cada `path`; preserve `deck`,
   `deeplink`, `vault_relative_path`, `link_mode`, tags e `content_sha256`.
   Texto colado sem nota vai para `Medicina::Inbox`, salvo deck explícito.
3. Garanta os modelos antes de pedir candidatos: chame
   `mcp_anki-mcp_modelNames`, `mcp_anki-mcp_modelFieldNames` e rode:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/flashcards/install_models.py" ensure --existing - --output -
   ```

   Execute as `actions` MCP indicadas. Se status `incompatible`, peça apagar ou
   renomear o modelo no Anki.
4. Leia `anki-mcp-twenty-rules.md` e `flashcard-ingestion.md`. Chame
   `med-flashcard-maker` em modo candidato; ele retorna `preferred_models`,
   `models` e `candidate_cards`, sem gravar no Anki.
5. Prepare o plano:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/flashcard_pipeline.py" prepare --input -
   ```

   Pare se a FSM retornar `blocked`, `waiting_human` ou `failed`. Decisão de
   reprocessamento vem antes de qualquer escrita.
6. Mostre o preview:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/flashcard_report.py" preview-cards --input -
   ```

   No modo padrão, não chame Anki antes da confirmação.
7. Em gravação, use só `new_cards` aprovados e `anki_find_queries`.
   Rode `mcp_anki-mcp_findNotes`, pule duplicados e só então use
   `mcp_anki-mcp_addNotes`/`mcp_anki-mcp_addNote`.
8. Depois do sucesso no Anki, aplique resultados:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/flashcard_pipeline.py" apply --input -
   ```

9. Marque somente notas com pelo menos um card aceito:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/obsidian_note_utils.py" add-tag --tag anki --effect-target flashcards.tag_obsidian --vault-guard-receipt <vault-guard-receipt.json> <arquivos...>
   ```

10. Gere o resumo final quando houver dados estruturados:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/flashcard_report.py" final --input -
   ```

   Termine pelo contrato de saída: status emoji, fontes, cards candidatos/novos,
   duplicados, notas marcadas `anki`, bloqueios e próxima ação.
