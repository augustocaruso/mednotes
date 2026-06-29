# Agent Role Contracts

Contrato canônico de fronteiras entre os agentes e códigos que participam de
`process-chats`, `link` e `fix-wiki`. Prompts de agentes referenciam este
documento em vez de duplicar política.

Regra-âncora:

> 1 meaning canônico = 1 nota Wiki.

Quem pode propor identidade semântica está fixado por papel. Quem pode
escrever Markdown está fixado por papel. Quem pode aplicar mutação na Wiki
está fixado por papel. Nenhum agente pode tomar decisão fora do seu papel,
mesmo que pareça mais eficiente.

## Matriz De Responsabilidade

| Papel | Lê | Produz | Não decide |
| --- | --- | --- | --- |
| **Triager** (`med-chat-triager`) | exatamente 1 raw chat atribuído | output top-level de triagem contendo `triage-note-plan.v2` com `meaning_claim` por unidade | existência na Wiki, merge target, canonical winner, cobertura |
| **Planner** (Python determinístico, camada de código fora dos prompts) | `note_plan` validado, Wiki, vocabulary DB, curator state | work items autocontidos para architect, ou bloqueios | corpo clínico, escrita de Markdown |
| **Architect** (`med-knowledge-architect`) | raw chat + work item + arquivos citados pelo work item | Markdown temporário ou rewrite exatamente da unidade pedida + `preservation_report` quando exigido | escopo da unidade, identidade semântica nova, decisão de merge, publicação, linker |
| **Graph Curator** (`med-link-graph-curator`) | nota publicada via path + content_hash + vocabulary DB | `note-semantic-ingestion.v1` (primary_meaning, aliases, surfaces, policies, `NoteMergeCandidate`) | re-triagem de raw chat, edição de Markdown, aplicação de merge |
| **Publish Guard** (`med-publish-guard`) | manifest, coverage, staged notes | parecer go/no-go com checklist auditável | clínica, meaning, qualidade de texto |

## Artefatos Permitidos Por Papel

- Triager: output top-level de triagem com `decision`, `raw_file` e
  `medical-notes-workbench.triage-note-plan.v2`. `agent_metrics` só é aceito
  como métrica de runtime; o agente não deve inventar contadores.
- Planner: artefatos internos de execução; nunca emite Markdown.
- Architect: Markdown em `temp_output`, mais
  `medical-notes-workbench.architect-preservation-report.v1` quando o work
  item exigir.
- Curator: `medical-notes-workbench.note-semantic-ingestion.v1`; pode propor
  `NoteMergeCandidate` dentro do mesmo envelope.
- Publish guard: `medical-notes-workbench.publish-guard-report.v1`.

## Decisões Proibidas Por Papel

### Triager nunca

- consulta vocabulary DB ou Wiki como autoridade de existência;
- emite ação de cobertura existente removida do contrato v2;
- escolhe `winner_path`, canonical target ou merge;
- usa título/stem/alias como identidade canônica;
- pede decisão humana para insegurança genérica que deveria virar
  `needs_context` ou critério editorial;
- divide o raw chat em fragmentos abaixo da unidade semântica.

### Planner nunca

- escreve corpo clínico;
- inventa `meaning_claim` que não veio do triager;
- decide existência por título/stem;
- substitui decisão humana por chute silencioso.

### Architect nunca

- re-triagena o raw chat;
- adiciona, remove, funde ou renomeia unidades planejadas;
- decide se um meaning já existe;
- escolhe merge target;
- aplica publicação, linker ou edição direta na Wiki;
- carrega caminhos antigos de `duplicate_merge` ou rodapé `Chat Original`.

### Curator nunca

- re-triagena raw chat;
- decide que um raw deveria ter gerado outra nota;
- edita Markdown;
- chama subagente;
- usa título/stem como detector de merge.

### Publish Guard nunca

- avalia qualidade clínica;
- resolve meaning;
- aceita publish sem consistência entre manifest, coverage e staged notes.

## Fronteira Code Vs Agente

Quando o que está em jogo é determinístico — leitura de vocabulary DB,
hashing, cruzamento de coverage, derivação de path canônico — a decisão
pertence ao planner Python, não a um agente. Agentes existem para tarefas
que exigem julgamento sobre conteúdo médico em linguagem natural. Tudo o que
puder ser implementado como função pura em `wiki.*` deve ficar lá.

## Como Os Prompts Referenciam Este Documento

Todo prompt sob `bundle/agents/` que toca process-chats / link / fix-wiki
carrega este doc via `${extensionPath}/docs/agent-role-contracts.md` e
acrescenta apenas o checklist específico do seu papel. Política compartilhada
não deve ser duplicada em prompt.
