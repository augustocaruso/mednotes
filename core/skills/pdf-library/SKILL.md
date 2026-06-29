---
name: pdf-library
description: Gerencia biblioteca local de figuras extraidas de PDFs e insere imagens revisadas em notas medicas.
---

# Skill: pdf-library

Resposta ao usuário: `${extensionPath}/docs/workflow-output-contract.md`.

## Quando usar

- O usuário quer procurar figuras em PDFs locais para ilustrar notas médicas.
- O usuário quer ingerir PDFs, buscar por termos ou revisar imagens antes de inserir.
- O usuário abriu `/mednotes:pdf-library`.

## Invariantes

- Estado editável vive em `~/.mednotes/pdf-library`.
- A experiência pública deve ser "just works": prepare ambiente, indexe, busque,
  revise e insira sem exigir que o usuário saiba nomes de subcomandos.
- Use a CLI interna via `node "${extensionPath}/scripts/run_python.mjs" scripts/mednotes/pdf_library/cli.py ...`.
- Confira dependências e prepare o ambiente automaticamente quando for seguro.
  Se houver bloqueio real, entregue uma única próxima ação em linguagem humana.
- PDFs, OCR bruto, imagens e Markdown clínico não entram em telemetria.
- Cloud é opcional. `gemini_cli` pode gerar âncoras; provedores free/open-model reservados bloqueiam como `provider_not_implemented`.
- Inserção sempre mostra uma prévia, informa que nada foi alterado ainda e só
  aplica depois de confirmação explícita.
- Mudança puramente visual do enricher (`images_*`, embed e caption) não chama `/mednotes:link`.

## Fluxo

1. Abra a biblioteca e diga ao usuário se ela já está pronta ou se você está
   preparando o ambiente.
2. Descubra PDFs locais a partir do pedido do usuário ou do caminho informado.
   Mostre quantos arquivos serão adicionados antes de indexar.
3. Indexe os PDFs aceitos, mantendo OCR opcional e local.
4. Busque figuras por termo, nota ou trecho clínico sem exigir sintaxe técnica.
5. Para revisão visual, abra a TUI inline. Se imagem não renderizar, mantenha a
   revisão textual disponível.
6. Para inserir em nota, mostre uma prévia clara, informe que nada foi alterado
   ainda e aplique somente após confirmação explícita com o vault guard ativo.

## Privacidade

Use recibos com hashes, paths, figure ids, captions e status. Nunca inclua PDF completo, dump de OCR, bytes de imagem, Markdown bruto de nota, `.env`, tokens ou chaves.
