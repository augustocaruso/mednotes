"""PDF ingestion into the local SQLite library."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.pdf import captions, db, mentions, ocr, paths, pdf_engine

INGEST_SCHEMA = "medical-notes-workbench.pdf-library-ingest-receipt.v1"
SCAN_SCHEMA = "medical-notes-workbench.pdf-library-scan-dry-run.v1"


def scan_dry_run(config, *, app_home: Path | None = None) -> dict[str, Any]:
    candidates: list[Path] = []
    missing: list[str] = []
    for root in config.paths:
        if not root.exists():
            missing.append(str(root))
            continue
        if root.is_file() and root.suffix.lower() == ".pdf":
            candidates.append(root)
        elif root.is_dir():
            candidates.extend(sorted(root.rglob("*.pdf")))
    return {
        "schema": SCAN_SCHEMA,
        "status": "ok" if not missing else "blocked",
        "phase": "ingest_scan",
        "pdf_count": len(candidates),
        "pdfs": [str(path.resolve(strict=False)) for path in candidates],
        "missing_paths": missing,
        "app_home": str(app_home or paths.app_home()),
        **({"blocked_reason": "pdf_library_paths_missing", "next_action": "fix configured PDF paths"} if missing else {}),
    }


def add_pdfs(pdf_paths: list[Path], *, app_home: Path | None = None) -> dict[str, Any]:
    root = app_home or paths.app_home()
    conn = db.open_database(paths.database_path(root))
    documents: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for pdf_path in pdf_paths:
        try:
            documents.append(_index_one(conn, pdf_path.expanduser().resolve(strict=False), app_home=root))
        except Exception as exc:
            warnings.append({"path": str(pdf_path), "error": type(exc).__name__, "message": str(exc)})
    return {
        "schema": INGEST_SCHEMA,
        "status": "ok" if documents else "failed",
        "phase": "ingest_add",
        "documents": documents,
        "warnings": warnings,
    }


def remove_pdf(*, pdf_sha256: str, app_home: Path | None = None) -> dict[str, Any]:
    root = app_home or paths.app_home()
    conn = db.open_database(paths.database_path(root))
    now = _now()
    with conn:
        row = conn.execute("SELECT pdf_sha256 FROM documents WHERE pdf_sha256 = ?", (pdf_sha256,)).fetchone()
        if not row:
            return {
                "schema": INGEST_SCHEMA,
                "status": "blocked",
                "phase": "ingest_remove",
                "blocked_reason": "pdf_not_indexed",
                "next_action": "ingest add PDF before removing it",
            }
        conn.execute("UPDATE documents SET removed_at = ? WHERE pdf_sha256 = ?", (now, pdf_sha256))
        db.delete_document_fts(conn, pdf_sha256=pdf_sha256)
    return {"schema": INGEST_SCHEMA, "status": "removed", "phase": "ingest_remove", "pdf_sha256": pdf_sha256, "removed_at": now}


def reindex_pdf(*, pdf_sha256: str | None = None, path: Path | None = None, app_home: Path | None = None) -> dict[str, Any]:
    root = app_home or paths.app_home()
    conn = db.open_database(paths.database_path(root))
    pdf_path = path
    if pdf_path is None and pdf_sha256:
        row = conn.execute("SELECT path FROM documents WHERE pdf_sha256 = ?", (pdf_sha256,)).fetchone()
        if row:
            pdf_path = Path(str(row["path"]))
    if pdf_path is None:
        return {
            "schema": INGEST_SCHEMA,
            "status": "blocked",
            "phase": "ingest_reindex",
            "blocked_reason": "pdf_not_indexed",
            "next_action": "pass --path PDF or index the PDF first",
        }
    return add_pdfs([pdf_path], app_home=root) | {"phase": "ingest_reindex"}


def _index_one(conn, pdf_path: Path, *, app_home: Path) -> dict[str, Any]:
    stat = pdf_path.stat()
    pdf_sha, page_count, pages = pdf_engine.extract_pages(pdf_path, app_home=app_home)
    title_guess = pdf_path.stem
    all_captions: list[captions.Caption] = []
    all_mentions: list[mentions.Mention] = []
    ocr_outcomes: list[ocr.OcrOutcome] = []
    page_rows: list[dict[str, Any]] = []
    figure_rows: list[dict[str, Any]] = []
    mention_rows: list[dict[str, Any]] = []
    now = _now()

    for page in pages:
        page_text = page.text
        text_source = "digital" if page.has_text_layer else "none"
        page_ocr_status = "not_needed" if page.has_text_layer else "needed"
        page_ocr_error = ""
        page_ocr_retry = 0
        if not page.has_text_layer:
            if shutil.which("tesseract") is None:
                page_ocr_status = "blocked"
                page_ocr_error = "missing_binary"
                page_ocr_retry = 1
                ocr_outcomes.append(ocr.OcrOutcome("blocked", "", None, "missing_binary", True))
            else:
                page_ocr_status = "needed"
                ocr_outcomes.append(ocr.OcrOutcome("needed", "", None, "", False))
        page_captions = captions.extract_captions(page_text, page_number=page.page_number)
        page_mentions = mentions.extract_mentions(page_text, page_number=page.page_number)
        all_captions.extend(page_captions)
        all_mentions.extend(page_mentions)
        page_rows.append(
            {
                "page_number": page.page_number,
                "text_source": text_source,
                "text": page_text,
                "ocr_status": page_ocr_status,
                "ocr_error_code": page_ocr_error,
                "ocr_retry_eligible": page_ocr_retry,
                "thumbnail": str(page.thumbnail_path or ""),
                "width_px": page.width_px,
                "height_px": page.height_px,
            }
        )

    mention_by_id: dict[str, list[mentions.Mention]] = {}
    for mention in all_mentions:
        mention_by_id.setdefault(mention.figure_id, []).append(mention)
    for cap in all_captions:
        linked = mentions.link_mentions(cap.page_number, cap.section_path_guess, mention_by_id.get(cap.figure_id, []), figure_id=cap.figure_id)
        figure_uid = _figure_uid(pdf_sha, cap.page_number, cap.figure_id, cap.text)
        evidence = "caption_and_mentions" if linked else "caption_only"
        figure_rows.append(
            {
                "figure_uid": figure_uid,
                "page_number": cap.page_number,
                "figure_id": cap.figure_id,
                "display_label": cap.figure_id,
                "caption": cap.text,
                "evidence_level": evidence,
                "is_low_confidence": 0 if linked else 1,
                "conflict_reason": "",
            }
        )
        for mention in linked:
            mention_rows.append(_mention_row(pdf_sha, figure_uid, mention))

    if not figure_rows and pages:
        first = pages[0]
        figure_uid = _figure_uid(pdf_sha, first.page_number, "page-context", first.text[:80])
        figure_rows.append(
            {
                "figure_uid": figure_uid,
                "page_number": first.page_number,
                "figure_id": "",
                "display_label": "page context",
                "caption": first.text[:240],
                "evidence_level": "page_context_only" if first.text else "visual_only",
                "is_low_confidence": 1,
                "conflict_reason": "no_caption_detected",
            }
        )

    if not ocr_outcomes:
        doc_ocr_status, doc_ocr_error, doc_ocr_retry = ("not_needed", "", 0)
    else:
        doc_ocr_status, doc_ocr_error, doc_ocr_retry = ocr.aggregate_status(ocr_outcomes)

    with conn:
        conn.execute("DELETE FROM mentions WHERE pdf_sha256 = ?", (pdf_sha,))
        conn.execute("DELETE FROM figures WHERE pdf_sha256 = ?", (pdf_sha,))
        conn.execute("DELETE FROM pages WHERE pdf_sha256 = ?", (pdf_sha,))
        db.delete_document_fts(conn, pdf_sha256=pdf_sha)
        conn.execute(
            """
            INSERT OR REPLACE INTO documents(
              pdf_sha256, path, filename, title_guess, page_count, has_text_layer,
              ocr_status, ocr_error_code, ocr_retry_eligible, source_mtime_ns,
              source_size_bytes, indexed_at, removed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                pdf_sha,
                str(pdf_path),
                pdf_path.name,
                title_guess,
                page_count,
                1 if any(page.has_text_layer for page in pages) else 0,
                doc_ocr_status,
                doc_ocr_error,
                doc_ocr_retry,
                stat.st_mtime_ns,
                stat.st_size,
                now,
            ),
        )
        for page in page_rows:
            conn.execute(
                """
                INSERT INTO pages(
                  pdf_sha256, page_number, text_source, text, ocr_confidence, ocr_status,
                  ocr_error_code, ocr_retry_eligible, section_path_guess, page_thumbnail_path,
                  width_px, height_px, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)
                """,
                (
                    pdf_sha,
                    page["page_number"],
                    page["text_source"],
                    page["text"],
                    None,
                    page["ocr_status"],
                    page["ocr_error_code"],
                    page["ocr_retry_eligible"],
                    page["thumbnail"],
                    page["width_px"],
                    page["height_px"],
                    now,
                ),
            )
            db.replace_page_fts(conn, pdf_sha256=pdf_sha, page_number=page["page_number"], title_guess=title_guess, section_path_guess="[]", text=page["text"])
        for figure in figure_rows:
            conn.execute(
                """
                INSERT INTO figures(
                  figure_uid, pdf_sha256, page_number, bbox_json, crop_path, thumbnail_path,
                  image_sha256, figure_id, display_label, caption, caption_bbox_json,
                  visual_quality_score, evidence_level, is_low_confidence, conflict_reason,
                  created_at, updated_at
                ) VALUES (?, ?, ?, '[]', '', '', '', ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)
                """,
                (
                    figure["figure_uid"],
                    pdf_sha,
                    figure["page_number"],
                    figure["figure_id"],
                    figure["display_label"],
                    figure["caption"],
                    0.7 if figure["evidence_level"] == "caption_and_mentions" else 0.4,
                    figure["evidence_level"],
                    figure["is_low_confidence"],
                    figure["conflict_reason"],
                    now,
                    now,
                ),
            )
            db.replace_figure_fts(conn, figure_uid=figure["figure_uid"], pdf_sha256=pdf_sha, figure_id=figure["figure_id"], display_label=figure["display_label"], caption=figure["caption"])
        for mention in mention_rows:
            conn.execute(
                """
                INSERT INTO mentions(
                  mention_uid, pdf_sha256, figure_uid, page_number, figure_id, display_label,
                  sentence, paragraph, section_path_guess, offset_start, offset_end,
                  confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mention["mention_uid"],
                    pdf_sha,
                    mention["figure_uid"],
                    mention["page_number"],
                    mention["figure_id"],
                    mention["display_label"],
                    mention["sentence"],
                    mention["paragraph"],
                    json.dumps(mention["section_path_guess"], ensure_ascii=False),
                    mention["offset_start"],
                    mention["offset_end"],
                    mention["confidence"],
                    now,
                ),
            )
            db.replace_mention_fts(conn, mention_uid=mention["mention_uid"], pdf_sha256=pdf_sha, figure_id=mention["figure_id"], sentence=mention["sentence"], paragraph=mention["paragraph"], section_path_guess=json.dumps(mention["section_path_guess"], ensure_ascii=False))

    return {
        "pdf_sha256": pdf_sha,
        "path": str(pdf_path),
        "filename": pdf_path.name,
        "page_count": page_count,
        "has_text_layer": any(page.has_text_layer for page in pages),
        "ocr_status": doc_ocr_status,
        "ocr_error_code": doc_ocr_error,
        "figure_count": len(figure_rows),
        "mention_count": len(mention_rows),
    }


def _mention_row(pdf_sha: str, figure_uid: str, mention: mentions.Mention) -> dict[str, Any]:
    return {
        "mention_uid": hashlib.sha256(f"{pdf_sha}:{figure_uid}:{mention.page_number}:{mention.offset_start}:{mention.sentence}".encode()).hexdigest(),
        "figure_uid": figure_uid,
        "page_number": mention.page_number,
        "figure_id": mention.figure_id,
        "display_label": mention.figure_id,
        "sentence": mention.sentence,
        "paragraph": mention.paragraph,
        "section_path_guess": list(mention.section_path_guess),
        "offset_start": mention.offset_start,
        "offset_end": mention.offset_end,
        "confidence": 0.8,
    }


def _figure_uid(pdf_sha: str, page_number: int, figure_id: str, text: str) -> str:
    digest = hashlib.sha256(f"{pdf_sha}:{page_number}:{figure_id}:{text}".encode()).hexdigest()[:16]
    return f"{pdf_sha}:{page_number}:{digest}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
