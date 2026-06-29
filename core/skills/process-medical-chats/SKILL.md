---
name: process-medical-chats
description: Processa Chats_Raw médicos em notas Wiki_Medicina usando wiki/cli.py, subagents, validação formal, publish dry-run e linker semântico. Use com /mednotes:process-chats.
---

# Skill: process-medical-chats

Resposta visível: `${extensionPath}/docs/workflow-output-contract.md`.
Resposta pública padrão: use `reports.public_report.lines` quando existir e
não exponha comandos literais, flags, schemas, recibos, hashes, paths internos
nem nomes de campos. Detalhes técnicos ficam no canal agente/debug ou
laboratório. Decisão humana aparece como pergunta, opções fechadas, item
afetado e retomada em linguagem humana.

Workflows FSM-first expõem `agent_directive` como o único
contrato FSM -> agente consumível por automação. Consumidores usam
`agent_directive.control` para enforcement e validação, e podem renderizar
`agent_directive.instructions` como contexto para o modelo. Não parseie
relatórios humanos nem preâmbulos em stderr para decidir o estado do workflow.

Adapter detecta fato. FSM decide política. CLI executa efeito.

Sem WorkflowEffect emitido pela FSM, não existe recovery automático.

blocked_reason, next_action, status e operation_payload não autorizam retry/recovery/apply.

Use para processar `Chats_Raw` em notas `Wiki_Medicina` ou continuar
`/mednotes:process-chats`.

## Fonte canônica

- CLI pública: `${extensionPath}/scripts/mednotes/wiki/cli.py`.
- Taxonomia: `${extensionPath}/scripts/mednotes/wiki_tree.py`,
  `taxonomy-canonical`, `taxonomy-tree`, `taxonomy-audit`.
- Estilo/Padrão Ouro: `${extensionPath}/docs/knowledge-architect.md`.
- Grafo/linker: `${extensionPath}/docs/semantic-linker.md`,
  `wiki/cli.py run-linker`.
- Engenharia de prompt e recovery de agentes:
  `${extensionPath}/docs/agent-prompt-hardening.md`.
- Ownership de fase fica na CLI pública e nos agentes listados nesta skill.

## Invariantes runtime

- O corpo do raw chat é imutável. YAML/status operacional pode ser alterado
  para auditoria, mas somente por `wiki/cli.py` (`triage`, `discard`,
  `publish-batch`); nunca por `write_file`, `replace`, shell redirection,
  `sed` ou script ad hoc.
- Mutação compartilhada é serial no agente principal: `triage`, `discard`,
  `stage-note`, dry-run, publish, linker.
- O objetivo é primeira passada publicável: triager deve gerar `note_plan` v2
  completo, com `planned_meaning`, `meaning_claim` explícito, limites
  semânticos e títulos/`staged_title` seguros como filename, sem duplicatas por
  acento/caixa. A triagem não emite `covered_by_existing`; cobertura existente,
  merge canônico e alvo final são decisões posteriores. O meaning planner
  resolve existência/canonicidade; architect deve gerar nota, H1, taxonomia e
  coverage coerentes com esse plano antes de qualquer staging.
- `triage` que muta YAML/status de raw chat médico exige
  `triager-prompt-eval.v1` aprovado e amarrado ao mesmo `raw_file` e
  `note_plan`, além de `subagent-run-receipt.v1` assinado/atestado pelo runner
  oficial para o output do triager. Sem isso, pare e gere o eval/recibo pela
  rota oficial; não edite o YAML manualmente.
- Se `eval-triager-output` retornar `needs_review`, não use `jq`, `cat`,
  `mv`, script ou edição manual para consertar output/`note_plan`/metrics do
  triager. Reenvie o erro ao `med-chat-triager` com `error_context` ou pare no
  bloqueio.
- Não peça ao triager para inventar `agent_metrics`. Métricas ausentes ou
  autodeclaradas são telemetria incompleta; o gate padrão de UX avalia o
  `note_plan`, não contadores de token fabricáveis. Use
  `--require-agent-metrics` apenas em laboratório de prompt.
- A fase de triagem de lote começa por `plan-subagents --phase triage`. O
  parent não lê o raw chat para resumir conteúdo, não monta prompt manual para
  `med-chat-triager` e não decide caminhos de artefatos por conta própria.
  Use `work_item.triager_output_path`, `work_item.note_plan_path` e
  `work_item.triager_eval_path`; o recibo de execução do subagente deve ser
  assinado pelo runner oficial para esse `work_item`, não escrito, editado ou
  assinado pelo parent.
- Paralelize só `work_items`; `batches` apenas agrupam `work_ids`.
- Continuação agentica executável vem somente de
  `agent_directive.control.capabilities.continue=true` com
  `agent_directive.control.effects[]`. `decision`, `human_decision_packet` e `progress_view_model.resume_action` orientam UX, decisão humana e retomada pública; sem `agent_directive.control.effects[]`, pare e reporte o bloqueio em linguagem humana.
- Bloqueio recuperável preserva `error_context`; subagent de reparo recebe
  causa, artefato, correção, `retry_scope` e `next_action`.
- Antes de chamar subagent para corrigir erro, inclua o pacote operacional do
  hardening: `app_version`, workflow, phase, comando oficial, erro exato,
  `blocked_reason`, `next_action`, paths permitidos, schema, hashes e recibo ou
  dry-run quando existir.
- Não crie scripts locais para editar raw chats, coverage, manifest, H1,
  taxonomia, YAML/status ou notas staged. Use os comandos oficiais da
  `wiki/cli.py` para triage, `stage-note --coverage`, `publish-status`,
  `publish-batch`, `fix-note` em temporárias, `apply-canonical-merge` e
  `taxonomy-*`; se faltar rota oficial, bloqueie com `error_context`,
  `next_action` e backlog em vez de contornar validações.
- Para first-pass prevention do relatório de telemetria 2026-05-16 22:29:
  `note_plan_invalid` bloqueia na triagem com `retry_scope=triage_note_plan_only`;
  `blocked.validation_errors` bloqueia em `validate_note` com `fix-note` ou
  `rewrite_prompt` oficial, nunca script; `taxonomy_resolution_required` segue
  `taxonomy-*`/decisão humana antes de staging; `coverage_invalid` exige
  regenerar `raw-coverage.v1` a partir do `note_plan` e repetir apenas
  `stage-note --coverage`. Em todos esses casos, não gere scripts.
- Com `agent.retry_loop` ou "Parar retries automáticos", pare a fase; só retome
  após mudar o input do `error_context` ou obter decisão humana.
- Registre `agent_events` redigidos quando houver retry/loop, fase errada,
  `next_action` ignorado, drift, mutação inesperada, bloqueio ou comando falho.
- Ambiente Python/uv/venv/PowerShell/path quebrado aponta para `/mednotes:setup`
  ou bootstrap/reset oficial; não contorne editando scripts/runbooks.
- Decisão humana usa `human_decision_packet`; resolva e retome por
  `resume_action` antes de publish.
- `note_plan` é da triagem. O architect aceita somente o contrato atual:
  escreve exatamente o `meaning_work_item` lançado pelo planner, gera cobertura
  para esse item e bloqueia qualquer formato antigo/incompleto antes de gastar
  tokens.
- `publish-batch` bloqueia sem `coverage_path`, raw sem `note_plan`, coverage
  divergente, staged fora do plano ou alvo duplicado por normalização.
- Merge multi-fonte usa `raw_files[]`/`sources[]`; `covered` exige
  delta/seção/referência. Falta vira `provenance_gap`.
- HTML salvo por `gemini-md-export` é obrigatório, iframe/linkado; nunca inline.
- Imagem gerada/exportada pelo Gemini é obrigatória: inclua embed Markdown,
  legenda `Figura:` com fonte `Gemini Web` e comentário `gemini-artifact`.
- Sempre rode `publish-batch --dry-run`; publish real exige recibo compatível e
  rollback automático em falha pós-mutação.
- `publish-batch` real gera `link-trigger-context.v1` e chama o linker uma vez
  ao final. Confira o resultado pelo payload FSM: `progress_view_model`,
  `state_machine_snapshot`, `receipt`, `agent_directive` e evidência de linker
  em `diagnostic_context`/`artifacts`.
- Dry-run limpo = `ready_to_publish`; publish real = `published`; linker com
  blockers = `completed_with_link_blockers`. Não chame de `completed`.
- Taxonomia é pasta de categoria; `title` vira o arquivo `.md`.

## Contrato de mudança do modelo de nota

Mudança em modelo de nota (dado, YAML, seção, footer, formatação, tabela,
alias ou proveniência) exige duas portas: `/mednotes:process-chats` valida notas
novas antes de `stage-note`/`publish-batch`; `/mednotes:fix-wiki` audita e
repara notas já ingeridas, ou bloqueia com rewrite/decisão humana. O gate é
`wiki.note_style.NOTE_MODEL_ISSUE_COVERAGE`: novo `StyleIssue` sem
`process_chats_new_notes` e `fix_wiki_retroactive` quebra o contrato.
`didactic_visual_opportunity` em nota nova exige rewrite pelo
`med-knowledge-architect` antes de publicar; siga
`${extensionPath}/docs/knowledge-architect.md` e não publique silenciosamente
uma nota que o validador marcou como precisando de visual/equação didática.

## Fluxo

1. Ache `${extensionPath}`; fallback:
   `~/.gemini/extensions/medical-notes-workbench`.
2. Rota rápida de backlog, antes de qualquer validação ampla:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" list-pending --summary --json
   ```

   Se o payload público indicar que não há chats novos e o workflow é
   terminal, encerre respondendo somente com
   `reports.public_report.lines`: nenhuma
   nota foi publicada/preparada, nenhum raw novo foi processado, nada foi
   escrito na Wiki, coverage/manifest não se aplicam e linker/grafo não
   precisa rodar. Não leia `config.toml`, não liste diretórios, não rode
   `validate`, `list-triados`, `plan-subagents`, `validate-wiki`, fix-wiki,
   linker, publish ou subagents depois desse terminal. Não mencione nomes de
   campos internos, schemas, hashes ou caminhos locais na resposta pública e
   não adicione seção técnica de resumo.
3. Havendo backlog real, bloqueio acionável ou continuação explícita, valide e
   carregue taxonomia:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" validate
   uv run python "${extensionPath}/scripts/mednotes/wiki_tree.py" --max-depth 4 --audit --format text
   ```

   Se `environment_preflight.status=blocked`, pare com setup/reset oficial.
   Organização de pastas usa `taxonomy-migrate --dry-run`; apply só com
   confirmação e rollback por `--rollback --receipt <recibo.json>`.
4. Oriente backlog restante com `list-pending --summary` e
   `list-triados --summary` somente quando a rota rápida não tiver retornado
   terminal sem chats novos. Confira `list-triados`/`plan-subagents --phase
   architect` somente se estiver continuando lote triado aberto.
5. Pendentes: `plan-subagents --phase triage --limit <N>`. Default 5; use 2/3
   em modo econômico. Lance no máximo um triager por `work_item.raw_file`;
   não substitua essa etapa por `read_file` do raw nem por prompt manual;
   salve o output top-level do triager, extraia o `note_plan` para arquivo
   separado usando os caminhos oficiais do `work_item`, preserve o
   `subagent-run-receipt.v1` assinado pelo runner oficial e rode
   `eval-triager-output --raw-file <raw.md> --output <triager-output.json> --subagent-run-receipt <subagent-run-receipt.json> --require-subagent-run-receipt --report <triager-eval.json> --json`
   e aplique em série
   `triage --note-plan <note-plan.json> --triager-eval <triager-eval.json> --json`
   ou `discard` somente se `triager-prompt-eval.v1` passar.
   O `triage` reabre o recibo, confere a assinatura Ed25519, reabre o output
   real do triager e verifica que o `note_plan` aplicado veio desse output; eval
   ou receipt fabricados pelo parent bloqueiam.
   Não peça ao triager para fabricar `agent_metrics`; métrica ausente é warning
   no gate de UX e só bloqueia em laboratório com `--require-agent-metrics`.
   Não escreva artefatos de triagem em `tmp/` na raiz do repo.
6. Triados: `plan-subagents --phase architect --temp-root <tmp> --limit <N>`.
   Se o plano retornar terminal sem chats novos, encerre o workflow usando
   `reports.public_report.lines`: nenhuma nota foi
   publicada/preparada, nenhum raw novo foi processado, nada foi escrito na
   Wiki, coverage/manifest não se aplicam e linker/grafo não precisa rodar.
   Não rode `validate-wiki`, `/mednotes:fix-wiki`, `run-linker`,
   `publish-batch` ou subagents depois desse terminal, e não mencione nomes de
   campos internos na resposta pública.
   Um architect por `work_item` com `launchable: true`; recebe raw(s),
   `temp_dir`, plano, taxonomia e árvore, e escreve só no `temp_dir`.
   `canonical_merge` com `target_kind: new_wiki_note` vira uma única nota nova;
   `canonical_merge` com `target_kind: existing_wiki_note` chama o architect
   para gerar rewrite completo do alvo existente e o parent aplica com
   `apply-canonical-merge`. `blocked_items` com `write_policy: no_temp_note` são
   parada real: não salve Markdown temporário e não empurre para `/mednotes:fix-wiki`.
   Ambiguidade bloqueia, sem nota paralela.
7. Valide temporárias com `validate-note`; se `requires_llm_rewrite`, passe
   `rewrite_prompt` e `error_context` ao architect, máximo 2 tentativas.
   `fix-note` só para YAML/erros determinísticos. Confira cobertura
   informacional. No OpenCode, antes de validar/stagear manualmente um output
   de architect, rode `finalize-opencode-architect-task` com o plano oficial,
   `work_id` e artifacts capturados pelo hook; não use
   `finalize-opencode-specialist-task`, que é exclusivo de `style_rewrite`.
8. Manifest único: `stage-note --coverage <coverage.json>`. Não edite manifest.
   Sem proveniência multi-fonte, bloqueie como `provenance_gap`.
9. Rode `publish-batch --manifest <manifest> --dry-run`. Chame `med-publish-guard`;
   publique só com `approve`.
   Se houver dúvida ou bloqueio de manifest/dry-run, rode
   `publish-status --manifest <manifest> --json`; use o `error_context` dele e
   não edite manifest/recibo manualmente.
10. Rode `publish-batch` real uma vez. A CLI publica, monta trigger context e
    chama o linker. Não rode `run-linker` manualmente depois de uma prévia; no
    publish real, não leia `linker_*` nem campos raiz equivalentes para decidir
    o status público. Use o resultado FSM: `reports`, `artifacts`,
    `diagnostic_context` e `agent_directive` trazem a evidência de grafo/linker
    já normalizada. Com blockers de grafo/linker, a resposta pública deve dizer
    que o grafo ficou pendente e orientar retomada pelo workflow oficial.
11. Registre feedback local quando possível, com `agent_events` e bloqueios de
    ambiente. Responda por `workflow-output-contract.md`, status real, sem JSON
    bruto.

## Paralelização

- Unidade indivisível: raw chat, nota temporária ou nota final.
- Fonte única de spawn: `work_items`; não processe `batches` como segunda fila.
- Um raw com várias notas continua com um architect; merge canônico usa
  ownership por `target_key`.
- Todo `planned_meaning` v2 lançado aparece em coverage/manifest; toda staged
  note aparece no `note_plan`.
- `plan-subagents --phase architect` passa pelo meaning planner atual; formato
  antigo, duplicata ambígua ou payload incompleto bloqueia antes de gastar
  tokens.
- Com 0/1 item, não crie paralelismo artificial.
- Se `truncated: true`, termine a fase atual antes de novo lote.
