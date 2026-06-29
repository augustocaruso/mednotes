# Merge Policy

Política canônica de merge para notas da `Wiki_Medicina`. Pareada com
`agent-role-contracts.md`, `triage-policy.md` e `semantic-linker.md`.

Regra-âncora:

> 1 meaning canônico = 1 nota Wiki.

## Onde Cada Camada Decide

- **Identidade na triagem** vive como `meaning_claim` na ação
  `planned_meaning` do `triage-note-plan.v2`. O triager declara meaning a
  partir do raw chat; ele não consulta a Wiki nem o vocabulary DB.
- **Identidade no DB** vive como `primary_meaning` na curadoria do
  `med-link-graph-curator`. Curator lê notas publicadas e consolida
  meaning/aliases/surfaces no vocabulary DB. Pode propor `NoteMergeCandidate`,
  nunca aplicar merge.
- **Identidade no plano de execução** vive como `target_policy` nos work
  items do planner (`new_note`, `canonical_rewrite`, `note_merge_candidate`
  ou `blocked`). O planner cruza `meaning_claim` contra Wiki e DB.

## Único Merge Permitido: `note_merge`

A única API de merge entre notas publicadas é
`bundle/scripts/mednotes/wiki/note_merge.py`. Caminho canônico:

```text
identidade semântica explícita / curator / decisão humana
  → NoteMergeCandidate
  → NoteMergePlan
  → med-knowledge-architect rewrite (com preservation_report)
  → apply-note-merge --dry-run
  → apply-note-merge
  → link trigger + reference repair
```

Hash de fontes prova estabilidade de input, não preservação clínica. O
`preservation_report` do architect é o gate de preservação.

## Proibições

- **Sem merge title-driven**: detectores `duplicate_stem`, `duplicate_title`
  ou similar não podem alimentar merge automático. Título parecido vira
  diagnóstico de higiene no `fix-wiki`, nunca candidato.
- **Sem `duplicate_merge` API**: `bundle/scripts/mednotes/wiki/duplicate_merge.py`
  e `fix-wiki --phase duplicate-merge` estão deprecados; o caminho novo é
  `note-merge`.
- **Sem merge no architect**: architect rewrites a nota canônica quando
  recebe `target_policy=canonical_rewrite` ou um work item de
  `note_merge_candidate`, mas nunca decide identidade nem escolhe winner.
- **Sem merge no triager**: triager não declara que duas unidades do mesmo
  raw são o mesmo meaning de notas existentes; ele só descreve unidades
  daquele raw.
- **Sem merge silencioso**: quando há ambiguidade real, o planner emite
  `human_decision_packet`; nada de chute por similaridade textual.

## Como `meaning_claim` Resolve

Para cada `planned_meaning` do triager, o planner faz uma lookup contra
`vocabulary DB` (camada de identidade canônica) e contra a Wiki publicada.
Saídas determinísticas:

- **Match direto** (DB tem `primary_meaning` equivalente e Wiki tem nota
  canônica): `target_policy = canonical_rewrite` apontando a nota existente.
- **Match com divergência** (DB indica meaning igual a uma nota, mas outra
  nota publicada também aparenta cobrir): `target_policy = note_merge_candidate`
  com referência ao candidato emitido pelo curator ou bloqueio para decisão
  humana.
- **Sem match** (DB não tem meaning equivalente): `target_policy = new_note`
  com path resolvido a partir de taxonomia + `staged_title`.
- **Ambiguidade real** (claim conflita com mais de um canonical ou DB não
  consegue cravar): `target_policy = blocked` com
  `human_decision_packet`.

Nenhuma dessas decisões usa título ou stem como chave primária. A chave é o
`primary_meaning` no DB e o `meaning_claim` no plano.

## Provenance Final

Toda nota canônica com chats conhecidos termina com
`## 🧬 Fontes Consolidadas` aplicada por `wiki.provenance`. O rodapé legado
`Chat Original` é deprecado e removido pelo backfill interno de
`/mednotes:fix-wiki`.

## Aplicabilidade Universal

- `/mednotes:process-chats` cria nota nova ou rewrite canônico usando
  `meaning_claim` da triagem, provenance via `wiki.provenance`, sem
  `duplicate_merge`. A resolução de `meaning_claim` pelo planner
  determinístico é a direção canônica (implementação em plano separado).
- `/mednotes:fix-wiki` normaliza o acervo antigo (backfill de `chats[]`,
  remoção do rodapé legado, sincronização de `Fontes Consolidadas`) e gera
  diagnósticos de higiene, mas **não** aplica merge title-driven.
- `/mednotes:link` opera somente em grafo/linker; nunca faz merge.
- Note merge real só roda via fase própria, alimentada por
  `NoteMergeCandidate` semanticamente justificado.

## Migração De Candidatos Legados

Candidatos `duplicate_stem` ainda persistidos no vocabulary DB precisam ser
revisados antes do detector ser removido. O caminho é um job de curator que:

- relê cada candidato;
- propõe `NoteMergeCandidate` quando há identidade semântica real (com
  evidência citável do DB ou da nota);
- descarta com motivo redigido quando o candidato era ruído title-driven.

Sem essa migração, candidatos legados ficam órfãos e a remoção do detector
perde trabalho.
