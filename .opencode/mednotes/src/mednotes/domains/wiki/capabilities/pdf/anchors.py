"""PDF-library anchor adapter."""
from __future__ import annotations

from pathlib import Path

from mednotes.domains.wiki.capabilities.illustrate.anchors import AnchorProviderConfig, build_or_load_anchors


def anchors_for_note(note_path: Path, *, cache_db: Path, max_anchors: int = 5, preferred_language: str = "pt-br"):
    return build_or_load_anchors(
        note_path,
        cache_db=cache_db,
        max_anchors=max_anchors,
        preferred_language=preferred_language,
        provider_config=AnchorProviderConfig(),
    )
