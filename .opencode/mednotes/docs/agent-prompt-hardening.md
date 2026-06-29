# Agent Prompt Hardening

Este contrato orienta agentes e subagentes da extensão quando um workflow
falha. A regra principal: diagnosticar com comando oficial, preservar contexto,
usar dry-run/recibo e parar em drift.

## Pacote Obrigatório

Todo agente chamado para investigar ou corrigir erro deve receber:

- `app_version`;
- workflow público e fase;
- comando oficial executado;
- erro exato;
- `blocked_reason`;
- `next_action`;
- paths operacionais permitidos;
- diagnóstico JSON e recibo/dry-run quando houver;
- schema esperado;
- hashes relevantes;
- `agent_metrics` somente quando vier de runtime/harness; não peça ao modelo
  para inventar contadores;
- estado resumido de DB/fila/manifest;
- status da ferramenta, exit code do processo e status semântico do workflow
  quando houver comando executado;
- escopo da evidência auditada, inclusive paths fora do escopo principal ou
  superfícies não examinadas;
- stop rules aplicáveis.

Não incluir conteúdo clínico bruto, raw chats, Markdown de notas, HTML, imagens,
tokens, chaves, `.env` ou logs completos.
Não imprima `config.toml`, `.env`, defaults de telemetria, feedback records ou
hook events. `auth_token`, tokens, senhas e chaves são segredo mesmo em ambiente
de laboratório. Para conferir configuração, prefira o JSON redigido de
`validate` e reporte só paths, booleanos, status e códigos.

## Escada De Decisão

1. Ler workflow, phase, contrato e comando oficial.
2. Rodar diagnóstico oficial antes de mutar.
3. Se houver dry-run, revisar dry-run antes de apply.
4. Aplicar apenas comando oficial com recibo.
5. Revalidar com diagnóstico oficial.
6. Se continuar bloqueado, produzir `diagnostic_context` e parar.
7. Se não houver ferramenta oficial, registrar backlog; não criar script ad hoc.

## Contratos

Cada contrato tem código `C<n>`, definição curta e, quando aplicável,
bloco-alvo do template Diagnóstico Read-Only (Bloco A–E em
`docs/workflow-output-contract.md`). Stop Rules, Pré-vôo e skills citam esses
códigos; suas definições vivem aqui.

A motivação é empírica: modelos rápidos (Flash) e grandes (Pro) exibiram
modos de falha recorrentes em runs contra o vault Wiki_Medicina quando o
contrato era prosa aberta. Cada contrato fixa um modo conhecido. Os códigos
são preservados entre relatórios para rastreabilidade.

### C1–C8: Diagnóstico Read-Only

- **C1 NEXT_ACTION_LITERAL — Fidelidade Literal A `next_action`.** Quando um payload fresco contém
  `next_action`, a resposta final copia a string literal. Caminho absoluto
  permanece absoluto. Reescrita exige `literal_match=não` + justificativa no
  Bloco D. Violação típica: agente recomenda
  `cli.py vocabulary-recover --mode rebuild-db` quando o JSON fresco do
  `vocabulary-status` traz comando absoluto com `--mode rebuild-db --dry-run`
  para DB ausente. Bloco-alvo: D.
- **C2 STALE_NEEDS_FRESH — Escopo De Confirmação De Artefatos Stale.** Artefato stale só vira
  "confirmado" quando o mesmo campo foi reemitido por comando fresco deste
  run. Violação típica: agente diz "todos os artefatos stale foram
  confirmados" depois de rodar apenas `graph-audit` e `vocabulary-status`
  frescos, deixando `run-linker-diagnose` baseline sem equivalente. Bloco-alvo:
  C.
- **C3 TOOL_OK_NOT_WORKFLOW_OK — Falhas De Ferramenta Mesmo Com Tool Status Success.** Se tool output
  contém `Blocked`, `Command injection detected`, `Exit Code:` ≠ 0, erro de
  parser, comando inexistente ou permissão negada, isso entra em "Comandos
  Falhos Ou Bloqueados" mesmo que a tool call apareça como `success` e mesmo
  que um comando posterior tenha funcionado. Violação típica: comparação de
  artefatos stale agrupada num shell com `$(cat ...)` é bloqueada pelo guard,
  e o agente omite o bloqueio no relatório final. Bloco-alvo: B (com
  sentinela literal quando vazio).
- **C4 NO_UNIVERSAL_WITHOUT_DISTRIBUTION — Não Reduzir Muitos Erros A Um Único Exemplo.** Quando
  `error_count > 1`, o agente não pode usar "estritamente", "apenas", "todos",
  "exclusivamente", "somente" sem computar distribuição (por path e por
  código) do output completo. Violação típica: declarar que os 21 erros de
  `validate-wiki` "são estritamente da nota `medicine.md`" quando o arquivo
  completo mostra reports distribuídos por várias notas. Bloco-alvo: E.
- **C5 DRY_RUN_IS_PLAN — Diferenciar Próxima Bateria De Resultado Prometido.** Dry-run produz
  plano/diagnóstico. Nunca pode ser descrito como aplicação, eliminação,
  correção ou limpeza. Violação típica: dizer que `fix-wiki --dry-run`
  "elimina nós/links órfãos". Bloco-alvo: D (`expected_mutation=nenhuma`).
- **C6 ONE_AUDITABLE_COMMAND — Um Comando Auditável Por Chamada.** Cada comando auditável precisa
  de exit code próprio. Ficam proibidos blocos `if`, loops, `$(...)`,
  backticks, múltiplos `jq` sobre artefatos diferentes na mesma chamada e
  mini scripts shell de diagnóstico. Violação típica: agrupar comparação de
  artefatos antigos num único shell com `BASE_DIR=$(cat ...)` e
  `for f in ...; do jq ...; done`. Bloco-alvo: A.
- **C7 DRY_RUN_FIRST_SHELL — Dry-Run Explícito Não Vira Preflight Livre.** Se o usuário pediu
  `fix-wiki --dry-run`, a primeira chamada shell executa o comando oficial
  `fix-wiki --dry-run --json`. `environment-preflight`, `validate-wiki`,
  `taxonomy-status`, `vocabulary-status`, `graph-audit` e
  `run-linker --diagnose` só entram depois se o JSON fresco pedir. Bloco-alvo:
  A + D.
- **C8 TOOL_ERROR_IS_FINDING — Erro De Tool Não-Shell É Achado.** `read_file`, `activate_skill`,
  tracker ou qualquer tool com `status=error` deve aparecer no relatório de
  debugging, inclusive `invalid_tool_params` e path fora do workspace. Bloco B
  não pode usar a sentinela vazia quando houve erro de ferramenta. Bloco-alvo:
  B.

### C9–C11: Shell E Retry

- **C9 NO_SHELL_PROBES — Probes Shell Antes De Dry-Run Explícito.** Pedido explícito de
  `fix-wiki --dry-run` proíbe probes preparatórios (`ls`, `uv --version`,
  teste de venv, descoberta de path). O primeiro shell command auditável é o
  dry-run oficial; se ele falhar por path ou venv, a falha do comando oficial
  vira evidência. Bloco-alvo: A.
- **C10 INHERIT_UV_ENV — `UV_PROJECT_ENVIRONMENT` Sobrescrito.** Não usar
  `export UV_PROJECT_ENVIRONMENT=<...> && uv run ...`; herdar o ambiente
  recebido pelo harness/runtime. Sobrescrita contamina o laboratório e
  mascara isolamento. Operacional; sem bloco-alvo direto.
- **C11 RETRY_DOESNT_ERASE_ERROR — Retry Apaga Erro Anterior.** Quando um comando falha e um retry
  recupera, o erro original continua sendo achado do run. *A successful
  retry does not erase the earlier tool error.* Bloco-alvo: B.

### C12–C18: Orquestração

Estes contratos operam fora da bateria read-only: mutação, avaliação,
subagentes e UX pública. Eles travam os modos de falha mais caros (apply
indevido, schema fabricado, bypass de evaluator).

- **C12 NEXT_ACTION_NOT_AUTHZ — `next_action` Orienta, Não Autoriza.** `next_action` indica o que
  reportar/preparar; só continue quando o payload canônico trouxer
  `agent_directive.control.capabilities.continue=true`, efeito executável e o
  pedido original permitir continuidade. Violação típica: ler `next_action` e
  executar enquanto `decision.kind=ask_human` ainda está pendente.
- **C13 OFFICIAL_ROUTE_ONLY — Rota Oficial Bloqueada Não Autoriza Fallback Paralelo.** Quando o
  comando oficial retornar `blocked`, NÃO usar Obsidian CLI direto, plugin
  export manual, regex de linkagem, `@generalist`, edição de SQLite/JSON ou
  script ad hoc. Reportar `blocked_reason` + `next_action` e parar.
- **C14 NO_PARENT_SCHEMA — Agente Pai Não Emite Schema De Subagente.** Pai NUNCA escreve
  `note-semantic-ingestion.v1`, `triage-note-plan.v2`,
  `atomicity-split-bundle.v1` ou outputs por `work_item` de
  `vocabulary-curator-batch-plan.v1`. Pai orquestra (`plan-subagents`,
  `collect-*`, `eval-*`, `apply-*`); o subagente designado é o único emissor
  do schema correspondente.
- **C15 EVAL_TERMINAL — `needs_review` É Terminal Até Output/Prompt Regenerado.**
  `needs_review` em `eval-curator-batch`, `eval-triager-output` ou outro
  evaluator oficial NÃO vira `approved` por edição manual do JSON de
  avaliação. Editar `curator-prompt-eval.json` para destravar apply é
  violação grave. Caminho oficial: regenerar output do subagent OU corrigir
  prompt e reexecutar o evaluator.
- **C16 NO_EVAL_BYPASS_IN_PUBLIC — `--skip-prompt-eval` Só Como Escape Técnico Local.** O flag (ou
  equivalente de bypass) exige `MEDNOTES_ALLOW_DEV_ESCAPE=1` +
  `--skip-prompt-eval-reason`. Workflow público do usuário nunca usa esse
  escape para destravar fluxo real; emerge como
  `agent.curator_prompt_eval_skip` no recibo se for usado.
- **C17 GAP_IS_STOP — `contract_gap.missing_next_action` É Stop Rule Pura.** Reportar,
  preservar `error_context.contract_gap`, parar. Não improvisar script,
  `@generalist`, shell paralelo, edição manual ou bypass para "destravar".
- **C18 HARD_STOP_DECISION — `decision.kind=ask_human` É HARD STOP.** Até receber resposta
  válida do usuário, NÃO avançar fase, NÃO chamar subagent, NÃO mutar, NÃO
  rodar recovery/reindex/curadoria automática. Mostrar
  `human_decision_packet` (opções, item afetado, `resume_action`); retomar
  só após resposta.

### C19–C20: Esqueleto Da Resposta (Mutação)

Esses contratos protegem a UX pública de workflows mutantes
(`fix-wiki --apply`, `publish-batch --apply`, `apply-*`, `run-linker --apply`,
restauração aplicada). Eles emergem do experimento C-pos-main Flash
(`docs/reports/controlled-experiments/2026-05-20-fix-wiki-apply-v70-post-main-cpos.md`),
onde Flash deslocou o `Exit Code: 3` central para uma seção auxiliar e
silenciou o pós-decisão quando `next_command=null` veio com
`resume_command` preenchido.

- **C19 PRIMARY_EXIT_CODE_IS_RESULT — Exit Code Do Comando Principal É Resultado.**
  Em workflow mutante, o `Exit Code:` do comando principal (`fix-wiki --apply`,
  `publish-batch --apply`, `apply-*`, `run-linker --apply`) pertence ao Bloco 1
  (Resultado Do Workflow), mesmo quando ≠ 0. `Exit Code: 3` com JSON
  `progress_view_model.status=blocked` ou `receipt.status=blocked` é o sinal
  central do workflow, não warning auxiliar.
  Avisos Auxiliares (Bloco 4) é seção restrita: tool calls `status=error`,
  retries, hook errors, parâmetros inventados. Violação típica: Flash classifica
  `Exit Code: 3` em "Aviso de execução" e mantém o estado bloqueado separado,
  ensinando o leitor que exit code central é evento auxiliar. Bloco-alvo:
  Bloco 1 do esqueleto de mutação (`docs/workflow-output-contract.md`).
- **C20 RESUME_COMMAND_AFTER_DECISION — `resume_command` Pós-Decisão Tem Texto Canônico.**
  Quando o payload bloqueado traz `next_command=null` e `resume_command`
  preenchido, o agente não pode (a) inventar próxima ação executável, (b)
  promover `resume_command` a `next_command` sem resposta humana, nem (c)
  silenciar o pós-decisão. Bloco 2 declara explicitamente: "Nenhuma próxima
  ação automática agora; após decisão, retomar pelo workflow oficial."
  Mostrar opções de `human_decision_packet`, item afetado e citar
  `resume_action` literal (com `--run-id <run_id>` redigido) como rota
  pós-decisão. Violação típica: o agente vê `next_command=null` e encerra
  sem texto pós-decisão, ou copia `resume_command` para "próximos passos"
  como se fosse executável agora. Bloco-alvo: Bloco 2 do esqueleto de mutação.

### Pares Positivo/Negativo

Exemplos compactos em pt-BR, ancorados em comandos reais do `wiki/cli.py`.
Caminhos `<abs>` representam o caminho absoluto literal vindo do payload
fresco.

```text
C1 Negativo: recomendar `cli.py vocabulary-recover --mode rebuild-db`.
C1 Positivo: copiar literal `next_action` do JSON fresco do `vocabulary-status`:
   `uv run python <abs>/bundle/scripts/mednotes/wiki/cli.py vocabulary-recover
   --mode rebuild-db --dry-run --json`.

C2 Negativo: "todos os artefatos stale foram confirmados pela telemetria fresca".
C2 Positivo: "`graph-audit` stale teve `error_count` e `dangling_link`
   reemitidos pelo `graph-audit` fresco (confirmado). `run-linker-diagnose`
   stale permanece baseline histórico apenas — não foi reexecutado nesta
   rodada".

C3 Negativo: encerrar a resposta sem mencionar o comando que foi bloqueado por
   `Command injection detected` mais cedo na bateria.
C3 Positivo: "Bloco B — Comandos Falhos Ou Bloqueados: `bash -c 'BASE_DIR=$(cat
   manifest.env | ...); for f in ...; do jq ... < $BASE_DIR/$f; done'` —
   bloqueado pelo guard em `stream-events.ndjson` com mensagem
   `Command injection detected: command substitution syntax`."

C4 Negativo: "os 21 erros de `validate-wiki` são estritamente da nota
   `medicine.md`."
C4 Positivo: "21 reports com erro distribuídos por várias notas; top 3 paths:
   `medicine.md` (N1), `cardio/<nota>.md` (N2), `pneumo/<nota>.md` (N3); total
   restante: 21 − (N1+N2+N3). Distribuição calculada a partir de
   `tool-output-files/<id>.txt`."

C5 Negativo: "`fix-wiki --dry-run` proverá eliminação de nós e links órfãos."
C5 Positivo: "Próxima bateria: `fix-wiki --dry-run` (`expected_mutation=nenhuma`);
   produz plano que detalha `dangling_link`, `orphan_note` e
   `few_related_links`; aplicação real fica para passo subsequente após
   revisão do plano."

C6 Negativo: uma única tool call rodando
   `bash -lc 'BASE_DIR=$(cat manifest.env); for f in graph-audit.json
   run-linker-diagnose.json; do jq "<seletor>" $BASE_DIR/$f; done'`.
   Sem exit code próprio por arquivo, e qualquer falha do `jq` mascarada.
C6 Positivo: três tool calls separadas, uma por arquivo: cada uma roda
   `uv run python <abs>/bundle/scripts/mednotes/wiki/cli.py <comando> --json`
   ou `jq "<seletor>" <abs>/<arquivo>.json`, com exit code e stdout próprios.

C7 Negativo: para `/mednotes:fix-wiki --dry-run`, rodar antes
   `validate-wiki --json`, `taxonomy-status --json` e só então
   `fix-wiki --dry-run --json`.
C7 Positivo: primeira chamada shell:
   `uv run python <abs>/scripts/mednotes/wiki/cli.py fix-wiki --dry-run --json`;
   depois reportar/parar se a FSM trouxer `decision.kind=ask_human`, blocker
   ou ausência de efeito executável.

C8 Negativo: `read_file` retorna `Path not in workspace`, mas o relatório diz
   "Nenhum comando bloqueado observado".
C8 Positivo: "Bloco B — Tool errors: `read_file` em `<path>` falhou com
   `Path not in workspace`; o workflow principal ainda emitiu JSON fresco,
   mas o erro de ferramenta permanece achado do run."

C9 Negativo: antes do dry-run, rodar `ls` no CLI ou na venv.
C9 Positivo: primeira chamada shell é o dry-run oficial; se falhar por path ou
   venv, a falha do comando oficial vira evidência.

C10 Negativo: `export UV_PROJECT_ENVIRONMENT=<global> && uv run ...`.
C10 Positivo: `uv run python <abs>/scripts/mednotes/wiki/cli.py fix-wiki --dry-run --json`
   herdando o ambiente já configurado pelo harness/runtime.

C11 Negativo: `read_file` falha com `invalid_tool_params`, retry em path local
   passa, e Bloco B só lista o Exit Code do workflow.
C11 Positivo: Bloco B lista o `read_file` falho e informa que o retry recuperou
   leitura, sem apagar o erro original.

C12 Negativo: payload traz `decision.kind=ask_human` + `next_action`; agente lê
   `next_action` como instrução,
   executa `vocabulary-recover --apply` e segue.
C12 Positivo: copiar literal `decision.next_action`, reportar a decisão
   pendente, mostrar `human_decision_packet.options` e parar; retomar só após
   resposta humana.

C13 Negativo: `related-notes-sync --recover-export --mode auto --json` retorna
   `blocked_reason: export_stale`; agente abre Obsidian CLI direto, dispara
   comando do plugin Related Notes e gera o export por fora.
C13 Positivo: reportar `export_stale`, copiar `next_action` literal,
   mostrar fallback oficial (`/mednotes:link-body` se
   `body_only_fallback.safe=true`) e parar.

C14 Negativo: pai lê notas em
   `Wiki_Medicina/cardio/<nota>.md`, infere `primary_meaning` e `aliases`,
   e escreve `note-semantic-ingestion.v1` em `output_path` por conta própria.
C14 Positivo: pai roda
   `plan-subagents --phase vocabulary-curation`, lança
   `med-link-graph-curator` por `work_item`, coleta com
   `collect-curator-outputs`, avalia com `eval-curator-batch` e aplica com
   `apply-curator-batch --prompt-eval`.

C15 Negativo: `eval-curator-batch` retorna `status=needs_review`; agente
   reescreve `curator-prompt-eval.json` ajustando `status=approved` e
   re-roda `apply-curator-batch --prompt-eval <edited>`.
C15 Positivo: agente identifica defeito do output (alias amplo, evidência
   vazada, defer ausente, rota complexa sem split), relança
   `med-link-graph-curator` com `error_context`, refaz `eval-curator-batch`
   e só aplica se o evaluator passar limpo.

C16 Negativo: para destravar `/mednotes:fix-wiki` com fila bloqueada em
   `needs_review`, agente roda
   `apply-curator-batch ... --skip-prompt-eval --skip-prompt-eval-reason
   "destravar"` sem `MEDNOTES_ALLOW_DEV_ESCAPE=1` e dentro de fluxo público.
C16 Positivo: reportar `needs_review`, regenerar outputs do batch ou ajustar
   prompt do curator; se inviável neste run, registrar
   `contract_gap`/feedback e parar.

C17 Negativo: workflow retorna `status=blocked`,
   `error_context.contract_gap=missing_next_action`; agente trata como
   "preciso destravar" e improvisa apply manual, edita JSON ou chama
   `@generalist`.
C17 Positivo: reportar gap literal, citar
   `error_context.contract_gap`, abrir registro de feedback redigido e parar
   sem mutação.

C18 Negativo: `run-linker --diagnose` retorna `decision.kind=ask_human` com
   `human_decision_packet`; agente
   continua para recovery do vocabulary DB, lança curator, gera outputs e
   aplica batch antes de pedir decisão.
C18 Positivo: mostrar `human_decision_packet.options`, item afetado e
   `resume_action`; pausar todo o pipeline; só após
   resposta humana explícita, executar `resume_action`.

C19 Negativo: relatório final de `/mednotes:fix-wiki --apply` blocked traz
   "Status: blocked" no topo e, em seção separada "Aviso de execução",
   coloca "Exit Code: 3 durante o processamento da Wiki". Exit code central
   classificado como auxiliar.
C19 Positivo: Bloco 1 — Resultado Do Workflow: `status=blocked`,
   `phase=fix_wiki_apply`, `Exit Code: 3` (bloqueio do workflow),
   `blocked_reason=requires_llm_rewrite`,
   `primary_human_decision_kind=taxonomy_review_required`,
   `changed_count=771`. Bloco 4 — Avisos Auxiliares: "Nenhum aviso auxiliar
   observado após varredura dos tool outputs."

C20 Negativo: payload bloqueado traz `next_command=null` e
   `resume_command` preenchido; agente encerra com "Próxima ação: nenhuma"
   sem citar a rota pós-decisão, ou copia `resume_command` literal para
   "Próximo comando" como se fosse executável agora.
C20 Positivo: Bloco 2 — Decisão Humana: opções de
   `human_decision_packet`, item afetado, e "Nenhuma próxima ação
   automática agora; após decisão, retomar pelo workflow oficial." Mostra
   `--run-id <run_id>` redigido; não executa `resume_action` antes de
   receber resposta humana válida.
```

### Como Cada Contrato Trava A Falha

Diagnóstico Read-Only (C1–C8) usa o template fechado de 5 blocos em
`docs/workflow-output-contract.md`:

- Bloco A trava C1/C6/C7 ao exigir a string exata do comando + literal de
  `next_action`.
- Bloco B trava C3/C8/C11 ao tornar a varredura por comandos
  bloqueados/falhos + tool errors obrigatória, com sentinela literal quando
  vazia.
- Bloco C trava C2 ao exigir resposta explícita "equivalente fresco rodado?"
  + "confirmado?" por artefato.
- Bloco D trava C1 (literal_match), C5 (`expected_mutation=nenhuma` em
  dry-run) e a prioridade fresco-sobre-stale; reforça C7.
- Bloco E trava C4 ao proibir quantificadores universais sem distribuição
  computada.

C9 é coberto por Bloco A (primeiro shell command auditável é o dry-run
oficial). C10 é operacional e auditável por `agent_events` /
`environment_context`. C11 é cobertura adicional de Bloco B.

Orquestração (C12–C18) opera fora do Diagnóstico Read-Only:

- C12 trava confusão entre orientação (relatar) e autorização (executar).
- C13 fecha rota: bloqueio oficial não autoriza shadow path.
- C14 protege divisão de papéis: pai não escreve schema de subagent.
- C15 protege o evaluator: avaliação não vira aprovação por edição.
- C16 isola o escape técnico: bypass exige env explícito e fica visível no
  recibo.
- C17 transforma gap em parada limpa, não em pretexto para improviso.
- C18 transforma decisão pendente em hard stop verificável antes de
  qualquer subagent/mutação.

Esqueleto Da Resposta (Mutação) (C19–C20) usa os 4 blocos fechados em
`docs/workflow-output-contract.md` §Esqueleto Da Resposta (Mutação):

- Bloco 1 trava C19 ao colocar `Exit Code:` do comando principal junto do
  `status` semântico do workflow, mesmo quando ≠ 0.
- Bloco 2 trava C20 ao exigir texto canônico pós-decisão quando
  `next_command=null` vem com `resume_command` preenchido, sem promover
  `resume_command` a comando executável.
- Bloco 4 reforça C19/C3/C8/C11: avisos auxiliares são seção restrita a tool
  errors auxiliares, retries, hook errors e parâmetros inventados;
  `Exit Code:` do comando principal não entra aqui.

## Stop Rules

Pare e escale. Condições gerais (sem código) acima; recap dos contratos
indexados abaixo.

Condições gerais:

- `UNIQUE constraint failed`;
- `sqlite_integrity_error`;
- schema drift não reparável por comando oficial;
- fila inconsistente sem dry-run oficial;
- path/hash mismatch;
- `next_action` ausente ou ignorado;
- argumento de ferramenta inventado, como `wait_for_previous`, ou qualquer
  parâmetro não documentado pelo schema da tool; sequencie comandos esperando
  o resultado da chamada anterior, não passando flags extras;
- `null` inesperado usado como dado válido em resumo JSON;
- artefato stale ou baseline preexistente misturado com output fresco sem
  rotulagem explícita;
- artefato salvo contradizendo output fresco sem reportar divergência e sem
  preferir a evidência mais recente;
- inferência sem evidência direta, como transformar "catálogo vazio" em
  "corrupção" sem erro, hash, validação ou diagnóstico específico;
- conclusão final contradizer os comandos realmente executados, como declarar
  que usou a rota oficial quando o terminal mostra comandos de descoberta,
  caminhos errados ou shells diferentes;
- conclusão ampla demais para a evidência, como declarar isolamento total,
  sucesso integral ou ambiente rigorosamente respeitado quando houve path fora
  do escopo principal ou artefato relevante só contado/não auditado;
- timeout, timeout repetido ou `max_turns` esgotado;
- output sem `agent_metrics`, especialmente em `timeout_or_max_turns`;
- tentativa de usar `@generalist` ou outro agente intermediário para
  orquestrar curadoria de vocabulário; o agente pai é o único orquestrador e
  deve lançar `med-link-graph-curator` diretamente;
- tentativa de editar SQLite diretamente;
- tentativa de mutar Markdown em massa sem dry-run/recibo;
- drift local de prompt/runbook/script da extensão.

Contratos indexados (definições e pares em §Contratos):

- C1 NEXT_ACTION_LITERAL — `next_action` reescrita sem `literal_match=não` justificado.
- C2 STALE_NEEDS_FRESH — "stale confirmado" sem equivalente fresco neste run.
- C3 TOOL_OK_NOT_WORKFLOW_OK — `tool status=success` mascarando `Exit Code:`≠0,
  `Blocked` ou parser error.
- C4 NO_UNIVERSAL_WITHOUT_DISTRIBUTION — quantificador universal sem distribuição computada.
- C5 DRY_RUN_IS_PLAN — dry-run descrito como aplicação/eliminação/correção/limpeza.
- C6 ONE_AUDITABLE_COMMAND — múltiplos comandos auditáveis empacotados em um único shell.
- C7 DRY_RUN_FIRST_SHELL — preflight antes de dry-run explícito.
- C8 TOOL_ERROR_IS_FINDING — tool `status=error` (não-shell) omitido do relatório.
- C9 NO_SHELL_PROBES — probes shell antes de dry-run oficial.
- C10 INHERIT_UV_ENV — `UV_PROJECT_ENVIRONMENT` sobrescrito em vez de herdado.
- C11 RETRY_DOESNT_ERASE_ERROR — retry "apagando" erro anterior.
- C12 NEXT_ACTION_NOT_AUTHZ — `next_action` tratado como autorização.
- C13 OFFICIAL_ROUTE_ONLY — fallback paralelo após rota oficial `blocked`.
- C14 NO_PARENT_SCHEMA — pai emitindo schema de subagente.
- C15 EVAL_TERMINAL — `needs_review` destravado por edição manual do JSON de eval.
- C16 NO_EVAL_BYPASS_IN_PUBLIC — `--skip-prompt-eval` em fluxo público sem env explícito.
- C17 GAP_IS_STOP — `contract_gap.missing_next_action` tratado como pretexto
  para workaround.
- C18 HARD_STOP_DECISION — `decision.kind=ask_human` ignorado.
- C19 PRIMARY_EXIT_CODE_IS_RESULT — `Exit Code:` do comando principal
  classificado como aviso auxiliar.
- C20 RESUME_COMMAND_AFTER_DECISION — `next_command=null` +
  `resume_command` preenchido sem texto pós-decisão canônico, ou
  `resume_command` promovido a `next_command` sem resposta humana.

## Pré-vôo

Antes de declarar aplicado/concluído/sucesso, o agente responde mentalmente
sete perguntas. Falha em qualquer uma = reportar estado real, não conclusão.

1. `decision.kind=ask_human` em algum payload sem resposta humana registrada?
   Pare (C18).
2. `eval-*` retornou `needs_review` que foi contornado por edição do JSON de
   avaliação ou `--skip-prompt-eval` em fluxo público? Bloqueio (C15/C16).
3. Algum comando oficial retornou `blocked_reason` e eu segui rota paralela
   (Obsidian CLI direto, regex, `@generalist`, edição manual)? Bloqueio (C13).
4. O pai emitiu schema designado a subagente
   (`note-semantic-ingestion.v1`, `triage-note-plan.v2`,
   `atomicity-split-bundle.v1`, output de
   `vocabulary-curator-batch-plan.v1`)? Bloqueio (C14).
5. Algum tool call `success` esconde `Exit Code:` ≠ 0 / `status=blocked` /
   parser error / `invalid_tool_params`? Reporte falha (C3/C8/C11).
6. Em mutação, `Exit Code:` do comando principal está no Bloco 1 (Resultado),
   e Bloco 4 (Avisos Auxiliares) lista só tool errors auxiliares/retries/hook
   errors/parâmetros inventados? `Exit Code: 3` central em "Aviso de execução"
   é bug de relatório (C19). Se o payload trouxer `next_command=null` com
   `resume_command` preenchido, Bloco 2 mostra opções, item afetado e a frase
   canônica "Nenhuma próxima ação automática agora; após decisão, retomar
   pelo workflow oficial." sem promover `resume_command` a `next_command`
   (C20).
7. A resposta visível menciona termo interno fora de `<details>`? Traduza
   por `docs/public-vocabulary.md`. Categorias a escanear:
   - Execução: `uv`, `--dry-run`, `--apply`, `manifest`, `batch`, `hash`,
     `schema drift`, `Exit Code`.
   - Estado: `next_action`, `blocked_reason`, `needs_review`,
     `status=blocked`, `human_decision_required`.
   - Armazenamento: `SQLite`, vocabulary DB.
   - Versionamento: `commit`, `branch`, `push`, `sync_status`.
   - Bypass técnico: `--skip-prompt-eval`, `MEDNOTES_ALLOW_DEV_ESCAPE`.
   - Agentes: `med-link-graph-curator`, `med-knowledge-architect`,
     `med-chat-triager`, `med-publish-guard`, `med-flashcard-maker`.

## Diagnóstico Read-Only

Quando o run é uma bateria de comandos `--json` sem mutação
(`environment-preflight`, `validate-wiki`, `taxonomy-status`,
`vocabulary-status`, `graph-audit`, `run-linker --diagnose`, ou equivalentes),
o agente herda um contrato fechado de relatório final, ancorado nos 5 blocos
da seção `Diagnóstico Read-Only` em `docs/workflow-output-contract.md`:

- Bloco A — comando exato + tool status + `Exit Code:` + workflow `status` +
  `next_action` literal.
- Bloco B — comandos falhos/bloqueados + tool calls `status=error` (com
  sentinela literal quando vazio).
- Bloco C — artefatos stale (só "confirmado" se reemitido por comando fresco).
- Bloco D — `source`/`freshness`/`payload_next_action_literal`/`literal_match`/
  `expected_mutation`.
- Bloco E — escopo quantitativo (distribuição por path/código antes de
  quantificador universal).

C1–C8 são os modos de falha que o template trava (ver §Contratos para
definições e §Como Cada Contrato Trava A Falha para o mapeamento). Onde a
violação escapa do bloco (ex. caminho absoluto reescrito com
`literal_match=sim` indevido), continua existindo a Stop Rule indexada.

## Ferramentas Oficiais

Vocabulary DB:

```bash
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-status --json
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode rebuild-db --dry-run --plan-output "<vocabulary-recovery-plan.json>" --json
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode reconcile-queue --dry-run --plan-output "<vocabulary-recovery-plan.json>" --json
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode <rebuild-db|reconcile-queue> --apply --plan "<vocabulary-recovery-plan.json>" --receipt "<vocabulary-recovery-receipt.json>" --json
```

Publish/manifest:

```bash
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" publish-status --manifest "<manifest.json>" --json
```

Curator batch:

```bash
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" eval-curator-batch --plan "<plan.json>" --outputs "<manifest.json>" --report "<eval.json>" --json
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" apply-curator-batch --plan "<plan.json>" --outputs "<manifest.json>" --prompt-eval "<eval.json>" --receipt "<receipt.json>" --json
```

Triager:

```bash
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" eval-triager-output --raw-file "<raw.md>" --output "<triager-output.json>" --subagent-run-receipt "<subagent-run-receipt.json>" --require-subagent-run-receipt --report "<eval.json>" --json
uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" triage --raw-file "<raw.md>" --tipo medicina --titulo "<titulo_triagem>" --fonte-id "<fonte_id>" --note-plan "<note-plan.json>" --triager-eval "<eval.json>" --json
```

O `subagent-run-receipt.json` deve ser assinado/atestado pelo runner oficial.
O parent não cria, edita nem re-assina recibo; assinatura ausente, inválida ou
sem chave pública confiável é bloqueio de autoria, não tarefa para remendar JSON.

## Garantia Esperada

O próximo run deve conseguir explicar, com telemetria redigida, por que um
agente escolheu continuar, aplicar, bloquear ou escalar. Root causes de
telemetria nunca devem ser consolidadas sem preservar `app_version`.

## Installed Runtime Is Not Source

Public workflow agents must never repair a Workbench bug by editing the
installed bundle/plugin directory. Installed paths are runtime projections
and can be overwritten by update/reinstall. Report
`installed_extension_runtime_edit_forbidden`, name the canonical source file
under `bundle/`, and stop or open a development task.
