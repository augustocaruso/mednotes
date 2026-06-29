---
name: create-medical-note
description: Cria notas médicas didáticas em Markdown para estudo, com estrutura clara para Obsidian e pontos que podem ser enriquecidos com imagens depois. Use quando o usuário pedir para criar, escrever, estruturar ou transformar um tema/material em nota médica.
---

# Skill: create-medical-note

Resposta ao usuário: `${extensionPath}/docs/workflow-output-contract.md`.

## Quando usar

- O usuário quer criar uma nota médica didática a partir de um tema, outline,
  transcrição, aula, texto colado ou pergunta clínica geral.
- O usuário quer uma nota Markdown organizada para estudo no Obsidian.
- O usuário quer preparar uma nota que depois possa receber figuras com
  `enrich-medical-note`.
- O usuário quer uma Mini-Aula no padrão da Wiki_Medicina; nesse caso, carregue
  e siga `${extensionPath}/docs/knowledge-architect.md`.

Não usar para:

- Dar aconselhamento médico individualizado para um paciente real.
- Diagnosticar ou prescrever conduta personalizada.
- Inserir imagens; para isso use `enrich-medical-note` depois que a nota existir.
- Processar `Chats_Raw`, decidir `note_plan`, publicar lote ou marcar raw chat;
  para isso use `/mednotes:process-chats`.

## Formato recomendado

Use Markdown limpo, com headings ATX (`#`, `##`, `###`). Prefira seções curtas,
boas para revisão e para futura inserção de imagens.

Estrutura padrão:

```markdown
---
tipo: nota-medica
tema: ...
status: rascunho
---

# Título

## Visão geral

## Anatomia/Fisiologia essencial

## Mecanismo ou fisiopatologia

## Quadro clínico

## Diagnóstico

## Tratamento ou manejo

## Armadilhas e diferenciais

## Pontos visuais sugeridos
```

Adapte a estrutura ao tema. Para Wiki_Medicina,
`${extensionPath}/docs/knowledge-architect.md` é o dono do Padrão Ouro: formato
de Mini-Aula, YAML mínimo, headings, fechamento, notas relacionadas,
rodapé de proveniência, taxonomia e links. Não replique nem substitua esse
contrato aqui.

Para farmacologia, prefira mecanismo, indicações,
efeitos adversos, contraindicações e interações. Para anatomia, prefira marcos,
relações, irrigação, inervação e correlações clínicas.

## Regras de escrita

- Escreva em português do Brasil por padrão.
- Seja didático, direto e preciso.
- Diferencie conhecimento consolidado de incerteza quando relevante.
- Não invente referências bibliográficas específicas.
- Evite linguagem de prontuário; a nota é material de estudo.
- Inclua uma seção "Pontos visuais sugeridos" quando houver conceitos que se
  beneficiem de figura, esquema, anatomia, histologia, radiologia ou gráfico.
- Não invente tags. Preserve apenas tags operacionais existentes, especialmente
  `anki` e `revisar`, se estiver transformando uma nota já existente.
  Especialidade clínica fica no caminho de pasta/taxonomia.

## Salvamento

Se o usuário pedir para salvar em arquivo, use nome curto em kebab-case com
extensão `.md`. Antes de sobrescrever arquivo existente, confirme com o usuário.
Ao finalizar, indique status emoji, caminho salvo quando houver, se alguma
sobrescrita foi evitada, pontos visuais sugeridos e próximo workflow natural
(`/mednotes:enrich`, `/mednotes:link` ou `/flashcards`).
