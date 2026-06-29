"""Configuration loading for the PDF library."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.pdf import paths

VALID_CLOUD_POLICIES = {"optional", "disabled"}
VALID_IMAGE_BACKENDS = {"auto", "sixel", "tgp", "halfcell", "chafa", "ascii", "none"}


@dataclass(frozen=True)
class PdfLibraryLocalConfig:
    text_index: str = "sqlite-fts5"
    image_embedding: str = "none"
    ocr: str = "tesseract"


@dataclass(frozen=True)
class PdfLibraryCloudConfig:
    enabled: bool = True
    default_provider: str = "gemini_cli"
    allow_free_api_quota: bool = True
    allow_hosted_open_model_free_quota: bool = True
    send_full_pdf: bool = False
    send_full_ocr_text: bool = False
    record_provider_receipts: bool = True


@dataclass(frozen=True)
class PdfLibraryContextConfig:
    detect_figure_ids: bool = True
    link_mentions: bool = True
    default_mention_window_pages: int = 20
    conflict_policy: str = "needs_review"


@dataclass(frozen=True)
class PdfLibraryOcrConfig:
    enabled: bool = True
    backend: str = "tesseract"
    languages: list[str] = field(default_factory=lambda: ["por", "eng"])
    auto_run: bool = False
    max_parallel_pages: int = 1
    page_timeout_seconds: int = 90


@dataclass(frozen=True)
class PdfLibraryTuiConfig:
    enabled: bool = True
    show_thumbnails: bool = True
    image_backend: str = "auto"
    confirm_insert: bool = True


@dataclass(frozen=True)
class PdfLibraryConfig:
    enabled: bool = True
    mode: str = "cost-constrained-12gb"
    paths: list[Path] = field(default_factory=list)
    index_granularity: str = "figure"
    cloud_policy: str = "optional"
    local: PdfLibraryLocalConfig = field(default_factory=PdfLibraryLocalConfig)
    cloud: PdfLibraryCloudConfig = field(default_factory=PdfLibraryCloudConfig)
    context: PdfLibraryContextConfig = field(default_factory=PdfLibraryContextConfig)
    ocr: PdfLibraryOcrConfig = field(default_factory=PdfLibraryOcrConfig)
    tui: PdfLibraryTuiConfig = field(default_factory=PdfLibraryTuiConfig)


def load_pdf_library_config(
    *,
    config_path: Path | None = None,
    cli_paths: list[Path] | None = None,
    cloud_policy: str | None = None,
    image_backend: str | None = None,
) -> PdfLibraryConfig:
    raw = _read_config(config_path or paths.app_config_path())
    section = raw.get("pdf_library") if isinstance(raw.get("pdf_library"), dict) else {}
    cfg = _from_section(section)

    env_paths = _env_paths()
    effective_paths = cli_paths if cli_paths is not None else env_paths if env_paths is not None else cfg.paths
    effective_cloud_policy = (
        cloud_policy
        or os.environ.get("MEDNOTES_PDF_LIBRARY_CLOUD_POLICY")
        or cfg.cloud_policy
    )
    effective_image_backend = (
        image_backend
        or os.environ.get("MEDNOTES_PDF_LIBRARY_IMAGE_BACKEND")
        or cfg.tui.image_backend
    )
    _validate_choice("cloud_policy", effective_cloud_policy, VALID_CLOUD_POLICIES)
    _validate_choice("image_backend", effective_image_backend, VALID_IMAGE_BACKENDS)
    return PdfLibraryConfig(
        enabled=cfg.enabled,
        mode=cfg.mode,
        paths=[_resolve_path(path) for path in effective_paths],
        index_granularity=cfg.index_granularity,
        cloud_policy=effective_cloud_policy,
        local=cfg.local,
        cloud=cfg.cloud,
        context=cfg.context,
        ocr=cfg.ocr,
        tui=PdfLibraryTuiConfig(
            enabled=cfg.tui.enabled,
            show_thumbnails=cfg.tui.show_thumbnails,
            image_backend=effective_image_backend,
            confirm_insert=cfg.tui.confirm_insert,
        ),
    )


def _read_config(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _from_section(section: dict[str, Any]) -> PdfLibraryConfig:
    local = section.get("local") if isinstance(section.get("local"), dict) else {}
    cloud = section.get("cloud") if isinstance(section.get("cloud"), dict) else {}
    context = section.get("context") if isinstance(section.get("context"), dict) else {}
    ocr = section.get("ocr") if isinstance(section.get("ocr"), dict) else {}
    tui = section.get("tui") if isinstance(section.get("tui"), dict) else {}
    return PdfLibraryConfig(
        enabled=bool(section.get("enabled", True)),
        mode=str(section.get("mode", "cost-constrained-12gb")),
        paths=[_resolve_path(Path(str(item))) for item in _as_list(section.get("paths"))],
        index_granularity=str(section.get("index_granularity", "figure")),
        cloud_policy=str(section.get("cloud_policy", "optional")),
        local=PdfLibraryLocalConfig(
            text_index=str(local.get("text_index", "sqlite-fts5")),
            image_embedding=str(local.get("image_embedding", "none")),
            ocr=str(local.get("ocr", "tesseract")),
        ),
        cloud=PdfLibraryCloudConfig(
            enabled=bool(cloud.get("enabled", True)),
            default_provider=str(cloud.get("default_provider", "gemini_cli")),
            allow_free_api_quota=bool(cloud.get("allow_free_api_quota", True)),
            allow_hosted_open_model_free_quota=bool(cloud.get("allow_hosted_open_model_free_quota", True)),
            send_full_pdf=bool(cloud.get("send_full_pdf", False)),
            send_full_ocr_text=bool(cloud.get("send_full_ocr_text", False)),
            record_provider_receipts=bool(cloud.get("record_provider_receipts", True)),
        ),
        context=PdfLibraryContextConfig(
            detect_figure_ids=bool(context.get("detect_figure_ids", True)),
            link_mentions=bool(context.get("link_mentions", True)),
            default_mention_window_pages=int(context.get("default_mention_window_pages", 20)),
            conflict_policy=str(context.get("conflict_policy", "needs_review")),
        ),
        ocr=PdfLibraryOcrConfig(
            enabled=bool(ocr.get("enabled", True)),
            backend=str(ocr.get("backend", "tesseract")),
            languages=[str(item) for item in _as_list(ocr.get("languages", ["por", "eng"]))],
            auto_run=bool(ocr.get("auto_run", False)),
            max_parallel_pages=int(ocr.get("max_parallel_pages", 1)),
            page_timeout_seconds=int(ocr.get("page_timeout_seconds", 90)),
        ),
        tui=PdfLibraryTuiConfig(
            enabled=bool(tui.get("enabled", True)),
            show_thumbnails=bool(tui.get("show_thumbnails", True)),
            image_backend=str(tui.get("image_backend", "auto")),
            confirm_insert=bool(tui.get("confirm_insert", True)),
        ),
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _env_paths() -> list[Path] | None:
    raw = os.environ.get("MEDNOTES_PDF_LIBRARY_PATHS")
    if raw is None:
        return None
    items = [part.strip() for part in raw.replace(",", os.pathsep).split(os.pathsep)]
    return [_resolve_path(Path(part)) for part in items if part]


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _validate_choice(name: str, value: str, choices: set[str]) -> None:
    if value not in choices:
        raise ValueError(f"invalid {name}: {value!r}; expected one of {sorted(choices)}")
