"""Lazy PyMuPDF boundary."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PdfImageBlock:
    bbox: tuple[float, float, float, float]
    image_sha256: str
    crop_path: Path
    width_px: int
    height_px: int


@dataclass(frozen=True)
class PdfPageExtract:
    pdf_sha256: str
    page_number: int
    width_px: int
    height_px: int
    text: str
    has_text_layer: bool
    image_blocks: list[PdfImageBlock] = field(default_factory=list)
    thumbnail_path: Path | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_pages(pdf_path: Path, *, app_home: Path) -> tuple[str, int, list[PdfPageExtract]]:
    import fitz

    pdf_sha = sha256_file(pdf_path)
    pages: list[PdfPageExtract] = []
    thumb_dir = app_home / "thumbnails" / pdf_sha
    thumb_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        for page_index in range(len(doc)):
            index = page_index + 1
            page = doc[page_index]
            raw_text = page.get_text("text")
            text = raw_text if isinstance(raw_text, str) else ""
            rect = page.rect
            thumb_path = thumb_dir / f"page-{index}.png"
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(0.35, 0.35), alpha=False)
                pix.save(str(thumb_path))
                width_px = int(pix.width)
                height_px = int(pix.height)
            except Exception:
                width_px = int(rect.width)
                height_px = int(rect.height)
                thumb_path = None
            pages.append(
                PdfPageExtract(
                    pdf_sha256=pdf_sha,
                    page_number=index,
                    width_px=width_px,
                    height_px=height_px,
                    text=text.strip(),
                    has_text_layer=bool(text.strip()),
                    image_blocks=[],
                    thumbnail_path=thumb_path,
                )
            )
    finally:
        doc.close()
    return pdf_sha, len(pages), pages
