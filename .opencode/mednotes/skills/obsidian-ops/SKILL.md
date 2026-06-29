---
name: obsidian-ops
description: Use when an agent will read, create, modify, move, delete, restore, or inspect Obsidian vault files, Wiki_Medicina notes, .obsidian data, plugin exports, vault Git state, or vault history.
---

# Obsidian Ops

Camada operacional obrigatória antes de qualquer interação com vault Obsidian.
Ela define segurança e roteia para o workflow certo; não substitui os comandos
públicos nem duplica contratos longos.

## Fonte Canônica

- Vault history, restore, rollback e pontos de restauração:
  `${extensionPath}/docs/vault-version-control.md` e
  `${extensionPath}/scripts/vault/vault_git.py`.
- Wiki e workflows médicos:
  `${extensionPath}/scripts/mednotes/wiki/cli.py`.
- Taxonomia, estilo e formato de nota:
  `${extensionPath}/docs/knowledge-architect.md`.
- Links, grafo, Related Notes e body linker:
  `${extensionPath}/docs/semantic-linker.md`.
- Proteção do vault e rollback:
  `${extensionPath}/docs/vault-version-control.md`.
- Resposta final ao usuário:
  `${extensionPath}/docs/workflow-output-contract.md`.

## Política

- Valide `wiki_dir` antes de ler ou mutar. A fonte é
  `~/.mednotes/config.toml` em `[paths].wiki_dir`;
  ambiguidade bloqueia.
- Antes de mutar vault versionado, inspecione o estado com Git ou com a CLI de
  segurança do vault. Não sobrescreva mudança do usuário.
- Use `wiki/cli.py` para operações Wiki. Não invente protocolos ou scripts
  locais do vault que não existem neste projeto.
- Não use redirecionamento de shell para escrever notas. Use CLI controlada ou
  edição de arquivo com UTF-8 preservado.
- Não use `rm`, `del` ou exclusão direta em notas do vault. Deleção, merge,
  migração de pasta e rollback passam por workflow com plano, recibo e
  rollback. Pastas vazias só podem ser removidas por fase determinística.
- Não mude `.obsidian/` sem pedido explícito e plano. Ler export de plugin é
  permitido quando o linker exige.
- Toda nota criada, apagada, movida, renomeada ou modificada gera
  `link-trigger-context.v1` e chama `/mednotes:link` uma vez por lote, ou
  retorna `linker_pending_reason`. Exceção: enrich puramente visual de imagens.
- Não use Git destrutivo (`reset --hard`, `checkout --`, `push --force`) sem
  pedido explícito e alvo inequívoco.

## CLI-First Para Obsidian

Antes de qualquer automação visual, tente nesta ordem: `obsidian help`;
`obsidian <comando>` para ação equivalente à Paleta de Comandos;
`obsidian eval` para APIs internas/plugins; leitura de logs, sync plans,
exports e estado do vault; GUI só para campo/configuração sem
comando, API ou arquivo seguro.

Regra crítica: comandos da Paleta de Comandos do Obsidian são acessíveis via CLI.
Sync, export de sync plans, reload de plugin, comando de plugin, console/erros
e debug usam `obsidian`/`obsidian eval`. Nome ou ID desconhecido: descubra por
CLI/eval primeiro.

Ao interagir com Obsidian aberto, plugins, DOM, screenshots, reload, debug ou
sync plans, carregue
`${extensionPath}/skills/obsidian-cli/SKILL.md` antes de agir.
`obsidian-ops` define segurança; `obsidian-cli`, mecânica.

## Gate anti-GUI obrigatório

Antes de mouse, screenshot, teclado ou UI Automation, declare: ação; por que
`obsidian`, `obsidian eval` e arquivo seguro não
cobrem; qual risco a GUI resolve. Dúvida bloqueia e volta para CLI.

GUI proibida: Abrir Paleta de Comandos, rodar comandos de plugin, exportar sync
plans, ler console/erros, reload/plugin/debug, ou observar settings legíveis
por CLI/eval. GUI permitida só para campo sem comando/API, estado visual
indisponível por CLI/arquivo, ou recovery com evidência de UI única.

## Segurança Git

Para qualquer mutação do vault, leia e siga
`${extensionPath}/docs/vault-version-control.md`. A regra curta é:

- `vault_git.py run-start` antes da mutação e `run-finish` depois.
- No `run-finish`, copie o `run_id` literal do JSON de `run-start`; não
  normalize nem remova separadores.
- Depois de `run-finish`, confirme `guard_lease.status=closed` no JSON fresco
  ou `guard-status.active_count=0`; com `blocked_reason=guard_lease_mismatch`,
  `guard_lease.status=missing` ou sem essa evidência, reporte pendência e não
  diga que o guard foi fechado.
- Em resposta pública, não imprima `run_id`, `guard_lease`, lease id/path,
  hash ou identificador curto de ponto de restauração como `7da9fcf`; diga
  apenas que a proteção do vault foi encerrada e que há ponto de restauração
  disponível. IDs ficam para debugging. Se listar `run-finish`, redija como
  `--run-id <run_id>`.
- Mudança humana aberta vira snapshot separado; não misture com mudança do
  agente.
- Um lote lógico vira um ponto de restauração, não um commit por arquivo.
- Ao terminar, a política deve sincronizar e dar push. Falha de rede/auth fica
  explícita como backlog local.
- Se o workflow não consegue aplicar essa política, bloqueie ou registre
  `blocked_reason` claro; não siga por edição manual silenciosa.

## Delegação Para Skills Oficiais

- Para operações Obsidian genéricas, carregue
  `${extensionPath}/skills/obsidian-cli/SKILL.md`: busca, leitura,
  propriedades, attachments, plugins, DOM, screenshot e rotinas de vault que
  não sejam específicas do Workbench.
- Para sintaxe Obsidian, carregue
  `${extensionPath}/skills/obsidian-markdown/SKILL.md`: WikiLinks, embeds,
  callouts, properties/frontmatter e detalhes de Markdown que não estejam
  definidos pelo Padrão Ouro médico.
- Essas skills vendorizadas são apoio de ferramenta/sintaxe. Elas não substituem
  `wiki/cli.py`, `knowledge-architect.md`, `semantic-linker.md`, a política de
  pontos de restauração, nem os workflows públicos `/mednotes:*`.

## Roteamento

- Criar nota: `${extensionPath}/skills/create-medical-note/SKILL.md`.
- Enriquecer imagens: `${extensionPath}/skills/enrich-medical-note/SKILL.md`.
- Processar raw chats:
  `${extensionPath}/skills/process-medical-chats/SKILL.md`.
- Saúde, estilo, taxonomia, duplicatas:
  `${extensionPath}/skills/fix-medical-wiki/SKILL.md`.
- Links, grafo, body linker, Related Notes:
  `${extensionPath}/skills/link-medical-wiki/SKILL.md`.
- Flashcards/deeplinks:
  `${extensionPath}/skills/create-medical-flashcards/SKILL.md`.
- Histórico/restore: `/mednotes:history` com `vault_git.py`.

## Saída

Siga `workflow-output-contract.md`: resumo em PT-BR, status, arquivos,
blockers, `next_action` e `required_inputs`. Não despeje JSON bruto salvo se o
usuário pedir.
