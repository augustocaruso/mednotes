# MedNotes Workbench Agent Contract

Este pacote contem workflows publicos para criar, enriquecer, processar,
auditar, linkar e estudar notas medicas. A experiencia publica deve conduzir
preparo, diagnostico, previa, confirmacao e proxima acao sem exigir que o
usuario conheca paths internos, schemas, hashes ou comandos tecnicos.

## Runtime

Trate este diretorio como a raiz de runtime distribuida. Use somente arquivos
empacotados aqui ou caminhos oficiais recebidos no payload do workflow.

- `commands/`: entrada curta dos workflows publicos.
- `skills/`: runbooks operacionais que descrevem a sequencia humana e segura.
- `agents/`: especialistas empacotados; nao fabrique prompts curtos no parent.
- `docs/`: contratos duraveis, politica de nota, seguranca e formato de saida.
- `scripts/`: CLIs, hooks, wrappers e adapters oficiais.
- `src/`: bibliotecas Python do produto.

Config global do usuario fica em `~/.mednotes/config.toml`, ou no arquivo
apontado por `MEDNOTES_CONFIG`. Paths vivem em `[paths]`, limites de fan-out em
`[parallelism]`, cada especialista em `[agents.<nome_do_agente>]` com `model` e
`reasoning_effort`, e referencias de segredo em `[secrets.*]`. Segredos reais
devem ser buscados no keyring do sistema primeiro, com env como fallback tecnico;
nunca escreva chaves no TOML. Depois de alterar `model`/`reasoning_effort`, rode
a sincronizacao do alvo de runtime antes da proxima sessao; sessao ja carregada
nao faz hot-reload.

## Workflows

Preserve os nomes publicos: `/mednotes:create`, `/mednotes:enrich`,
`/mednotes:process-chats`, `/mednotes:fix-wiki`, `/mednotes:link`,
`/mednotes:link-body`, `/mednotes:link-related`, `/mednotes:pdf-library`,
`/mednotes:setup`, `/mednotes:status`, `/mednotes:history`,
`/mednotes:telemetry`, `/flashcards` e `/report`.

Workflows FSM-first devem seguir o payload oficial: `progress_view_model`,
`state_machine_snapshot`, `decision`, `receipt`, `reports`,
`agent_directive` e, quando houver problema acionavel, `diagnostic_context`.
Nao parseie texto humano como verdade operacional.

## Safety

- Mutacoes da Wiki devem passar pelos CLIs oficiais e pela protecao de vault.
- Conteudo clinico bruto, HTML, imagens, logs completos, tokens e credenciais
  nao devem ser colados no prompt do parent nem em relatorios publicos.
- Especialistas recebem work items tipados, paths oficiais, hashes e output
  path; eles leem a fonte designada dentro do proprio escopo.
- Hooks sao defesa de contrato: quando houver cartao FSM ativo, bloqueiam rota
  errada, payload bruto, metadata fabricada e finalizacao prematura.

## Reports

Responda em portugues do Brasil. Primeiro diga o resultado real do workflow,
depois o que foi alterado, o que ficou pendente e a proxima acao. Se uma tool
terminou com sucesso mas o JSON oficial veio `blocked`, `failed` ou pediu
decisao humana, reporte o bloqueio como o resultado verdadeiro.
