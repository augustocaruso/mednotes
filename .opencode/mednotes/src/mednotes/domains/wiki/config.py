"""Configuration and resolved path helpers for Wiki workflows."""
from __future__ import annotations

import argparse
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

import mednotes.platform.paths as mednotes_paths_module
from mednotes.domains.wiki.common import ValidationError
from mednotes.domains.wiki.contracts.paths import PathResolutionResult, blocker_from_wiki_resolution
from mednotes.kernel.base import JsonObject, JsonObjectAdapter
from mednotes.platform.paths import (
    WikiPathResolution,
    config_encoding_warnings,
    environment_preflight,
    expand_path,
    find_config,
    read_toml,
    resolve_raw_dir,
    resolve_wiki_dir,
    user_state_dir,
)
from mednotes.platform.user_config import MedNotesUserConfig, load_user_config

DEFAULT_CATALOG_PATH = mednotes_paths_module.DEFAULT_CATALOG_PATH
DEFAULT_RAW_DIR = mednotes_paths_module.DEFAULT_RAW_DIR
DEFAULT_WIKI_DIR = ""
DEFAULT_VOCABULARY_DB_PATH = "~/.mednotes/vocabulary.sqlite"


@dataclass(frozen=True)
class MedConfig:
    raw_dir: Path
    wiki_dir: Path
    catalog_path: Path
    vocabulary_db_path: Path | None = None
    artifact_dir: Path | None = None
    state_dir: Path | None = None
    wiki_source: str = ""
    wiki_compat_warnings: tuple[str, ...] = ()
    wiki_memory_path: Path | None = None
    config_path: Path | None = None
    user_config: MedNotesUserConfig = field(default_factory=MedNotesUserConfig)


def _path(value: str | os.PathLike[str]) -> Path:
    return expand_path(value)


class WikiPathResolutionError(ValidationError):
    """Raised when wiki_dir cannot be resolved safely before a workflow."""

    def __init__(self, resolution: WikiPathResolution):
        self.resolution = resolution
        super().__init__(resolution.next_action or resolution.blocked_reason)

    def payload(self, *, phase: str = "resolve_wiki_dir") -> dict[str, object]:
        resolution_payload = self.resolution.as_payload(phase=phase)
        blocker = blocker_from_wiki_resolution(resolution_payload)
        result = PathResolutionResult(status="blocked", blocker=blocker)
        return {
            **resolution_payload,
            **result.model_dump(mode="json")["blocker"],
            "status": "blocked",
        }


def _user_state_dir() -> Path:
    return user_state_dir()


def _default_catalog_path() -> Path:
    return user_state_dir() / "CATALOGO_WIKI.json"


def _json_field(payload: JsonObject, key: str) -> object:
    if key not in payload:
        return None
    return payload[key]


def _json_str_field(payload: JsonObject, key: str) -> str:
    value = _json_field(payload, key)
    return value if isinstance(value, str) else ""


def _json_list_field(payload: JsonObject, key: str) -> list[object]:
    value = _json_field(payload, key)
    return value if isinstance(value, list) else []


def _json_object_field(payload: JsonObject, key: str) -> JsonObject:
    value = _json_field(payload, key)
    return JsonObjectAdapter.validate_python(value) if isinstance(value, dict) else {}


def _read_toml(path: Path | None) -> JsonObject:
    try:
        return JsonObjectAdapter.validate_python(read_toml(path))
    except RuntimeError as exc:
        raise ValidationError(str(exc)) from exc


def _load_user_config(path: Path | None) -> MedNotesUserConfig:
    try:
        return load_user_config(path)
    except (OSError, tomllib.TOMLDecodeError, PydanticValidationError) as exc:
        raise ValidationError(f"invalid MedNotes config: {exc}") from exc


def _find_config(explicit: str | None) -> Path | None:
    return find_config(explicit, start=Path.cwd())


def resolve_config(args: argparse.Namespace) -> MedConfig:
    explicit_config = getattr(args, "config", None)
    config_path = _find_config(explicit_config)
    user_config = _load_user_config(config_path)
    cfg = _read_toml(config_path)
    section = _json_object_field(cfg, "chat_processor")

    def pick(name: str, env: str, default: str | os.PathLike[str]) -> Path:
        cli_value = getattr(args, name, None)
        value = cli_value or os.getenv(env) or _json_field(section, name) or default
        return _path(str(value))

    def pick_optional(name: str, env: str) -> Path | None:
        cli_value = getattr(args, name, None)
        value = cli_value or os.getenv(env) or _json_field(section, name)
        return _path(str(value)) if value else None

    vocabulary_value = (
        getattr(args, "vocabulary_db", None)
        or os.getenv("MEDNOTES_VOCABULARY_DB")
        or _json_field(section, "vocabulary_db_path")
        or _json_field(section, "vocabulary_db")
    )
    vocabulary_db_path = _path(str(vocabulary_value)) if vocabulary_value else user_state_dir() / "vocabulary.sqlite"

    wiki_resolution = resolve_wiki_dir(
        explicit=getattr(args, "wiki_dir", None),
        config=explicit_config,
        start=Path.cwd(),
        enable_gemini_probe=False,
    )
    if not wiki_resolution.ok:
        raise WikiPathResolutionError(wiki_resolution)
    wiki_dir = wiki_resolution.path
    if wiki_dir is None:
        raise WikiPathResolutionError(wiki_resolution)

    return MedConfig(
        raw_dir=resolve_raw_dir(
            explicit=getattr(args, "raw_dir", None),
            config=explicit_config,
            start=Path.cwd(),
        ),
        wiki_dir=wiki_dir,
        catalog_path=pick("catalog_path", "MED_CATALOG_PATH", _default_catalog_path()),
        vocabulary_db_path=vocabulary_db_path,
        artifact_dir=pick_optional("artifact_dir", "MED_ARTIFACT_DIR"),
        state_dir=user_state_dir(),
        wiki_source=wiki_resolution.source,
        wiki_compat_warnings=wiki_resolution.compat_warnings,
        wiki_memory_path=wiki_resolution.memory_path,
        config_path=config_path,
        user_config=user_config,
    )


def validate_config(config: MedConfig) -> JsonObject:
    preflight = environment_preflight(
        extension_root=Path(__file__).resolve().parents[4],
        state_dir=config.state_dir or user_state_dir(),
        sample_paths=[
            config.raw_dir,
            config.wiki_dir,
            config.catalog_path,
            *( [config.vocabulary_db_path] if config.vocabulary_db_path else [] ),
        ],
    )
    preflight_payload = JsonObjectAdapter.validate_python(preflight)
    return {
        "phase": "validate_environment",
        "status": _json_str_field(preflight_payload, "status") or "completed",
        "blocked_reason": _json_str_field(preflight_payload, "blocked_reason"),
        "next_action": _json_str_field(preflight_payload, "next_action"),
        "required_inputs": _json_list_field(preflight_payload, "required_inputs"),
        "human_decision_required": False,
        "raw_dir": str(config.raw_dir),
        "raw_dir_exists": config.raw_dir.exists(),
        "wiki_dir": str(config.wiki_dir),
        "wiki_dir_exists": config.wiki_dir.exists(),
        "wiki_source": config.wiki_source,
        "wiki_memory_path": str(config.wiki_memory_path) if config.wiki_memory_path else "",
        "config_path": str(config.config_path) if config.config_path else "",
        "config_encoding_warnings": config_encoding_warnings(config.config_path),
        "wiki_compat_warnings": list(config.wiki_compat_warnings),
        "catalog_path": str(config.catalog_path),
        "catalog_path_exists": config.catalog_path.exists(),
        "vocabulary_db_path": str(config.vocabulary_db_path) if config.vocabulary_db_path else "",
        "vocabulary_db_exists": bool(config.vocabulary_db_path and config.vocabulary_db_path.exists()),
        "artifact_dir": str(config.artifact_dir) if config.artifact_dir else "",
        "artifact_dir_exists": bool(config.artifact_dir and config.artifact_dir.exists()),
        "environment_preflight": preflight,
    }
