# Política Canônica de Taxonomia

Este contrato governa apenas a hierarquia de pastas da `Wiki_Medicina`.
A fonte executável de verdade é
`bundle/scripts/mednotes/wiki/taxonomy/policy.py`; este documento é a
projeção humana distribuída com a extensão.

## Hierarquia

- Nível 1: cinco grandes áreas numeradas, em conjunto fechado.
- Nível 2: especialidades canônicas sob cada grande área, em conjunto fechado.
- Nível 3: subcategoria opcional sob uma especialidade canônica.
- Nível 4+: leaf ou pasta de nota.

`schema.py` e os subcomandos `taxonomy-*` derivam áreas, especialidades e
aliases de `policy.py`. Subcategorias/leaves continuam governadas por
`allow_new_leaf=True` e bloqueio de similaridade próxima.

## Grandes Áreas

- `1. Clínica Médica`
- `2. Cirurgia`
- `3. Ginecologia e Obstetrícia`
- `4. Pediatria`
- `5. Medicina Preventiva`

## Ginecologia E Obstetrícia

A grande área canônica é `3. Ginecologia e Obstetrícia`.

Especialidades canônicas:

- `Ginecologia`
- `Obstetrícia`

Aliases legados preservados por compatibilidade:

- `#. Ginecologia e Obstetricia` -> `3. Ginecologia e Obstetrícia`
- `3. Ginecologia e Obstetricia` -> `3. Ginecologia e Obstetrícia`
- `Ginecologia_Obstetricia` -> `3. Ginecologia e Obstetrícia`
- `Ginecologia e Obstetricia` -> `3. Ginecologia e Obstetrícia`
- `Ginecologia e Obstetrícia` -> `3. Ginecologia e Obstetrícia`
- `Obstetricia` -> `3. Ginecologia e Obstetrícia/Obstetrícia`

## Versão Da Política

A versão executável atual é `2026-05-15.taxonomy-v1`. Planos e recibos de
migração registram essa versão e não devem ser aplicados quando a versão atual
for diferente.

## Status Humano

Use `taxonomy-status` para entender o estado da árvore real antes de aplicar
migração. O comando emite JSON e pode escrever relatório Markdown com
`--report-output`.

## Criação De Leaf Nova

Dry-run pode sugerir `taxonomy_new_dirs`. Publish real só cria diretórios novos
quando o recibo de dry-run para o mesmo manifest autorizou exatamente esses
diretórios.

## Decisão Operacional

- Preservar caminhos que já estão sob área e especialidade canônicas.
- Resolver aliases conhecidos para o destino canônico antes de publicar ou
  migrar.
- Criar leaf nova somente sob pai canônico coerente e sem similaridade fuzzy
  bloqueadora.
- Bloquear para decisão humana quando a área, especialidade, similaridade ou
  destino for ambíguo.
- Mover pastas somente via `taxonomy-migrate`, com plano, recibo e rollback.

## Comandos

- `taxonomy-canonical`
- `taxonomy-tree`
- `taxonomy-audit`
- `taxonomy-status`
- `taxonomy-resolve`
- `taxonomy-plan`
- `taxonomy-apply`
- `taxonomy-rollback`
- `taxonomy-migrate`

`fix-wiki` pode auditar e planejar taxonomia, mas migração de pastas continua
passando pelo mecanismo de plano/aplicação/rollback de `taxonomy-migrate`.
`taxonomy-plan`, `taxonomy-apply` e `taxonomy-rollback` são nomes mais claros
para o mesmo fluxo. `taxonomy-migrate --dry-run|--apply|--rollback` permanece
compatível.
