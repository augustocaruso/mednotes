---
name: med-auto-linker
description: Contrato compacto do /mednotes:link: diagnostico, desambiguacao contextual e apply auditavel do grafo da Wiki.
---

# Semantic Linker Contract

No workflow público `/mednotes:link`, use a CLI pública `wiki/cli.py run-linker`.
`/mednotes:link-body` usa o mesmo `run-linker` com `--no-related-notes`.
`/mednotes:link-related` usa `wiki/cli.py related-notes-sync`, cujo output
público é `medical-notes-workbench.link-related-fsm-result.v1`. O payload
operacional antigo de Related Notes é detalhe interno: consumidores leem
`progress_view_model`, `state_machine_snapshot`, `decision`, `receipt`,
`artifacts`, `diagnostic_context` e `error_context`. O linker cuida só de
`knowledge_graph`: WikiLinks, aliases, vocabulary DB, meanings, body linker,
Related Notes, backlinks e validação do grafo. Não corrige estilo, YAML,
publicação, conteúdo didático, taxonomia nem índice Dataview.
`/mednotes:link` é o dono de todo reparo de grafo; curadoria semântica é fase
interna de /mednotes:link, não tarefa manual final para o usuário.

Fonte operacional: `~/.gemini/medical-notes-workbench/vocabulary.sqlite`.
YAML `aliases` é projeção humana do DB, não autorização suficiente para linkar
corpo. `CATALOGO_WIKI.json`, quando existir, é legado/apoio; não é fonte
primária de decisão semântica.

Contrato público: `run-linker --diagnose --json` lê Wiki/Git/DB/aliases/links,
export do Related Notes e estado do grafo, salva `link-diagnosis.json` e não
muta Markdown. `run-linker --diagnose --no-related-notes --json` pula
`related_notes_sync` para o comando body-only. Workflows que mudaram notas
passam `--trigger-context <json>`. `run-linker --apply --diagnosis <json>
--json` consome o diagnóstico salvo, revalida snapshot/hash/Git e bloqueia se
estiver stale. Se o diagnóstico contém `wiki_dir`, `catalog_path` e
`vocabulary_db_path`, o apply usa esses caminhos quando overrides explícitos não
forem passados. `--receipt` nunca sobrescreve arquivo existente; nesse caso o
workflow bloqueia com `receipt_path_exists`. Apply nunca chama LLM.
O diagnóstico grava `last_diagnosis_attempt` em `link-state.v2`; repetição sem
mudança pode retornar `skipped_reason=redundant_diagnosis_without_state_change`.
Se o vocabulary DB ainda não existe, o diagnóstico registra
`vocabulary_bootstrap.status=planned` e bloqueia `body_term_linker` com
`vocabulary_bootstrap_required` quando houver notas a ingerir; criar SQLite,
limpar aliases/WikiLinks antigos e enfileirar notas é responsabilidade de um
apply workflow-aware do próprio `/mednotes:link` ou de um workflow que o chama,
como `/mednotes:fix-wiki`.
No apply, `vocabulary_semantic_repair` resolve a fila simples com meanings por
título/nota, policy direta só para título e aliases contextuais; depois força
novo diagnóstico e aplica. Só bloqueia em decisão humana, conflito ou erro real.

Fases fixas:

1. `reference_repair`
2. `contextual_alias_disambiguation`
3. `body_term_linker`
4. `related_notes_sync`
5. `graph_validation`

Regras semânticas:

- `1 meaning canônico = 1 nota Wiki`.
- Atomicidade/split segue `atomicity-splitting-policy.md`: o DB decide a partir
  de `semantic_signal` do corpo, nunca de title-only signal; só
  `split_required` vira `deferred_work_items.status=pending`.
- Várias notas para o mesmo meaning é duplicata/merge.
- `direct` só linka quando uma surface tem um meaning e uma nota canônica.
- `requires_context` exige decisão por ocorrência. Exemplo: `PCR` pode ser
  Proteína C Reativa, Parada Cardiorrespiratória ou Reação em Cadeia da
  Polimerase, mesmo se o vault ainda só tiver uma candidata.
- Matches seguros de único alvo canônico são resolvidos pelo script. Ambiguidades
  médicas reais exigem orquestração oficial por agente/subagent; o script não
  abre `gemini -p` escondido. Alvo inventado, confiança baixa ou contexto
  insuficiente vira `defer`/`no_link`.

Body linker usa scanner Aho-Corasick, preserva YAML, headings, code, imagens,
embeds, footer e `## 🔗 Notas Relacionadas`, exceto quando a fase dela roda.
Qualidade do body linker é medida por `evaluate-body-linker` com fixtures
redigidas; falso positivo deve bloquear o gate.

`reference_repair` usa trigger context ou Git para `created`, `modified`,
`deleted`, `renamed`, `moved` e `merged`. Rename/move/merge validado reescreve
links entrantes; delete sem substituto vira `structural_deleted`.

Related Notes é seção gerenciada pelo linker: remover só o bloco abaixo de
`## 🔗 Notas Relacionadas`, preservar as demais seções finais e reescrever a partir do export
estável do plugin (`score >= 0.78`, teto configurado, ordem de relevância).
O recovery oficial do export tenta primeiro o comando do plugin via Obsidian
CLI. Se a CLI não existir, mas o plugin estiver instalado no vault, o Workbench
pode reconstruir `index.json` e `medical-notes-export.json` pela rota headless
compatível com o plugin, lendo a chave do `data.json` sem expor segredo. O
export público nunca carrega API key, embeddings, cache interno, Markdown bruto
ou conteúdo clínico; falha de quota vira
`related_notes_headless_quota_exhausted`. Execução longa demais vira
`related_notes_headless_time_budget_exhausted`, com índice parcial salvo e
retomada oficial. A rota headless impõe no mínimo 10s entre chamadas externas
de embedding, mesmo se `embeddingRequestDelayMs` vier menor, zero ou ausente.
O orçamento padrão de execução headless é 120s e pode ser ajustado por
`MEDNOTES_RELATED_NOTES_HEADLESS_MAX_SECONDS` em testes/automação.
Quando a rota headless já salvou parte do índice, o blocker deve carregar
payload tipado de progresso Related Notes com total, restante, janela de retry e
`resume_supported=true`. Se o bloqueio for quota externa ou orçamento de tempo,
o plano deve deixar claro que a retomada é posterior (`executable_now=false`),
não continuação imediata; isso não é decisão humana nem instrução para o usuário
editar Related Notes manualmente.
O linker não mantém índice Dataview. Notas operacionais marcadas com
`indice`/`índice` são ignoradas no grafo/linker e devem preservar queries,
code blocks e forma operacional própria.

Proveniência não é Related Notes: `chats[]` é metadata consultável da fonte e
`## 🧬 Fontes Consolidadas` é a seção visível final, gerenciada pelos workflows
de publicação/fix-wiki. O linker não cria nem apaga `chats[]`.
Export stale usa `related-notes-sync --recover-export --mode auto --json`.
Se Related Notes for o único blocker e body/reference estiverem seguros,
`run-linker --diagnose` inclui `body_only_fallback.safe=true`; caso contrário,
`safe=false`.
Quando `fix-wiki --apply` não consegue recuperar o export, mas os blockers de
grafo estão dentro da seção gerenciada, ele pode registrar
`related-notes-safety-cleanup.v1`: remove apenas WikiLinks inválidos já
presentes em `## 🔗 Notas Relacionadas`, preserva links existentes que ainda
resolvem para uma única nota e mantém `related_notes_blocked` até o plugin gerar
um export fresco. Essa limpeza não calcula embeddings nem novas relações.

Recibos/diagnósticos não gravam Markdown clínico bruto nem diff textual. O
`med-link-graph-curator` mantém meanings, aliases, policies e work items; ele
não edita Markdown diretamente e não chama outro subagente.
Para `deferred_work_items[].reason=non_atomic_note`, use
`atomicity-splitting-policy.md`: `semantic_signal` deve trazer evidência do
corpo, relação/fragmento e estimativa dos filhos; sem isso o apply bloqueia como
`semantic_ingestion.atomicity_signal_required`.
O parent passa `agent-work-packet.v1`: path/hash/DB, ações, stop conditions,
`difficulty_route`, rubrica e contrato. `simple_atomic` é curto;
`complex_semantic_review` classifica/defer riscos; `blocked_preflight` para
antes de gastar tokens. `plan_hash` é corpus-only sem runtime; `prompt_identity`
valida no apply.
Quando o diagnóstico emitir `vocabulary_curator_batch_plan_path`, o apply ainda
deve tentar `vocabulary_semantic_repair`; se sobrar pendência humana, continue a
cadeia dentro do `/mednotes:link` e não encerre como próximo passo manual.
Depois dos outputs, gere manifest com `collect-curator-outputs`; não escreva
manual. Rode `eval-curator-batch --report <report> --json`.
O gate emite `curator-prompt-eval.v1` e bloqueia alias amplo, vazamento, rota
complexa sem defer/split, orçamento excedido ou falta de `agent_metrics`.
Apply real exige `--prompt-eval <report.json>`; `--skip-prompt-eval` exige
`MEDNOTES_ALLOW_DEV_ESCAPE=1` e `--skip-prompt-eval-reason`, e registra
`agent.curator_prompt_eval_skip`.
