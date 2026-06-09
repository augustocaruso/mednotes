# mednotes

`mednotes` e um projeto publico de skills, agents e hooks para estudo medico em
ambientes de agentes de terminal.

O modelo mental do repositorio e simples:

- `core/` e a fonte da verdade: conteudo educacional, agents e scripts Python
  compartilhados.
- `adapters/` contem apenas a diferenca de empacotamento de cada host.
- Artefatos construidos ficam em `dist/` e nao entram no Git.

## Estado atual

Este repositorio esta no bootstrap inicial. A primeira meta e provar a
arquitetura `core/` + `adapters/` antes de migrar conteudo real do MedNotes.

Alvos planejados:

- Antigravity CLI: alvo principal, como bundle com `plugin.json`, `hooks.json`,
  `skills/`, `agents/` e `scripts/`.
- opencode: alvo secundario, como pacote npm com shim TypeScript.

Gemini CLI extension nao e um alvo ativo. Em 9 de junho de 2026, a documentacao
publica do Google indica transicao para Antigravity CLI em 18 de junho de 2026
para usuarios individuais/free/Google AI Pro/Ultra, com continuidade para
licencas enterprise e chaves pagas de API.

## Verificacao local

```bash
python3 -m unittest discover -s tests -v
```

Para montar um bundle Antigravity local:

```bash
python3 adapters/antigravity/build.py --output dist/antigravity/mednotes
```

O bundle gerado fica fora do Git por design.

## Fronteira publico/rascunho

O repositorio publico usa allowlist: so entra o que esta pronto para ser
publicado. Experimentos, rascunhos e WIP devem ficar fora deste repositorio.

O script `core/scripts/public_guard.py` roda uma verificacao basica de prontidao
publica para bloquear caminhos privados obvios antes de empacotar ou publicar.
