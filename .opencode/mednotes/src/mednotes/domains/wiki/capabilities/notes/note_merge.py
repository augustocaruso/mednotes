"""Apply semantic note merges with provenance and link triggers."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mednotes.domains.wiki.batch_state import file_sha256
from mednotes.domains.wiki.capabilities.notes.note_style.frontmatter import (
    dump_frontmatter_yaml,
    load_frontmatter_yaml,
    split_frontmatter,
)
from mednotes.domains.wiki.capabilities.notes.provenance import ChatProvenance, apply_note_provenance
from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.common import (
    NOTE_MERGE_APPLY_SCHEMA,
    NOTE_MERGE_PLAN_SCHEMA,
    MissingPathError,
    ValidationError,
)
from mednotes.domains.wiki.config import MedConfig
from mednotes.domains.wiki.flows.link.link_triggers import LINK_TRIGGER_CONTEXT_SCHEMA, validate_trigger_context


def apply_note_merge(
    config: MedConfig,
    plan_path: Path,
    content_path: Path,
    *,
    dry_run: bool = False,
    backup: bool = False,
) -> dict[str, Any]:
    plan = _load_plan(plan_path)
    if not content_path.exists():
        raise MissingPathError(f"Merge content not found: {content_path}")
    if str(plan.get("temp_output") or "") and Path(str(plan["temp_output"])) != content_path:
        raise ValidationError("content_path does not match note merge plan temp_output")

    winner = _wiki_path(config, str(plan["winner_path"]))
    losers = [_wiki_path(config, str(path)) for path in plan["loser_paths"]]
    for path in [winner, *losers]:
        if not path.exists():
            raise MissingPathError(f"Merge source note not found: {path}")
    _validate_source_hashes(config, plan)
    preservation = _load_preservation_report(Path(str(plan["preservation_report_path"])))
    content = content_path.read_text(encoding="utf-8")
    merged_text = _prepare_merged_text(
        winner_text=winner.read_text(encoding="utf-8"),
        content=content,
        aliases_expected=[str(item) for item in plan["aliases_expected"]],
        chats_expected=[str(item) for item in plan["chats_expected"]],
    )
    validation = {
        "errors": [],
        "preservation_report_status": preservation.get("status", ""),
        "source_count": len(losers) + 1,
        "loser_count": len(losers),
    }
    trigger_context = _link_trigger_context(config, winner=winner, losers=losers, before_hash=file_sha256(winner))
    result: dict[str, Any] = {
        "schema": NOTE_MERGE_APPLY_SCHEMA,
        "phase": "note_merge_apply",
        "status": "validated" if dry_run else "applied",
        "dry_run": dry_run,
        "group_id": str(plan["group_id"]),
        "winner_path": str(winner),
        "loser_paths": [str(path) for path in losers],
        "validation": validation,
        "backup_paths": [],
        "written": False,
        "removed_count": 0,
        "link_trigger_context": trigger_context,
        "next_action": "Aplicar o merge e depois rodar /mednotes:link." if dry_run else "Rodar /mednotes:link uma vez para reparar o grafo.",
    }
    if dry_run:
        return result

    originals = {path: path.read_text(encoding="utf-8") for path in [winner, *losers]}
    backup_paths: list[str] = []
    try:
        atomic_write_text(winner, merged_text)
        removed_count = 0
        for loser in losers:
            loser.unlink()
            removed_count += 1
        result["written"] = True
        result["removed_count"] = removed_count
        result["backup_paths"] = backup_paths
        result["after_hash"] = file_sha256(winner)
        result["link_trigger_context"] = _link_trigger_context(
            config,
            winner=winner,
            losers=losers,
            before_hash=trigger_context["changed_notes"][0].get("before_hash", ""),
            after_hash=str(result["after_hash"]),
        )
        return result
    except Exception:
        for path, text in originals.items():
            atomic_write_text(path, text)
        raise


def _load_plan(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingPathError(f"Note merge plan not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid note merge plan JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Note merge plan must be a JSON object")
    if data.get("schema") != NOTE_MERGE_PLAN_SCHEMA:
        raise ValidationError(f"Note merge plan schema must be {NOTE_MERGE_PLAN_SCHEMA}")
    for key in (
        "group_id",
        "winner_path",
        "loser_paths",
        "source_hashes",
        "meaning_identity",
        "aliases_expected",
        "chats_expected",
        "temp_output",
        "preservation_report_path",
    ):
        if key not in data:
            raise ValidationError(f"Note merge plan missing {key}")
    if not isinstance(data["loser_paths"], list) or not data["loser_paths"]:
        raise ValidationError("Note merge plan requires loser_paths[]")
    if not isinstance(data["source_hashes"], list) or not data["source_hashes"]:
        raise ValidationError("Note merge plan requires source_hashes[]")
    if not isinstance(data["aliases_expected"], list) or not data["aliases_expected"]:
        raise ValidationError("Note merge plan requires aliases_expected[]")
    if not isinstance(data["chats_expected"], list) or not data["chats_expected"]:
        raise ValidationError("Note merge plan requires chats_expected[]")
    identity = data.get("meaning_identity")
    if not isinstance(identity, dict) or not str(identity.get("source") or "") or not str(identity.get("meaning_id") or ""):
        raise ValidationError("Note merge plan requires official meaning_identity.source and meaning_identity.meaning_id")
    return data


def _load_preservation_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingPathError(f"Preservation report not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid preservation report JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Preservation report must be a JSON object")
    if data.get("status") != "pass":
        raise ValidationError("preservation_report_status_not_pass")
    return data


def _wiki_path(config: MedConfig, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else config.wiki_dir / path


def _validate_source_hashes(config: MedConfig, plan: dict[str, Any]) -> None:
    for item in plan.get("source_hashes", []):
        if not isinstance(item, dict):
            raise ValidationError("source_hashes[] items must be objects")
        path = _wiki_path(config, str(item.get("path") or ""))
        expected = str(item.get("sha256") or "")
        if not expected:
            raise ValidationError("source_hashes[] missing sha256")
        actual = file_sha256(path)
        if actual != _strip_sha_prefix(expected):
            raise ValidationError(f"source_hash_mismatch: {path}")


def _prepare_merged_text(
    *,
    winner_text: str,
    content: str,
    aliases_expected: list[str],
    chats_expected: list[str],
) -> str:
    text = _preserve_winner_image_frontmatter(winner_text=winner_text, content=content)
    data = load_frontmatter_yaml(text)
    aliases_value = data.get("aliases")
    aliases = [str(item).strip() for item in aliases_value if str(item).strip()] if isinstance(aliases_value, list) else []
    missing_aliases = [alias for alias in aliases_expected if alias not in aliases]
    if missing_aliases:
        raise ValidationError(f"missing_unified_aliases: {', '.join(missing_aliases)}")
    result = apply_note_provenance(
        text,
        chats=[ChatProvenance(chat) for chat in chats_expected],
        chat_lookup=_StaticChatLookup(chats_expected),
    )
    return str(result["text"])


def _preserve_winner_image_frontmatter(*, winner_text: str, content: str) -> str:
    winner_data = load_frontmatter_yaml(winner_text)
    content_data = load_frontmatter_yaml(content)
    image_items = {
        key: value
        for key, value in winner_data.items()
        if str(key).startswith("images_") or str(key).startswith("image_")
    }
    missing = {key: value for key, value in image_items.items() if key not in content_data}
    if not missing:
        return content
    _frontmatter, body = split_frontmatter(content)
    merged = {**content_data, **missing}
    return f"---\n{dump_frontmatter_yaml(merged)}---\n{body.lstrip(chr(10))}"


def _link_trigger_context(
    config: MedConfig,
    *,
    winner: Path,
    losers: list[Path],
    before_hash: str,
    after_hash: str = "",
) -> dict[str, Any]:
    winner_rel = _relative_to_wiki(config, winner)
    changed_notes: list[dict[str, Any]] = [
        {
            "change_type": "modified",
            "content_change": "structural",
            "path": winner_rel,
            "title": winner.stem,
            "before_hash": before_hash,
            "after_hash": after_hash,
        }
    ]
    for loser in losers:
        changed_notes.append(
            {
                "change_type": "merged",
                "content_change": "structural",
                "old_path": _relative_to_wiki(config, loser),
                "old_title": loser.stem,
                "replacement_path": winner_rel,
                "replacement_title": winner.stem,
            }
        )
    return validate_trigger_context(
        {
            "schema": LINK_TRIGGER_CONTEXT_SCHEMA,
            "source_workflow": "/mednotes:fix-wiki",
            "changed_notes": changed_notes,
            "catalog_changed": True,
            "related_notes_export_changed": False,
        }
    )


def _relative_to_wiki(config: MedConfig, path: Path) -> str:
    try:
        return path.relative_to(config.wiki_dir).as_posix()
    except ValueError:
        return str(path)


class _StaticChatLookup:
    def __init__(self, chat_ids: list[str]) -> None:
        self.chat_ids = [ChatProvenance(chat).id for chat in chat_ids]

    def lookup_chat(self, chat_id: str) -> SimpleNamespace:
        normalized = ChatProvenance(chat_id).id
        return SimpleNamespace(
            id=normalized,
            title=f"Chat {normalized}",
            url=f"https://gemini.google.com/app/{normalized}",
            date_created="",
            date_exported="",
        )


def _strip_sha_prefix(value: str) -> str:
    return value.removeprefix("sha256:")
