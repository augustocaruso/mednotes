---
description: "Verifica configuracao local do Medical Notes Workbench."
---

<!-- Generated from commands/mednotes/status.toml. Do not edit directly. -->

Verifique o status local do Medical Notes Workbench.

Use `.opencode/mednotes` como raiz da extensão e `.opencode/mednotes/docs/workflow-output-contract.md` para a resposta final.

Checklist nao mutante:
1. Confirme `gemini extensions list` e raiz `~/.gemini/extensions/medical-notes-workbench`.
2. Verifique `~/.mednotes/config.toml` sem imprimir o arquivo inteiro; nunca mostre tokens, `auth_token`, `.env`, defaults de telemetria, feedback records ou hook events.
3. Confira `~/.mednotes/config.toml` com `[paths].wiki_dir` e `[paths].raw_dir`; o status não deve alterar notas. Se faltar caminho, a próxima ação oficial é `set-paths`; destaque compatibilidade ou bloqueio por ambiguidade.
4. Valide o enricher pela porta explícita de script: `node ".opencode/mednotes/scripts/run_python.mjs" "scripts/enrich_notes.py" --help`. Não passe `-m` para `run_python.mjs`; o wrapper recebe um caminho `.py` relativo à raiz da extensão.
5. Leia `[gemini].binary`, resolva `gemini.cmd` no Windows quando aplicavel e rode `<binary> --version` sem imprimir segredos.
6. Rode `node ".opencode/mednotes/scripts/run_python.mjs" "scripts/mednotes/wiki/cli.py" validate --config ~/.mednotes/config.toml` e destaque `environment_preflight`, `wiki_source`, `wiki_compat_warnings`, `config_encoding_warnings` e `vocabulary_db_exists`.
7. Rode `node ".opencode/mednotes/scripts/run_python.mjs" "scripts/mednotes/wiki/cli.py" markdown-query-status --config ~/.mednotes/config.toml --json` e trate a saída como snapshot técnico tipado do índice Markdown: ready = "Índice Markdown pronto."; missing/stale = "Índice Markdown precisa ser preparado; próxima ação: /mednotes:setup."; bloqueio = traduza causa e retomada oficial em linguagem humana, sem transformar campos técnicos raiz em estado paralelo.
8. Rode `node ".opencode/mednotes/scripts/run_python.mjs" "scripts/mednotes/feedback_report.py" integrity status --format json` e destaque drift de prompts/runbooks/scripts.
9. Confira `SERPAPI_KEY`/`SERPAPI_API_KEY` por setting, ambiente ou `.env` persistente sem imprimir chave; se faltar, dê o comando de configuração.
10. Não publique, não corrija Wiki, não aplique linker e não altere notas.
11. Registre feedback local com `node ".opencode/mednotes/scripts/run_python.mjs" "scripts/mednotes/feedback_report.py" record --workflow /mednotes:status --agent`.
    Se algum comando anterior encontrou pendência, falha ou aviso, grave um payload tipado de snapshot/status com causa, retomada oficial e evidência redigida; nunca grave um resumo final saudável vazio depois de detectar ambiente pendente.
    Se houve retry, fase errada, drift, mutação inesperada ou comando falho, use `--payload -` com `agent_events` e `error_context` tipados.
    Se encontrar Python/uv/venv/PowerShell/path Windows quebrado, registre `environment_blocker.windows_path_or_venv`, sugira `/mednotes:setup` ou bootstrap/reset oficial e pare sem editar scripts, prompts ou runbooks.
    Se qualquer comando imprimir "missing `RECORD`", "Failed to uninstall package", `Uninstalled ... Installed ...` repetidamente ou `Acesso negado`, registre `uv_repair_churn` ou `environment_blocker.windows_path_or_venv`; não diga que o ambiente está perfeito mesmo que o JSON principal venha `ready`.
Responda em portugues com pronto / pendente e proximas acoes concretas.
