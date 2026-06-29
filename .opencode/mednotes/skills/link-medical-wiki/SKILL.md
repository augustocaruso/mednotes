---
name: link-medical-wiki
description: Roda a linkagem da Wiki_Medicina com diagnóstico auditável, vocabulary DB, grafo e Related Notes via export do plugin. Use com /mednotes:link, /mednotes:link-body e /mednotes:link-related.
---

# Skill: link-medical-wiki

Resposta visível: `${extensionPath}/docs/workflow-output-contract.md`.

## Pré-vôo (Hard Stops antes de qualquer comando)

Releia `${extensionPath}/docs/agent-prompt-hardening.md` antes de mutar.
Stop rules específicas deste workflow:

- C19 FSM_POLICY_BOUNDARY:
  Adapter detecta fato. FSM decide política. CLI executa efeito.
  Sem WorkflowEffect emitido pela FSM, não existe recovery automático.
  blocked_reason, next_action, status e operation_payload não autorizam retry/recovery/apply.
- C13 OFFICIAL_ROUTE_ONLY: `related-notes-sync --recover-export --mode auto
  --json` é a ÚNICA rota de recovery do export. Se ele bloquear, reportar e
  parar. NÃO abrir Obsidian CLI direto, NÃO disparar comando do plugin
  Related Notes por fora, NÃO escrever a seção `## 🔗 Notas Relacionadas`
  por regex/manual.
- C14 NO_PARENT_SCHEMA: o agente pai NUNCA escreve
  `note-semantic-ingestion.v1`. Só `med-link-graph-curator` emite esse
  schema por `work_item`. Se já existir um output_path com objeto que parece
  fabricado pelo pai (sem `agent_metrics`, `agent` errado, `primary_meaning`
  como string, aliases sem `kind`/`link_policy`), invalide e relance o
  subagent.
- C15 EVAL_TERMINAL: `eval-curator-batch` em `needs_review` é terminal. NÃO
  editar `curator-prompt-eval.json` para `approved`. Regenerar outputs
  (relançar curator com `error_context`) ou ajustar prompt; reavaliar; só
  aplicar se passar limpo.
- C16 NO_EVAL_BYPASS_IN_PUBLIC: `--skip-prompt-eval` só com
  `MEDNOTES_ALLOW_DEV_ESCAPE=1` + `--skip-prompt-eval-reason`. Em fluxo
  público do usuário, nunca usar esse escape para destravar. Reportar
  `needs_review` e parar.
- C17 GAP_IS_STOP: `contract_gap.missing_next_action` em qualquer payload do
  batch = parar com `error_context`. Sem script, sem `@generalist`, sem
  edição manual.
- C18 HARD_STOP_DECISION: `decision.kind=ask_human` ou
  `human_decision_packet` pendente em qualquer payload canônico = HARD STOP.
  Mostrar a pergunta/opções do pacote e parar. NÃO continuar recovery, reindex
  ou curadoria automática até resposta do usuário. Campo técnico legado
  `human_decision_required` não substitui essa verificação canônica.

## Quando usar

Use para interconectar `Wiki_Medicina`, sincronizar `Notas Relacionadas`, curar
vocabulário do grafo, ou separar corpo/relacionadas. `/mednotes:link` é dono de
todo reparo de grafo: DB, meanings, aliases, body linker, Related Notes e
validação final. Não mantém índice Dataview.

## Fonte canônica

- CLI pública: `${extensionPath}/scripts/mednotes/wiki/cli.py run-linker` com
  `--diagnose` para plano salvo e `--apply --diagnosis <json>` para aplicar só
  diagnóstico validado.
- Body-only: `run-linker --diagnose --no-related-notes --json`, seguido de
  `run-linker --apply --no-related-notes --diagnosis <json> --json`.
- Related-only: `related-notes-sync --dry-run --json`, seguido de
  `related-notes-sync --apply --receipt <json> --json`.
- Export stale do Related Notes: antes de instrução manual, use
  `related-notes-sync --recover-export --mode auto --json`.
- No pacote completo de `/mednotes:link`, não trate uma única aplicação de
  Related Notes como conclusão. Depois de mutar notas, revalide o export e
  confira se a próxima prévia tem zero mudanças; caso ainda haja mudanças
  seguras, continue pela rota oficial em vez de devolver isso ao usuário.
- Retry: se diagnóstico retornar
	  `skipped_reason=redundant_diagnosis_without_state_change`, não execute campo
	  legado como comando. Reuse o artefato indicado ou retome somente por
	  `agent_directive.control.effects`; `--force-diagnose` é escape técnico de
	  debugging.
- Regras semânticas: `${extensionPath}/docs/semantic-linker.md`.
- Prompt hardening: `${extensionPath}/docs/agent-prompt-hardening.md`.
- Recovery oficial do DB de vocabulário:
  `${extensionPath}/docs/vocabulary-db-recovery.md`.
- Saída visível: `${extensionPath}/docs/workflow-output-contract.md`.
- Curadoria semântica é fase interna de /mednotes:link; se o diagnóstico emitir
  `vocabulary_curator_batch_plan_path`, continue o batch e não encerre como
  próximo passo manual.
- Em apply, `vocabulary_semantic_repair` resolve a fila simples pelo baseline
  título/aliases antes de linkar; só pare em decisão humana ou erro operacional.

## Fluxo

1. Localize `${extensionPath}` e `<wiki/cli.py>`.
2. Use `[paths].wiki_dir` do config como destino normal; `--wiki-dir` é override.
   Vocabulary DB é a fonte operacional do body linker.
3. Rode o diagnóstico auditável pela CLI pública:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" run-linker --diagnose --json
   ```

4. Revise `diagnosis_path`, blockers, `reference_repair.note_actions[]`,
   `contextual_alias_disambiguation`, body linker e Related Notes. Ambiguidades reais
   aparecem em `human_decision_packets[]`.
   Para auditoria de grafo, use campos reais: `metrics`, `error_count`,
   `warning_count`, `blocker_count` e agregação de `.errors[].code`. Não use
   campos inexistentes como `broken_links`/`orphaned_notes`; `null` ou `0`
   derivado de campo ausente é falha de schema. Para `run-linker --diagnose`,
   trate campos técnicos como evidência de diagnóstico, não como rota
   operacional paralela. A continuação pública vem do payload FSM/root
   `agent_directive.control` ou do outcome tipado emitido pelo adapter oficial.
   `related_notes_sync.blocked_reason` é evidência diagnóstica do adapter de
   Related Notes, não uma segunda fonte de estado fora da FSM.
   Para `vocabulary-status`, destaque `db_exists`, `queue_counts` e a ação
   pública já normalizada no contrato.
   Se o DB de vocabulário estiver ausente, o diagnóstico só registra
   `vocabulary_bootstrap.status=planned`; ele não cria SQLite nem limpa notas.
   Com notas a ingerir, `vocabulary_bootstrap_required` exige apply antes da curadoria.
   Para DB ausente, rode `vocabulary-status --json` e use a rota pública
   normalizada no payload fresco, que deve apontar rebuild:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode rebuild-db --dry-run --plan-output "<rebuild-plan.json>" --json
   ```

   Para drift/SQLite/fila em DB existente, rode:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-status --json
	   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode reconcile-queue --dry-run --plan-output "<recovery-plan.json>" --json
   ```

   Aplique recovery só com plano revisado e recibo:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode <rebuild-db|reconcile-queue> --apply --plan "<recovery-plan.json>" --receipt "<recovery-receipt.json>" --json
   ```

   Aliases `requires_context` são avaliados no diagnóstico, com
   `--llm-disambiguation auto`. Matches seguros de único alvo canônico são
   resolvidos pelo script; ambiguidades médicas reais não podem abrir
   `gemini -p` escondido e devem seguir orquestração oficial por agente/subagent
   ou virar skip/defer.
   Em `/mednotes:link`, o contrato público é FSM-first:
   `progress_view_model`, `state_machine_snapshot`, `decision`, `receipt`,
   `reports.public_report`, `agent_directive` e `diagnostic_context` quando
   houver problema acionável. Campos técnicos de diagnóstico como `status`,
   `blocked_reason` e `next_action` podem aparecer em payloads internos do
   linker, mas não são fonte de verdade para concluir, bloquear ou responder ao
   usuário.
   Se o blocker for `vocabulary_semantic_ingestion_pending`, o apply deve
   resolver `vocabulary_semantic_repair` e repetir o diagnóstico. Se sobrar
   decisão humana, continue no mesmo `/mednotes:link`: use
	   `vocabulary_curator_batch_plan_path`, lance `med-link-graph-curator`, colete
   outputs com `collect-curator-outputs`, valide e aplique. Só pare para o usuário se a
   FSM expuser `decision.kind=ask_human`, `human_decision_packet` pendente ou
   erro operacional real.

   ```bash
	   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" plan-subagents --phase vocabulary-curation --vocabulary-db "<vocabulary.sqlite>" --output "<vocabulary-curator-batch-plan.json>" --json
	   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" collect-curator-outputs --plan "<vocabulary-curator-batch-plan.json>" --manifest "<manifest.json>" --json
	   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" eval-curator-batch --plan "<vocabulary-curator-batch-plan.json>" --outputs "<manifest.json>" --report "<curator-prompt-eval.json>" --json
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" apply-curator-batch --plan "<vocabulary-curator-batch-plan.json>" --outputs "<manifest.json>" --validate-only --json
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" apply-curator-batch --plan "<vocabulary-curator-batch-plan.json>" --outputs "<manifest.json>" --prompt-eval "<curator-prompt-eval.json>" --receipt "<receipt.json>" --json
   ```

   Não use `@generalist`: o agente pai é o único orquestrador; lance `med-link-graph-curator` diretamente por `work_items[]`.
   Curator não edita Markdown nem chama subagente. Se `eval-curator-batch`
   retornar `needs_review`, não aplique; corrija output/prompt e preserve o DB.
	   `--skip-prompt-eval` só é aceitável como escape técnico local com
	   `MEDNOTES_ALLOW_DEV_ESCAPE=1` e `--skip-prompt-eval-reason`; fluxo público
	   deve usar `--prompt-eval`.
   Depois de aplicar o batch, repita `run-linker --diagnose` e aplique o plano
   seguro.
   Comandos mutantes geram `link-trigger-context.v1` e chamam o linker. Exceção:
   mudança puramente visual por imagens/captions/frontmatter `images_*`.
5. Se o diagnóstico estiver coerente e sem blockers, aplique o mesmo plano:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" run-linker --apply --diagnosis "<link-diagnosis.json>" --json
   ```

   Apply consome o diagnóstico salvo e não chama LLM.
   Não repita `--wiki-dir`, `--catalog-path` ou `--vocabulary-db` quando o
   diagnóstico salvo já contém esses caminhos, a menos que o comando peça
   explicitamente. Use um `--receipt` novo por tentativa; recibo existente é
   bloqueado para preservar a evidência.

6. O JSON inclui `related_notes_sync`; com export do plugin, reescreve a seção
   gerenciada. Sem export, fica `skipped`.
   Se aparecer `related_notes_hash_mismatch` ou export stale, rode:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" related-notes-sync --recover-export --mode auto --json
   ```

   O recovery tenta Obsidian CLI, checa plugin/comandos e só então bloqueia com
   fallback manual. Se o diagnóstico trouxer `body_only_fallback.safe=true`,
   você pode oferecer `/mednotes:link-body`; nunca faça downgrade silencioso.
7. Para atualizar somente WikiLinks no corpo, sem tocar `Notas Relacionadas`:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" run-linker --diagnose --no-related-notes --json
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" run-linker --apply --no-related-notes --diagnosis "<link-diagnosis.json>" --json
   ```

8. Para depurar a seção gerenciada sem rodar o linker completo:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" related-notes-sync --dry-run --json
   ```

9. Para aplicar somente a seção gerenciada, use o recibo:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" related-notes-sync --apply --receipt "<receipt.json>" --json
   ```

10. Responda usando `workflow-output-contract.md`, sem despejar JSON bruto.

11. Para gate de qualidade do body linker em CI, use `evaluate-body-linker`
    com fixtures redigidas.

## Limites

- Não use regex manual para linkar notas.
- Não preencha `Notas Relacionadas` com a heurística de vocabulário/catálogo;
  a seção é gerenciada apenas pelo export do plugin.
- Não publique chats nem corrija estilo/YAML/taxonomia; este skill só linka
  conteúdo já existente.
