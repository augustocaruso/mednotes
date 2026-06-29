---
description: "Prepara o ambiente Python local do Medical Notes Workbench."
---

<!-- Generated from commands/mednotes/setup.toml. Do not edit directly. -->

Prepare o Medical Notes Workbench para uso nesta maquina; conduza o usuario passo a passo e pare em login/decisao ate confirmacao clara.
Use `.opencode/mednotes` como raiz da extensão e `.opencode/mednotes/docs/workflow-output-contract.md` para a resposta final.
Modelo mental:
1. `~/.gemini/extensions/medical-notes-workbench` é o bundle; estado persistente fica em `~/.mednotes`.
2. Use `/mednotes:setup` como workflow FSM-first. O estado público é somente `setup-fsm-result.v1`: `state_machine_snapshot`, `progress_view_model`, `receipt`, `decision`, `human_decision_packet`, `reports` e `agent_directive`.
3. Execute somente `agent_directive.control.effects[]`, em ordem, e alimente o resultado de cada adapter de volta como evento tipado da FSM. Não transforme stdout de adapter privado em estado público.
4. Effects públicos esperados incluem `setup:set-paths`, `setup:validate-config`, `setup:repair-config` com adapter `--agent-repair`, `setup:bootstrap-python`, `setup:wait-obsidian`, `setup:rebuild-markdown-runtime`, `setup:rebuild-markdown-index` para preparar o índice Markdown, `setup:vault-guard`, `setup:start-github-login`, `setup:choose-local-only`, `setup:confirm-github-remote`, `setup:resolve-ambiguous-remote`, `setup:confirm-main-branch` e `setup:resolve-policy`.
5. `setup:vault-guard` pode executar `vault_git.py setup` apenas como adapter privado e deve ser projetado pelo stack FSM antes de qualquer comunicação pública.
6. Se `decision.kind=ask_human`, use a ferramenta nativa de pergunta/seleção com as opções do `human_decision_packet`; só retome pelo `resume_action` oficial.
7. Se o receipt trouxer `git_identity_github_attribution`, explique que autoria Git preservada não garante avatar/link/filtro no GitHub; para isso use email de uma conta GitHub ou bot real.
8. Não edite `config.toml` manualmente; use o adapter oficial preservando bytes UTF-8; não rode `git add`, não rode `git commit`, não rode `git push`, não limpe arquivos e não entre em nested repos/plugins para "limpar" o vault.
9. No Windows, se aparecer "missing `RECORD`", `Failed to uninstall package`, repetição de uninstall/install ou `Acesso negado`, reporte `uv_repair_churn`/`windows_path_or_venv` e use as rotas oficiais `bootstrap_windows_python_uv.ps1` ou `reset_windows_python_uv.ps1`; não diga que o ambiente está perfeito.
10. SerpAPI é opcional via `gemini extensions config medical-notes-workbench SERPAPI_KEY`, `config.toml`/`.env` persistentes; nunca imprima segredo.
11. Registre feedback local com `scripts/mednotes/feedback_report.py record --workflow /mednotes:setup --agent`; falha de feedback não muda o resultado do setup.
Explique em portugues o que foi configurado/falta; se a proteção local estiver pronta, inclua: "Proteção local pronta. A extensão também ativou uma trava de segurança: agentes Gemini não conseguem alterar o vault diretamente sem ponto de restauração ativo."
