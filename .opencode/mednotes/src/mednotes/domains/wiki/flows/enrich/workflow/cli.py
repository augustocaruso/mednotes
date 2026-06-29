"""CLI entrypoint for the image enrichment workflow."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from mednotes.domains.wiki.capabilities.illustrate.core.config import load as load_config
from mednotes.domains.wiki.capabilities.illustrate.core.config import wiki_memory_path
from mednotes.domains.wiki.flows.enrich.workflow import reporting
from mednotes.domains.wiki.flows.enrich.workflow.inputs import _resolve_note_inputs
from mednotes.domains.wiki.flows.enrich.workflow.models import _EXIT_SOURCE_QUOTA, NoteResult
from mednotes.domains.wiki.flows.enrich.workflow.runner import (
    _log_run_header,
    _print_summary,
    _process_note,
    _resolve_vault,
)
from mednotes.domains.wiki.flows.enrich.workflow.utils import _log
from mednotes.domains.wiki.flows.enrich.workflow.vault_guard_bridge import VaultGuardError, require_enrich_guard
from mednotes.platform.feedback import command_string, safe_record_workflow_run


def main(argv: list[str] | None = None) -> int:
    started_at = time.time()
    parser = argparse.ArgumentParser(
        prog="enrich_notes",
        description="Orquestrador end-to-end (gemini CLI + enricher toolbox).",
    )
    parser.add_argument(
        "notes",
        nargs="+",
        type=Path,
        help="Caminho(s) da(s) nota(s) .md",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enriquece mesmo se images_enriched já é true.",
    )
    parser.add_argument(
        "--quality-report",
        type=Path,
        default=None,
        help="Escreve relatório local JSON com fontes, candidatos e razões de aceite/recusa.",
    )
    parser.add_argument(
        "--quality-profile",
        choices=["clinical", "broad"],
        default="clinical",
        help="Perfil de curadoria visual. clinical é estrito; broad preserva comportamento mais permissivo.",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    vault = _resolve_vault(cfg, args.config)
    if vault is None:
        _log(
            f"erro: configure [vault].path no config.toml ou [paths].wiki_dir em {wiki_memory_path()}.",
            err=True,
        )
        safe_record_workflow_run(
            workflow="/mednotes:enrich",
            command=command_string(),
            payload={
                "phase": "enrich_preflight",
                "status": "failed",
                "blocked_reason": "missing_vault_config",
                "next_action": "Configurar [paths].wiki_dir em ~/.mednotes/config.toml ou [vault].path no config.toml e rodar novamente.",
                "required_inputs": ["config", "wiki_dir"],
            },
            exit_code=4,
            started_at=started_at,
        )
        return 4

    notes, input_errors = _resolve_note_inputs(args.notes)
    for note in notes:
        try:
            require_enrich_guard(note, command="enrich_notes")
        except VaultGuardError as exc:
            payload = exc.to_payload()
            _log(str(payload["human_message"]), err=True)
            safe_record_workflow_run(
                workflow="/mednotes:enrich",
                command=command_string(),
                payload=payload,
                exit_code=exc.exit_code,
                started_at=started_at,
            )
            return exc.exit_code
    _log_run_header(
        cfg=cfg,
        config_path=args.config,
        vault=vault,
        notes_count=len(notes),
    )

    results: list[NoteResult] = list(input_errors)
    for result in input_errors:
        _log(f"erro: {result.message}", err=True)
    for index, note in enumerate(notes, start=1):
        if index > 1:
            _log("")
        result = _process_note(
            note,
            cfg=cfg,
            vault=vault,
            force=args.force,
            index=index,
            total=len(notes),
            quality_profile=args.quality_profile,
        )
        results.append(result)
        if result.code == _EXIT_SOURCE_QUOTA:
            break

    _print_summary(results)
    if args.quality_report:
        reporting.write_quality_report(args.quality_report, {
            "schema": "medical-notes-workbench.enricher-quality-report.v1",
            "note_count": len(results),
            "quality_profile": args.quality_profile,
            "sources_enabled": cfg["sources"]["enabled"],
            "notes": [
                result.quality_report
                for result in results
                if result.quality_report
            ],
        })
    exit_code = 0
    for result in results:
        if result.code != 0:
            exit_code = result.code
            break
    safe_record_workflow_run(
        workflow="/mednotes:enrich",
        command=command_string(),
        payload=_feedback_payload(results, vault=vault, force=args.force),
        exit_code=exit_code,
        started_at=started_at,
        snippets=[result.message for result in results if result.message],
    )
    return exit_code


def _feedback_payload(results: list[NoteResult], *, vault: Path, force: bool) -> dict[str, object]:
    enriched = sum(1 for item in results if item.status == "enriched")
    skipped = sum(1 for item in results if item.status == "skipped")
    no_insert = sum(1 for item in results if item.status == "no_insert")
    failures = [item for item in results if item.code != 0]
    source_counts: dict[str, int] = {}
    for result in results:
        for source, count in result.sources_count.items():
            source_counts[source] = (source_counts[source] if source in source_counts else 0) + count
    return {
        "phase": "enrich_notes",
        "status": "failed" if failures else "completed_with_warnings" if no_insert or skipped else "completed",
        "blocked_reason": "source_quota" if any(item.code == _EXIT_SOURCE_QUOTA for item in failures) else "",
        "next_action": "Revisar falhas e rodar novamente apenas para as notas afetadas." if failures else "",
        "required_inputs": ["notes", "config", "wiki_dir"],
        "vault_dir": str(vault),
        "force": force,
        "quality_reports_available": any(item.quality_report for item in results),
        "summary": {
            "note_count": len(results),
            "enriched_count": enriched,
            "skipped_count": skipped,
            "no_insert_count": no_insert,
            "failure_count": len(failures),
            "inserted_count": sum(item.inserted_count for item in results),
        },
        "source_counts": source_counts,
        "notes": [
            {
                "path": str(item.note),
                "status": item.status,
                "inserted_count": item.inserted_count,
                "code": item.code,
            }
            for item in results
        ],
        "errors": [item.message for item in failures if item.message],
    }


if __name__ == "__main__":
    raise SystemExit(main())
