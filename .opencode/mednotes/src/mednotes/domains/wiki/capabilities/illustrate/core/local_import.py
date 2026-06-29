"""Local image staging/commit helpers for reviewed PDF crops."""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class StagedImage:
    path: Path
    sha256: str
    filename: str


@dataclass(frozen=True)
class ImportedImage:
    path: Path
    filename: str
    sha256: str


def stage_crop(crop_path: Path, *, app_home: Path) -> StagedImage:
    crop_path = crop_path.expanduser().resolve(strict=True)
    with Image.open(crop_path) as image:
        image.verify()
    sha = _sha256_file(crop_path)
    staging = app_home / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    suffix = crop_path.suffix.lower() if crop_path.suffix else ".png"
    filename = f"{sha[:16]}{suffix}"
    target = staging / filename
    if not target.exists():
        shutil.copy2(crop_path, target)
    return StagedImage(path=target, sha256=sha, filename=filename)


def commit_staged_image(staged: StagedImage, *, attachments_dir: Path) -> ImportedImage:
    attachments_dir.mkdir(parents=True, exist_ok=True)
    target = attachments_dir / staged.filename
    if not target.exists():
        shutil.copy2(staged.path, target)
    return ImportedImage(path=target, filename=target.name, sha256=staged.sha256)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
