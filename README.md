# MedNotes for OpenCode

MedNotes é um plugin OpenCode para criar, organizar, revisar, linkar e estudar
notas médicas em Markdown/Obsidian. O produto foi desenhado para uso pessoal de
estudo clínico em português do Brasil, com fluxos guiados por máquina de estados
para evitar comandos soltos, estados paralelos e mutações inseguras no vault.

O contrato público é simples: você chama um workflow, o agente conduz preparo,
diagnóstico, prévia, confirmação e próxima ação. O código interno pode ter JSON,
hashes, recibos e validações, mas a experiência humana deve parecer direta.

## Instalação

Enquanto o pacote `mednotes-opencode` ainda não estiver publicado no registry
npm, instale pelo repo público GitHub:

```bash
npm install -g github:augustocaruso/mednotes
```

Registre o plugin no OpenCode apontando para o mesmo spec GitHub:

```bash
mednotes-opencode install --plugin github:augustocaruso/mednotes
```

Quando o pacote npm estiver publicado, o caminho equivalente pelo registry será:

```bash
npm install -g mednotes-opencode
```

E o registro do plugin poderá usar o spec curto:

```bash
mednotes-opencode install
```

Esse comando atualiza `~/.config/opencode/opencode.json` no macOS/Linux, ou o
caminho equivalente em `%APPDATA%` no Windows. Ele cria backup antes de alterar
um arquivo existente e pode ser auditado sem escrever nada:

```bash
mednotes-opencode install --dry-run
```

Depois disso, abra o OpenCode normalmente. O plugin é carregado pelo próprio
OpenCode como pacote npm/GitHub e sincroniza a configuração de runtime no boot.

## Atualização

Se instalado pelo GitHub, atualize reinstalando o spec público:

```bash
npm install -g github:augustocaruso/mednotes
mednotes-opencode install --plugin github:augustocaruso/mednotes
```

Quando o pacote registry estiver publicado, atualize como qualquer pacote npm:

```bash
npm update -g mednotes-opencode
```

Se o `opencode.json` usar o spec `mednotes-opencode`, o OpenCode também pode
resolver versões novas pelo mecanismo nativo de plugins npm. Para congelar uma
versão, use um spec com versão explícita no instalador:

```bash
mednotes-opencode install --plugin mednotes-opencode@0.1.0
```

## Configuração

A configuração global do usuário fica em:

```text
~/.mednotes/config.toml
```

Esse arquivo guarda caminhos, modelos dos especialistas, effort level e limites
de paralelismo. Segredos não entram no TOML. Chaves como SerpAPI devem vir de
variáveis de ambiente ou do keyring do sistema.

Exemplo mínimo:

```toml
[paths]
wiki_dir = "/caminho/para/Wiki_Medicina"
raw_dir = "/caminho/para/Chats_Raw"

[agents.med_chat_triager]
model = "antigravity/gemini-3.5-flash"
reasoning_effort = "medium"

[agents.med_knowledge_architect]
model = "antigravity/gemini-3.1-pro"
reasoning_effort = "high"

[workflows]
fix_wiki_max_parallel_rewrites = 3
process_chats_max_parallel_architects = 3
```

O TOML é lido no runtime. O usuário pode trocar modelo e effort sem editar o
plugin distribuído.

## Workflows

Comandos públicos preservados:

- `/mednotes:create`
- `/mednotes:enrich`
- `/mednotes:process-chats`
- `/mednotes:fix-wiki`
- `/mednotes:link`
- `/mednotes:link-body`
- `/mednotes:link-related`
- `/mednotes:pdf-library`
- `/mednotes:history`
- `/mednotes:setup`
- `/mednotes:status`
- `/mednotes:telemetry`
- `/flashcards`
- `/report`

Os workflows críticos são FSM-first. A FSM é a fonte de verdade para estado,
transições, bloqueios, recuperação e efeitos. Adapters executam efeitos; eles
não decidem política de fluxo.

## Segurança do vault

Workflows que mutam a Wiki usam ponto de restauração e validações antes de
aplicar mudanças. Prévia e confirmação humana aparecem quando há risco real:
mutação em lote, escolha clínica, credencial ausente, caminho ambíguo,
quota/capacidade de modelo ou validação de qualidade pendente.

O produto não deve expor detalhes internos por padrão. Termos como hashes,
recibos, schemas e comandos técnicos são superfícies de automação, não a UX
principal.

## Estrutura do pacote

O pacote npm exporta o plugin OpenCode por:

```text
.opencode/plugins/mednotes-fsm.mjs
```

Arquivos principais:

- `.opencode/`: plugin, agentes, comandos e runtime OpenCode gerados.
- `core/`: fontes canônicas públicas de agentes, comandos e skills.
- `contracts/`: contratos de agentes usados pelos geradores.
- `adapters/`: projeções secundárias mantidas por compatibilidade.
- `bin/mednotes-opencode.mjs`: instalador idempotente do plugin OpenCode.

Tudo nessa árvore pública é gerado a partir do repo privado por allowlist. A
árvore pública não deve ser editada manualmente.

## Release

O release público deve passar por estes gates:

```bash
npm run release:public:check
```

Esse gate valida FSMs, adapters, geração da árvore pública, auditoria do repo
público, contrato do pacote npm e smoke do plugin OpenCode.

O pipeline público publica o pacote `mednotes-opencode` no npm e cria o release
GitHub correspondente no repo `augustocaruso/mednotes`.

## Desenvolvimento

O desenvolvimento acontece no repo privado. Para regenerar os adapters e a
projeção pública:

```bash
npm run adapters:generate
node tools/run_python.mjs tools/public_repo/generate.py --repo-root . --output public/mednotes
```

Para validar só o pacote OpenCode:

```bash
npm run opencode:smoke
node tools/run_python.mjs tools/release/audit_opencode_npm_package.py --public-root public/mednotes
```

Mudanças observáveis devem passar por contrato, implementação e teste. Se um
workflow é FSM-first, não adicione estado paralelo em CLI, hook, adapter,
relatório humano ou payload legado.
