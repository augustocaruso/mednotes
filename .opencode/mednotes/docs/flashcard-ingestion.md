# Flashcard Ingestion Design

Doc e fonte unica de regras locais de ingestao p/ flashcards medicos no Anki. Metodologia vive em `${extensionPath}/docs/anki-mcp-twenty-rules.md`, copia operacional do prompt MCP `/twenty_rules` do servidor `anki-mcp`. Copia local existe pq subagents Gemini CLI nao chamam slash prompts MCP. Doc define decisoes de design da extensao Medical Notes Workbench.

## Contrato Runtime

`/flashcards` e FSM-first. Agentes e consumidores devem orientar o fluxo por
`progress_view_model`, `state_machine_snapshot`, `decision`, `receipt`,
`reports` e `agent_directive.control`, nao por conclusoes soltas do agente.
Quando existir, `diagnostic_context` e apenas evidencia opcional para explicar
problema, retry, bloqueio ou investigacao; ele nao escolhe rota nem autoriza
efeito.

## Modelos Anki Gerenciados

Skill mantem **dois** note types no Anki, provisionados via Anki MCP de `${extensionPath}/docs/anki-templates/`:

- `Medicina` (Q&A, `isCloze: false`) — campos `Frente`, `Verso`, `Verso Extra`, `Obsidian`. Cards pergunta/resposta classica.
- `Medicina Cloze` (cloze, `isCloze: true`) — campos `Texto`, `Verso Extra`, `Obsidian`. Campo `Texto` carrega `{{c1::...}}`, `{{c2::...}}`. Um card por grupo de cloze.

**Roteamento por card:** subagent decide modelo por candidato. Use `Medicina Cloze` p/ definicao/fato encadeado/enumeracao curta (Twenty Rules #5, #9). Use `Medicina` p/ pergunta com resposta atomica. Cada `candidate_card` deve declarar `note_model`.

**Provisao dos modelos:** antes de gravar, skill chama `mcp_anki-mcp_modelNames` + `mcp_anki-mcp_modelFieldNames` e roda:

```bash
uv run python ${extensionPath}/scripts/mednotes/flashcards/install_models.py ensure --existing - --output -
```

JSON resultante traz lista `actions` com `mcp_anki-mcp_createModel` (modelo ausente) ou `mcp_anki-mcp_updateModelTemplates` + `mcp_anki-mcp_updateModelStyling` (HTML/CSS divergiu). Se modelo aparecer como `incompatible` (mesmo nome, campos diferentes), pare e peca usuario apagar/renomear no Anki Desktop.

Arquivos HTML/CSS em `${extensionPath}/docs/anki-templates/` sao fonte de verdade. Nao edite modelos no Anki Desktop — alteracoes sao sobrescritas no proximo run.

## Regras De Conteúdo Para Cards Bonitos

Templates dao consistencia visual; conteudo limpo e responsabilidade da skill. Aplique sempre:

- **Frente Q&A:** pergunta atomica, ate 120 chars. Sem ponto-final desnecessario, sem prefixo "Pergunta:".
- **Verso Q&A:** 1-2 frases curtas, direto. Nao repita pergunta, nao comece com "A resposta e".
- **Verso Extra (ambos):** raciocinio, contexto, mnemonicos, fontes curtas. Comece com `\n\n` (texto puro) ou `<br><br>` (HTML). Use bullets `<ul><li>` ou `-`. Nunca repita Verso/cloze.
- **Cloze (`Texto`):** enunciado fluente. Max 2-3 grupos `{{cN::...}}` por card. Cada cloze = unidade atomica; nunca paragrafo inteiro. Mantenha contexto suficiente p/ leitura com sentido.
- **Obsidian:** deeplink copiado do manifest tipado da fonte. Dentro de vault
  confiavel, prefira `obsidian://open?vault=Wiki_Medicina&file=Cardio%2FPonte.md`
  com path POSIX relativo ao vault. Fora de vault confiavel, use fallback de
  path real como
  `obsidian://open?path=%2FUsers%2Fleo%2FWiki%20Medicina%2FCardio%2FPonte.md`.
  Template renderiza como botao "Abrir no Obsidian".
- **Sem markdown solto:** evite headings (`#`, `##`), negrito Markdown (`**...**`), codigo com crase. Use HTML p/ enfase (`<strong>`, `<em>`, `<code>`); Anki nao converte Markdown.

## Especificacoes De Design

1. Hierarquia de decks: reproduza estrutura de diretorios do Obsidian como subdecks no Anki.

   Exemplo:

   ```text
   Wiki_Medicina/Cardiologia/Ponte_Miocardica.md
   -> Wiki_Medicina::Cardiologia::Ponte_Miocardica
   ```

   Para arquivos em `Wiki_Medicina`, use `Wiki_Medicina` como raiz, preserve diretorios intermediarios, nome do arquivo sem `.md` como folha.

   Nao achate hierarquia. Se `mcp_anki-mcp_createDeck` recusar mais de dois niveis, tente criar cards diretamente no deck completo com `mcp_anki-mcp_addNotes`/`mcp_anki-mcp_addNote`; se ainda recusar, reporte falha sem trocar deck.

2. Tags Anki: nao adicionar tags. Omita `tags` ou envie lista vazia. Tags Obsidian podem selecionar notas mas nao viram tags Anki.

3. Formatacao do campo: antes de inserir em `Verso Extra`, adicione espaco visual no inicio — `\n\n` (texto puro) ou `<br><br>` (HTML).

4. Campo de origem: todo card de nota Markdown usa o deeplink do
   `FlashcardSourceManifest`. O agente copia `fields.Obsidian` do manifest ou
   deixa vazio para o pipeline tipado preencher; ele nunca fabrica uma URI.

   ```bash
   uv run python ${extensionPath}/scripts/mednotes/obsidian_note_utils.py deeplink <nota.md>
   ```

   O comando acima e utilitario tecnico para diagnostico/geracao do manifest;
   nao substitui o manifest tipado. Dentro de vault, o link preferido e
   `obsidian://open?vault=Wiki_Medicina&file=Cardio%2FPonte.md`, com path POSIX
   vault-relativo. Fora de vault confiavel, o fallback e
   `obsidian://open?path=%2FUsers%2Fleo%2FWiki%20Medicina%2FCardio%2FPonte.md`,
   usando path real resolvido. Nao dependa da Obsidian CLI p/ extrair esse link.

   Resolver pode inferir raiz do vault por `--vault-root`, `[paths].wiki_dir` em
   `~/.gemini/medical-notes-workbench/config.toml`, compatibilidade
   (`MED_WIKI_DIR`/config legado) ou diretorio `.obsidian` ancestral p/
   preencher metadata (`vault_root`, `vault_relative_path`), deck e deeplink
   preferido. `--vault-file` continua existindo como compatibilidade tecnica,
   mas o padrao de manifest dentro do vault ja prefere `vault=...&file=...`.

5. Marcacao da nota-fonte: apos pelo menos um card criado com sucesso, marque apenas essa nota com tag Obsidian `anki` via:

   ```bash
   uv run python ${extensionPath}/scripts/mednotes/obsidian_note_utils.py add-tag --tag anki <nota.md>
   ```

   Para desfazer:

   ```bash
   uv run python ${extensionPath}/scripts/mednotes/obsidian_note_utils.py remove-tag --tag anki <nota.md>
   ```

   Nao marque notas sem cards criados. Em sucesso parcial, marque so arquivos com pelo menos um card aceito.

## Regra De Base De Conhecimento

`/twenty_rules` sem namespace e reservado para prompt MCP `twenty_rules` do servidor global `anki-mcp`. Extensao nao declara outro Anki MCP no manifest (evita duplicacao com `~/.gemini/settings.json`) e nao cria comando local `/twenty_rules` (evita colisao).
Referencia upstream: `@ankimcp/anki-mcp-server/dist/mcp/primitives/essential/prompts/twenty-rules.prompt/content.md`.
Agente carrega metodologia por `read_file` em `${extensionPath}/docs/anki-mcp-twenty-rules.md`.
Comando `/flashcards` aceita arquivo, multiplos arquivos, diretorios, globs, filtros por tag Obsidian e instrucoes em linguagem natural.
Tag Obsidian `anki` e reservada p/ notas que ja geraram cards com sucesso.

Ao receber `/flashcards <escopo>`, agente deve:

1. Resolver escopo com `flashcard_sources.py resolve --scope "<escopo>" --dry-run`.
2. Usar `read_file` p/ extrair conteudo de cada arquivo em `manifest.notes[].path`.
3. Formular cards candidatos sem gravar no Anki.
4. Preparar plano com `flashcard_pipeline.py prepare`.
5. No modo padrao, mostrar cards no terminal e pedir confirmacao antes de gravar. Criacao direta so permitida quando usuario pedir explicitamente.
6. Usar exclusivamente conteudo lido desses arquivos como base (o "O QUE" dos flashcards).
7. Aplicar rigorosamente `${extensionPath}/docs/anki-mcp-twenty-rules.md` e especificacoes deste doc como "COMO".

Nao use conhecimento externo p/ acrescentar fatos. Conhecimento medico geral pode ser usado apenas p/ entender, segmentar e redigir melhor o conteudo ja presente.

## Resolucao De Escopo Para `/flashcards`

1. Use resolver deterministico antes de ler notas ou chamar subagent:

   ```bash
   uv run python ${extensionPath}/scripts/mednotes/flashcard_sources.py resolve --scope "<argumentos>" --dry-run --skip-tag anki
   ```

   Retorna JSON com `schema`, `summary`, `scope`, `notes`, `skipped_notes` e `warnings`.

2. Arquivos explicitos, diretorios e globs: resolver inclui apenas Markdown (`.md`/`.markdown`), ignora `dist/`, `.git/`, caches, anexos, imagens e nao-Markdown.
3. Tags Obsidian: resolver filtra por frontmatter `tags`/`tag` e hashtags inline. Tag e criterio de selecao, exceto marcacao pos-sucesso `anki`.
4. Pastas em linguagem natural: p/ frases como `notas com tag #revisar na
   pasta Cardiologia`, resolver procura pasta dentro de `--vault-root`, `--wiki-dir` ou `wiki_dir` de `~/.gemini/medical-notes-workbench/config.toml`; env/config legado aceitos como compatibilidade.
5. Escopo ambiguo: se resolver falhar pedindo raiz, pergunte qual vault/wiki e rode com `--vault-root <pasta>` ou `--wiki-dir <pasta>`.
6. Notas ja processadas: por padrao, `/flashcards` deve passar `--skip-tag anki` p/ evitar duplicacao. Se usuario pedir refazer/regenerar, rode sem esse filtro. Notas puladas aparecem em `skipped_notes` com `skip_reason: "skip_tag"` e `skip_tags: ["anki"]`.
7. Manifest por nota: cada item em `notes` traz `path`, `deck`, `deeplink`, `vault_relative_path`, `link_mode`, `tags`, `already_marked_anki`, `content_sha256`, `line_count` e `heading_count`. Use esses campos como fonte operacional de deck/link; leia conteudo factual separadamente com `read_file`.
8. Lotes grandes: se `summary.requires_confirmation` for verdadeiro, mostre previa e peca confirmacao antes de formular/gravar. Para previa textual:

   ```bash
   uv run python ${extensionPath}/scripts/mednotes/flashcard_sources.py preview --scope "<argumentos>" --dry-run --skip-tag anki
   ```

   `preview` usa mesma resolucao de `resolve` mas emite texto humano em vez de JSON.

## Manifest De Cards Candidatos E Idempotencia

Apos resolver fontes e ler arquivos com `read_file`, agente formula cards candidatos antes de chamar Anki MCP. Formato minimo:

```json
{
  "source_manifest": {},
  "preferred_models": {
    "qa": "Medicina",
    "cloze": "Medicina Cloze"
  },
  "models": {
    "Medicina": ["Frente", "Verso", "Verso Extra", "Obsidian"],
    "Medicina Cloze": ["Texto", "Verso Extra", "Obsidian"]
  },
  "candidate_cards": [
    {
      "source_path": "/path/nota.md",
      "source_content_sha256": "sha256-da-nota",
      "deck": "Wiki_Medicina::Cardiologia::Ponte_Miocardica",
      "note_model": "Medicina",
      "fields": {
        "Frente": "...",
        "Verso": "...",
        "Verso Extra": "\n\n...",
        "Obsidian": "obsidian://open?vault=Wiki_Medicina&file=Cardiologia%2FPonte_Miocardica.md"
      }
    },
    {
      "source_path": "/path/nota.md",
      "source_content_sha256": "sha256-da-nota",
      "deck": "Wiki_Medicina::Cardiologia::Ponte_Miocardica",
      "note_model": "Medicina Cloze",
      "fields": {
        "Texto": "A {{c1::ponte miocárdica}} envolve mais frequentemente a {{c2::DA}}.",
        "Verso Extra": "\n\nDescrita pela primeira vez em 1737.",
        "Obsidian": "obsidian://open?vault=Wiki_Medicina&file=Cardiologia%2FPonte_Miocardica.md"
      }
    }
  ]
}
```

`preferred_model` (singular) ainda aceito como atalho legado quando todos cards sao Q&A. Para fluxo padrao, use `preferred_models` com duas chaves.

Antes de gravar, filtre duplicados locais:

```bash
uv run python ${extensionPath}/scripts/mednotes/flashcard_index.py check --candidates <candidate_cards.json>
```

Grave somente `new_cards`. Apos Anki MCP aceitar, registre apenas cards aceitos:

```bash
uv run python ${extensionPath}/scripts/mednotes/flashcard_index.py record --accepted <accepted_cards.json>
```

Indice padrao em `~/.gemini/medical-notes-workbench/FLASHCARDS_INDEX.json`, sobrescrito por `MED_FLASHCARDS_INDEX` ou `--index`. Tag Obsidian `anki` continua como marcador visual/filtro; idempotencia real passa pelo indice local.

Para fluxo completo, prefira orquestrador deterministico:

```bash
uv run python ${extensionPath}/scripts/mednotes/flashcard_pipeline.py prepare --input <run.json>
uv run python ${extensionPath}/scripts/mednotes/flashcard_pipeline.py apply --input <accepted-run.json>
```

`prepare` combina validacao de modelo, status de fontes alteradas, checagem de duplicidade, queries de `findNotes` e payload `anki_notes` p/ `addNotes`. `apply` registra cards aceitos e devolve relatorio estruturado.

Payload de `prepare` precisa incluir campos de modelo capturados do Anki MCP. Em modo candidato, subagent chama `mcp_anki-mcp_modelNames` + `mcp_anki-mcp_modelFieldNames`, escolhe `preferred_model` quando compativel e devolve `models` como `{modelo: [campos...]}` ou lista `{name, fields}`. Em modo de gravacao, use `anki_find_queries` do plano p/ rodar `mcp_anki-mcp_findNotes` antes de `addNotes`; cards encontrados no Anki sao pulados como duplicados.

## Preview Antes Da Escrita

Comportamento padrao de `/flashcards` e preview-first: apos formular `candidate_cards` e rodar `flashcard_pipeline.py prepare`, mostre cards no terminal e aguarde confirmacao antes de chamar `mcp_anki-mcp_addNotes`/`mcp_anki-mcp_addNote`.

Use plano retornado por `prepare` como entrada:

```bash
uv run python ${extensionPath}/scripts/mednotes/flashcard_report.py preview-cards --input <write-plan.json>
```

Se usuario nao confirmar, finalize sem escrever no Anki, sem registrar no `FLASHCARDS_INDEX.json` e sem marcar notas com tag `anki`.

Modo direto opcional: se usuario pedir explicitamente `--create`, `--direct`, `--yes`, `--no-preview`, "criar diretamente", "crie direto", "sem preview", "sem previa" ou "sem confirmacao", pule apenas essa confirmacao de preview. Fluxo direto ainda valida modelo, filtra duplicados, respeita falhas do Anki MCP e registra apenas cards aceitos.

Se houver mais de 40 cards candidatos, modo padrao mostra preview completo e pede confirmacao antes de qualquer escrita.

## Validacao De Modelo Anki

Antes de chamar `mcp_anki-mcp_addNotes`/`mcp_anki-mcp_addNote`, valide modelo:

```bash
uv run python ${extensionPath}/scripts/mednotes/anki_model_validator.py validate --models-json <models.json>
```

JSON de entrada representa resultado de `modelNames` + `modelFieldNames`:

```json
{
  "Medicina": ["Frente", "Verso", "Verso Extra", "Obsidian"],
  "Medicina Cloze": ["Texto", "Verso Extra", "Obsidian"]
}
```

Para validar os dois modelos juntos, use `validate-set`:

```bash
uv run python ${extensionPath}/scripts/mednotes/anki_model_validator.py validate-set --models-json <models.json>
```

Se algum modelo faltar campos obrigatorios, pare e rode `flashcards/install_models.py ensure` p/ instalar/atualizar.

## Sincronizacao Das Twenty Rules

Para auditar copia local contra pacote Anki MCP instalado:

```bash
uv run python ${extensionPath}/scripts/mednotes/sync_anki_twenty_rules.py check
```

Use `--source <content.md>` p/ apontar explicitamente para o prompt upstream. Use `write` apenas p/ substituir copia local pela upstream.

## Relatorio Final

Quando fluxo tiver dados estruturados de fontes, duplicados, cards aceitos, validacao de modelo e erros do Anki MCP, gere resposta final com:

```bash
uv run python ${extensionPath}/scripts/mednotes/flashcard_report.py final --input <run-result.json>
```

Relatorio deve separar: notas processadas, cards criados, cards pulados por duplicidade, notas puladas, erros de modelo/campos e erros do Anki MCP.
