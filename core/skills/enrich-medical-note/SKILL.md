---
name: enrich-medical-note
description: Enriquece notas médicas em Markdown com imagens usando o módulo enricher empacotado no Medical Notes Workbench. Use quando o usuário pedir para enriquecer, ilustrar, adicionar figuras ou buscar imagens para uma ou mais notas médicas `.md`.
---

# Skill: enrich-medical-note

Resposta ao usuário: `${extensionPath}/docs/workflow-output-contract.md`.

## Quando usar

- O usuário pede para enriquecer/ilustrar uma ou mais notas Markdown médicas.
- O usuário aponta um ou mais `.md` e quer figuras de anatomia, histologia, mecanismos,
  esquemas, radiologia ou fotos clínicas.
- O usuário quer embutir imagens no formato Obsidian `![[...]]`.

Não usar para:

- Geração de imagens novas. O projeto busca e baixa imagens externas/locais.
- Reescrever livremente o conteúdo textual da nota.

## Invariantes

- `${extensionPath}` é o bundle auto-updatable; use-o só para ler arquivos e
  executar `scripts/enrich_notes.py`.
- Estado editável vive em `~/.mednotes`; não grave
  segredo/config como única cópia dentro de `${extensionPath}`.
- Execute todos os alvos em uma invocação. Use `--force` somente se o usuário
  pedir refazer notas já marcadas com `images_enriched: true`.
- Só adicione blocos de imagem/caption e frontmatter próprio do enricher; esse
  frontmatter é additive-only. Não reescreva texto clínico.
- Não rode `/mednotes:link` por padrão; imagem/caption/frontmatter visual não
  muda o grafo de notas.
- Se Python/config/venv estiverem quebrados, rode ou peça `/mednotes:setup`.

## Pré-condições Mínimas

1. Cada nota alvo é um arquivo `.md` legível.
2. `~/.mednotes/config.toml` tem `[paths].wiki_dir`,
   ou `config.toml` tem `[vault].path` legado preenchido.
3. `uv` e `UV_PROJECT_ENVIRONMENT="$HOME/.mednotes/.venv"`
   apontam para a venv persistente quando a extensão estiver instalada.
4. O `gemini` CLI está autenticado para âncoras e rerank visual.

## Como executar

Linux/macOS:

```bash
cd "${extensionPath}"
export UV_PROJECT_ENVIRONMENT="$HOME/.mednotes/.venv"
uv run python scripts/enrich_notes.py "<nota-ou-pasta-ou-glob>" [mais alvos] --config ~/.mednotes/config.toml
```

Windows:

```powershell
Set-Location "${extensionPath}"
$env:UV_PROJECT_ENVIRONMENT = "$HOME\.mednotes\.venv"
uv run python scripts\enrich_notes.py "<nota-ou-glob>" [mais alvos] --config "$HOME\.mednotes\config.toml"
```

Diretórios/globs são aceitos; o orquestrador deduplica e ignora anexos/cache.
Use `--force` só para refazer notas enriquecidas.

## Como interpretar

Reporte ao usuário:

- Número de âncoras encontradas.
- Quantas imagens foram inseridas.
- Notas puladas por `images_enriched: true`.
- Notas sem inserção e falhas por nota.
- Fontes usadas (`wikimedia`, `web_search`, etc.).
- Caminhos finais das notas.
- Falhas toleradas, como downloads `403` ou thumbs indisponíveis.

## Critério de qualidade visual

O orquestrador deve tratar busca e rerank como curadoria médica, não como
decoração de nota:

- Âncoras devem apontar o achado visual exato que ajudaria revisão clínica ou
  prova de residência.
- Queries devem usar termos médicos específicos. Quando a fonte real for
  `web_search`, operadores `site:` podem funcionar como adapter virtual para
  fontes confiáveis (`site:nih.gov`, `site:ncbi.nlm.nih.gov`,
  `site:radiopaedia.org`, etc.), sem prometer que existam adapters nativos.
- Fotos clínicas, histologia, radiologia e anatomia devem ser reais ou
  academicamente confiáveis. Ilustração genérica, imagem decorativa, watermark
  pesado, texto ilegível ou tópico apenas vizinho devem ser recusados.
- `radiopaedia`, `nih_open_i`, `openstax`, `dermnet` e `teachmeanatomy` são
  perfil web confiável sobre `web_search` + SerpAPI com `site:` explícito, não
  APIs nativas.
- Se nenhuma candidata servir, o rerank deve retornar `null`; se
  `minimum_quality_met=false`, o perfil `clinical` não insere imagem meia-certa.
- Use `--quality-report <arquivo.json>` quando precisar revisar fontes e razões
  de aceite/recusa; o relatório local não deve conter Markdown bruto ou imagens.

Use o contrato de saída para transformar logs e JSON em resumo curto com status
emoji, contagens, arquivos relevantes, warnings e próxima ação. Não despeje JSON
bruto por padrão.

## Falhas comuns

- **Vault/Wiki não configurado**: peça o caminho e atualize
  `[paths].wiki_dir` em `~/.mednotes/config.toml`; use
  `[vault].path` apenas como compatibilidade.
- **Gemini CLI sem login**: peça para autenticar o Gemini CLI.
- **Gemini CLI não encontrado**: no Windows, o orquestrador tenta `gemini.cmd`;
  ajuste `[gemini].binary` no config persistente só se necessário.
- **Sem `SERPAPI_KEY`/`SERPAPI_API_KEY`**: `web_search` e os perfis web
  confiáveis retornam `[]`; Wikimedia continua. Configure via
  `gemini extensions config medical-notes-workbench SERPAPI_KEY` ou `.env`
  persistente, nunca dentro de `${extensionPath}`.
- **Cota/limite SerpAPI esgotado**: o lote para imediatamente com `rc=9` e
  aviso claro para evitar novas chamadas à API. Oriente o usuário a renovar a
  cota/chave ou rodar novamente só com fontes disponíveis.
- **Downloads 403**: o downloader tenta headers browser-like e fallback de
  thumbnail SerpAPI quando disponível; se ainda falhar, pule a candidata.
