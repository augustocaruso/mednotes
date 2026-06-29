"""SQLite helpers for the PDF library."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from mednotes.domains.wiki.capabilities.pdf import schema


def open_database(path: Path) -> sqlite3.Connection:
    path.expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.executescript(schema.SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
            (schema.SCHEMA_VERSION,),
        )
    return conn


def schema_version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    return str(row[0]) if row else ""


def replace_page_fts(
    conn: sqlite3.Connection,
    *,
    pdf_sha256: str,
    page_number: int,
    title_guess: str,
    section_path_guess: str,
    text: str,
) -> None:
    conn.execute("DELETE FROM page_fts WHERE pdf_sha256 = ? AND page_number = ?", (pdf_sha256, page_number))
    conn.execute(
        "INSERT INTO page_fts(pdf_sha256, page_number, title_guess, section_path_guess, text) VALUES (?, ?, ?, ?, ?)",
        (pdf_sha256, page_number, title_guess, section_path_guess, text),
    )


def replace_figure_fts(
    conn: sqlite3.Connection,
    *,
    figure_uid: str,
    pdf_sha256: str,
    figure_id: str,
    display_label: str,
    caption: str,
) -> None:
    conn.execute("DELETE FROM figure_fts WHERE figure_uid = ?", (figure_uid,))
    conn.execute(
        "INSERT INTO figure_fts(figure_uid, pdf_sha256, figure_id, display_label, caption) VALUES (?, ?, ?, ?, ?)",
        (figure_uid, pdf_sha256, figure_id, display_label, caption),
    )


def replace_mention_fts(
    conn: sqlite3.Connection,
    *,
    mention_uid: str,
    pdf_sha256: str,
    figure_id: str,
    sentence: str,
    paragraph: str,
    section_path_guess: str,
) -> None:
    conn.execute("DELETE FROM mention_fts WHERE mention_uid = ?", (mention_uid,))
    conn.execute(
        "INSERT INTO mention_fts(mention_uid, pdf_sha256, figure_id, sentence, paragraph, section_path_guess) VALUES (?, ?, ?, ?, ?, ?)",
        (mention_uid, pdf_sha256, figure_id, sentence, paragraph, section_path_guess),
    )


def delete_document_fts(conn: sqlite3.Connection, *, pdf_sha256: str) -> None:
    conn.execute("DELETE FROM page_fts WHERE pdf_sha256 = ?", (pdf_sha256,))
    conn.execute("DELETE FROM figure_fts WHERE pdf_sha256 = ?", (pdf_sha256,))
    conn.execute("DELETE FROM mention_fts WHERE pdf_sha256 = ?", (pdf_sha256,))
