"""Shared term, alias, and catalog helpers for Wiki graph/linker code."""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL | re.MULTILINE)
CATALOG_CONTAINER_KEYS = ("entities", "entidades", "notes", "notas", "items", "catalog", "catalogo")
TARGET_KEYS = ("target", "target_file", "arquivo", "file", "filename", "nota", "note", "path", "caminho")
ALIAS_KEYS = ("aliases", "alias", "sinonimos", "sinônimos", "synonyms", "siglas", "acronyms", "termos", "terms")
TITLE_KEYS = ("titulo", "title", "nome", "name")
INDEX_TARGET_KEYS = {"_indice_medicina"}
INDEX_TAG_KEYS = {"indice"}


def normalize_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value).strip().casefold()


def obsidian_target_name(value: str) -> str:
    target = str(value or "").strip().replace("\\", "/")
    if not target:
        return ""
    name = target.rsplit("/", 1)[-1].strip()
    return name[:-3] if name.casefold().endswith(".md") else name


def is_index_target(value: str) -> bool:
    return normalize_key(obsidian_target_name(value)) in INDEX_TARGET_KEYS


def extract_tags(content: str) -> list[str]:
    tags: list[str] = []
    match = FRONTMATTER_RE.search(content)
    if not match:
        return tags
    yaml_block = match.group(1)

    list_match = re.search(r"tags:\s*\[(.*?)\]", yaml_block, re.IGNORECASE)
    if list_match:
        tags.extend(clean_yaml_scalar(item).lstrip("#") for item in list_match.group(1).split(",") if item.strip())

    multi_line_match = re.search(r"tags:\s*\n((?:\s*-\s*.*(?:\n|$))+)", yaml_block, re.IGNORECASE)
    if multi_line_match:
        for line in multi_line_match.group(1).strip().split("\n"):
            item = re.sub(r"^\s*-\s*", "", line).strip()
            if item:
                tags.append(clean_yaml_scalar(item).lstrip("#"))

    return [tag for tag in tags if tag]


def is_index_note_content(content: str) -> bool:
    return any(normalize_key(tag) in INDEX_TAG_KEYS for tag in extract_tags(content))


def is_index_note(path: Path, content: str) -> bool:
    return is_index_target(path.stem) or is_index_note_content(content)


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def clean_yaml_scalar(value: str) -> str:
    return value.strip().strip("'\"").strip()


def extract_aliases(content: str) -> list[str]:
    aliases: list[str] = []
    match = FRONTMATTER_RE.search(content)
    if not match:
        return aliases
    yaml_block = match.group(1)

    list_match = re.search(r"aliases:\s*\[(.*?)\]", yaml_block, re.IGNORECASE)
    if list_match:
        aliases.extend(clean_yaml_scalar(item) for item in list_match.group(1).split(",") if item.strip())

    multi_line_match = re.search(r"aliases:\s*\n((?:\s*-\s*.*(?:\n|$))+)", yaml_block, re.IGNORECASE)
    if multi_line_match:
        for line in multi_line_match.group(1).strip().split("\n"):
            item = re.sub(r"^\s*-\s*", "", line).strip()
            if item:
                aliases.append(clean_yaml_scalar(item))

    return [alias for alias in aliases if alias]


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def string_values(value: Any) -> list[str]:
    return [item.strip() for item in as_list(value) if isinstance(item, str) and item.strip()]


def catalog_entries(data: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(data, list):
        return [("", item) for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    for key in CATALOG_CONTAINER_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [("", item) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [(str(k), item) for k, item in value.items() if isinstance(item, dict)]

    return [(str(key), value) for key, value in data.items() if isinstance(value, dict)]


def target_from_entry(entry: dict[str, Any], fallback_key: str = "") -> str | None:
    for key in TARGET_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return obsidian_target_name(value.strip())
    if fallback_key:
        return obsidian_target_name(fallback_key)
    for key in TITLE_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def aliases_from_entry(entry: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ALIAS_KEYS:
        aliases.extend(string_values(entry.get(key)))
    return aliases


def terms_from_entry(entry: dict[str, Any], target: str) -> list[str]:
    terms = [target]
    terms.extend(aliases_from_entry(entry))
    for key in TITLE_KEYS:
        terms.extend(string_values(entry.get(key)))
    return terms
