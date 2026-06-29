# Vocabulary DB Recovery

Use este runbook quando `/mednotes:link` ou `/mednotes:fix-wiki` apontarem
`schema_drift`, `queue_inconsistent`, `sqlite_integrity_error`,
`pending_semantic_ingestion` ou fila semântica incoerente.

## Escada Operacional

1. Diagnostique sem mutar:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-status --json
   ```

2. Se o diagnóstico estiver `ready`, volte para `run-linker`.

3. Se `blocked_reason=vocabulary_db_missing`, faça dry-run de rebuild:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode rebuild-db --dry-run --plan-output "<vocabulary-recovery-plan.json>" --json
   ```

4. Se o bloqueio for tabela faltante, schema drift bootstrapável ou fila
   inconsistente em DB existente, faça dry-run de reconcile:

   ```bash
	   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode reconcile-queue --dry-run --plan-output "<vocabulary-recovery-plan.json>" --json
   ```

5. Aplique apenas operações oficiais e salve recibo:

   ```bash
   uv run python "${extensionPath}/scripts/mednotes/wiki/cli.py" vocabulary-recover --mode <rebuild-db|reconcile-queue> --apply --plan "<vocabulary-recovery-plan.json>" --receipt "<vocabulary-recovery-receipt.json>" --json
   ```

6. Rode diagnóstico de novo. Se sobrar `pending_semantic_ingestion`, use
	   `plan-subagents --phase vocabulary-curation --output <plan.json>`,
	   `collect-curator-outputs`, `eval-curator-batch` e
	   `apply-curator-batch --prompt-eval`.

## Stop Rules

Nunca edite SQLite, Markdown, manifest ou fila por script ad hoc. Pare e
produza `diagnostic_context` quando houver:

- `sqlite_integrity_error`;
- tabela existente com schema incompatível que o repair não declarou como
  operação segura;
- `UNIQUE constraint failed`;
- mismatch de `content_hash`, `note_path`, `work_id` ou manifest;
- JSON UTF-16/BOM de redirecionamento PowerShell; regenere pelo CLI com
  `--plan-output`, `--output` ou `--report`;
- output de subagente sem `agent_metrics`;
- timeout repetido.

O pacote enviado a agente/subagente deve incluir: path real do DB, erro exato,
`app_version`, workflow, phase, `next_action`, diagnóstico JSON, contrato
esperado e recibo/dry-run quando existir.
