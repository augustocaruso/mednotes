---
name: fix-medical-wiki
description: Fix-wiki para taxonomia, estilo, grafo. Use com /mednotes:fix-wiki.
---

# Skill: fix-medical-wiki

`${extensionPath}` é a raiz da extensão/plugin ativo. Se a skill vier de
`~/.gemini/config/skills/fix-medical-wiki` ou de contexto stale, pare,
recarregue `${extensionPath}/skills/fix-medical-wiki/SKILL.md` e use só esse
root. Não chame `read_file` em `${extensionPath}/docs/workflow-output-contract.md` só para responder.

`Wiki_Medicina`: estilo, YAML, grafo, taxonomia.

## Pré-vôo (Hard Stops antes de qualquer comando)

Responda sempre em português do Brasil, inclusive avisos intermediários.
Releia `${extensionPath}/docs/agent-prompt-hardening.md` antes.
`/mednotes:fix-wiki` é o workflow mais arriscado: chama linker, curator
batch, taxonomy migration e style rewrite. Stop rules específicas:

- C23 FSM_POLICY_BOUNDARY:
  Adapter detecta fato. FSM decide política. CLI executa efeito.
  Sem WorkflowEffect emitido pela FSM, não existe recovery automático.
  blocked_reason, next_action, status e operation_payload não autorizam retry/recovery/apply.
- C22 AGY_APPLY_NO_DISCOVERY: no AGY, depois de carregar esta skill e
  `obsidian-ops`, o próximo tool call em `--apply` deve abrir a proteção do
  vault com `<plugin-root>/scripts/vault/vault_git.py run-start`. Não use
  `grep_search`, `list_permissions`, `list_dir`, `list_directory`, `ls`,
  `echo` ou `--help` para confirmar scripts, permissões ou plugin root; o
  `<plugin-root>` já é o diretório desta skill.
- C18 HARD_STOP_DECISION: `decision.kind=ask_human` ou
  `human_decision_packet` pendente em qualquer payload canônico é HARD STOP.
  Mostre a pergunta/opções em pt-BR e pare; não continue recovery, reindex,
  curadoria, plan-subagents ou apply sem resposta humana.
- C15 EVAL_TERMINAL: `eval-curator-batch` ou `eval-triager-output` em
  `needs_review` = regenerar outputs ou ajustar prompt; nunca editar o JSON
  de avaliação.
- C16 NO_EVAL_BYPASS_IN_PUBLIC: `--skip-prompt-eval` é dev-escape; nunca em
  fluxo público.
- C14 NO_PARENT_SCHEMA: pai NÃO escreve `note-semantic-ingestion.v1`. Use
  `plan-subagents --phase vocabulary-curation`, lance
  `med-link-graph-curator` por `work_item`, colete via
  `collect-curator-outputs`.
- C13 OFFICIAL_ROUTE_ONLY: `related-notes-sync --recover-export` é a única
  rota de recuperação do export Related Notes. Ela pode acionar Obsidian CLI ou
  fallback headless; sem `agent_directive.control.effects`, reporte e pare.
   - C12 NEXT_ACTION_NOT_AUTHZ: `next_action` é orientação. Continue só com
     `agent_directive.control.status=waiting_agent`,
     `agent_directive.control.capabilities.continue=true`, efeitos executáveis
     e pedido original permitindo continuar.
     Se `agent_directive.control.status=waiting_agent`, não finalize o guard e não responda
     ainda; execute ou bloqueie explicitamente essa continuação primeiro.
- C17 GAP_IS_STOP: `contract_gap.missing_next_action` = reportar e parar;
  sem workarounds.
- C19 INSTALLED_BUNDLE_IMMUTABLE: não edite o bundle instalado em
  `~/.gemini/extensions`, `~/.gemini/config/plugins`, `C:\Users\<usuario>\.gemini\extensions`
  ou `C:\Users\<usuario>\.gemini\config\plugins`. Reporte
  `installed_extension_runtime_edit_forbidden`, aponte o fonte em `bundle/`
  e pare; correção real exige rebuild/reinstall oficial.
- C20 LAYERED_STATUS_REQUIRED: responda sempre com estado em camadas
  (ambiente Python, índice Markdown, proteção do vault, linker, Related Notes,
  cota/especialista). Um layer verde não torna o workflow concluído. Mostre um
  bloqueio atual e uma próxima ação.
- C21 PACKAGED_SPECIALIST_AGENT: não fabrique prompt curto para
  `med-knowledge-architect`. Para `style_rewrite`, consuma o efeito
  `call_specialist_model` e seu `current_batch_items`. No AGY, leia
  `agents/med-knowledge-architect.md`,
  chame `define_subagent`, invoque exatamente um item tipado por vez com
  `Prompt` igual ao JSON do `current_batch_item` e finalize a evidência com
  `finalize-agy-specialist-task` para gerar recibo validável;
  no OpenCode, use a tool `task` somente no harness OpenCode, com um único
  work item tipado dentro de JSON raiz contendo somente `current_batch_items`,
  e finalize com `uv run python
  bundle/scripts/mednotes/wiki/cli.py finalize-opencode-specialist-task`
  sem `--task-metadata` manual; o hook prova provider/modelo por `work_id`;
  não recrie `agent.json`; não envie conteúdo clínico por `send_message`. Sem
  capacidade/modelo/evidência no runtime, bloqueie
  `specialist_agent_runtime_unavailable`, `agy_specialist_model_evidence_missing`
  ou `specialist_model_quota_exhausted`.

UX pública: use `${extensionPath}/docs/public-vocabulary.md`; não exponha
`uv`, `--dry-run`, `manifest`, `hash`, `SQLite`, `needs_review`,
`skip-prompt-eval` ou nomes de subagent por padrão.
Resposta pública padrão: use `reports.public_report.lines` quando existir e
não exponha comandos literais, flags, schemas, recibos, hashes, paths internos,
`run_id` nem nomes de campos. Detalhes técnicos ficam no canal agente/debug ou
laboratório. Decisão humana aparece como pergunta, opções fechadas, item
afetado e retomada em linguagem humana.

## Fonte canônica

- Estilo: `${extensionPath}/docs/knowledge-architect.md`.
- Grafo/linker: `${extensionPath}/docs/semantic-linker.md`.
- CLI pública: `${extensionPath}/scripts/mednotes/wiki/cli.py`; domínio em `wiki.*`.
- Taxonomia: `wiki.health` e `wiki.taxonomy.migration`.
- Links: /mednotes:fix-wiki não implementa grafo; chama /mednotes:link.
- Reescrita/merge LLM: `med-knowledge-architect` só com pedido da CLI.

## Contrato de mudança do modelo de nota

Mudança no modelo exige `/mednotes:process-chats` novo e `/mednotes:fix-wiki`
retroativo. Novo `StyleIssue` entra em
`wiki.note_style.NOTE_MODEL_ISSUE_COVERAGE`; sem rota retroativa, bloqueie.
`didactic_visual_opportunity` em nota existente vira `style-rewrite`; regra em
`${extensionPath}/docs/knowledge-architect.md`, nunca saúde verde.
Notas geradas de vídeo, aula, livro, artigo ou outra fonte não-chat continuam
notas normais da Wiki. Preserve `source`, `sources` e `source_*`; não trate a
ausência de `chats[]` como problema quando a nota já documenta fonte não-chat.

## Fluxo

1. Use sempre o `${extensionPath}` carregado. Execute CLI por
   `node "${extensionPath}/scripts/run_python.mjs" "${extensionPath}/scripts/mednotes/wiki/cli.py" ...`.
   Cada `run_shell_command` deve conter um único comando completo; não crie
   variável de shell como `EXT_PATH`, não use `export`, não envie script
   multiline e não encadeie comandos.
   No AGY, para comandos oficiais longos (`fix-wiki --apply`,
   `finalize-agy-specialist-task` e `apply-specialist-style-rewrite`), use
   `WaitMsBeforeAsync=120000` para receber o JSON direto da tool em vez de cair
   em task log/background. Para `run-start` e
   `run-finish`, use `WaitMsBeforeAsync=30000`. Se o AGY ainda mover para
   segundo plano, não use `schedule`/timer e não descubra logs: leia
   imediatamente somente o task log indicado pela própria tool como fallback
   oficial, parseie o JSON final desse log e, se precisar mencionar isso ao
   usuário, diga em linguagem humana que você leu o log indicado pela
   ferramenta. Não use rótulo técnico literal para esse fallback.
   `run-finish` deve usar o
   same plugin root as run-start:
   `${extensionPath}/scripts/vault/vault_git.py`; qualquer caminho como
   `~/.gemini/config/plugins/vault/vault_git.py` ou
   `config/plugins/vault/vault_git.py` está errado e não deve ser executado.
   Não invente variantes como `dist/gemini-cli-experiment`, não use
   `bundle/scripts/...` relativo ao cwd. Do not execute `cli.py` directly.
   Do not set/export `UV_PROJECT_ENVIRONMENT`.
   No preparatory shell probes before explicit `fix-wiki --dry-run`: deve ser
   o primeiro shell command, salvo pedido do JSON fresco. Se stdout truncar, leia artefatos
   oficiais; não repita o mesmo `fix-wiki --dry-run --json` e não redirecione stdout do workflow para scratch.
   Do not self-debug `uv run` failures: se houver erro de `uv`, Python, venv,
   import, path ou PowerShell, feche o guard se aberto, reporte
   `environment_blocker.windows_path_or_venv`, aponte `/mednotes:setup` e pare.
   Não conserte com `env`, `pip list`, `read_file`, direct venv Python ou
   `PYTHONPATH`.
   Se o usuário já pediu continuidade e `/mednotes:setup`/rebuild resolver
   pendência determinística, retome o workflow quando o payload fresco trouxer
   `agent_directive.control.effects` sem decisão humana.
2. Pedido explícito com `--apply` não é dry-run: abra proteção com
   `node "${extensionPath}/scripts/run_python.mjs" "${extensionPath}/scripts/vault/vault_git.py" run-start --agent gemini-cli --workflow /mednotes:fix-wiki --json`;
   aguarde o `tool_result` do `run-start`, leia `run_id` não vazio e só então execute
   `node "${extensionPath}/scripts/run_python.mjs" "${extensionPath}/scripts/mednotes/wiki/cli.py" fix-wiki --apply --json`.
   Não envie `run-start` e `fix-wiki --apply` no mesmo lote. Examine o JSON
   fresco antes de fechar o guard. Se houver
   `agent_directive.control.status=waiting_agent`,
   `agent_directive.control.capabilities.continue=true`,
   `agent_directive.control.capabilities.final_report=false` e
   `agent_directive.control.effects`, execute a fase indicada no mesmo guard
   antes de responder ou rodar `run-finish`. Feche com
   `node "${extensionPath}/scripts/run_python.mjs" "${extensionPath}/scripts/vault/vault_git.py" run-finish --agent gemini-cli --workflow /mednotes:fix-wiki --run-id <run_id> --title "Reparo da Wiki_Medicina" --public-json --json`.
   Copie o `run_id` literal; `--run-id ""` e `run-finish` sem `--workflow` são
   blockers. Não entre em Plan mode, não escreva plano `.md`, não peça
   confirmação de estratégia. `tracker_create_task`/`tracker_update_task` não
   são necessários neste fluxo; uso ou erro de tracker deve entrar como atrito
   de UX, mas `update_topic` normal não é desvio do workflow. Não converta
   `/mednotes:fix-wiki --apply` em dry-run.
   Nota Markdown estritamente vazia na raiz da Wiki é higiene estrutural: o CLI
   deve arquivar antes de style/taxonomia/grafo. Nota raiz com conteúdo
   inválido entra na camada de reparo/aviso, não em taxonomia. Só trate nota
   raiz como decisão humana de taxonomia quando ela já for uma nota Wiki válida.

   Não rode `set-paths` como preflight genérico. Use só se o JSON fresco indicar
   `wiki_dir` ou `raw_dir` vazio/inválido. Não edite `config.toml` manualmente;
   para paths, use:

   ```bash
   node "${extensionPath}/scripts/run_python.mjs" "${extensionPath}/scripts/mednotes/wiki/cli.py" set-paths --wiki-dir "<Wiki_Medicina>" --raw-dir "<Chats_Raw>" --agent-repair --json
   ```

	   Se retornar `path_conflict.requires_decision`, mostre o pacote e pare. Para
	   mojibake de template, use `repair-config-template --json`.

   `--apply` usa a proteção do vault como rollback primário. `.bak` adjacente
   de Markdown está aposentado e não deve ser criado. Taxonomia exige
   confirmação extra por `taxonomy_plan_path` e `--apply-taxonomy`.
3. Interprete `progress_view_model`, `state_machine_snapshot`, `decision`,
   `receipt`, `reports`, `agent_directive`, blockers, rewrites, backups,
   higiene e linker.
   Não rode `root-hygiene-audit` dentro de `/mednotes:fix-wiki`; ele é
   diagnóstico de `/mednotes:status` e polui o relatório da Wiki.
	   Se houver mutação, confira os artefatos de link em `artifacts`/`receipt`
	   e o status canônico em `agent_directive.control`. Se o grafo bloquear, continue
	   dentro do fluxo público de `fix-wiki`, mas chame apenas os comandos oficiais
	   de `/mednotes:link`; não implemente reparo privado de grafo.
   `requires_llm_rewrite` não pula linker: em `--apply`, aplique grafo/body
	   linker/Related Notes quando seguro. Se embeddings bloquear em recuperação
	   de Related Notes, continue antes de responder; sem plano
   pronto, reporte o bloqueio. MarkdownDB não é gate global.
   Related Notes estável exige export revalidado + prévia zero; senão continue
   ou bloqueie.
   Nunca remova `chats:` do YAML no bootstrap/reset de vocabulário ou estilo.
   Em apply, `vocabulary_semantic_repair` resolve a fila simples antes do
   linker. Se ainda houver `blocked_pending`, use o plano do link
	   (`vocabulary_curator_batch_plan_path`), lance `med-link-graph-curator` por
   `work_items[]`, colete outputs via `collect-curator-outputs` e aplique o
   lote como fase interna de `/mednotes:link`:

   Se o payload fresco vier com
   `agent_directive.control.status=waiting_agent` e efeitos executáveis,
   isso não é bloqueio terminal:
   continue somente por `agent_directive.control.effects[]`. Para
   `call_specialist_model`, consuma
   `agent_directive.control.effects[].payload.current_batch_items`; para
   `run_subworkflow`, execute a sub-rotina indicada pelo próprio efeito e
   valide o recibo retornado. Não repita `fix-wiki --dry-run` enquanto houver
   efeito executável fresco. `continuation_steps` não é rota operacional.

   ```bash
	   uv run python "<wiki/cli.py>" plan-subagents --phase vocabulary-curation --vocabulary-db <vocabulary.sqlite> --output <vocabulary-curator-batch-plan.json> --json
	   uv run python "<wiki/cli.py>" collect-curator-outputs --plan <vocabulary-curator-batch-plan.json> --manifest <manifest.json> --json
	   uv run python "<wiki/cli.py>" eval-curator-batch --plan <vocabulary-curator-batch-plan.json> --outputs <manifest.json> --report <curator-prompt-eval.json> --json
   uv run python "<wiki/cli.py>" apply-curator-batch --plan <vocabulary-curator-batch-plan.json> --outputs <manifest.json> --prompt-eval <curator-prompt-eval.json> --receipt <receipt.json> --json
   ```

	   Batch valida prompt eval, hashes, enum `atomic_status` e link nota->meaning;
	   escreve só no SQLite, marca itens e depois repete `/mednotes:link`.
   Corpus de ouro: `init-curator-expectations`, 2+ asserções/item e
   `--expectations <golden.json>` só no eval.
   Não use `@generalist`: o agente pai é o único orquestrador; lance `med-link-graph-curator` diretamente por `work_items[]`.
4. Com `write_error_count > 0`, trate como IO bloqueado: linker real fica
   pendente por erro de escrita; peça liberar
   iCloud/Obsidian/antivírus/processo antes de retentar.
5. Continuação automática é proibida sem autorização explícita do payload. O
   campo `next_action` é orientação para reportar ao usuário; ele não autoriza
   execução. Repita ou avance só se `agent_directive.control.status=waiting_agent`,
   `agent_directive.control.capabilities.continue=true`,
   `agent_directive.control.effects` existir e o pedido original permitir
   continuidade. Se não houver efeitos executáveis, `dry_run=true` ou
   `human_decision_packet` pendente, mostre a pergunta/opções do pacote humano,
   a ação de `decision.next_action`/`receipt.next_action` quando existir, e pare.
   Pedido inicial com `--apply` autoriza somente a continuação automática
   descrita por `agent_directive.control.effects` quando
   `agent_directive.control.capabilities.continue=true`,
   `capabilities.final_report=false` e não houver decisão humana; isso ainda
   faz parte do mesmo workflow protegido. Sem efeito canônico executável, depois
   de payload bloqueado só avance com nova confirmação explícita.
   Para recuperação de Related Notes, aguarde retry, retome o índice parcial
   e reexecute apply; sem dry-run ou edição manual.
6. Só planeje reescrita LLM quando o payload fresco trouxer
   `agent_directive.control.status=waiting_agent` com
   `agent_directive.control.effects` para `style_rewrite`, sem decisão humana,
   ou quando o usuário pedir continuidade depois de ver o bloqueio e o payload
   não exigir decisão humana. Não planeje rewrite se o mesmo payload tiver
   `human_decision_packet` pendente ou se faltar `agent_directive.control.effects`,
   mesmo que `next_action` mencione uma rota técnica:

   Use o `agent_directive.control.effects[].payload.current_batch_items` do JSON fresco. Antes de invocar
   o subagente, garanta que
   `agent_directive.control.effects[].payload.agent_workspace_requirements.required_workspace_dirs`
   estão disponíveis no workspace do runtime/subagente. Se algum `temp_output` oficial
   não for gravável, bloqueie como `agent_workspace_missing`; não use scratch,
   `run_command`, Python inline, cópia paralela ou Markdown colado como workaround.
   Se precisar
   regenerar o plano técnico fora do apply principal, mantenha lote pequeno:

   ```bash
   uv run python "<wiki/cli.py>" plan-subagents --phase style-rewrite --max-concurrency 3 --limit 3 --temp-root <tmp-rewrites>
   ```

   Use exatamente um `med-knowledge-architect` por `current_batch_items[].target_path`.

   ### Reescrita Especializada Por Harness

   Quando `progress_view_model.status=waiting_external`,
   `can_continue_now=false` ou não houver efeito canônico executável,
   pare e reporte a pausa; não invoque subagente, não procure schema e não
   tente fabricar recibo. Quando `progress_view_model.status=waiting_agent` e
   `agent_directive.control.effects` trouxer `call_specialist_model`, continue
   no harness atual:

   - Gemini CLI: invoque o especialista empacotado com um único
     `current_batch_item` do efeito `call_specialist_model` e aguarde o
     `specialist-task-run-receipt.v1` validável.
   - AGY: leia o template empacotado indicado pelo
     `specialist_agent_invocation_contract.antigravity_cli`, use
     `define_subagent` com o template completo, chame `invoke_subagent` com
     `Prompt` igual ao JSON de um único `current_batch_item` tipado e finalize
     com `finalize-agy-specialist-task` usando o transcript/task log oficial e
     `--runtime-log` quando houver janela AGY settings switch.
   - OpenCode: use `task` somente quando o harness atual for OpenCode.
     O parent pode estar em Flash para orquestrar, mas a task especialista deve
     receber JSON raiz contendo somente `current_batch_items` e provar modelo
     especialista via metadata OpenCode; depois rode
     `uv run python bundle/scripts/mednotes/wiki/cli.py
     finalize-opencode-specialist-task --plan <plan> --work-id <work_id>
     --json`, sem `--task-metadata` manual. Não exija OpenCode
     quando o usuário estiver no Gemini CLI ou AGY; nao exija OpenCode
     fora do harness OpenCode.

   O especialista deve receber somente o work item tipado, paths oficiais,
   hashes e `temp_output`. Não cole Markdown bruto nem raw chat no parent.
   Depois que o especialista escrever `temp_output`, use somente o
   `medical-notes-workbench.specialist-task-run-receipt.v1` atestado pela borda
   oficial do Workbench. O parent não deve copiar, editar ou simular esse
   recibo; no AGY, a criação permitida é via `finalize-agy-specialist-task`
   com evidência de transcript/task log e runtime log quando houver settings
   switch; no OpenCode, é via `finalize-opencode-specialist-task` com metadata
   oficial da task. Se não
   houver `receipt_attestation` válida, bloqueie como
   `specialist_task_run_receipt_attestation_required`. Nunca use
   `--gemini-binary`, script em scratch, mock ou backup manual de
   `temp_output` para assinar saída de outro processo. Depois do recibo, use
   `apply-specialist-style-rewrite --specialist-run-receipt` para finalizar,
   coletar e aplicar o `work_id` em uma única chamada oficial; não divida
   finalize/collect/apply em tool calls separadas. Depois de aplicar o lote,
   rerode `/mednotes:fix-wiki` quando a fila estiver vazia e reporte qualidade
   antes/depois do lote.

   No AGY, o caminho padrão é o subagente empacotado do próprio AGY, não o
   runner Gemini CLI. Nunca coloque duas notas no mesmo prompt de especialista;
   faça uma chamada oficial por `work_id`, em série. O especialista escreve só no `temp_output` literal do
   `work_item`; não substitua por `~/.gemini/tmp`, caminho inventado ou cópia
   paralela. O pai não deve chamar `read_file` para colar a nota no prompt do
   subagente; passe apenas os campos oficiais do `work_item` (`target_path`,
   `rewrite_prompt`, `temp_output`, hashes/ids e o
   `subagent_output_contract`) e deixe o subagente ler a nota dentro do escopo
   dele. `output_attestation_path` é campo parent-only: o especialista não deve
   criar, editar ou simular atestação. A rota deve respeitar
	   `model_policy=medical_specialist_authoring.v1`; Gemini usa o modelo
	   configurado no subagente, e runtimes com fallback precisam registrar um
	   modelo aceito pela política quando esse dado existir. Não invente
	   `actual_model`: a atestação do Workbench trata argumento do parent como
	   alegação não verificada. O caminho forte é o
	   `specialist-task-run-receipt.v1` atestado pelo runner. Sem runner Python,
	   siga os efeitos canônicos no harness atual. Só reporte
	   `specialist_model_capacity_unavailable` após
	   falha real do runtime/modelo; então bloqueie e feche o guard. Depois de
	   uma parada por quota, capacidade ou validação, a resposta pública final
	   deve ser humana e curta: não anexe bloco diagnóstico, JSON, XML, YAML,
	   campos internos, recibos, hashes ou caminhos locais. Detalhes técnicos
	   ficam no log/JSON de validação.
	   Depois de
	   aplicar um lote, não rode a conferência completa do `fix-wiki` se ainda há
   reescritas pendentes; use `plan-subagents --phase style-rewrite` para montar
   o próximo lote e deixe a verificação completa para quando a fila de
   reescrita estiver vazia. Repetir o workflow inteiro entre lotes é bug de
   performance/UX.
   Para aplicação da reescrita, use sempre
   `agent_directive.control.effects[].payload.plan_path` e
   `agent_directive.control.effects[].payload.manifest_path`. Não use
   `fix_wiki_plan_path`: esse é o
   plano geral do workflow, não o plano `subagent-plan.v1` exigido pela rota
   atômica.
7. Aplicação atômica de cada rewrite:

   ```bash
   uv run python "<wiki/cli.py>" apply-specialist-style-rewrite --plan <style-rewrite-plan.json> --manifest <style-rewrite-manifest.json> --work-id <work_id> --specialist-run-receipt <specialist-task-run-receipt.json> --json
   ```

   O recibo do runner deve registrar o modelo realmente usado pelo especialista,
   os hashes do pacote de entrada, output e transcript, e uma assinatura válida
   do runner oficial; vazio, `unknown`, `auto`, Flash ou recibo sem evidência
   operacional bloqueiam a aplicação. O apply real não aceita Markdown solto. A
   rota atômica cria a atestação Workbench, coleta um manifest de item único e
   aplica o rewrite validado. O plano também precisa vir de `plan-subagents` com
   `subagent-plan-attestation.v1`; não copie, edite ou fabrique o JSON do
   plano, porque a rota atômica bloqueia plano sem hash/assinatura oficial. Use
   limite 2 tentativas por nota; aplique rewrites em série por `work_id`, um
   comando por tool call. Depois de cada apply, leia o
   `agent_directive` root do stdout compacto. Se
   `agent_directive.control.effects` ainda trouxer trabalho especialista
   executável, continue por `current_batch_items` sem reler plano/manifest e
   sem rodar validação final. Se o lote acabou, reporte um checkpoint humano
   com quantas notas foram corrigidas, quantas faltam, estado honesto do grafo
   e estado de Notas Relacionadas; depois siga somente efeitos canônicos novos
   emitidos pela FSM.
   Rode o workflow completo novamente só quando a fila de reescrita estiver
   vazia. Se o manifest, atestação, hash do output, hash do plano ou hash da
   nota alvo divergir, pare e reporte o bloqueio (`agent_notice`,
   `error_context`, `next_action`). Nunca reescreva a mesma nota em paralelo.
8. Se a FSM/`agent_directive` indicar merge de notas como rota recuperável, não
   pare no primeiro `fix-wiki`. O diagnostic context é apenas evidência privada;
   a rota executável continua sendo `agent_directive.control`. Se vier
   `title_driven_merge_review`, confirme a identidade semântica antes de criar
   qualquer merge. Planeje merges:

   ```bash
   uv run python "<wiki/cli.py>" plan-subagents --phase note-merge --max-concurrency 3 --temp-root <tmp-merges>
   ```

   Use um `med-knowledge-architect` por `work_item.group_id`. Payload completo
   vem só de `work_items`; `batches[].work_ids` apenas escolhe a rodada.
9. Valide cada merge:

    ```bash
    uv run python "<wiki/cli.py>" apply-note-merge --plan <plan.json> --content <merged.md> --dry-run --json
    ```

    Aplique automaticamente só se o plano oficial trouxer identidade semântica
    auditável, nenhuma `decision.kind=ask_human`, nenhum
    `human_decision_packet` pendente e nenhum conflito `images_*`:

    ```bash
    uv run python "<wiki/cli.py>" apply-note-merge --plan <plan.json> --content <merged.md> --json
    ```

    Apply valida hashes, contrato Wiki, provenance e preservação; rollback se
    falhar depois de mutar. Quando aplica, grava recibo,
    `linker_trigger_context_path` e chama o pacote completo do linker.
10. Atomicidade segue `${extensionPath}/docs/atomicity-splitting-policy.md`:
    vem do DB, nunca de título/tamanho aparente. Só
    `atomicity_decision=split_required`, derivado de `semantic_signal` no corpo,
    cria split aplicável. Se
    `atomicity_split_plan_path` existir ou
    `identity.atomicity.one_note_multiple_meanings` aparecer pendente, planeje:

    ```bash
    uv run python "<wiki/cli.py>" plan-subagents --phase atomicity-split --fix-wiki-plan <fix-wiki-plan.json> --temp-root <tmp-splits> --json
    ```

    Use um `med-knowledge-architect` por `work_item.source_path`; ele escreve
    apenas `atomicity-split-bundle.v1` e Markdown temporário. O bundle deve
    copiar `work_id`, `source_path` e `source_hash` do work item oficial.
    Preserve `work_item.semantic_signal`; não substitua por opinião sobre nota
    nem por title-only signal.
    Aplique por:

    ```bash
    uv run python "<wiki/cli.py>" apply-atomicity-split --bundle <atomicity-split-bundle.json> --json
    ```

    O apply marca o `deferred_work_item` como `completed` no vocabulary DB.
    Enquanto o plano existir, trate `blocked_reason=atomicity_split_required`
    como pendência real; não encerre como saúde verde. `--defer-linker` só vale
    com `--parent-batch-id` dentro de lote pai que vai rodar `/mednotes:link`
    uma vez ao final.
11. Após rewrites/merges/splits aceitos, revalide:

    ```bash
    uv run python "<wiki/cli.py>" fix-wiki --apply --json
    ```

12. Se `taxonomy_action_required` vier com
    `taxonomy_apply_requires_confirmation=true`, nenhum movimento de pasta foi
    feito. Revise `taxonomy_plan_path`; se estiver correto:

    ```bash
    uv run python "<wiki/cli.py>" fix-wiki --apply --apply-taxonomy --json
    ```

    Se persistir depois disso, falta decisão humana/semântica. Use `decision`,
    `human_decision_packet`, `receipt`, `agent_directive` e evidência privada em
    `diagnostic_context`; não dirija o fluxo por campos diagnósticos soltos.
13. Backups `.bak` são legado e não devem ser criados. Se a higiene encontrar
    `.bak` antigo ou `.rewrite`, eles devem ficar fora do vault; confira higiene
    e cleanup antes de concluir.
14. Resposta final: priorize o objetivo do usuário. Se o JSON fresco trouxer
    `reports.public_report.lines`, use essas linhas como fonte determinística da saída
    pública e não reconstrua um relatório operacional. Em revisão, confira
    bloqueio antes de mutar.
    Para dry-run/apply bloqueado, não exponha `blocked_reason`,
    `blocking_reasons`, `required_inputs`, paths de artefatos ou schemas por
    padrão. Esses campos são contrato de automação/debugging; só aparecem se o
    usuário pedir laboratório ou se você estiver fazendo relatório técnico. Não
    use "concluído", "concluída", "concluiu", "finalizado", "pronto",
    "sucesso", "com sucesso" ou "comportamento esperado" para descrever
    workflow bloqueado/parcial. Subetapa atualizada não é workflow completo:
    escreva "Notas Relacionadas atualizadas", "grafo sem blockers" ou "proteção
    do vault encerrada", nunca "etapa concluída com sucesso" quando a Wiki ainda
    aguarda especialista/cota.
    Se você rodou `list_directory`/listou diretório fora do roteiro,
    reporte como listagem de diretório. Não rode
    `plan-subagents` por iniciativa própria e não invente campos ausentes/nulos
    como `linker_pending_reason`.
    Preserve os rótulos de artefato: `fix_wiki_plan_path` aponta para
    `fix-wiki-plan.json`, e `run_state_path` aponta para `run_state.json`.
    Se `Exit Code:` for diferente de zero, reporte o código mesmo quando
    `tool status=success`; `Exit Code: 3` com JSON `status=blocked` é blocker
    do workflow, não warning auxiliar. Workflows FSM-first expõem
    `agent_directive` como o único contrato FSM -> agente
    consumível por automação. Consumidores usam `agent_directive.control` para
    enforcement e validação, e podem renderizar `agent_directive.instructions`
    como contexto para o modelo. Não parseie relatórios humanos nem preâmbulos
    em stderr para decidir o estado do workflow.
    Se aplicou reescrita de nota, reporte uma auditoria antes/depois do lote:
    preservação de YAML/proveniência/links, se a pendência indicada foi
    resolvida na seção correta e se a qualidade médica/didática ficou boa.
    Nota formalmente válida mas ruim é bug de UX/conteúdo.
    Em saída pública, não liste comandos internos, `node`, paths de script,
    `--json`, `run-start` ou `run-finish`; traduza para proteção preparada,
    reparo executado e proteção encerrada. não invente warning para ferramenta
    que não foi chamada. `update_topic` segue a mesma política de redação
    pública, mas `update_topic` bem-sucedido não é desvio; se for mencionado,
    não inclua `run_id`, hash curto, comando interno ou flag técnica. Em
    resposta pública normal, não liste `required_inputs`; traduza o que falta
    para linguagem acionável. Em relatório técnico/debugging, cite
    `decision.next_action`/`receipt.next_action` como campo canônico; na saída
    pública prefira `reports.public_report.lines`. Não misture ação de decisão
    com retomada; se mostrar `resume_action`, use rótulo separado.
    Em relatório técnico, preserve códigos de bloqueio nos campos canônicos da
    FSM (`decision`, `diagnostic_context`, `error_context` ou recibos técnicos)
    sem transformá-los em verdade paralela. Não copie `blocked_reason`
    top-level como contrato público. Se houver
    `primary_human_decision_kind`, `human_decision_kinds[]` ou
    `human_decision_packet.decision_kind`, reporte em rótulo separado; não
    transforme isso em `blocked_reason` composto. Backups `.bak` novos não
    devem ser relatados como criados; `.bak`/`.old` legados são pendência de
    higiene explícita, não mutação automática de `fix-wiki`. Use
    `final_validation.hygiene.bak_or_rewrite` apenas como sinal técnico de
    legado pendente. Se
    `change_count_context.changed_count_applied=false`, `changed_count` é plano
    de prévia, não mutação no vault; mutação real vem de `written_count`,
    `total_changed_count` e
    `version_control_mutation_summary.changed_file_count`; não derive contagem
    de backups de `changed_count`/`written_count`.
    Em run com mutação, reporte a evidência de fechamento do guard:
    `guard_lease.status=closed` no `run-finish` ou `guard-status.active_count=0`.
    Se `blocked_reason=guard_lease_mismatch`, `guard_lease.status=missing` ou
    houver lease ativa, reporte como pendência; não diga que o guard foi fechado.
    Em resposta pública, traduza isso para "proteção do vault encerrada" ou
    "pendência na proteção do vault"; não imprima `guard_lease`, lease id/path,
    `run_id`, hash de ponto de restauração ou identificador curto como
    `7da9fcf` salvo se o usuário pedir debugging. Nunca escreva "Ponto de
    restauração `<id>` disponível"; escreva apenas "ponto de restauração
    disponível". Se listar o comando `run-finish`, redija o argumento como
    `--run-id <run_id>`, nunca com o valor literal.
    Em dry-run bloqueado sem efeito canônico executável, reporte e pare: não rode `feedback_report.py` manualmente,
    não rode `feedback_report.py --help` e
    não crie comandos extras de registro. O workflow já registra feedback
    quando apropriado.
    Se `decision.kind=ask_human` ou `human_decision_packet` estiver pendente,
    mostre pergunta, opções, item afetado e `resume_action` do pacote. Antes da resposta final, revise
    todos os `tool_result`: qualquer tool call falha (`status=error`,
    parâmetros inválidos, `invalid_tool_params`, `tracker_update_task` ou
    `read_file` fora do workspace) vira `warnings de execução`, mesmo se retry
    posterior corrigiu; tool call `status=success` nunca vira warning. Se o
    workflow ficou parcial, bloqueado, aguardando agente ou aguardando recurso
    externo, não use a palavra `sucesso` nem a expressão `com sucesso` em
    nenhuma camada do relatório; diga aplicado, atualizado, parcial, guard
    fechado ou ponto de restauração disponível. Subetapa atualizada não é
    workflow completo. Se ocorreu `list_permissions`, reporte como
    probe de permissões. Se o AGY leu log indicado pela própria ferramenta,
    reporte em linguagem humana e não use rótulo técnico literal.
    `update_topic` bem-sucedido é normal e não deve ser listado como atrito.
    `read_file` fora do workspace em YOLO é baixa severidade quando não afeta a
    conclusão, mas ainda deve ser listado.
    Use `reports.public_report.lines` como base da resposta humana e deixe
    comandos, flags e recibos técnicos apenas para debugging explícito. Inclua
    erros de ferramenta de preparação, como `activate_skill` com parâmetro não
    aceito. A successful retry does not erase the earlier tool error.

## Limites

- Não edite YAML/status de raw chats.
- Não publique notas.
- Não use regex manual para links; use `fix-wiki`, grafo/linker ou
  `/mednotes:link`.
- Não escreva manualmente sobre a Wiki; use `fix-wiki` ou
  `apply-style-rewrite`/`apply-note-merge`.
- Não mova pastas manualmente; hierarquia é `taxonomy-migrate` com plano,
  recibo e rollback. No `fix-wiki`, só execute com `--apply-taxonomy`.
