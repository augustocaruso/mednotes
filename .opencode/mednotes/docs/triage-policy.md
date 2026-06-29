# Triage Policy

Política editorial para o `med-chat-triager`. Define como dividir um único
raw chat em unidades semânticas duráveis sem decidir nada sobre a Wiki
existente. Pareado com `agent-role-contracts.md` e `merge-policy.md`.

Regra-âncora:

> 1 meaning canônico = 1 nota Wiki. O triager declara meanings; ele não
> decide se eles já existem.

## Ações Permitidas (`triage-note-plan.v2`)

### `planned_meaning`

Use para cada unidade médica durável que merece virar trabalho downstream
(nota nova, rewrite canônico ou merge candidato — quem decide é o planner,
não o triager).

Critérios:

- representa um conceito médico atômico, com escopo definível em uma frase;
- o raw chat fornece evidência suficiente para descrever esse escopo;
- a unidade tem `meaning_claim` com `label`, `scope`, `boundaries`, `kind` e
  `evidence_summary` redigido;
- `staged_title` é um nome de arquivo limpo em pt-BR (sem path separators,
  caracteres proibidos Windows, controle, ponto/espaço final).

Granularidade:

- não fragmente abaixo da unidade semântica (ex.: não emita
  `Mecanismo de ação dos ISRS` separado de `ISRS` quando o raw discute apenas
  ISRS como um todo);
- não infle granularidade (ex.: não emita uma nota por critério de
  diagnóstico isolado quando o raw discute o quadro completo);
- preferir uma unidade ampla com `attach_to_planned_meaning` em vez de
  várias unidades pequenas mal sustentadas.

### `attach_to_planned_meaning`

Use para informação útil subordinada a outra unidade do mesmo raw chat.
Requer `target_item_id` apontando para uma `planned_meaning` válida do mesmo
plano.

`reason_code` fechado:

- `supporting_detail`: detalhe clínico/farmacológico que reforça a unidade
  alvo;
- `boundary_clarification`: discussão que ajuda a delimitar o escopo da
  unidade alvo;
- `example_or_case`: exemplo/caso que ilustra a unidade alvo;
- `cross_reference`: menção breve que aponta a unidade alvo para outra
  unidade do mesmo raw.

`attach_to_planned_meaning` não vira nota separada. Ele é dica para o
architect agregar conteúdo dentro da unidade alvo.

### `not_a_note`

Use para conteúdo do raw chat que não deve virar nota.

`reason_code` fechado:

- `administrative_chatter`: pedido operacional, metacomentário, saudação;
- `repetition_no_new_information`: repete unidade já planejada sem novidade;
- `out_of_scope_for_medical_wiki`: assunto fora do escopo da Wiki médica;
- `low_value_fragment`: fragmento sem conteúdo médico aproveitável.

`reason` em texto livre é obrigatório e deve ser redigido e curto.

### `needs_context`

Use quando o raw chat **não permite segmentação semântica segura** para a
unidade em questão. Não é dúvida sobre cobertura existente: é dúvida sobre
identidade da unidade no próprio raw.

`reason_code` fechado:

- `evidence_insufficient`: o raw chat não traz texto suficiente para
  sustentar `meaning_claim.scope`/`boundaries`;
- `multiple_topics_undifferentiated`: o raw mistura múltiplas unidades sem
  que o triager consiga separá-las com segurança a partir do texto;
- `clinical_ambiguity`: contradição clínica no próprio raw que precisa de
  revisão humana antes de virar nota;
- `language_or_encoding_blocker`: o raw está corrompido, truncado ou em
  formato que o triager não consegue ler.

`needs_context` exige `reason` redigido. Pode existir junto de
`planned_meaning`/`attach_to_planned_meaning` (planos mistos são permitidos);
um plano inteiro só com `needs_context` é também válido e cabe ao planner
decidir entre re-triagem manual e bloqueio.

## Proibições

- Não declarar que uma nota já existe na Wiki. Isso é decisão do planner
  contra Wiki/vocabulary DB.
- Não emitir `winner_path`, `merge_target` ou similar.
- Não consultar vocabulary DB como autoridade.
- Não usar título de nota Wiki existente como identidade — `meaning_claim`
  vive em texto, não em filename.
- Não pedir decisão humana genérica. Se a dúvida é editorial, usar
  `needs_context` com `reason_code` específico.

## Forma De `meaning_claim`

```json
{
  "label": "Uso de ISRS em gestantes",
  "scope": "seguranca, contraindicacoes e conduta clinica na gestacao",
  "boundaries": [
    "nao cobre mecanismo geral dos ISRS",
    "nao cobre depressao puerperal como entidade separada"
  ],
  "kind": "clinical_concept",
  "evidence_summary": "O chat discute risco e conduta de ISRS na gestacao."
}
```

Campos:

- `label`: nome humano da unidade. Curto.
- `scope`: o que está dentro da unidade. Uma frase.
- `boundaries`: 1–3 itens em lista descrevendo o que parece relacionado mas
  fica fora. Lista vazia é permitida apenas quando a unidade não tem vizinho
  próximo no raw.
- `kind`: tipo operacional fechado:
  - `clinical_concept` (entidades clínicas, síndromes, manifestações);
  - `drug_concept` (fármaco/classe);
  - `diagnostic_criterion` (critério/escore);
  - `management_strategy` (conduta/protocolo);
  - `procedure` (procedimento);
  - `physiology_or_mechanism` (mecanismo/fisiologia);
  - `epidemiology_or_definition` (definição/epidemiologia).
- `evidence_summary`: resumo operacional **redigido** do que no raw sustenta
  a unidade. Sem conteúdo clínico bruto longo, sem citação direta extensa.

## Critérios Editoriais Curtos

- Se o raw aborda dois assuntos vizinhos com escopo claro e evidência
  separada → duas `planned_meaning` com `boundaries` apontando uma para a
  outra.
- Se o raw aborda um assunto com vários detalhes que enriquecem aquela
  unidade → uma `planned_meaning` + N `attach_to_planned_meaning`.
- Se o raw é principalmente operacional ou administrativo → o plano só
  contém `not_a_note`. Plano todo `not_a_note` é válido e marca o raw como
  descarte editorial.
- Se o raw é claramente médico mas o triager não consegue separar com
  segurança → uma ou mais entradas `needs_context` com `reason_code` exato.

## O Que A Triagem Não Decide

- Existência prévia na Wiki.
- Path canônico ou taxonomia final.
- Quem é "winner" entre dois meanings parecidos.
- Se duas unidades de raws diferentes representam o mesmo meaning.

Tudo isso é responsabilidade do planner Python e/ou curator.

## Exemplos Curtos

Correto — granularidade ampla com anexos:

```text
T001 planned_meaning  ISRS na gestação
T002 attach_to_planned_meaning → T001 (supporting_detail) farmacocinética
T003 attach_to_planned_meaning → T001 (boundary_clarification) puerpério não cobre
```

Errado — fragmento abaixo da unidade:

```text
T001 planned_meaning  Mecanismo de ação dos ISRS
T002 planned_meaning  Meia-vida dos ISRS
T003 planned_meaning  Recaptação de serotonina pelos ISRS
```

Correto — descarte editorial:

```text
T001 not_a_note (administrative_chatter) Pedido operacional sem conteúdo médico.
```

Correto — raw insuficiente:

```text
T001 needs_context (evidence_insufficient) Discussão fragmentada sobre HAS sem fechamento de escopo.
```
