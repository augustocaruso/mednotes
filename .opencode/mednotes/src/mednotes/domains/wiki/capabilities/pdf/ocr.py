"""OCR state machine and Tesseract seam."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class OcrOutcome:
    status: str
    text: str
    confidence: float | None
    error_code: str = ""
    retry_eligible: bool = False


class OcrRunner(Protocol):
    def run(self, image_path: Path, languages: list[str]) -> OcrOutcome:
        ...


class MissingTesseractRunner:
    def run(self, image_path: Path, languages: list[str]) -> OcrOutcome:
        return OcrOutcome(status="blocked", text="", confidence=None, error_code="missing_binary", retry_eligible=True)


class TesseractRunner:
    def run(self, image_path: Path, languages: list[str]) -> OcrOutcome:
        if shutil.which("tesseract") is None:
            return MissingTesseractRunner().run(image_path, languages)
        try:
            import pytesseract
            from PIL import Image
        except Exception:
            return OcrOutcome("failed", "", None, "engine_error", True)
        try:
            text = str(pytesseract.image_to_string(Image.open(image_path), lang="+".join(languages)))
        except pytesseract.TesseractError as exc:
            message = str(exc).lower()
            if "failed loading language" in message or "could not initialize tesseract" in message:
                return OcrOutcome("blocked", "", None, "missing_language", True)
            return OcrOutcome("failed", "", None, "engine_error", True)
        except Exception:
            return OcrOutcome("failed", "", None, "engine_error", True)
        stripped = text.strip()
        return OcrOutcome("complete" if stripped else "failed", stripped, None, "" if stripped else "engine_error", not bool(stripped))


def ocr_page(image_path: Path, *, languages: list[str], runner: OcrRunner | None = None) -> OcrOutcome:
    return (runner or TesseractRunner()).run(image_path, languages)


def aggregate_status(outcomes: list[OcrOutcome]) -> tuple[str, str, int]:
    if not outcomes:
        return "not_needed", "", 0
    statuses = {outcome.status for outcome in outcomes}
    retry = 1 if any(outcome.retry_eligible for outcome in outcomes) else 0
    error = next((outcome.error_code for outcome in outcomes if outcome.error_code), "")
    if statuses == {"blocked"}:
        return "blocked", error, retry
    if statuses == {"failed"}:
        return "failed", error, retry
    if "complete" in statuses and statuses <= {"complete", "not_needed"}:
        return "complete", "", retry
    if "complete" in statuses:
        return "partial", error, retry
    if "blocked" in statuses:
        return "blocked", error, retry
    return "needed", error, retry
