#!/usr/bin/env python
"""Internal JSON CLI for /mednotes:pdf-library."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mednotes.domains.wiki.capabilities.pdf import config as config_mod
from mednotes.domains.wiki.capabilities.pdf import doctor, ingest
from mednotes.kernel.base import JsonObject, JsonObjectAdapter

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_IO = 5


def scan_dry_run(cfg: config_mod.PdfLibraryConfig, *, app_home: Path | None = None) -> JsonObject:
    return _as_payload(ingest.scan_dry_run(cfg, app_home=app_home))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = dispatch(args)
    except Exception as exc:
        payload = _as_payload({
            "schema": "medical-notes-workbench.pdf-library-error.v1",
            "status": "failed",
            "phase": getattr(args, "command", "unknown"),
            "error": type(exc).__name__,
            "message": str(exc),
        })
        _emit(payload)
        return EXIT_IO
    _emit(payload)
    status = _payload_status(payload)
    if status in {"blocked", "blocked_vault_guard_required"}:
        return EXIT_VALIDATION
    if status == "failed":
        return EXIT_IO
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdf-library")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--json", action="store_true")
    doctor_p.add_argument("--config", type=Path)
    doctor_p.add_argument("--path", "--pdf-dir", dest="paths", type=Path, action="append")

    setup_p = sub.add_parser("setup")
    setup_p.add_argument("--json", action="store_true")
    setup_p.add_argument("--dry-run", action="store_true")

    tui_p = sub.add_parser("tui")
    tui_p.add_argument("--note", type=Path)
    tui_p.add_argument("--image-backend", default=None)
    tui_p.add_argument("--open-mode", choices=["inline", "split-auto"], default="inline")

    ingest_p = sub.add_parser("ingest")
    ingest_sub = ingest_p.add_subparsers(dest="ingest_command", required=True)
    scan_p = ingest_sub.add_parser("scan")
    scan_p.add_argument("--dry-run", action="store_true", required=True)
    scan_p.add_argument("--json", action="store_true")
    scan_p.add_argument("--config", type=Path)
    scan_p.add_argument("--path", "--pdf-dir", dest="paths", type=Path, action="append")
    add_p = ingest_sub.add_parser("add")
    add_p.add_argument("pdfs", type=Path, nargs="+")
    add_p.add_argument("--json", action="store_true")
    remove_p = ingest_sub.add_parser("remove")
    remove_p.add_argument("--pdf-sha256", required=True)
    remove_p.add_argument("--json", action="store_true")
    reindex_p = ingest_sub.add_parser("reindex")
    reindex_p.add_argument("--pdf-sha256")
    reindex_p.add_argument("--path", type=Path)
    reindex_p.add_argument("--json", action="store_true")

    search_p = sub.add_parser("search")
    group = search_p.add_mutually_exclusive_group(required=False)
    group.add_argument("--query")
    group.add_argument("--note", type=Path)
    search_p.add_argument("--anchor-id", default="")
    search_p.add_argument("--provider", default="local")
    search_p.add_argument("--top-k", type=int, default=20)
    search_p.add_argument("--json", action="store_true")

    insert_p = sub.add_parser("insert")
    insert_sub = insert_p.add_subparsers(dest="insert_command", required=True)
    preview_p = insert_sub.add_parser("preview")
    preview_p.add_argument("--note", type=Path, required=True)
    preview_p.add_argument("--figure-uid", required=True)
    preview_p.add_argument("--anchor-id", default="")
    preview_p.add_argument("--section", action="append", dest="section_path")
    preview_p.add_argument("--crop", type=Path)
    preview_p.add_argument("--json", action="store_true")
    apply_p = insert_sub.add_parser("apply")
    apply_p.add_argument("--preview-receipt", type=Path, required=True)
    apply_p.add_argument("--confirm", action="store_true")
    apply_p.add_argument("--json", action="store_true")
    return parser


def dispatch(args: argparse.Namespace) -> JsonObject:
    if args.command == "doctor":
        cfg = config_mod.load_pdf_library_config(config_path=args.config, cli_paths=args.paths)
        return _as_payload(doctor.run_doctor(cfg=cfg))
    if args.command == "setup":
        return _as_payload(doctor.setup_payload(dry_run=args.dry_run))
    if args.command == "tui":
        from mednotes.domains.wiki.capabilities.pdf.tui.app import PdfLibraryApp
        from mednotes.domains.wiki.capabilities.pdf.tui.state import PdfLibraryState

        app = PdfLibraryApp(state=PdfLibraryState(selected_note=args.note), image_backend=args.image_backend or "auto")
        app.run()
        return _as_payload({"schema": "medical-notes-workbench.pdf-library-tui.v1", "status": "closed", "phase": "tui"})
    if args.command == "ingest":
        if args.ingest_command == "scan":
            cfg = config_mod.load_pdf_library_config(config_path=args.config, cli_paths=args.paths)
            return _as_payload(ingest.scan_dry_run(cfg))
        if args.ingest_command == "add":
            return _as_payload(ingest.add_pdfs(args.pdfs))
        if args.ingest_command == "remove":
            return _as_payload(ingest.remove_pdf(pdf_sha256=args.pdf_sha256))
        if args.ingest_command == "reindex":
            return _as_payload(ingest.reindex_pdf(pdf_sha256=args.pdf_sha256, path=args.path))
    if args.command == "search":
        from mednotes.domains.wiki.capabilities.pdf import search

        return _as_payload(
            search.search(
                search.SearchRequest(
                    query_text=args.query or "",
                    note_path=args.note,
                    anchor_id=args.anchor_id,
                    provider=args.provider,
                    top_k=args.top_k,
                )
            )
        )
    if args.command == "insert":
        from mednotes.domains.wiki.capabilities.pdf import insert

        if args.insert_command == "preview":
            crop = args.crop
            if crop is None:
                return _as_payload({
                    "schema": insert.PREVIEW_SCHEMA,
                    "status": "blocked",
                    "phase": "insert_preview",
                    "blocked_reason": "crop_path_required",
                    "next_action": "pass --crop PATH",
                })
            section = list(args.section_path or [])
            return insert.preview(note_path=args.note, figure_uid=args.figure_uid, section_path=section, crop_path=crop)
        if args.insert_command == "apply":
            return insert.apply_preview(receipt_path=args.preview_receipt, confirm=args.confirm)
    raise AssertionError(f"unhandled command: {args.command}")


def _as_payload(payload: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(payload)


def _payload_status(payload: JsonObject) -> str:
    value = _as_payload(payload).get("status")
    return value if isinstance(value, str) else ""


def _emit(payload: JsonObject) -> None:
    print(json.dumps(_as_payload(payload), ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
