"""Gemini exported artifact discovery and Wiki-note validation."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mednotes.domains.wiki.capabilities.notes.raw_chats import read_note_meta
from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.domains.wiki.config import _path
from mednotes.kernel.base import JsonObject

ARTIFACT_HTML_MANIFEST_SCHEMA = "gemini-md-export.artifact-html-manifest.v1"
ARTIFACT_IMAGE_MANIFEST_SCHEMA = "gemini-md-export.artifact-image-manifest.v1"
ARTIFACT_VALIDATION_SCHEMA = "medical-notes-workbench.gemini-artifact-validation.v1"
ARTIFACT_HTML_VALIDATION_SCHEMA = "medical-notes-workbench.artifact-html-validation.v1"
_SUPPORTED_MANIFEST_SCHEMAS = {ARTIFACT_HTML_MANIFEST_SCHEMA, ARTIFACT_IMAGE_MANIFEST_SCHEMA}
_IMAGE_SUFFIXES = {".avif", ".gif", ".jpg", ".jpeg", ".png", ".svg", ".webp"}

_EXPLICIT_MANIFEST_KEYS = (
    "artifact_manifest",
    "artifact_manifests",
    "artifact_html_manifest",
    "artifact_html_manifests",
    "artifact_image_manifest",
    "artifact_image_manifests",
    "artifact_manifest_path",
    "artifact_manifest_paths",
    "artifact_image_manifest_path",
    "artifact_image_manifest_paths",
    "gemini_artifact_manifest",
    "gemini_artifact_manifests",
    "gemini_image_artifact_manifest",
    "gemini_image_artifact_manifests",
)
_FULL_HTML_RE = re.compile(r"(?is)<\s*!doctype\b|<\s*html\b|<\s*/\s*html\s*>|<\s*head\b|<\s*body\b|<\s*script\b")
_IFRAME_SRC_RE = re.compile(r"(?is)<iframe\b[^>]*\bsrc\s*=\s*([\"'])(.*?)\1[^>]*>")
_MARKDOWN_LINK_RE = re.compile(r"(?s)\[[^\]]+\]\(([^)\s]+(?:\s[^)]*)?)\)")
_MARKDOWN_IMAGE_RE = re.compile(r"(?s)!\[[^\]]*\]\(([^)\s]+(?:\s[^)]*)?)\)")
_COMMENT_RE = re.compile(r"(?is)<!--\s*gemini-artifact\b(?P<body>.*?)-->")


@dataclass(frozen=True)
class ArtifactHtml:
    chat_id: str
    source_url: str
    manifest_path: Path
    file_path: Path
    sha256: str
    kind: str = "html"
    turn_index: str = ""
    mime_type: str = ""
    caption: str = ""

    def to_json(self) -> dict[str, str]:
        payload = {
            "kind": self.kind,
            "chat_id": self.chat_id,
            "source_url": self.source_url,
            "manifest": str(self.manifest_path),
            "file": str(self.file_path),
            "sha256": self.sha256,
        }
        if self.turn_index:
            payload["turn_index"] = self.turn_index
        if self.mime_type:
            payload["mime_type"] = self.mime_type
        if self.caption:
            payload["caption"] = self.caption
        return payload


@dataclass(frozen=True)
class ArtifactManifest:
    path: Path
    chat_id: str
    source_url: str
    saved_count: int
    artifacts: tuple[ArtifactHtml, ...]
    schema: str = ARTIFACT_HTML_MANIFEST_SCHEMA

    def to_json(self) -> JsonObject:
        return {
            "schema": self.schema,
            "path": str(self.path),
            "chat_id": self.chat_id,
            "source_url": self.source_url,
            "saved_count": self.saved_count,
            "artifacts": [artifact.to_json() for artifact in self.artifacts],
        }


def _paths_match(left: str | Path, right: Path) -> bool:
    left_path = _path(str(left))
    try:
        return left_path.resolve() == right.resolve()
    except OSError:
        return str(left_path) == str(right)


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _coerce_manifest_paths(value: str) -> list[Path]:
    parsed = _json_value(value.strip())
    raw_values: list[Any]
    if isinstance(parsed, list):
        raw_values = parsed
    elif isinstance(parsed, str):
        raw_values = re.split(r"[,;\n]", parsed)
    else:
        raw_values = [parsed]
    return [_path(str(item).strip()) for item in raw_values if str(item).strip()]


def _extract_chat_id(value: str) -> str:
    value = value.strip().strip("\"'")
    if not value:
        return ""
    if re.match(r"^https?://", value):
        parsed = urlparse(value)
        parts = [part for part in parsed.path.split("/") if part]
        if "app" in parts:
            index = parts.index("app")
            if index + 1 < len(parts):
                return parts[index + 1].strip("/")
        return parts[-1].strip("/") if parts else ""
    if "/app/" in value:
        return value.rsplit("/app/", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
    return value.rstrip("/").split("/")[-1].split("?", 1)[0].split("#", 1)[0]


def chat_id_from_raw(raw_file: Path) -> str:
    return _extract_chat_id(read_note_meta(raw_file).get("fonte_id", ""))


def _source_url(raw_meta: dict[str, str], chat_id: str, manifest_data: dict[str, Any]) -> str:
    for key in ("sourceUrl", "source_url", "geminiUrl", "gemini_url", "chatUrl", "chat_url", "url", "source"):
        value = str(manifest_data.get(key) or "").strip()
        if value:
            return value
    fonte_id = raw_meta.get("fonte_id", "").strip()
    if re.match(r"^https?://", fonte_id):
        return fonte_id
    return f"https://gemini.google.com/app/{chat_id}" if chat_id else ""


def _manifest_chat_id(path: Path, data: dict[str, Any], fallback: str) -> str:
    for key in ("chatId", "chat_id", "chatID", "sourceChatId", "source_chat_id"):
        chat_id = _extract_chat_id(str(data.get(key) or ""))
        if chat_id:
            return chat_id
    match = re.match(r"artifact-(?P<chat_id>.+)-manifest\.json$", path.name)
    return _extract_chat_id(match.group("chat_id")) if match else fallback


def _saved_count(data: JsonObject, artifact_count: int) -> int:
    value = data.get("savedCount", data.get("saved_count", artifact_count))
    if value is None:
        value = artifact_count
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ValidationError("Gemini artifact manifest savedCount must be an integer") from err


def _artifact_items(data: dict[str, Any]) -> list[Any]:
    for key in ("artifacts", "files", "htmlFiles", "html_files", "savedArtifacts", "saved_artifacts", "saved", "outputs"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _item_path(value: Any, manifest_dir: Path) -> Path | None:
    if isinstance(value, str):
        raw_path = value
    elif isinstance(value, dict):
        raw_path = ""
        for key in (
            "file",
            "path",
            "filePath",
            "file_path",
            "htmlPath",
            "html_path",
            "absolutePath",
            "absolute_path",
            "outputPath",
            "output_path",
            "savedPath",
            "saved_path",
            "relativePath",
            "relative_path",
            "fileName",
            "filename",
            "name",
        ):
            raw_path = str(value.get(key) or "").strip()
            if raw_path:
                break
    else:
        return None
    if not raw_path:
        return None
    path = _path(raw_path)
    return path if path.is_absolute() else manifest_dir / path


def _item_sha256(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("sha256", "sha_256", "sha256Hex", "sha256_hex", "sha256Digest", "sha256_digest", "hash"):
        value = str(item.get(key) or "").strip().lower()
        if value:
            return value.removeprefix("sha256:")
    return ""


def _item_turn_index(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("turnIndex", "turn_index", "turn", "index"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _item_mime_type(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("mimeType", "mime_type", "mimetype", "contentType", "content_type"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _item_caption(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("caption", "alt", "altText", "alt_text", "description", "title", "prompt"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_artifact_manifest(path: Path, raw_file: Path) -> ArtifactManifest | None:
    if not path.exists():
        raise MissingPathError(f"Gemini artifact manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid Gemini artifact manifest JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Gemini artifact manifest must be a JSON object")
    schema = str(data.get("schema") or "")
    if schema not in _SUPPORTED_MANIFEST_SCHEMAS:
        return None
    artifact_kind = "image" if schema == ARTIFACT_IMAGE_MANIFEST_SCHEMA else "html"

    raw_meta = read_note_meta(raw_file)
    raw_chat_id = _extract_chat_id(raw_meta.get("fonte_id", ""))
    chat_id = _manifest_chat_id(path, data, raw_chat_id)
    if raw_chat_id and chat_id and chat_id != raw_chat_id:
        raise ValidationError(
            f"Gemini artifact manifest chatId {chat_id!r} does not match raw chat fonte_id {raw_chat_id!r}"
        )

    source_url = _source_url(raw_meta, chat_id or raw_chat_id, data)
    artifacts: list[ArtifactHtml] = []
    for index, item in enumerate(_artifact_items(data), start=1):
        file_path = _item_path(item, path.parent)
        if file_path is None:
            raise ValidationError(f"Gemini artifact manifest item #{index} is missing a file path")
        suffix = file_path.suffix.lower()
        mime_type = _item_mime_type(item)
        if artifact_kind == "html" and suffix != ".html":
            raise ValidationError(f"Artifact file must remain an isolated .html file: {file_path}")
        if artifact_kind == "image" and suffix not in _IMAGE_SUFFIXES:
            raise ValidationError(f"Gemini image artifact file must be an image file: {file_path}")
        if artifact_kind == "image" and mime_type and not mime_type.lower().startswith("image/"):
            raise ValidationError(f"Gemini image artifact mimeType must start with image/: {file_path}")
        if not file_path.exists():
            raise MissingPathError(f"Gemini artifact file not found: {file_path}")
        computed_sha = _sha256_file(file_path)
        manifest_sha = _item_sha256(item)
        if manifest_sha and manifest_sha != computed_sha:
            raise ValidationError(f"Gemini artifact SHA-256 mismatch for {file_path}")
        artifacts.append(
            ArtifactHtml(
                chat_id=chat_id or raw_chat_id,
                source_url=source_url,
                manifest_path=path,
                file_path=file_path,
                sha256=manifest_sha or computed_sha,
                kind=artifact_kind,
                turn_index=_item_turn_index(item),
                mime_type=mime_type,
                caption=_item_caption(item),
            )
        )

    saved_count = _saved_count(data, len(artifacts))
    if saved_count > 0 and not artifacts:
        raise ValidationError("Gemini artifact manifest savedCount > 0 but no artifact files were listed")
    if saved_count > 0 and len(artifacts) != saved_count:
        raise ValidationError(
            f"Gemini artifact manifest savedCount={saved_count} but listed {len(artifacts)} artifact files"
        )
    return ArtifactManifest(
        path=path,
        chat_id=chat_id or raw_chat_id,
        source_url=source_url,
        saved_count=saved_count,
        artifacts=tuple(artifacts),
        schema=schema,
    )


def _explicit_manifest_paths(raw_file: Path) -> list[Path]:
    meta = read_note_meta(raw_file)
    paths: list[Path] = []
    for key in _EXPLICIT_MANIFEST_KEYS:
        value = meta.get(key)
        if value:
            paths.extend(_coerce_manifest_paths(value))
    return paths


def _candidate_search_roots(raw_file: Path, artifact_dir: Path | None) -> list[Path]:
    roots = [path for path in (artifact_dir, raw_file.parent, raw_file.parent / "artifacts", raw_file.parent.parent / "artifacts") if path]
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root.expanduser())
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def discover_artifact_manifests(raw_file: Path, *, artifact_dir: Path | None = None) -> list[ArtifactManifest]:
    """Find required Gemini artifact manifests for one raw chat."""

    chat_id = chat_id_from_raw(raw_file)
    candidates = _explicit_manifest_paths(raw_file)
    if chat_id:
        patterns = (
            f"artifact-{chat_id}-manifest.json",
            f"artifact-{chat_id}-images-manifest.json",
            f"artifact-{chat_id}-image-manifest.json",
            f"media-{chat_id}-manifest.json",
            f"image-{chat_id}-manifest.json",
        )
        for root in _candidate_search_roots(raw_file, artifact_dir):
            if root.is_file() and root.name in patterns:
                candidates.append(root)
            elif root.is_dir():
                for pattern in patterns:
                    candidates.extend(sorted(root.rglob(pattern)))

    manifests: list[ArtifactManifest] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser())
        if key in seen:
            continue
        seen.add(key)
        manifest = load_artifact_manifest(candidate, raw_file)
        if manifest is not None and manifest.saved_count > 0:
            manifests.append(manifest)
    return manifests


def _file_uri_variants(path: Path) -> set[str]:
    resolved = path.resolve()
    variants = {resolved.as_uri(), resolved.as_posix(), str(path)}
    if resolved.as_posix().startswith("/"):
        variants.add("file://" + resolved.as_posix())
    return variants


def _comment_fields(content: str) -> list[dict[str, str]]:
    comments: list[dict[str, str]] = []
    for match in _COMMENT_RE.finditer(content):
        fields: dict[str, str] = {}
        for line in match.group("body").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
        comments.append(fields)
    return comments


def _has_artifact_comment(comments: list[dict[str, str]], artifact: ArtifactHtml) -> bool:
    for fields in comments:
        if fields.get("chat_id", "") != artifact.chat_id:
            continue
        if fields.get("sha256", "").lower() != artifact.sha256.lower():
            continue
        if not _paths_match(fields.get("manifest", ""), artifact.manifest_path):
            continue
        if not _paths_match(fields.get("file", ""), artifact.file_path):
            continue
        return True
    return False


def _has_iframe(content: str, variants: set[str]) -> bool:
    return any(src.strip() in variants for _quote, src in _IFRAME_SRC_RE.findall(content))


def _has_markdown_link(content: str, variants: set[str]) -> bool:
    return any(target.strip() in variants for target in _MARKDOWN_LINK_RE.findall(content))


def _has_markdown_image(content: str, variants: set[str]) -> bool:
    return any(target.strip() in variants for target in _MARKDOWN_IMAGE_RE.findall(content))


def _has_caption(content: str, artifact: ArtifactHtml) -> bool:
    if not artifact.caption:
        return bool(re.search(r"(?im)^\s*\*?\s*(?:Figura|Legenda)\s*:", content))
    return artifact.caption in content


def _artifact_key(artifact: ArtifactHtml) -> str:
    return f"{artifact.kind}:{artifact.sha256}:{artifact.file_path}"


def _artifact_presence(content: str, artifact: ArtifactHtml) -> dict[str, Any]:
    variants = _file_uri_variants(artifact.file_path)
    has_iframe = _has_iframe(content, variants)
    has_link = _has_markdown_link(content, variants)
    has_image = _has_markdown_image(content, variants)
    has_caption = _has_caption(content, artifact)
    has_comment = _has_artifact_comment(_comment_fields(content), artifact)
    missing_parts = []
    if artifact.kind == "html":
        if not has_iframe:
            missing_parts.append("iframe")
        if not has_link:
            missing_parts.append("Markdown link")
        complete = has_iframe and has_link and has_comment
        touched = has_iframe or has_link or has_comment
    else:
        if not has_image:
            missing_parts.append("Markdown image")
        if not has_caption:
            missing_parts.append("caption")
        complete = has_image and has_caption and has_comment
        touched = has_image or has_caption or has_comment
    if not has_comment:
        missing_parts.append("gemini-artifact provenance comment")
    return {
        "artifact": artifact.to_json(),
        "key": _artifact_key(artifact),
        "has_iframe": has_iframe,
        "has_markdown_link": has_link,
        "has_markdown_image": has_image,
        "has_caption": has_caption,
        "has_provenance_comment": has_comment,
        "complete": complete,
        "touched": touched,
        "missing_parts": missing_parts,
    }


def _note_artifact_report(
    content: str,
    *,
    manifests: list[ArtifactManifest],
    artifacts: list[ArtifactHtml],
    note: str | None = None,
) -> dict[str, Any]:
    statuses = [_artifact_presence(content, artifact) for artifact in artifacts]
    included = [status["artifact"] for status in statuses if status["complete"]]
    missing = [status["artifact"] for status in statuses if not status["complete"]]
    partial = [status for status in statuses if status["touched"] and not status["complete"]]
    errors: list[str] = []
    if artifacts and _FULL_HTML_RE.search(content):
        errors.append("captured HTML must stay in isolated .html files; do not inline full HTML into the Markdown note")
    for status in partial:
        errors.append(
            "incomplete Gemini artifact declaration"
            + (f" in {note}" if note else "")
            + f" for {status['artifact']['file']}: missing {', '.join(status['missing_parts'])}"
        )

    result: dict[str, Any] = {
        "schema": ARTIFACT_VALIDATION_SCHEMA,
        "scope": "note",
        "required": bool(artifacts),
        "manifest_count": len(manifests),
        "artifact_count": len(artifacts),
        "included_artifact_count": len(included),
        "missing_artifact_count": len(missing),
        "manifests": [manifest.to_json() for manifest in manifests],
        "artifacts": [artifact.to_json() for artifact in artifacts],
        "included_artifacts": included,
        "missing_artifacts": missing,
        "partial_artifacts": partial,
        "errors": errors,
    }
    if note is not None:
        result["note"] = note
    return result


def required_artifacts_for_raw(raw_file: Path, *, artifact_dir: Path | None = None) -> list[ArtifactHtml]:
    artifacts: list[ArtifactHtml] = []
    for manifest in discover_artifact_manifests(raw_file, artifact_dir=artifact_dir):
        artifacts.extend(manifest.artifacts)
    return artifacts


def validate_note_artifacts(
    content: str,
    *,
    raw_file: Path,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate one note's artifact syntax without requiring full raw-chat coverage."""

    manifests = discover_artifact_manifests(raw_file, artifact_dir=artifact_dir)
    artifacts = [artifact for manifest in manifests for artifact in manifest.artifacts]
    result = _note_artifact_report(content, manifests=manifests, artifacts=artifacts)
    if result["errors"]:
        raise ValidationError("Gemini artifact validation failed: " + "; ".join(result["errors"]))
    return result


def validate_artifact_batch(
    notes: list[dict[str, str]],
    *,
    raw_file: Path,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate that a raw chat's staged note group covers all required artifacts."""

    manifests = discover_artifact_manifests(raw_file, artifact_dir=artifact_dir)
    artifacts = [artifact for manifest in manifests for artifact in manifest.artifacts]
    note_reports = [
        _note_artifact_report(
            str(note.get("content") or ""),
            manifests=manifests,
            artifacts=artifacts,
            note=str(note.get("title") or note.get("content_path") or ""),
        )
        for note in notes
    ]
    included_keys = {
        f"{item.get('kind', 'html')}:{item['sha256']}:{_path(str(item['file']))}"
        for report in note_reports
        for item in report["included_artifacts"]
    }
    missing = [artifact for artifact in artifacts if _artifact_key(artifact) not in included_keys]
    errors = [
        error
        for report in note_reports
        for error in report["errors"]
    ]
    for artifact in missing:
        errors.append(f"missing required Gemini artifact across staged note group: {artifact.file_path}")

    result: dict[str, Any] = {
        "schema": ARTIFACT_VALIDATION_SCHEMA,
        "scope": "raw_chat_batch",
        "required": bool(artifacts),
        "manifest_count": len(manifests),
        "artifact_count": len(artifacts),
        "covered_artifact_count": len(artifacts) - len(missing),
        "missing_artifact_count": len(missing),
        "manifests": [manifest.to_json() for manifest in manifests],
        "artifacts": [artifact.to_json() for artifact in artifacts],
        "missing_artifacts": [artifact.to_json() for artifact in missing],
        "notes": note_reports,
        "errors": errors,
    }
    if errors:
        raise ValidationError(
            "Gemini artifact batch validation failed (Gemini artifact HTML batch validation failed): "
            + "; ".join(errors)
        )
    return result
