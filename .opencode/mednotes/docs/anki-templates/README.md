# Anki Card Templates

Templates oficiais dos modelos Anki gerenciados pela Medical Notes Workbench.
Os arquivos aqui são fonte de verdade — `flashcards/install_models.py` lê este
diretório para montar os payloads de `mcp_anki-mcp_createModel`,
`mcp_anki-mcp_updateModelTemplates` e `mcp_anki-mcp_updateModelStyling`.

## Modelos

### `Medicina` — Q&A
- Tipo: básico (não cloze).
- Campos: `Frente`, `Verso`, `Verso Extra`, `Obsidian`.
- Templates: `qa.front.html`, `qa.back.html`.

### `Medicina Cloze` — Cloze
- Tipo: cloze (`isCloze: true`).
- Campos: `Texto`, `Verso Extra`, `Obsidian`.
- Templates: `cloze.front.html`, `cloze.back.html`.
- O campo `Texto` é o cloze field (`{{cloze:Texto}}` nos templates) e carrega
  `{{c1::...}}`, `{{c2::...}}` etc.

## CSS compartilhado

`style.css` é único para os dois modelos. Mantém tipografia/espaçamento
consistente entre Q&A e Cloze, com suporte a `nightMode`.

Convenções para evitar colisão com Anki/outros modelos: tudo é prefixado com
`mnw-` (Medical Notes Workbench), exceto `.cloze` (classe que o próprio Anki
injeta nos clozes; estilizamos só dentro de `.mnw-cloze`).

## Como atualizar

1. Edite os arquivos HTML/CSS aqui.
2. A skill `create-medical-flashcards` deve, no boot do `/flashcards`, chamar
   `install_models.py ensure --output -` e mandar o payload resultante para o
   Anki MCP (`createModel` se faltar, `updateModelTemplates` +
   `updateModelStyling` se já existir e estiver desatualizado).
3. Não edite os modelos manualmente no Anki Desktop — qualquer mudança ali
   é sobrescrita no próximo run.
