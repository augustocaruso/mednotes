# Dicionário Público (Internal → User)

Tradução canônica para resposta visível em workflow público. "Não dizer" é
proibido por padrão; "Dizer" é a forma humana. Termos internos só aparecem
em `<details>` técnicos quando o usuário pediu, em `/mednotes:status`, em
debug explícito, ou na seção `Detalhes técnicos` ao final do relatório.

Workflows que exigem UX "just works" (esconder internals por padrão):
`/mednotes:create`, `/mednotes:enrich`, `/mednotes:fix-wiki`,
`/mednotes:link`, `/mednotes:link-body`, `/mednotes:link-related`,
`/mednotes:process-chats`, `/mednotes:pdf-library`, `/flashcards`.

Workflows mistos (mais técnico no default): `/mednotes:setup`,
`/mednotes:status`, `/mednotes:history`, `/mednotes:telemetry`, `/report`.

## Execução E Ambiente

| Interno | Não dizer | Dizer |
|---|---|---|
| `uv run python ...` / `uv` | "rodei `uv`", "vou rodar `uv run`" | "preparei o ambiente e rodei", "executei" |
| `--dry-run`, dry-run | "rodei `--dry-run`", "dry-run limpo" | "fiz uma prévia", "nada foi alterado ainda" |
| `--apply`, apply | "vou rodar apply" | "vou aplicar" / "aplico se você confirmar" |
| receipt / recibo (técnico) | "gerei recibo `<path>.json`" | "registrei o que foi feito" / "guardei ponto de restauração" |
| manifest / batch | "manifest do batch" | "lote" só se precisar contar; preferir "essas 4 notas" |
| hash / sha256 / `content_hash` | "hash bate", "sha256=…" | omitir; ou "identifiquei a versão da nota" |
| schema / schema drift / plan v4/v5 | "schema drift", "plano v5" | "o formato esperado mudou; vou recalcular" |
| `Exit Code:` | "Exit Code 1" | "o comando falhou: <causa curta>" |
| `tool status=success` | "tool status success" | omitir; falar do resultado |
| `agent_metrics` / `turns_used` / `max_turns` | "12 turns used" | omitir; em bloqueio: "o agente esgotou tentativas; precisa de revisão" |

## Estado E Decisão

| Interno | Não dizer | Dizer |
|---|---|---|
| `next_action` / `next_command` | "next_action", "next_command" | "próxima ação" |
| `human_decision_required` | "human decision required" | "preciso de uma decisão sua" |
| `human_decision_packet` | "packet", "opções do packet" | "opções para você escolher" |
| `blocked_reason` / `status=blocked` | "status=blocked", "blocked_reason=…" | "bloqueio: <causa em pt-BR>" |
| `preview_ready` / `ready_to_publish` | "preview_ready", "ready_to_publish" | "prévia pronta", "pronto para publicar" |
| `phase` (ex. `architect`, `triage`) | "fase architect" | "etapa atual: arquitetura/triagem" |
| `contract_gap` / `error_context` | "contract_gap.missing_next_action" | "o sistema não me deu próxima ação clara; preciso de orientação sua" |

## Armazenamento

| Interno | Não dizer | Dizer |
|---|---|---|
| SQLite / vocabulary DB / DB | "SQLite", "DB de vocabulário" | "índice de vocabulário" |
| `vocabulary_bootstrap` / `bootstrap_required` | "bootstrap_required" | "o índice de vocabulário precisa ser construído" |
| `link-trigger-context.v1` | "link trigger context" | omitir; o linker é dito como "reparei as conexões entre notas" |
| `images_*` frontmatter | "frontmatter `images_*`" | "imagens da nota" |

## Versionamento E Vault

| Interno | Não dizer | Dizer |
|---|---|---|
| restore point / `restore_point_id` / commit | "commit", "branch", "SHA `<abc>`" | "ponto de restauração" |
| Git / push / `sync_status` | "git push", "sync_status=skipped" | "backup online" / "backup online pendente" |
| `run-start` / `run-finish` / vault guard | "rodei `run-start`" | omitir; protegido por "ponto de restauração" |

## Bypass Técnico (Nunca Em UX Pública)

| Interno | Não dizer | Dizer |
|---|---|---|
| `MEDNOTES_ALLOW_DEV_ESCAPE` | nunca em UX pública | nunca em UX pública |
| `--skip-prompt-eval` | nunca em UX pública | nunca em UX pública |
| `--force-diagnose` | nunca em UX pública | nunca em UX pública |

## Eval E Avaliação

| Interno | Não dizer | Dizer |
|---|---|---|
| `eval-curator-batch` / evaluator | "evaluator devolveu needs_review" | "a verificação automática marcou itens para revisão" |
| `needs_review` | "needs_review" | "preciso revisar antes de aplicar" |
| `prompt-eval` / curator-prompt-eval.json | "curator-prompt-eval" | omitir; preferir "verificação do plano" |

## Agentes E Subagentes

| Interno | Não dizer | Dizer |
|---|---|---|
| `med-link-graph-curator` | nome do agente | "etapa de curadoria de vocabulário" |
| `med-knowledge-architect` | nome do agente | "etapa de reescrita" |
| `med-chat-triager` | nome do agente | "etapa de triagem do chat" |
| `med-publish-guard` | nome do agente | "verificação antes de publicar" |
| `med-flashcard-maker` | nome do agente | "etapa de geração de cards" |
| `@generalist` / subagent genérico | mencionar | nunca (rota oficial não usa) |

## Quando Internals Podem Aparecer

- Sempre em `<details>` recolhido com cabeçalho "Detalhes técnicos" (camada
  visível padrão continua humana).
- Em `/mednotes:status`, `/mednotes:setup`, `/mednotes:telemetry`,
  `/mednotes:history` no resumo principal quando o usuário pediu inspeção.
- Em debug explícito (usuário pediu "mostre o JSON", "rode com `--json`",
  "explique o erro técnico").

## Anti-Padrão Recorrente (Run Real)

Em `/mednotes:fix-wiki`, foi exposto ao usuário: "batch de curadoria",
"plano v5", "schema drift", "hash mismatch", "uv run", "manifest",
"needs_review" e "skip-prompt-eval". Tradução correta:

> "Fiz uma prévia da Wiki. Encontrei um conflito de versão e preciso da sua
> decisão antes de continuar. Nada foi alterado ainda. Próxima ação:
> <frase curta>."
