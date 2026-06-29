# Domínio Wiki (`mednotes.domains.wiki`)

Bounded context da Wiki Médica dentro da extensão. Reorganizado na Fase 4c em
**flows × capabilities × contracts** (ver `docs/migration/0005-arvore-ddd-spec.md` §8).

## Portas públicas (não mude a superfície sem atualizar os testes de contrato)

- `cli.py`: única porta de **execução** dos workflows Wiki (parser + dispatch).
  Preserva subcomandos como `fix-wiki`, `publish-batch`, `taxonomy-migrate`,
  `graph-audit`, `run-linker`, `related-notes-sync`, `plan-subagents`. O shim fino
  invocável vive em `bundle/scripts/mednotes/wiki/cli.py`; este aqui é a LIB.
- `api.py`: única porta **programática** para código Python fora deste pacote.
  Scripts externos importam `mednotes.domains.wiki.api`, nunca módulos internos.
- `common.py` / `config.py` / `batch_state.py` / `performance.py`: primitivas e
  estado do domínio compartilhados entre flows e capabilities.

## `flows/` — a UI (os workflows públicos `/mednotes:X`)

Orquestradores dirigidos por FSM. **Flows são a interface**; eles *orquestram*
capabilities, não contêm a lógica de verbo. (Por isso `publish`/`style` NÃO estão
aqui — viraram capabilities; correção do dono, spec 0005 §8.)

- `process_chats/`: `/mednotes:process-chats` (calibrado — mexer por último).
- `fix_wiki/`: `/mednotes:fix-wiki` — inclui `health.py` (orquestração do fix-wiki).
- `link/`: `/mednotes:link` — `linking.py` (linker moderno: vocabulary DB, reparo de
  referências, body linker, Related Notes, recibo) e `reference_repair.py`.
- `enrich/`: `/mednotes:enrich` — `workflow/` procedural (dívida FSM conhecida).

## `capabilities/` — os verbos que os flows orquestram

- `notes/`: modelo de nota, `note_plan`, `note_style/`, `note_merge`, `provenance`,
  `raw_chats`, `meaning_planner`, `note_iter`.
- `vocabulary/`: `taxonomy/` (schema, normalização, resolução, auditoria, migração,
  rollback), `link_terms`, curadoria/bootstrap de vocabulário.
- `graph/`: `graph.py` (auditoria de grafo, WikiLinks, catálogo, aliases),
  `graph_fixes.py`, `coverage.py`.
- `body_link/`, `related_notes/`: linkagem de corpo e seção `## 🔗 Notas Relacionadas`.
- `illustrate/`: enrich foldado (`core` + `sources` + `anchors`) — capability de imagem.
- `pdf/`: pdf_library foldado — ingest/search/insert de figuras de PDF.
- `markdown/`: `markdown_query.py` + `markdown_db_adapter.mjs` (o `.mjs` mora ao lado
  do `.py` e é resolvido por `with_name` — não separe os dois).
- `publish/`, `style/`: staging/publish e validação/correções formais de notas
  (capabilities orquestradas pelos flows, não a UI).
- `quality/`: `*_eval` / `*_validation` / `audit` / `corpus` em runtime (o agent usa —
  não confundir com `tests/`).
- `specialist/`, `subagents/`, `atomicity/`, `hygiene/`, `effects/`.

## `contracts/` — schemas, DTOs e a API tipada de decisão

`schema_registry` (a ÚNICA ponte wiki↔flashcards — EXCEÇÃO REGISTRY do gate de
camadas), `effect_payloads`, `curator`, `note_plan`, `publish`, `specialist`,
`style_rewrite`, `related_notes(+runtime)`, `agents`, e a stack de workflow:
`workflow_outcomes` (decision API tipada), `workflow_guardrails`, `workflow_blockers`,
`workflow_receipts`.

## Regras que os gates fixam

- Resolução de path centralizada em `mednotes.platform.paths.extension_root()` — sem
  `Path(__file__).parents[N]`. Mover um módulo de profundidade não pode quebrar nada.
- O gate de camadas DDD (`tools/audit/import_layering.py`) proíbe a wiki de importar
  outro bounded context (exceto `schema_registry`) e proíbe platform/kernel de importar
  domínio.
- A superfície pública (`api.py`, `cli.py`) é coberta por `test_med_pipeline_streamline`
  e `test_architecture_contracts` — atualize-os junto com qualquer move.
