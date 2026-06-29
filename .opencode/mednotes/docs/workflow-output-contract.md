# Workflow Output Contract

## Princípio

Resposta: resumo curto, sem dump de JSON/log salvo pedido.
Resumo: aconteceu, não aconteceu, decisão faltante.

A UX pública é guiada: O usuário não precisa saber subcomandos, flags,
`dry-run`, recibos, schemas, hashes, `uv`, Git ou cache. Traduza para "vou
preparar o ambiente", "vou mostrar prévia", "nada foi alterado", "confirme para
aplicar" e "próxima ação".
Não use relatório público técnico como "Status e Metadados",
"Pacote de Decisão Humana", "Diretrizes de Apply", "Blockers", "Desvios de
Contrato" ou "Artefatos Oficiais", salvo debugging/laboratório.
Formato público padrão:

- uma frase dizendo se algo foi alterado ou se foi só conferência;
- o que foi corrigido ou preparado;
- o que ainda impede concluir o objetivo;
- continuação automática, quando houver;
- a próxima decisão humana real.

Resposta pública padrão deve vir de `reports.public_report.lines` quando esse
campo existir. Comandos literais, flags, schemas, recibos, hashes e paths ficam
no canal agente/debug ou em relatório de laboratório, nunca no texto padrão do
usuário. Decisões humanas usam `decision.reason_code` e
`human_decision_packet` como fonte operacional, mas esses nomes não apareçam como texto cru:
mostre a pergunta, opções fechadas, item afetado e como retomar em linguagem humana.

Em resposta pública, não exponha `run_id`, `guard_lease`, lease id/path,
hash de `restore_point_id`, `snapshot_hash` ou paths de cache salvo debugging.
Traduza para "ponto de restauração criado", "proteção do vault encerrada" ou
"rollback disponível". Não escreva identificador curto de ponto de restauração
como `7da9fcf`; é debug.
Ao listar `run-finish`: `--run-id <run_id>`, nunca o valor literal.

## API obrigatória

Workflows FSM-first usam a FSM como estado operacional. Para esses workflows, a
API raiz canônica é: `progress_view_model`, `state_machine_snapshot`,
`decision`, `receipt`, `reports`, `agent_directive`, `artifacts`,
`diagnostic_context` e `error_context`. `diagnostic_context` e `error_context`
só aparecem quando houver causa acionável, erro, bloqueio, aviso, desvio ou
investigação.

Regra curta de boundary:

Adapter detecta fato. FSM decide política. CLI executa efeito.

Sem WorkflowEffect emitido pela FSM, não existe recovery automático.

blocked_reason, next_action, status e operation_payload não autorizam retry/recovery/apply.

O grupo FSM-first atual/alvo deste contrato inclui `/mednotes:fix-wiki`,
`/mednotes:link`, `/mednotes:process-chats`, `/mednotes:link-related`,
`/flashcards`, `/mednotes:setup` e `/mednotes:history`. Enquanto um desses
fluxos ainda estiver em migração, qualquer campo legado preservado é evidência
de auditoria ou compatibilidade temporária; ele não é root truth e não pode
autorizar continuação, sucesso, bloqueio, mutação ou mensagem final.

`schema` pode existir como identificador técnico de versão do payload; ele não
é estado, fase, blocker nem autorização de continuação. Campos raiz legados como
`phase`, `status`, `blocked_reason`, `next_action`, `required_inputs` e
`human_decision_required` só continuam aceitáveis em comandos não-FSM ou em
adapters transitórios explicitamente marcados como legado. Eles não devem ser
reintroduzidos como contrato público dos workflows FSM-first. Bloqueio por
agente, retry, erro ou contrato quebrado inclui `error_context`; escolha humana
inclui `human_decision_packet`.
Continuação assistida por subagente em workflow FSM-first usa
`agent_directive.control.effects`, diferente de orientação humana em
`decision.next_action` e de decisão humana.

`diagnostic_context` é opcional e só deve aparecer quando houver problema,
bloqueio, falha, aviso, desvio ou investigação. Ele não carrega
`agent_directive`, não é sinônimo de contexto adicional e nunca contém plano
executável para hook/agente.
`diagnostic_context.effect_results` é operador, não público.
Quando `effect_results[]` existir, `payload.operation_payload` é evidência de
auditoria, não verdade de controle. O adapter precisa validar output
operacional privado em payload Pydantic específico antes de decidir status,
recibo, retomada ou próxima ação. Falha nessa camada aparece como
`error_context.root_cause=effect_payload_contract_invalid` e deve ser reportada
como bug de workflow/stack.
Quando houver `workflow_exit_code`, trate-o como resultado do workflow, nunca
como warning.
Workflows FSM-first expõem `agent_directive` como contrato raiz único
FSM -> agente consumível por automação. Consumidores usam
`agent_directive.control` para enforcement e validação, e podem renderizar
`agent_directive.instructions` como contexto para o modelo. Não parseie
relatórios humanos nem preâmbulos em stderr para decidir o estado do workflow.
Para continuação assistida, `agent_directive.instructions` deve reforçar que o
parent usa o efeito `call_specialist_model` ou agente empacotado do harness,
não fabrica prompt curto de subagente, e não cola Markdown bruto de nota em
`invoke_agent`, `invoke_subagent` ou `send_message`. No AGY,
`define_subagent` só é aceitável quando o parent leu o template empacotado e
usou o conteúdo completo com
`packaged_agent_template_contract: medical-notes-workbench.packaged-agent-template.v1`.
Depois que o subagente AGY escreve `temp_output`, o parent chama
`finalize-agy-specialist-task` com transcript/task log oficial, e `--runtime-log`
quando houver janela AGY settings switch, para gerar o
`specialist-task-run-receipt.v1`; não cria, copia ou edita esse recibo à mão.
No OpenCode, o parent chama a tool `task` somente no harness OpenCode, com
prompt igual ao item tipado emitido pela FSM e sem Markdown bruto/chat bruto no
prompt. Para `fix-wiki`/`style_rewrite`, o objeto JSON raiz deve conter somente
`current_batch_items`; campos como `target_path`, `rewrite_prompt`,
`temp_output` e `subagent_output_contract` aparecem apenas dentro do item.
Para `process-chats`/`architect`, o item vem do plano de architect oficial e
carrega `work_id`, `raw_file`, `temp_output`, `coverage_path`, `taxonomy`,
`write_policy` e `expected_output_schema`. Flash pode orquestrar, mas a autoria
especialista não pode cair para Flash/Lite/Nano. Depois da task, o parent chama
o finalizer da fase executada: `finalize-opencode-specialist-task` para
`style_rewrite` e `finalize-opencode-architect-task` para `architect`. O modelo
efetivo vem do metadata OpenCode capturado pelo hook nativo por `work_id`, não
de texto manual nem de JSON escrito pelo agente. `--task-metadata` é override
técnico para diagnóstico, não a rota normal do workflow. O parent não faz probe
de `hook-state/opencode-task-metadata` com `ls`, `test`, `stat`, `cat`, `find`,
`grep` ou equivalente antes do finalizer; o próprio finalizer valida a metadata
capturada e bloqueia se ela estiver ausente ou inválida.
No Gemini CLI, `style_rewrite` consome o efeito `call_specialist_model` com
um único `current_batch_items[]` e o especialista empacotado. O agente não
fabrica prompt curto de subagente em nenhuma rota.
O contrato operacional deve passar exatamente um item completo de
`agent_directive.control.effects[].payload.current_batch_items[]` por chamada,
incluindo `work_id`, `target_path`, `target_hash_before`, `rewrite_prompt`,
`temp_output`, `specialist_task_run_receipt_path` e
`subagent_output_contract`. Prompt
artesanal que resume, reescreve ou omite campos do work item é bug de contrato.
Quando esse item já existe, `glob`, `grep`, `list_dir` ou busca equivalente no
parent para redescobrir alvo, rota ou finalizer é desvio de contrato.
Se o runtime não expuser o template/agente ou modelo esperado, reporte o
bloqueio em vez de contornar com conteúdo copiado.
Quando `MEDNOTES_AGENT_PREAMBLE=stderr` estiver habilitado, a CLI pode
renderizar um preambulo agent-facing antes do JSON em stderr. Esse preambulo e
apenas uma renderizacao de `agent_directive.instructions`:
ele nao muda schema, status, exit code, retomada nem autorizacao. Stdout segue
JSON puro. Nao use como UX publica padrao. Em workflows longos, ele reforça:
aguarde JSON final.
Em workflow FSM-first, `decision.kind=ask_human` sem
`human_decision_packet` é bug. Sem opções fechadas, não fabrique decisão humana:
retorne uma `decision` operacional com `reason_code`/`next_action` ou um
`WorkflowEffect` retomável.

`ask_human` é último recurso. Antes, use `auto_fix`, `auto_defer` ou
`auto_plan` quando seguro. Decisão humana sem contexto, opções fechadas e prova
de insegurança automática é bug do workflow.

`blocked`/`failed` sem `decision.next_action`, `resume_action` ou efeito de
recuperação é bug do workflow. A fonte nativa do workflow emite a ação; a
camada comum só converte para `contract_gap.missing_next_action`, preservando o
motivo original em `diagnostic_context.contract_gap`.
Compatibilidade de auditoria: `blocked`/`failed` sem `next_action` é bug do
workflow quando o payload ainda estiver em migração; normalize para
`decision.next_action`, `resume_action` ou efeito retomável. Quando houver
múltiplas causas, preserve `blocking_reasons[]` como evidência diagnóstica, mas
não use esse array como estado paralelo.
Frase legada preservada para auditoria de corpus: `next_action` orienta apenas
normalização para `decision.next_action`; não autoriza execução automática.
O contrato canônico confirma que não há comando automático legado a executar.
`blocked`/`failed` sem `next_action` é bug do workflow. Campos diagnósticos como
`primary_human_decision_kind` ajudam a explicar a causa, mas a escolha humana
canônica continua em `decision` e `human_decision_packet`.

### Layered readiness

For public workflow reports, separate readiness by layer before summarizing:
Python environment, index, vault protection, linker, Related Notes, specialist capacity.
Do not collapse a green environment layer into workflow success. A report can say
"ambiente pronto" only for that layer; if linker, vocabulary curation, Related
Notes export, or specialist capacity is blocked, the workflow status remains
blocked or waiting.

Public reports must expose one current workflow status, one current blocker and
one next action. Additional layers may appear as short supporting bullets. If
the payload contains `error_context.root_cause`, `decision.reason_code`,
typed Related Notes evidence derived from the FSM, or specialist quota data,
use those fields before free-form narrative. Do not read legacy
`related_notes_sync.status` as root truth; if it still exists during migration,
it must be normalized into a typed FSM field or kept as opaque audit evidence.

Repeated `uv run` package repair output such as missing `RECORD`, repeated
uninstall/install, or Windows `Acesso negado` is diagnostic evidence. Report it
as environment warning or environment blocker; never use a later JSON success to
erase the warning.

### Installed extension bundle safety

Agents must not patch the installed extension bundle as a repair path. Forbidden
targets include `~/.gemini/extensions/medical-notes-workbench`,
`~/.gemini/config/plugins/medical-notes-workbench`,
`C:\\Users\\leo\\.gemini\\extensions\\medical-notes-workbench`, and
`C:\\Users\\leo\\.gemini\\config\\plugins\\medical-notes-workbench`.

Patch canonical source under bundle/ in the repository, then rebuild,
publish, reinstall, or sync through the official distribution path. If a public
workflow discovers an installed-bundle code issue, report
`installed_extension_runtime_edit_forbidden` with the canonical source file and
stop before editing the installed copy.

Artefato sem `agent_directive` canônico não autoriza execução. Normalize na
borda para o contrato FSM-first ou bloqueie como contrato inválido; não converta
campos textuais em comando executável.
Em workflow FSM-first, continue automaticamente só com JSON fresco contendo
`agent_directive.control.status=waiting_agent`,
`agent_directive.control.capabilities.continue=true` e efeito executável em
`agent_directive.control.effects[]`, sem decisão humana e com pedido original
permitindo continuidade. Sem efeito executável, em prévia, decisão humana ou
espera externa: reporte e pare. Quando houver efeito executável, a FSM deve usar
`progress_view_model.status=waiting_agent`,
`state_machine_snapshot.current_category=waiting_agent` e
`progress_view_model.can_continue_now=true`; `blocked` ou
`can_continue_now=false` com efeito executável é bug.
`call_specialist_model`: ausência de runner Python oficial não é cota cheia.
Se o plano estiver executável, use `waiting_agent`, `can_continue_now=true` e
`agent_directive.control.effects[]` para o harness atual chamar o
especialista empacotado. Só use `waiting_external` depois de tentativa real do
runtime/subagente falhar por quota, capacidade ou modelo indisponível.
Quando `call_specialist_model` estiver pronto, o agente deve continuar pelo
harness atual. Gemini CLI usa o especialista empacotado com o item tipado do
efeito; AGY usa o template empacotado e subagente do próprio AGY, seguido de
`finalize-agy-specialist-task`; OpenCode usa `task` somente se o usuário já
estiver em OpenCode, seguido do finalizer OpenCode específico da fase sem
metadata manual. O hook OpenCode materializa a evidência oficial da task.
Nenhum harness deve exigir instalação de outro para concluir o workflow. Em
`style_rewrite`, a evidência normalizada entra em
`medical-notes-workbench.specialist-task-run-receipt.v1`; em `architect`, o
finalizer emite recibo local de task e o próximo passo serial de `stage-note`,
sem substituir a FSM de `process-chats` como fonte de verdade.
Para `fix-wiki`/`style_rewrite`, `current_batch_items` é o lote humano de
progresso, não permissão para disparar todos os especialistas ao mesmo tempo.
No Gemini CLI, invoque o especialista empacotado para um item por vez usando o
JSON de `agent_directive.control.effects[].payload.current_batch_items[]`,
aguarde o `specialist-task-run-receipt.v1` oficial do item anterior e só então
avance. O agente não deve adicionar `sleep`, `&&`, `;` ou parâmetro inventado
como `wait_for_previous`. Não gere prompt manual substituto para o
especialista. Se o recibo não aparecer, pare e reporte o bloqueio; não lance
outro especialista nem fabrique recibo.
No AGY, leia `agents/med-knowledge-architect.md`, chame `define_subagent` com
o template completo, invoque exatamente um item tipado por vez com `Prompt`
igual ao JSON do `current_batch_items[]` e aguarde o `temp_output`. Em seguida execute
`finalize-agy-specialist-task --plan <plan> --work-id <work_id> --transcript
<agy-transcript-or-task-log> [--runtime-log <agy-cli.log>] --json`. Se o
transcript/runtime log não trouxer modelo observado suficiente, bloqueie
`agy_specialist_model_evidence_missing`; se o runtime log mostrar modelo
diferente do solicitado, bloqueie `agy_specialist_model_evidence_mismatch`.
Não aceite alegação manual de modelo.
Se o subprocesso especialista expirar depois de escrever `temp_output`, o
runner oficial ainda pode finalizar a fronteira de recibo desde que consiga
validar o Markdown, extrair metadado de modelo do transcript e assinar
`specialist-task-run-receipt.v1`. Nesse caso `status=completed` do runner é
entrada tipada para a FSM emitir o próximo `WorkflowEffect`; ele não autoriza
execução direta fora de `agent_directive.control.effects[]`. O agente não deve
tratar o timeout bruto como falha quando o payload oficial já voltou concluído.
O payload de `style_rewrite` recebido em `agent_directive.control.effects[]`
deve estar fresco contra a versão final da nota naquele payload. Se
o runtime especialista retornar
`style_rewrite_stale_target_hash`, não trate como falha clínica nem fabrique
output: replaneje somente o lote de reescrita pela rota oficial e retome o
mesmo workflow. Se o runner retornar `specialist_model_quota_exhausted`, reporte
como espera externa por capacidade do modelo especialista; não chame Flash, não
use `invoke_agent` nativo e não marque o workflow como concluído.
Quando a invocação especialista retornar `status=completed`, entregue o recibo
validado à FSM e continue pelo `WorkflowEffect` de apply que ela projetar em
`agent_directive.control.effects[]`. `next_apply_step.arguments`, quando existir
em payload de runner durante a migração, é apenas a origem tipada desse efeito,
não uma segunda API executável. Não leia manifest/plan, não rode
`fix-wiki --apply`, não chame `plan-subagents` e não lance outro especialista
antes desse apply. O recibo já foi validado pelo runner oficial; probes
intermediários são atrito de UX e podem deixar a fila stale.
Com `MEDNOTES_AGENT_STDOUT=compact`, `apply-specialist-style-rewrite` emite
`medical-notes-workbench.style-rewrite-atomic-apply-agent-stdout.v1`, não o
payload completo de linker. Esse stdout traz `agent_directive` no root para o
hook atualizar a FSM. Se houver efeito `call_specialist_model`, continue por
`agent_directive.control.effects[].payload.current_batch_items` sem reler
plano/manifest e sem validar relatório final. Se o lote acabou, reporte o
checkpoint humano do lote com qualidade da nota, preservação de
YAML/proveniência/links, estado honesto do grafo/linker e itens restantes; só
então execute o efeito `run_subworkflow` em `agent_directive.control.effects[]`
para planejar a próxima leva. O `plan-output-receipt.v1` do próximo lote carrega
`agent_directive` com efeito especialista tipado; o hook OpenCode
reinjeta esse pacote após compactação de contexto, e o agente não deve
reconstruí-lo por histórico, arquivos privados ou prompts anteriores. Não rode
`fix-wiki --apply` no meio da fila.
Em workflow com guard, `run-finish` é a última operação: não finalize antes de
executar ou bloquear a continuação de um efeito pronto.
Com `decision.kind=ask_human`, `decision.next_action` descreve decisão
pendente, não `plan-subagents`, `apply-*`, `run-linker`, `fix-wiki --apply` ou
efeito executável. Payload técnico não-FSM com flag de decisão humana deve ser
normalizado para `decision`/`human_decision_packet` antes de dirigir qualquer
fluxo.
Detalhe `human_decision_packet`: pergunta, opções fechadas, itens/evidência
afetados, automações rejeitadas e retomada. Se a decisão humana não explicar
por que `auto_fix`, `auto_defer` ou `auto_plan` eram inseguros, reporte possível
bug de UX.

### `/mednotes:fix-wiki`

Output canônico: `medical-notes-workbench.fix-wiki-fsm-result.v1`. Reporte
`progress_view_model.status`, `state_machine_snapshot.current_category`,
`decision.reason_code`, `decision.next_action` e `human_decision_packet` quando
existirem. Nunca transforme tool success em sucesso do workflow nem derive
comando de texto livre.
Use `agent_directive.control` como cartão operacional do agente. Se
`control.status=waiting_agent`,
`control.capabilities.continue=true` e `control.effects` contém
`call_specialist_model`, o próximo passo é executar os efeitos oficiais
indicados pela FSM antes do relatório final. Se `control.status` for `blocked`,
`waiting_external`, `waiting_human` ou `failed`, use `control.blockers`,
`control.resume` e `summary` para reportar o bloqueio sem inventar comando
alternativo.

Se `progress_view_model.status` for `waiting_external`, `waiting_human` ou
`blocked`, reporte progresso parcial, decisão humana ou bloqueio literal e
pare. Em mutação com Git disponível, confira
`version_control_mutation_summary`; reporte divergência de contadores,
deleções ou arquivos inesperados.

Todo relatório final de `/mednotes:fix-wiki` deve responder o objetivo
primário antes de discutir detalhes internos:

- fixou a Wiki ou não;
- o que foi realmente alterado no vault;
- se o grafo terminou melhor, limpo ou ainda bloqueado;
- se `Notas Relacionadas` foi atualizado, ficou pendente ou aguarda cota.

Quando houver reescrita de nota, o relatório também deve incluir auditoria de
conteúdo do lote aplicado: se cada nota realmente resolveu a pendência indicada,
se preservou YAML/proveniência/links e se a qualidade médica/didática ficou boa
para estudo. Nota formalmente válida mas ruim deve ser reportada como bug de
UX/conteúdo.

O payload de validação pós-run inclui
`fix-wiki-primary-objective-summary`; use-o como fonte tipada para esse resumo.
Se `Related Notes` estiver em `waiting_external` por quota/embeddings, omitir a
cota é bug de relatório.

Quando o validador emitir `workflow-public-report-view-model`, use esse objeto
como fonte da resposta pública: `objective_answer` responde se fixou,
`mutation_summary` diz o que mudou, `remaining_work_summary` diz o que falta,
`next_step_summary` diz como continuar e `user_attention_required` decide se o
usuário realmente precisa agir. Não substitua esse resumo por bloco técnico
extraído de JSON bruto.

### `/mednotes:process-chats`

Output canônico: `medical-notes-workbench.process-chats-fsm-result.v1`.
Reporte `progress_view_model.status`, `progress_view_model.state`,
`state_machine_snapshot.current_category`, `decision.reason_code`, `receipt`,
`reports`, `agent_directive`, `artifacts`, `diagnostic_context` e
`error_context`. Nunca leia `status`, `phase`, `next_action`,
`dry_run_receipt`, `publish_receipt`, `linker_applied` ou `linker_*_path` como
campos raiz do resultado público.

O resultado privado de `publish-batch` consumido por essa FSM é tipado por
`medical-notes-workbench.process-chats-publish-operation-result.dev.v1`.
Contadores, status, recibo, dry-run receipt, taxonomia canonicalizada, raw
updates e linker run não devem ser lidos de dicionário cru para decidir UX ou
exit code.

Todo relatório final de `/mednotes:process-chats` deve responder o objetivo
primário antes de discutir detalhes internos:

- publicou notas ou só preparou prévia;
- quais raw chats foram cobertos/processados;
- o que foi realmente escrito na Wiki;
- se coverage/manifest/staged notes batem;
- se linker/grafo rodou, ficou limpo ou ficou pendente.

O payload de validação pós-run inclui
`process-chats-primary-objective-summary`; use-o como fonte tipada para esse
resumo. `ready_to_publish` não é conclusão; `completed_with_link_blockers`
significa publicação feita com linker/grafo ainda pendente.

Quando o validador emitir `workflow-public-report-view-model`, use esse objeto
como fonte da resposta pública: ele já traduz publicação, escrita na Wiki,
pendências de coverage/linker e necessidade real de atenção humana. O agente não
deve pedir intervenção só porque uma etapa interna está pendente se a FSM ainda
tem continuação segura.

Ausência de efeito executável com rota de retomada preenchida não é fim do
relatório nem "nada a dizer". Em mutação bloqueada por decisão humana, o Bloco
2 da resposta declara: "Nenhuma próxima ação automática agora; após decisão,
retomar pelo workflow oficial." A rota de retomada orienta o pós-decisão, não
autoriza execução paralela; nunca transforme orientação de retomada em execução
automática sem resposta humana registrada.

### `/mednotes:link`

Output canônico: `medical-notes-workbench.link-fsm-result.v1`. Reporte
`progress_view_model.status`, `state_machine_snapshot.current_category`,
`decision`, `human_decision_packet` e `receipt`; payload bruto de auditoria
do linker, quando necessário para diagnóstico, fica apenas em payload opaco
como `diagnostic_context.link_audit_payload`, nunca como fonte de decisão. Em
`waiting_external`, reporte progresso parcial e não declare sucesso. Em
`blocked`, use
`decision.reason_code`/`decision.next_action`; apply parcial, tool success ou
recibo escrito não concluem o workflow.

## Sequência

1. Separe status da ferramenta, exit code do processo e status semântico do
   workflow. `tool status=success` só quer dizer que a chamada retornou; se o
   output FSM trouxer `progress_view_model.status=blocked`,
   `progress_view_model.status=failed` ou `state_machine_snapshot.current_category`
   bloqueante, reporte o bloqueio. Em comandos legados transitórios, trate JSON
   `status=blocked`/`failed` como entrada de migração, não como contrato
   canônico.
2. Em workflow FSM-first, identifique `progress_view_model.status`,
   `progress_view_model.state`, `state_machine_snapshot.current_category`,
   `decision.reason_code` e `decision.next_action`; trate a próxima ação como
   pendente, nunca como já executada.
3. Em relatório técnico/debugging de fluxo legado, reflita `blocked_reason`,
   `required_inputs` e `human_decision_required` somente como compatibilidade
   transitória. Em FSM-first, use `decision`, `human_decision_packet` e
   `error_context`. Decisão humana nunca é concatenada ao reason code; se houver
   lista de razões, mostre-a como evidência, mantendo a decisão oficial da FSM
   como rótulo primário.
4. Em workflow FSM-first, continue só com
   `agent_directive.control.effects[]` executável; senão pergunte/confirme ou
   mostre `human_decision_packet`.

Na resposta pública final, nunca anexe bloco diagnóstico, JSON, XML, YAML ou
campos técnicos como `blocked_reason`, `receipt`, `schema`, `hash`,
`progress_view_model` ou caminhos locais. Esses detalhes pertencem ao log,
relatório técnico ou JSON de validação. A resposta do usuário deve ser uma
síntese humana do que foi corrigido, do que ficou pendente e de como retomar.

Em experimentos controlados, a resposta final também deve citar a métrica de
happy path quando o relatório for para o mantenedor: total de runs, runs happy,
prevalência em porcentagem e principais categorias de desvio. Na UX pública
normal, essa métrica não aparece; ela é instrumento de qualidade do produto.

Em diagnóstico read-only com `--json`, a resposta pública continua curta:
nenhuma alteração feita, principais achados e próxima ação. Os 5 blocos de
`Diagnóstico Read-Only` são debugging, não resposta padrão.

Em workflow mutante, resposta prioriza o objetivo. O
`Esqueleto Da Resposta (Mutação)` é formato de debugging; não use como default.
Em debug, o `Exit Code:` principal entra no Bloco 1, mesmo
quando é 3 ou outro ≠ 0; tool errors auxiliares entram no Bloco 4.

Antes da resposta final, varra `tool_result`; em geral só `status=error` vira
`warnings de execução`; nunca promova `status=success` a warning fora da exceção
estreita abaixo e não invente warning de ferramenta que não foi chamada. Exceção estreita:
em `/mednotes:fix-wiki` linear, `tracker_create_task`/`tracker_update_task`/`tracker_visualize`
bem-sucedidos também são atrito de rota e devem aparecer como baixo impacto. Em mutação, warnings vão ao Bloco 4.
`update_topic` segue a mesma política de redação. Em saída pública, não inclua `run_id`,
hashes, comandos, paths de script ou flags em título,
summary ou strategic intent. Liste
evidência:

- comandos falhos: comando, exit code, fase, resumo;
- `Exit Code: 3` do CLI com JSON `status=blocked` é o processo sinalizando
  blocker de validação; reporte no status do workflow (Bloco 1 em mutação),
  nunca como "warning de execução" auxiliar ou Bloco 4;
- tool calls `status=error`, incluindo `invalid_tool_params`,
  `tracker_update_task`, `update_topic` e `read_file` fora do workspace, com
  resumo. `read_file` fora do workspace em YOLO pode ser baixa severidade, mas
  ainda entra em `warnings de execução`;
- no `/mednotes:fix-wiki` linear, uso de `tracker_create_task`,
  `tracker_update_task` ou `tracker_visualize` é atrito de UX mesmo com
  `status=success`; `update_topic` bem-sucedido continua normal;
- erro inicial continua sendo achado mesmo se comando posterior recupera;
- `;`/`&&` mascaram exit code; prefira uma ferramenta por comando auditável;
- em pedido explícito de `fix-wiki --dry-run`, não rode bateria diagnóstica antes
  do `fix-wiki --dry-run --json`, salvo se JSON fresco do próprio `fix-wiki`
  pedir;
- em pedido explícito de `/mednotes:fix-wiki --apply`, não use `--dry-run`;
  use run-start/run-finish com `run_id` literal e não vazio; não normalize nem
  remova separadores do `run_id`; `--run-id ""` é blocker; payload bloqueado é
  apply bloqueado;
- não execute `cli.py` diretamente; `Permission denied`/`Exit Code: 126`
  continua falha real depois de retry;
- comando oficial usado: escreva o executado, não o ideal;
- em saída pública normal, não liste comandos internos, `node`, paths de script,
  `--json`, `run-start` ou `run-finish`; traduza para "preparei a proteção",
  "executei o reparo" e "encerrei a proteção". Comandos literais ficam só para
  debugging/laboratório;
- prompts suspeitos; scripts suspeitos: path + trecho;
- sem dono preventivo: "Nenhum prompt ou script encarregado de prevenir este comportamento foi identificado".

Campos `null` inesperados são falha de schema. Rotule artefato stale. Se output
fresco e artefato salvo divergem, reporte a divergência e use o fresco. Toda conclusão precisa ter o mesmo escopo da evidência: não diga "total", "integral" ou "rigorosamente respeitado" quando
auditou só parte, houve path fora do escopo ou faltou artefato.

Backups `.bak` novos não fazem parte dos workflows protegidos por vault guard.
`.bak`/`.old` legados são pendência de higiene explícita, não mutação
automática de `fix-wiki`; `final_validation.hygiene.bak_or_rewrite` é apenas
sinal técnico de legado pendente. Não derive contagem de backups de
`changed_count`, `written_count` ou `total_changed_count`.

## Matriz De Estado

- ✅ `progress_view_model.status=completed`/`published`: só diga aplicado/concluído com mutação
  finalizada, validação e fechamento.
- 👀 `progress_view_model.status=preview_ready`/`ready_to_publish`: diga que nada foi escrito e peça a
  decisão de aplicar, publicar, restaurar, gravar no Anki ou descartar.
- ⚠️ `completed_with_link_blockers`/warnings: diga o que foi feito e o que
  ficou pendente; nunca sucesso simples.
- ⛔ `progress_view_model.status=blocked`/falha: traduza
  `decision.reason_code`, mostre `decision.next_action`,
  `human_decision_packet` ou `resume_action`, e pare.
- 🔁 `continuation_ready`: não é sucesso final nem bloqueio terminal. Continue
  agora por `agent_directive.control.effects[]`; `validation_only=true`
  é só conferência interna e não conclui o workflow. Não volte para
  `fix-wiki --dry-run`.
- 🧭 Fase limitada (`triagem`, `arquitetura`, `publish dry-run`): execute só a
  fase confirmada e feche com novo resumo.

Sem `decision.next_action`, `resume_action` ou efeito retomável em bloqueio,
pare como `contract_gap.missing_next_action` com `error_context`. Não invente
script, `@generalist`, shell, workaround ou edição manual quando há rota
oficial.

## Diagnóstico Read-Only

Em bateria `--json` sem mutação, use template fechado. Para FSM-first, os
campos vêm de `progress_view_model`, `state_machine_snapshot`, `decision`,
`receipt` e `agent_directive`. Para comando legado em migração, os campos
top-level podem ser citados apenas como evidência de transição.

- Bloco A: comando exato, tool status, `Exit Code:`, status semântico do
  workflow, reason/action oficiais da FSM e freshness.
- Bloco B: comandos falhos/bloqueados e tool calls `status=error`; varra
  `Blocked`, `Command injection detected`, parser error,
  `invalid_tool_params`, `denied`, `not found`. Se vazio:
  "Nenhum comando bloqueado observado após varredura dos tool outputs".
- Bloco C: artefatos stale; só chame "confirmado" se comando fresco reemitiu o
  campo. Se output fresco e artefato salvo divergem, use o fresco.
- Bloco D: `source`, `freshness`, action oficial literal,
  `recommended_action`, `literal_match`, `expected_mutation`. Copie
  `decision.next_action`/`resume_action` byte a byte; dry-run =
  `expected_mutation=nenhuma`.
- Bloco E: escopo quantitativo. Para contagens/listas >1, só use "todos",
  "apenas" ou "estritamente" após distribuição por path/código no output
  completo (`tool-output-files/<id>.txt` ou equivalente).

Mapeamento dos contratos C1–C8 aos blocos: A trava C1/C6/C7; B trava
C3/C8/C11; C trava C2; D trava C1/C5 e reforça C7; E trava C4. Definições
em `docs/agent-prompt-hardening.md` §Contratos.

## Esqueleto Da Resposta (Mutação)

Em workflow mutante (`fix-wiki --apply`, `publish-batch --apply`,
`apply-canonical-merge`, `apply-curator-batch`, `apply-note-merge`,
`run-linker --apply`, `pdf-library insert --apply`, restauração aplicada), use
quatro blocos fechados, nesta ordem. Cada bloco tem fonte JSON declarada; falta
de campo vira sentinela explícita, não bloco omitido.

- **Bloco 1 — Resultado Do Workflow.** Status semântico fresco do comando
  principal. Em FSM-first, campos obrigatórios: `progress_view_model.status`,
  `progress_view_model.state`, `state_machine_snapshot.current_category`,
  `decision.reason_code`, `decision.next_action` quando houver, `receipt` e o
  `Exit Code:` do comando principal. Em comando legado transitório, `status`,
  `phase` e `blocked_reason` podem ser citados apenas como evidência de
  migração, nunca como fonte canônica. `Exit Code:` ≠ 0 com estado bloqueado ou
  falho é sinal central do workflow e fica aqui, nunca em Avisos Auxiliares.
  Inclua contagens-chave do payload (`changed_count`, `written_count`,
  `archived_count` etc.) quando publicamente úteis.
- **Bloco 2 — Decisão Humana.** Aparece somente quando
  `decision.kind=ask_human` ou `human_decision_packet` existir. Mostre
  `human_decision_packet.options`, item/escopo afetado e
  `resume_action`/`resume_command`. Quando não houver próxima ação automática e
  a rota de retomada estiver preenchida, use a frase
  canônica: "Nenhuma próxima ação automática agora; após decisão, retomar
  pelo workflow oficial." Não invente execução automática a partir de
  `resume_command`; não execute `resume_action` sem resposta humana válida.
  Em mostragem de comando, escreva `--run-id <run_id>` em vez do valor
  literal.
- **Bloco 3 — Segurança Do Vault.** Frases públicas: "ponto de restauração
  criado", "proteção do vault encerrada", "proteção do vault pendente",
  "backup online pendente" ou "alteração bloqueada por segurança". Derive de
  `version_control_safety`, `guard_lease.status` e `sync_status`. Não
  imprima `run_id` literal, `guard_lease`, lease id/path, hash curto de
  ponto de restauração ou `vault_dir`. Confirme encerramento somente com
  `guard_lease.status=closed` ou `guard-status.active_count=0` no JSON
  fresco; senão, reporte pendência.
- **Bloco 4 — Avisos Auxiliares.** Escopo restrito: tool calls `status=error`
  (incluindo `invalid_tool_params`, `tracker_update_task`, `update_topic`,
  `read_file` fora do workspace), retries com erro inicial preservado, hook
  errors, parâmetro de tool não documentado (`wait_for_previous` etc.),
  comandos preparatórios bloqueados pelo guard e, somente em `/mednotes:fix-wiki`
  linear, uso de tracker (`tracker_create_task`/`tracker_update_task`/
  `tracker_visualize`) mesmo quando bem-sucedido. Proibido aqui: `Exit Code:`
  do comando principal, reason/action oficiais do workflow ou root fields
  legados equivalentes. Se vazio, escreva a sentinela literal: "Nenhum aviso auxiliar
  observado após varredura dos tool outputs". Bloco vazio sem sentinela é
  bug de relatório.

Ordem fixa: Bloco 1 → Bloco 2 (se aplicável) → Bloco 3 → Bloco 4. Não
embaralhe; não pule sentinela; não promova auxiliar a resultado nem rebaixe
resultado a auxiliar. Contratos C19 (Exit Code central) e C20 (resume após
decisão) em `docs/agent-prompt-hardening.md` §Contratos.

## Preview-First

Se nada foi escrito, diga. Se falta confirmação, termine com decisão concreta:
aplicar com backup, migrar taxonomia, gravar no Anki ou descartar plano.

## Formato

Bullets curtos, um emoji por bullet:

- status com emoji;
- contagens: vistos, criados/alterados/planejados, pulados, falhas;
- até 3 caminhos relevantes, ou 3 exemplos + total restante;
- blockers/warnings: taxonomia, grafo, Anki, Gemini, cota/API, IO;
- próxima ação: pergunta nativa, confirmação, comando seguro ou decisão humana.

Fase limitada: diga em linguagem humana, execute só a fase confirmada e pare.
Em bloqueio FSM-first, mostre `decision.reason_code`,
`decision.next_action`, `human_decision_packet` ou `resume_action`. Em payload
legado transitório, normalize `blocked_reason`/`next_action` antes de usar.
Sem ação oficial, bloqueie como `contract_gap.missing_next_action`; nada de
script, `@generalist`, shell ou edição manual.

Um emoji por bullet.

## Registro de entrega

Relatório versionado de agente não é relatório de commit. Use em camadas:
Em uma frase; O que mudou para você; Como conferir; Pontos de atenção; Próxima ação.
Sem risco: meia tela. Com blocker/mutação parcial: detalhe.
`Detalhes técnicos` em `<details>` só se úteis.

## Perguntas Ao Usuário

Para confirmação, decisão humana ou opções fechadas, use a ferramenta nativa de pergunta/seleção.

- 2 a 4 opções; a opção recomendada vem primeiro, com impacto curto.
- Em mutação/publicação/restauração/Anki/migração, inclua cancelar, revisar ou
  manter preview. Não execute a opção recomendada sem confirmação.
- Transforme `human_decision_packet.options[]`, `decision.next_action`,
  `resume_action` e, em payload legado transitório já normalizado,
  `required_inputs` em opções fechadas sempre que possível.
- Se a ferramenta nativa não estiver disponível, mostre as mesmas opções
  numeradas e peça escolha explícita.
- Com `decision.kind=ask_human`, apresente `human_decision_packet.options[]`
  por esse fluxo e continue só após resposta humana explícita.

## Anti-padrões

Nunca:

- despeje JSON/log bruto por padrão;
- imprima `config.toml`, `.env`, defaults de telemetria, feedback records, hook
  events, tokens, `auth_token`, senhas ou chaves; use campos redigidos dos
  comandos oficiais;
- diga "concluído" para preview, dry-run, blocker parcial ou run sem
  fechamento;
- diga "isolamento total", "sucesso integral" ou conclusão ampla quando a
  própria evidência é parcial, contém exceção ou não foi auditada;
- diga que usou `uv run python ...` ou outro comando oficial quando os comandos
  realmente executados foram `python`, `$UV_PROJECT_ENVIRONMENT/bin/python` ou
  outro caminho;
- peça Git ao usuário;
- transforme campos de ação em comando automático; em mutação/Anki/publicação/taxonomia,
  confirme. Sem efeito executável canônico = reportar e parar;
- misture `blocked_reason` top-level com `human_decision_packet.decisions[].kind`
  ou invente contagem/local de backup sem campo explícito;
- invente workaround, script manual ou edição direta com rota oficial
  disponível.

## Segurança Do Vault

Versionamento interno: diga `ponto de restauração`, `histórico`,
`preview de restauração`, `proteção local pronta`, `backup online pendente`,
`backup online atualizado`, `login GitHub necessário`,
`repositório privado proposto` ou `alteração bloqueada por segurança`; não use
commit, branch, merge, rebase, worktree ou SHA na resposta padrão.

Em restauração, nada muda antes de confirmação. Em `/mednotes:setup`, conduza
passo a passo; preserve histórico e mudanças locais; pare em login GitHub ou
criação de repositório. `backup_online=false`,
`sync_status=skipped_no_remote` e
`local_checkpoints_pending` = ponto local salvo com backup online pendente.
Alteração bloqueada por segurança exige workflow correto, uma vez por lote.
Fluxos paralelos podem bloquear com `blocked_online_backup_required`.

Com `git_identity_github_attribution`, `git_author` é autoria no commit.
Prometa avatar/link/filtro só com `github_profile_link_expected=true`; se falso,
diga que a autoria foi preservada e que o GitHub clicável exige setup nativo com
email de conta/bot real.

Em `/mednotes:history`, resuma `restore_point_id`, `can_restore`,
`restore_preview_path` e `affected_files`; sempre confirme antes de aplicar.

Workflow declara `no_resource_mutation=true` ou resume:
`Version Control Safety`: guard, `run-start`, `run-finish`, pontos,
`sync_status`, `backup_online`, `direct_mutation_forbidden` e
`mutation_without_guard`. Não diga que versionou sem fechamento. Em workflow
mutante, só diga que o guard foi fechado quando o JSON fresco trouxer
`guard_lease.status=closed` ou `guard-status.active_count=0`; com
`blocked_reason=guard_lease_mismatch`, `guard_lease.status=missing`, lease
ativa ou ausência dessa evidência, reporte pendência.
Na resposta pública, diga "proteção do vault encerrada" ou "pendência na
proteção do vault"; não imprima `guard_lease`, lease id/path, `run_id` ou hash
de ponto de restauração salvo se a pergunta for de debugging. Frases como
"Ponto de restauração `7da9fcf` disponível" ainda violam este contrato; use
apenas "ponto de restauração disponível". Ao listar comandos executados, mostre
`run-finish --run-id <run_id>` em vez do ID real.

## Feedback

Feedback local fica em `~/.mednotes/feedback/runs/`;
falha é fail-open e arquivos antigos são podados automaticamente. Não rode
registro manual se o workflow já registrou. Se o agente piorou o run, registre
`agent_events` redigidos: retry/loop, fase
errada, `next_action` ignorado, drift, mutação inesperada, intervenção manual,
workflow bloqueado ou comando falho. Nunca envie conteúdo clínico, Markdown
bruto, HTML, imagens ou logs completos. Telemetria remota é silenciosa/fail-open.

Em experimentos controlados, a resposta final do agente também deve passar por
`validate-agent-run-report` quando houver payload oficial e transcript. Esse
gate compara a resposta final com `progress_view_model`, `receipt`, erros de
tool e caminhos reais; o transcript pode ser JSON/NDJSON puro ou log misto do
Gemini CLI com linhas JSON válidas entre warnings/stack traces. Se ele bloquear,
reporte o bug de relatório do agente em vez de tratar a rodada como confiável.

## Campos Por Workflow

- `enrich` — notas, âncoras, imagens, fontes, puladas por `images_enriched`,
  sem inserção, falhas.
- `create` — tema, destino, sobrescrita evitada, pontos visuais, próximo
  workflow.
- `fix-wiki` — use `progress_view_model`, `state_machine_snapshot`,
  `decision`, `receipt`, `reports`, `artifacts` e
  `agent_directive`, `diagnostic_context` e `error_context`. Use
  `agent_directive.control` para enforcement/validação
  e `agent_directive.instructions` como contexto agent-facing. Detalhes ficam em
  `diagnostic_context.counts`, `change_count_context`, `final_validation` e
  payloads tipados de auditoria. Não use campos root legados
  (`status`, `phase`, `blocked_reason`, `next_action`, `workflow_exit_code`,
  `execution_gate`, planos de orquestração legados, `requested_apply`,
  `effective_apply`, `blocker_resolution`, `final_validation`) como verdade.
  Com `write_errors`, `requires_llm_rewrite_count`, linker, Related Notes,
  `atomicity_split_required` ou `waiting_external`, reporte pendência e não
  conclua.
- `process-chats` — use `progress_view_model`, `state_machine_snapshot`,
  `decision`, `receipt`, `reports`, `agent_directive`, `artifacts`,
  `diagnostic_context` e `error_context`.
  Estados canônicos incluem `ready_to_publish`, `published` e
  `completed_with_link_blockers`; destaque publicação, raws, escrita na Wiki,
  coverage/manifest e linker/grafo.
- `link` — use `progress_view_model`, `state_machine_snapshot`, `decision`,
  `receipt`, `reports`, `artifacts`, `agent_directive`, `diagnostic_context` e
  `error_context`. Enquanto a implementação ainda estiver em migração, dados
  herdados do `run-linker` podem aparecer apenas como `link_audit_payload`
  opaco; `status`, `phase`, `blocked_reason`, `next_action`,
  `related_notes_sync`, `body_term_linker` e `reference_repair` não são verdade
  raiz pública.
- `link-related` — use `progress_view_model`, `state_machine_snapshot`,
  `decision`, `receipt`, `reports`, `agent_directive`, `artifacts`,
  `diagnostic_context` e `error_context`.
  `related-notes-sync` e recovery público emitem
  `medical-notes-workbench.link-related-fsm-result.v1`; `updates`,
  `skipped_edges`, `blocked_reason` e `related_notes_recovery_state` não são
  campos raiz públicos.
- `flashcards` — FSM-first: use `progress_view_model`,
  `state_machine_snapshot`, `decision`, `receipt`, `agent_directive`,
  `reports`, `artifacts`, `diagnostic_context` e `error_context`. Sucesso só
  existe depois de resposta real do Anki com `anki_note_id` e tag Obsidian
  `anki` aplicada.
- `setup` — alvo FSM-first de recuperação de ambiente/configuração. Use
  `progress_view_model`, `state_machine_snapshot`, `decision`, `receipt`,
  `reports`, `agent_directive`, `artifacts`, `diagnostic_context` e
  `error_context`; não transforme blockers de ambiente em texto solto sem
  `resume_action`.
- `history` — alvo FSM-first para restore preview/apply. Use
  `progress_view_model`, `state_machine_snapshot`, `decision`, `receipt`,
  `reports`, `agent_directive`, `artifacts`, `diagnostic_context`,
  `human_decision_packet` e `error_context`; restore aplicado exige
  confirmação humana e ponto de restauração selecionado.

## Dicionário Público

Tradução canônica `internal → user` em `docs/public-vocabulary.md`. Use-a
antes de devolver qualquer resposta visível em workflow público. Termos
internos só aparecem em `<details>` quando o usuário pediu, em
`/mednotes:status` ou em debug explícito.
