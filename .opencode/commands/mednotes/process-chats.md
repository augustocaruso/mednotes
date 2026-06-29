---
description: "Processa backlog de chats medicos brutos para notas Obsidian."
---

<!-- Generated from commands/mednotes/process-chats.toml. Do not edit directly. -->

Processe chats médicos brutos de `Chats_Raw` para `Wiki_Medicina`. Argumentos do usuário: $ARGUMENTS. Guardrails enfáticos do launcher:
- Rota rápida obrigatória: primeiro rode exatamente `node ".opencode/mednotes/scripts/run_python.mjs" ".opencode/mednotes/scripts/mednotes/wiki/cli.py" list-pending --summary --json`; não leia skills/docs, diretórios, `config.toml` nem rode `validate` antes dessa checagem.
- Se o payload público indicar que não há chats novos e o workflow é terminal, responda somente com `reports.public_report.lines` e encerre. Não adicione "Resumo do Trabalho"; não mencione nomes de campos internos, schemas, hashes ou caminhos locais.
- Resposta pública padrão: use `reports.public_report.lines` quando existir e não exponha comandos literais, flags, schemas, recibos, hashes, paths internos nem nomes de campos. Detalhes técnicos ficam no canal agente/debug ou laboratório; decisões humanas devem aparecer como pergunta, opções fechadas e item afetado.
- Só carregue a skill `process-medical-chats` e `.opencode/mednotes/skills/obsidian-ops/SKILL.md` se houver backlog real, bloqueio acionável ou continuação explícita. Resposta: `.opencode/mednotes/docs/workflow-output-contract.md`. Continuação agentica executável vem somente de `agent_directive.control.capabilities.continue=true` com `agent_directive.control.effects[]`; `decision`, `human_decision_packet` e `progress_view_model.resume_action` orientam UX, decisão humana e retomada pública, mas não autorizam mutação ou chamada de subagente sem `agent_directive.control`.
- Se a fase `architect` também indicar zero itens e terminal sem chats novos, encerre: não rode `validate-wiki`, `/mednotes:fix-wiki`, `run-linker`, `publish-batch` nem subagentes. Responda que não havia chat novo, nada foi escrito e linker/grafo não precisava rodar.
- Para lote explícito, use `plan-subagents --limit <N>` com `--phase triage` antes de ler raw; um raw chat por subagent; parent não monta prompt manual e usa paths do `work_item`.
- Não substitua `med-chat-triager`; `triage-note-plan.v2` é autoridade; rode `eval-triager-output --report`; se falhar, reenvie ao triager, não remende JSON; nunca peça metrics fabricadas.
- Exija coverage `raw-coverage.v1` derivada do plano e `stage-note --coverage` no manifest único.
- No OpenCode, depois de uma task `architect` concluída, use somente `wiki/cli.py finalize-opencode-architect-task` com `--plan`, `--work-id` e `--json`; não use `finalize-opencode-specialist-task`, não leia código para descobrir finalizer e não siga para `stage-note` antes desse payload validar metadata, `architect-output.v1`, coverage e nota.
- Corpo de raw chat é imutável; YAML/status só muda via `wiki/cli.py` (`triage`, `discard`, `publish-batch`). Não use `write_file`, `replace`, shell redirection, `sed` ou scripts para editar raw chats, coverage, manifests, H1, taxonomia, YAML/status ou notas staged; first-pass prevention: `note_plan_invalid` para na triagem; `blocked.validation_errors` usa `fix-note`/`rewrite_prompt`; `taxonomy_resolution_required` usa `taxonomy-*`; `coverage_invalid` repete só `stage-note --coverage`; não gere scripts.
- Se a CLI apontar `environment_blocker.windows_path_or_venv`, rode `/mednotes:setup` ou bootstrap/reset oficial; não edite scripts/runbooks.
- Para `canonical_merge_required`, `provenance_gap`, `batch_state_mismatch` ou decisão humana, renderize a decisão pelo `decision`/`human_decision_packet` e só continue automaticamente quando `agent_directive.control.effects[]` trouxer a próxima operação executável. Campos legados como `human_decision_required`/`next_action` em payload técnico não autorizam publicação.
- Se houver `artifact_manifests`, cubra todos os HTMLs; nunca inline HTML. Sempre rode `publish-batch --dry-run`, depois `med-publish-guard`; publish real tem rollback automático.
- Não rode `run-linker` manualmente depois do dry-run; o `publish-batch` real já chama o linker. Se links bloquearem após publish, próxima ação é `/mednotes:fix-wiki --dry-run`.
- Em bloqueios recuperáveis, preserve `error_context`; em retry/fase errada/comando falho, registre `agent_events` redigidos.
- Não chame dry-run de concluído: dry-run limpo é `ready_to_publish`, publish é `published`, linker com blockers é `completed_with_link_blockers`.
